"""Schema migrations, applied in order by `PRAGMA user_version`. Additive only: a migration never drops or
rewrites stored data, and every new column states what NULL means for rows written before it.

v1 (RGY-004):
  accounts(id, name)            dictionary of account ids (facts reference it)
  kinds(id, name)               dictionary of fact kinds
  facts                         raw facts, append-only. Key UNIQUE(receipt, origin) — see rugyomi.parse.
                                receipt / tx: base58 decoded to bytes (32 bytes; tx may be NULL);
                                amount: decimal string in base units; extra: compact JSON; ts_ns: block time (UTC ns)
  blocks(height PK, status)     every processed height: ok | null (a skipped height) | error (being retried)
  parse_errors                  one row per action / log item that could not be read
  launches                      derived: one row per launch token, recomputable from facts (rebuild_launches)
  collector_status(key, value)  live state of the collector (JSON values)

v2 (RGY-006):
  index facts(token, ts_ns)     per-token reads (snapshots)
  launch_snapshots(token, minute)  continuous metrics at T+5/15/30/60 (rugyomi.snapshots). Derived: recomputable
                                from facts + launches; rows of v1 databases: none until computed (the collector
                                computes the missing ones on its next block; `rebuild-snapshots` recomputes).
                                error IS NOT NULL = the computation failed for that row (metrics NULL).

v3 (RGY-007):
  manual_labels                 manual labels of accounts (who / when / why; source manual | admin_bot). Append-only:
                                a revoke sets revoked_at + revoke_reason on the row, rows are never deleted.
                                Read through rugyomi.labels. Not used by facts, launches or snapshots.

v4 (RGY-009): history from an earlier collection (23.10 – 30.10.2026), raw facts only, in SEPARATE tables with a
  source tag (`legacy_import`) — never mixed into facts / launches / launch_snapshots. Times are block time, UTC ns;
  amounts are decimal strings in base units.
  hist_launches                 launches seen then (token, launchpad, creator, height, time, dev buy, pool)
  hist_trades                   trades of the first ~2 h of those launches, as recorded: `swapper` is the DCL swapper;
                                `account` = the beneficiary — the swapper, except nearly dev buys made through the
                                lock account, where account = the launch creator (account_role dev_buy_via_lock)
  hist_dev_moves / hist_cashouts  token moves by devs, cash-outs by creators
  hist_fundings                 incoming funding of tracked accounts, deduplicated (dup_count = identical source rows)
  hist_accounts / hist_account_info  account creation (height, time, creator) / raw explorer answers
  reference_candidates          private candidates for the labelled reference set (status pending until confirmed
                                or rejected in the admin bot); never public labels

v5 (RGY-012): quarantine of a height whose PROCESSING fails deterministically (rugyomi.collector). New nullable
  columns of `blocks`; rows written before v5 read as "never quarantined, no processing error counted":
  quarantined_at / quarantine_error  set when the height went to quarantine (status stays 'error'; the cursor treats
                                it as done). Kept for history when `reprocess-quarantine` later turns the row 'ok'.
  process_errors                processing failures (parse / store / launches / snapshots) counted on the height;
                                NULL = 0. Network / source errors are not counted here.
  error_since                   wall-clock time of the first failed attempt of the height (NULL on rows that failed
                                only before v5: set by their next failure)

v6 (RGY-011): history of all launches.
  launch_index                  one row per token of a launch found by the chain index (nearly view methods, justhoot
                                get_launched_tokens + Tx API): token, launchpad (nearly | justhoot_v1 | justhoot_v2),
                                launch_id (nearly), creator (nearly only: the launchbot account for launches made
                                through it; justhoot: NULL — the creator comes from the launch block events), the
                                creating transaction, its height and time (block time of the TRANSACTION; the launch
                                event follows 1–8 blocks later), nearly state fields, raw (JSON), source
                                'chain_index'. Rows are never changed: a re-import inserts 0.
  launches.coverage             NULL = seen live / in a fully covered range (every row written before v6);
                                'launch_only' = found in the few blocks around a historical launch (`ingest-blocks`):
                                no window facts, no snapshots (rugyomi.snapshots.window_covered enforces it too).
"""
from __future__ import annotations

MIGRATIONS: list[str] = [
    # ---- v1
    """
    CREATE TABLE accounts(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
    CREATE TABLE kinds(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
    CREATE TABLE facts(
        id INTEGER PRIMARY KEY,
        kind INTEGER NOT NULL REFERENCES kinds(id),
        height INTEGER NOT NULL,
        ts_ns INTEGER NOT NULL,
        tx BLOB,
        receipt BLOB NOT NULL,
        origin TEXT NOT NULL,
        a INTEGER REFERENCES accounts(id),
        b INTEGER REFERENCES accounts(id),
        token INTEGER REFERENCES accounts(id),
        amount TEXT,
        extra TEXT,
        UNIQUE(receipt, origin)
    );
    CREATE INDEX facts_a_ts ON facts(a, ts_ns);
    CREATE INDEX facts_b_ts ON facts(b, ts_ns);
    CREATE INDEX facts_ts ON facts(ts_ns);
    CREATE INDEX facts_kind_height ON facts(kind, height);
    CREATE TABLE blocks(
        height INTEGER PRIMARY KEY,
        status TEXT NOT NULL CHECK (status IN ('ok', 'null', 'error')),
        block_ts_ns INTEGER,
        n_facts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 1,
        processed_at TEXT NOT NULL
    );
    CREATE TABLE parse_errors(
        height INTEGER NOT NULL,
        receipt_id TEXT,
        origin TEXT NOT NULL,
        error TEXT NOT NULL,
        at TEXT NOT NULL,
        UNIQUE(height, receipt_id, origin)
    );
    CREATE TABLE launches(
        token TEXT PRIMARY KEY,
        launchpad TEXT NOT NULL,
        creator TEXT,
        owner TEXT,
        owner_evidence TEXT,
        pool TEXT,
        dev_buy TEXT,
        height INTEGER NOT NULL,
        ts_ns INTEGER NOT NULL,
        tx TEXT,
        detected_at TEXT NOT NULL,
        extra TEXT
    );
    CREATE INDEX launches_ts ON launches(ts_ns);
    CREATE TABLE collector_status(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL);
    """,
    # ---- v2 (RGY-006): launch snapshots — derived, recomputable from facts + launches
    """
    CREATE INDEX facts_token_ts ON facts(token, ts_ns);
    CREATE TABLE launch_snapshots(
        token TEXT NOT NULL,
        minute INTEGER NOT NULL CHECK (minute IN (5, 15, 30, 60)),
        at_ts_ns INTEGER NOT NULL,
        n_swaps INTEGER,
        unattributed_swaps INTEGER,
        price_last TEXT,
        price_first_buyer TEXT,
        max_x REAL,
        dev_acquired TEXT,
        dev_sold TEXT,
        dev_sold_share_bps INTEGER,
        dev_transferred_out TEXT,
        dev_first_sell_after_s REAL,
        buyers_count INTEGER,
        buyers_spent TEXT,
        buyers_received TEXT,
        buyers_unsold_value TEXT,
        buyers_pnl TEXT,
        holders_count INTEGER,
        top10_share_bps INTEGER,
        metric_version INTEGER NOT NULL,
        computed_at TEXT NOT NULL,
        error TEXT,
        PRIMARY KEY (token, minute)
    );
    """,
    # ---- v3 (RGY-007): manual account labels — a separate layer, append-only
    """
    CREATE TABLE manual_labels(
        id INTEGER PRIMARY KEY,
        account TEXT NOT NULL,
        label TEXT NOT NULL,
        reason TEXT NOT NULL,
        source TEXT NOT NULL,
        set_by TEXT NOT NULL,
        set_at TEXT NOT NULL,
        revoked_at TEXT,
        revoke_reason TEXT
    );
    CREATE INDEX manual_labels_account ON manual_labels(account);
    """,
    # ---- v4 (RGY-009): history imported from an earlier collection — separate tables, never mixed into facts
    """
    CREATE TABLE hist_launches(
        token TEXT PRIMARY KEY, launchpad TEXT NOT NULL, creator TEXT, height INTEGER, ts_ns INTEGER NOT NULL,
        dev_buy TEXT, pool TEXT, source TEXT NOT NULL, imported_at TEXT NOT NULL
    );
    CREATE TABLE hist_trades(
        id INTEGER PRIMARY KEY, token TEXT NOT NULL, ts_ns INTEGER NOT NULL, account TEXT, side TEXT NOT NULL,
        near TEXT, tokens TEXT, account_role TEXT NOT NULL CHECK (account_role IN ('swapper', 'dev_buy_via_lock')),
        swapper TEXT, source TEXT NOT NULL, imported_at TEXT NOT NULL,
        UNIQUE(token, ts_ns, swapper, side, near, tokens)
    );
    CREATE INDEX hist_trades_token_ts ON hist_trades(token, ts_ns);
    CREATE INDEX hist_trades_account ON hist_trades(account);
    CREATE TABLE hist_dev_moves(
        id INTEGER PRIMARY KEY, token TEXT NOT NULL, ts_ns INTEGER NOT NULL, dev TEXT, to_account TEXT, amount TEXT,
        source TEXT NOT NULL, imported_at TEXT NOT NULL, UNIQUE(token, ts_ns, dev, to_account, amount)
    );
    CREATE TABLE hist_cashouts(
        id INTEGER PRIMARY KEY, creator TEXT NOT NULL, ts_ns INTEGER NOT NULL, dest TEXT, intents_account TEXT,
        token TEXT, amount TEXT, tx_hash TEXT, source TEXT NOT NULL, imported_at TEXT NOT NULL,
        UNIQUE(tx_hash, creator, dest, token, amount)
    );
    CREATE INDEX hist_cashouts_creator ON hist_cashouts(creator, ts_ns);
    CREATE TABLE hist_fundings(
        id INTEGER PRIMARY KEY, account TEXT NOT NULL, ts_ns INTEGER NOT NULL, kind TEXT, funder TEXT,
        intents_account TEXT, token TEXT, amount TEXT, tx_hash TEXT, dup_count INTEGER NOT NULL,
        source TEXT NOT NULL, imported_at TEXT NOT NULL, UNIQUE(account, tx_hash, token, amount)
    );
    CREATE INDEX hist_fundings_account ON hist_fundings(account, ts_ns);
    CREATE INDEX hist_fundings_funder ON hist_fundings(funder, ts_ns);
    CREATE TABLE hist_accounts(
        account TEXT PRIMARY KEY, creator TEXT, ts_ns INTEGER NOT NULL, height INTEGER,
        source TEXT NOT NULL, imported_at TEXT NOT NULL
    );
    CREATE TABLE hist_account_info(
        account TEXT PRIMARY KEY, created_ts_ns INTEGER, txns INTEGER, fetched_ts_ns INTEGER, raw TEXT,
        source TEXT NOT NULL, imported_at TEXT NOT NULL
    );
    CREATE TABLE reference_candidates(
        id INTEGER PRIMARY KEY, account TEXT NOT NULL, source TEXT NOT NULL, evidence TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', imported_at TEXT NOT NULL, UNIQUE(account, source)
    );
    """,
    # ---- v5 (RGY-012): quarantine of a height whose processing fails deterministically
    """
    ALTER TABLE blocks ADD COLUMN quarantined_at TEXT;
    ALTER TABLE blocks ADD COLUMN quarantine_error TEXT;
    ALTER TABLE blocks ADD COLUMN process_errors INTEGER;
    ALTER TABLE blocks ADD COLUMN error_since TEXT;
    CREATE INDEX blocks_quarantined ON blocks(quarantined_at) WHERE quarantined_at IS NOT NULL;
    """,
    # ---- v6 (RGY-011): the index of all launches, and the coverage tag of a launch
    """
    CREATE TABLE launch_index(
        token TEXT PRIMARY KEY,
        launchpad TEXT NOT NULL,
        launch_id INTEGER,
        creator TEXT,
        create_tx TEXT,
        create_height INTEGER,
        create_ts_ns INTEGER,
        txs_count INTEGER,
        step TEXT,
        dev_buy_near TEXT,
        created_at_ms INTEGER,
        fees_near_total TEXT,
        claims INTEGER,
        raw TEXT,
        source TEXT NOT NULL,
        imported_at TEXT NOT NULL
    );
    CREATE INDEX launch_index_creator ON launch_index(creator);
    CREATE INDEX launch_index_height ON launch_index(create_height);
    ALTER TABLE launches ADD COLUMN coverage TEXT;
    CREATE INDEX launches_creator ON launches(creator);
    CREATE INDEX launches_owner ON launches(owner);
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)
