"""Schema migrations, applied in order by `PRAGMA user_version`. Additive only: a migration never drops or
rewrites stored data, and every new column states what NULL means for rows written before it. The one exception
is v8, which rebuilds the DERIVED table `launch_snapshots` (see there): every row is copied, none is lost.

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
                                or rejected by the maintainer); never public labels

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

v7 (RGY-015): risk cards.
  cards                         one row per live launch: the score, band and reasons computed at launch time (rules
                                module), frozen with config_version / config_hash / rules_version. IMMUTABLE: triggers
                                abort UPDATE and DELETE and ignore a second INSERT of the same token (also INSERT OR
                                REPLACE). `signals` = JSON {"results": every signal incl. the silent ones, "notes": [...]}.
                                Rows of older databases: none; launches before v7 never get a card.

v8 (RGY-016): the T+24 h snapshot, the price peak and the insider set. `launch_snapshots` is REBUILT (new table with
  the same columns, all rows copied, old table dropped, new one renamed): SQLite cannot widen a CHECK, and one table
  keeps every reader (site, rules, diag) free of a UNION. It is derived data, and the copy is verified by the
  migration test. New nullable columns; NULL on rows written before v8 means "metric version 1, not computed" —
  `rebuild-snapshots` fills them (metric_version becomes 2):
  minute                        now also 1440 (T+24 h)
  price_peak / peak_at          highest trade price since the launch up to the cutoff (decimal string, NEAR base
                                units per token base unit) and the block time of that trade (UTC ns)
  insiders_count                the dev (real owner + creator, launchbot excluded) + every account the dev
                                transferred launch tokens to (not a sell leg, not to the pool, token contract or a
                                service account) before the cutoff
  insiders_acquired / _sold     tokens the insiders got from the pool (net buys + a mint in the launch tx) and
                                sold into it (gross); insiders_sold_share_bps = sold / acquired
  insiders_pnl                  insiders' NEAR result from pool trades (received - spent + unsold at price_last), a
                                fact, not a condition of any label; a justhoot v2 mint has no recorded cost
  insiders_evidence             JSON: the sellers (account, role, sold), the 80 % crossing time / tx, first sell tx
  The buyers_* columns now exclude all insiders (before v8 only the dev).

v9 (RGY-017): table `launch_outcomes` — the stored outcome labels of a launch, DERIVED from launch_snapshots by the
  collector (the only writer) with the config in force, and rewritten when a snapshot changes (not frozen, unlike
  `cards`). One row per launch that has a T+60 or T+24 h snapshot; no row = nothing to say yet.
  rug_fast / rug_late / rug  1 / 0, NULL = not decidable yet (see rules/rug.py)
  severity_near              the outside buyers' loss in NEAR when rug = 1
  insiders_pnl               insiders' NEAR result (yocto string) at the latest usable snapshot — a fact
  dev_exit / buyers_loss     the RGY-015 outcome facts, 1 / 0 / NULL
  evidence                   JSON: the full label objects (reason + evidence) and the outcome facts
  config_version/_hash       the config the labels were computed with (the production override included)
  final                      1 once the T+24 h snapshot exists and is usable

v10 (RGY-019): links between the accounts that launch, linked groups and funding clusters — five DERIVED tables,
  rebuilt as a whole by `rugyomi.links.rebuild_links` (the collector between blocks, the `rebuild-links` CLI); never
  read back into facts. The raw facts are not touched. Written by the collector side only; the site reads.
  link_edges                    direct links between two launch PARTICIPANTS (accounts that created or own a launch):
                                type funded | created | launchbot_owner | token_transfer, src -> dst, time, the
                                transaction (NULL when the history has none), amount (base units; NEAR / wNEAR for
                                `funded`, launch tokens for `token_transfer`), source facts | history, group_id
  link_groups / link_members    the connected components of the edges (linked accounts); group_id = the smallest
                                account name; size = all members, truncated = more members than links.group_max
                                (only the earliest group_max by first launch are stored as members)
  link_clusters / _members      a narrow non-service, non-participant funding source that funded several different
                                creators before their launches, and the funded creators with the funding, the launch
                                and the seconds between them

v11 (RGY-020): backfill of the history through the FastNear Transactions API (rugyomi.backfill). Additive:
  launch_snapshots.source / launch_outcomes.source   NULL = written by the live collector (every row written before
                                v11); 'backfill' = computed from `api_facts` for a `launch_only` launch. Readers that
                                must show live data only filter on launches.coverage IS NULL (the accuracy table joins
                                `cards`, which history launches never have).
  api_facts                     facts read from the API for ONE launch window (scope = the launch token), same columns
                                as `facts` (account / kind dictionaries shared) plus an EXPLICIT order: (height, ord,
                                seq) with ord = execution_outcome.index (reproduces facts.id; receipt_index does NOT)
                                and seq = position among the facts of one receipt. UNIQUE(scope, receipt, origin);
                                only the facts a snapshot reads (the launch token's ft_* legs, the swaps of its pool).
                                in_blocks = 1 when the same (receipt, origin) is also a row of `facts`; readers that
                                combine both tables skip those. Written ONLY by the backfill; never mixed into
                                `facts`, and `blocks` is not touched (a partly fetched height is never a parsed block).
  backfill_queue                one row per unit of backfill work: kind 'launch' (key = token), later 'transfers' /
                                'buyers'; status pending | running | done | partial | skipped | error, hops (= credits)
                                spent on it, txs, facts, snapshots written, truncated, note (the reason of a
                                partial / skipped / error row).
v12 (RGY-020 phase C): the incoming NEAR / wNEAR transfers of every launch participant, full history, from the FastNear
  Transfers API (rugyomi.backfill_transfers). Additive: one table.
  transfers_api                 one incoming (dir 'in') transfer of `account` >= 0.1 NEAR: height, ts_ns, tx, receipt,
                                tindex (transfer_index), other (the sender; NULL when the API has none), asset 'NEAR' |
                                'wNEAR', amount (yocto, text), method (method_name), ttype (transfer_type). UNIQUE(account,
                                dir, asset, receipt, tindex). Read ONLY by `links._incoming` (a `funded` edge with source
                                'transfers_api'); never mixed into `facts`. An account with more than 20 pages per asset
                                is cut (oldest rows dropped) and marked truncated in backfill_queue (kind 'transfers_in').
                                Phase D (rugyomi.backfill_buyers) adds the rows of the BUYERS: only the 7 days before the wallet's first
                                buy (queue kind 'buyers_in'); `links` reads the rows of the participants only.
v13 (RGY-021 step 0, F-019): who signed the incoming transfers of `transfers_api`. Additive: two nullable columns.
  signer / pred                 the transaction's signer and the predecessor of the transfer receipt (Transfers API
                                `signer_id` / `predecessor_id`). NULL on rows loaded before v13 (reload them:
                                `run_backfill.py reload`). A `funded` link is a DIRECT transfer: signer = predecessor = sender.
v20 (RGY-043): the Live state block. Additive, TWO DERIVED tables (rugyomi.livestate; the collector writes them, the site only reads them; no card, signal or rug label depends on them):
  launch_live_state    one row per launch: the CURRENT state ('ok' = a number, 'no_trading' = nothing traded for the configured hours, 'history_gap' = a history launch
                       whose trades between its 24 h window and the start of the live data are missing: facts that need no gap only, never a number), the number and its
                       four parts, the facts as of `asof_ns`, the version (`live_state.version`) and the hash of the live_state config. Recomputed: every minute for a launch with a trade in
                       the last hour, otherwise hourly.
  launch_live_state_at the same number, parts and facts at the fixed minutes (live_state.fixed_minutes), kept per version for later study; never shown as a track record.
v19 (RGY-050): the as-of feature store. Additive, ONE DERIVED table (rugyomi.rules.features; the site never reads or writes it, the cards never depend on it):
  signal_features      one row per (launch, signal, mkey): `measure` = the raw as-of value the signal's measure returned (JSON; points and texts are NOT in it), `mkey` = what the
                       value depends on besides the launch (signal and measure versions, the settings the measure declares, the published lists' versions, the context settings),
                       `mark` = how far the appended data reached when it was measured (MAX ids of the source tables), `computed_at`. Filled by `fill-features`; a profile is scored
                       from it with the signals' own `score` (no database read per launch). Rebuildable: dropping the rows loses nothing.
v18 (RGY-036): who holds the supply of a launch at T+5 / T+15 / T+60. Additive, two DERIVED tables (rugyomi.holdings; the site only reads them):
  launch_holdings      per (launch, minute): the supply minted by the cutoff (NULL + a note when it cannot be told) and the tokens held (bought - sold, per wallet
                       never negative) by the dev, the wallets that got tokens from the dev, the early buyers split by the first rule that applies (dev side,
                       bundle, pair / group, fresh wallet, age unknown, other); top-10 holders raw and bundle-adjusted in `extra`
  launch_holdings_done one row per launch computed (resumable; final = the T+60 window is complete)
v17 (RGY-031): trader groups without chaining. Additive, three DERIVED tables, rebuilt with `buyer_links` in one transaction:
  buyer_wallets        per early-buyer wallet: in how many launches it is an early buyer (`launches`), how many launches (with buyers computed) lie between
                       its first and last early buy (`span_launches`), the share in bps and `frequent` (it buys almost everything: it joins no group)
  buyer_pairs          one row per pair of wallets (a < b) that bought the same launch within `co.seconds` in >= `co.min_launches` launches; the fact is kept
                       for frequent wallets too; `group_key` = the trader group both belong to ('' = none)
  buyer_pair_launches  the evidence of a pair: per launch the gap in ms and both first-buy transactions
  A trader group (`buyer_groups` type 'trader_group') is a CLIQUE of >= 3 non-frequent wallets in which every two are a stored pair; no chaining.
v16 (RGY-028): who buys a launch. Additive, four DERIVED tables (never edited by hand; the site only reads them):
  launch_buyers        the first buyers of a launch (rank by first buy): when, how much, first sell, P&L at T+60, role (buyer | insider =
                       received launch tokens from the dev)
  launch_buyers_done   one row per launch the buyers were computed for (resumable; final = the T+60 window is complete)
  buyer_links          one row per (launch, buyer, type) with its evidence (tx): dev_side_buyer | bundle | co_buyers
  buyer_groups / buyer_group_members   the clusters of the two group types: bundle (key = the common funding source) and trader_group
                       (key = the smallest member account). STORAGE FORK (RGY-028): separate tables, not a `type` column on `link_clusters`:
                       its primary key is `source`, so a type could not be added additively, and a trader group has no source.

v15 (RGY-027): retro_cards - the retroactive AS-OF cards (rules v1) of the launches that have no live card. Additive, DERIVED and rebuildable
  (`rebuild-retro-cards`); written by the CLI only, never by the site; NOT `cards` (immutable published forecasts); never read by the publisher,
  the live accuracy or the rules context. Key (token, config_version); `in_sample` = launch time < `fit.train_until` of the v1 config.

v14 (RGY-023): the creator dossier and the rankings over the whole history (rugyomi.dossier). Additive: three derived tables,
  rebuilt as a whole by the collector (never edited by hand; the site only reads them).
  dossier_launch               one row per launch of a participant: group key (gk), live/history, the live card's score, the outcome,
                                the outside buyers' loss at T+60 min, the insiders' result, seconds since the latest direct funding,
                                NEAR sent on within 1 h / 24 h (NULL = outgoing transfers not loaded or cut off)
  dossier_flow                 per group and direction ('in' before the launches, 'out' after them) the top counterparties by NEAR, each
                                with a class (linked | participant | cluster_source | service | platform | unknown) and its largest tx
  dossier_group                per group: size, launches, first funding, totals, members with incoming / outgoing transfers loaded
  `transfers_api` rows with dir 'out' (the participants' outgoing NEAR / wNEAR, backfill_queue kind 'transfers_out') need no migration:
  the column and its CHECK exist since v12, and every reader of the incoming rows filters dir = 'in'.
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
    # ---- v7 (RGY-015): risk cards — the frozen prediction at launch time. Immutable (triggers), additive
    """
    CREATE TABLE cards(
        token TEXT PRIMARY KEY,
        issued_at TEXT NOT NULL,
        issued_height INTEGER NOT NULL,
        launch_ts_ns INTEGER NOT NULL,
        score INTEGER NOT NULL,
        band TEXT NOT NULL,
        reasons TEXT NOT NULL,
        signals TEXT NOT NULL,
        dev_account TEXT,
        config_version TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        rules_version TEXT NOT NULL,
        computed_ms INTEGER NOT NULL
    );
    CREATE TRIGGER cards_no_update BEFORE UPDATE ON cards
        BEGIN SELECT RAISE(ABORT, 'cards are immutable'); END;
    CREATE TRIGGER cards_no_delete BEFORE DELETE ON cards
        BEGIN SELECT RAISE(ABORT, 'cards are immutable'); END;
    CREATE TRIGGER cards_no_replace BEFORE INSERT ON cards
        WHEN EXISTS (SELECT 1 FROM cards WHERE token = NEW.token)
        BEGIN SELECT RAISE(IGNORE); END;
    """,
]

MIGRATIONS.append("""
    CREATE TABLE launch_snapshots_v8(
        token TEXT NOT NULL,
        minute INTEGER NOT NULL CHECK (minute IN (5, 15, 30, 60, 1440)),
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
        price_peak TEXT,
        peak_at INTEGER,
        insiders_count INTEGER,
        insiders_acquired TEXT,
        insiders_sold TEXT,
        insiders_sold_share_bps INTEGER,
        insiders_pnl TEXT,
        insiders_evidence TEXT,
        PRIMARY KEY (token, minute)
    );
    INSERT INTO launch_snapshots_v8(token, minute, at_ts_ns, n_swaps, unattributed_swaps, price_last,
        price_first_buyer, max_x, dev_acquired, dev_sold, dev_sold_share_bps, dev_transferred_out,
        dev_first_sell_after_s, buyers_count, buyers_spent, buyers_received, buyers_unsold_value, buyers_pnl,
        holders_count, top10_share_bps, metric_version, computed_at, error)
    SELECT token, minute, at_ts_ns, n_swaps, unattributed_swaps, price_last,
        price_first_buyer, max_x, dev_acquired, dev_sold, dev_sold_share_bps, dev_transferred_out,
        dev_first_sell_after_s, buyers_count, buyers_spent, buyers_received, buyers_unsold_value, buyers_pnl,
        holders_count, top10_share_bps, metric_version, computed_at, error
    FROM launch_snapshots;
    DROP TABLE launch_snapshots;
    ALTER TABLE launch_snapshots_v8 RENAME TO launch_snapshots;
""")  # ---- v8 (RGY-016): see the module docstring

MIGRATIONS.append("""
    CREATE TABLE launch_outcomes(
        token TEXT PRIMARY KEY,
        rug_fast INTEGER,
        rug_late INTEGER,
        rug INTEGER,
        severity_near REAL,
        insiders_pnl TEXT,
        dev_exit INTEGER,
        buyers_loss INTEGER,
        evidence TEXT NOT NULL,
        config_version TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        final INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX launch_outcomes_rug ON launch_outcomes(rug, final);
""")  # ---- v9 (RGY-017): see the module docstring

MIGRATIONS.append("""
    CREATE TABLE link_edges(
        id INTEGER PRIMARY KEY,
        type TEXT NOT NULL,
        src TEXT NOT NULL,
        dst TEXT NOT NULL,
        ts_ns INTEGER,
        tx TEXT,
        amount TEXT,
        asset TEXT,
        token TEXT,
        source TEXT NOT NULL,
        group_id TEXT
    );
    CREATE INDEX link_edges_src ON link_edges(src);
    CREATE INDEX link_edges_dst ON link_edges(dst);
    CREATE INDEX link_edges_group ON link_edges(group_id);
    CREATE TABLE link_groups(
        group_id TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        truncated INTEGER NOT NULL DEFAULT 0,
        launches INTEGER NOT NULL,
        first_launch_ns INTEGER,
        last_launch_ns INTEGER
    );
    CREATE TABLE link_members(
        account TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        launches INTEGER NOT NULL,
        first_launch_ns INTEGER
    );
    CREATE INDEX link_members_group ON link_members(group_id);
    CREATE TABLE link_clusters(
        source TEXT PRIMARY KEY,
        creators INTEGER NOT NULL,
        recipients INTEGER NOT NULL,
        first_funded_ns INTEGER,
        last_funded_ns INTEGER,
        total_near TEXT
    );
    CREATE TABLE link_cluster_members(
        source TEXT NOT NULL,
        account TEXT NOT NULL,
        funded_ns INTEGER NOT NULL,
        tx TEXT,
        amount TEXT NOT NULL,
        amount_total TEXT NOT NULL,
        fundings INTEGER NOT NULL,
        asset TEXT,
        edge_source TEXT NOT NULL,
        launch_token TEXT,
        launch_ns INTEGER NOT NULL,
        seconds_before INTEGER NOT NULL,
        PRIMARY KEY (source, account)
    );
    CREATE INDEX link_cluster_members_account ON link_cluster_members(account);
""")  # ---- v10 (RGY-019): derived link tables, rebuilt by `rugyomi.links` (collector / rebuild-links); see that module

MIGRATIONS.append("""
    ALTER TABLE launch_snapshots ADD COLUMN source TEXT;
    ALTER TABLE launch_outcomes ADD COLUMN source TEXT;
    CREATE TABLE api_facts(
        id INTEGER PRIMARY KEY,
        scope TEXT NOT NULL,
        height INTEGER NOT NULL,
        ord INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        kind INTEGER NOT NULL REFERENCES kinds(id),
        ts_ns INTEGER NOT NULL,
        tx BLOB,
        receipt BLOB NOT NULL,
        origin TEXT NOT NULL,
        a INTEGER REFERENCES accounts(id),
        b INTEGER REFERENCES accounts(id),
        token INTEGER REFERENCES accounts(id),
        amount TEXT,
        extra TEXT,
        in_blocks INTEGER NOT NULL DEFAULT 0,
        UNIQUE(scope, receipt, origin)
    );
    CREATE INDEX api_facts_scope_order ON api_facts(scope, height, ord, seq);
    CREATE TABLE backfill_queue(
        kind TEXT NOT NULL,
        key TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'partial', 'skipped', 'error')),
        ts_ns INTEGER,
        height INTEGER,
        hops INTEGER NOT NULL DEFAULT 0,
        txs INTEGER,
        facts INTEGER,
        snapshots INTEGER,
        truncated INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        PRIMARY KEY (kind, key)
    );
    CREATE INDEX backfill_queue_status ON backfill_queue(kind, status, ts_ns);
""")  # ---- v11 (RGY-020): see the module docstring

MIGRATIONS.append("""
    CREATE TABLE transfers_api(
        id INTEGER PRIMARY KEY,
        account TEXT NOT NULL,
        dir TEXT NOT NULL CHECK (dir IN ('in', 'out')),
        height INTEGER NOT NULL,
        ts_ns INTEGER NOT NULL,
        tx TEXT,
        receipt TEXT NOT NULL DEFAULT '',
        tindex INTEGER NOT NULL DEFAULT 0,
        other TEXT,
        asset TEXT NOT NULL,
        amount TEXT NOT NULL,
        method TEXT,
        ttype TEXT,
        UNIQUE(account, dir, asset, receipt, tindex)
    );
    CREATE INDEX transfers_api_account ON transfers_api(account, dir, ts_ns);
    CREATE INDEX transfers_api_other ON transfers_api(other, dir);
""")  # ---- v12 (RGY-020 phase C): see the module docstring

MIGRATIONS.append("""
    ALTER TABLE transfers_api ADD COLUMN signer TEXT;
    ALTER TABLE transfers_api ADD COLUMN pred TEXT;
""")  # ---- v13 (RGY-021 step 0): see the module docstring

MIGRATIONS.append("""
    CREATE TABLE dossier_launch(
        token TEXT PRIMARY KEY,
        gk TEXT NOT NULL,
        account TEXT NOT NULL,
        launchpad TEXT NOT NULL,
        ts_ns INTEGER NOT NULL,
        live INTEGER NOT NULL,
        score INTEGER,
        band TEXT,
        rug INTEGER,
        final INTEGER NOT NULL DEFAULT 0,
        severity_near REAL,
        has_snapshot INTEGER NOT NULL DEFAULT 0,
        loss_near REAL NOT NULL DEFAULT 0,
        buyers_pnl_near REAL,
        insiders_pnl_near REAL,
        funded_before_s INTEGER,
        funder TEXT,
        out_1h_near REAL,
        out_24h_near REAL
    );
    CREATE INDEX dossier_launch_gk ON dossier_launch(gk, ts_ns);
    CREATE INDEX dossier_launch_account ON dossier_launch(account);
    CREATE INDEX dossier_launch_ts ON dossier_launch(ts_ns);
    CREATE TABLE dossier_flow(
        gk TEXT NOT NULL,
        dir TEXT NOT NULL CHECK (dir IN ('in', 'out')),
        peer TEXT NOT NULL,
        kind TEXT NOT NULL,
        near REAL NOT NULL,
        n INTEGER NOT NULL,
        first_ns INTEGER,
        last_ns INTEGER,
        tx TEXT,
        rank INTEGER NOT NULL,
        PRIMARY KEY (gk, dir, peer)
    );
    CREATE INDEX dossier_flow_rank ON dossier_flow(gk, dir, rank);
    CREATE TABLE dossier_group(
        gk TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        launches INTEGER NOT NULL,
        in_loaded INTEGER NOT NULL,
        out_loaded INTEGER NOT NULL,
        members INTEGER NOT NULL,
        first_funded_ns INTEGER,
        first_funder TEXT,
        first_funded_tx TEXT,
        in_total_near REAL NOT NULL DEFAULT 0,
        out_total_near REAL NOT NULL DEFAULT 0
    );

""")  # ---- v14 (RGY-023): see the module docstring

MIGRATIONS.append("""
    -- v15 (RGY-027): retroactive "as-of" cards of the launches that have no live card. DERIVED and rebuildable (`rebuild-retro-cards`), written by
    -- the CLI only (never by the site); NOT the `cards` table (those are immutable forecasts published at the time). Keyed by token + config version.
    CREATE TABLE retro_cards(
        token TEXT NOT NULL,
        rules_version TEXT NOT NULL,
        config_version TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        launch_ts_ns INTEGER NOT NULL,
        score INTEGER NOT NULL,
        band TEXT NOT NULL,
        reasons TEXT NOT NULL,
        signals TEXT NOT NULL,
        notes TEXT NOT NULL DEFAULT '[]',
        in_sample INTEGER NOT NULL,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (token, config_version)
    );
    CREATE INDEX retro_cards_ts ON retro_cards(launch_ts_ns);
""")  # ---- v15 (RGY-027): see the module docstring

MIGRATIONS.append("""
    -- v16 (RGY-028): who buys a launch. ALL FOUR TABLES ARE DERIVED and rebuildable (rugyomi.buyers; the CLI `rebuild-buyer-links` and the
    -- collector between blocks); the site only reads them. Not part of the cards, the rules or the rug label.
    CREATE TABLE launch_buyers(
        token TEXT NOT NULL,
        buyer TEXT NOT NULL,
        rank INTEGER NOT NULL,
        first_buy_ns INTEGER NOT NULL,
        first_buy_tx TEXT,
        buys INTEGER NOT NULL,
        spent TEXT NOT NULL,
        bought TEXT NOT NULL,
        sold TEXT NOT NULL,
        received TEXT NOT NULL,
        first_sell_ns INTEGER,
        first_sell_tx TEXT,
        pnl TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'buyer',
        PRIMARY KEY (token, buyer)
    );
    CREATE INDEX launch_buyers_buyer ON launch_buyers(buyer, first_buy_ns);
    CREATE TABLE launch_buyers_done(
        token TEXT PRIMARY KEY,
        buyers_total INTEGER NOT NULL,
        kept INTEGER NOT NULL,
        source TEXT NOT NULL,
        final INTEGER NOT NULL,
        config_hash TEXT NOT NULL,
        computed_at TEXT NOT NULL
    );
    CREATE TABLE buyer_links(
        token TEXT NOT NULL,
        buyer TEXT NOT NULL,
        type TEXT NOT NULL CHECK (type IN ('dev_side_buyer', 'bundle', 'co_buyers')),
        group_key TEXT NOT NULL DEFAULT '',
        ref TEXT,
        tx TEXT,
        ts_ns INTEGER,
        amount TEXT,
        relation TEXT,
        detail TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (token, buyer, type, group_key)
    );
    CREATE INDEX buyer_links_token ON buyer_links(token);
    CREATE INDEX buyer_links_group ON buyer_links(type, group_key);
    CREATE INDEX buyer_links_buyer ON buyer_links(buyer);
    CREATE TABLE buyer_groups(
        type TEXT NOT NULL CHECK (type IN ('bundle', 'trader_group')),
        group_key TEXT NOT NULL,
        size INTEGER NOT NULL,
        launches INTEGER NOT NULL,
        first_ns INTEGER,
        last_ns INTEGER,
        truncated INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (type, group_key)
    );
    CREATE TABLE buyer_group_members(
        type TEXT NOT NULL,
        group_key TEXT NOT NULL,
        account TEXT NOT NULL,
        launches INTEGER NOT NULL,
        PRIMARY KEY (type, group_key, account)
    );
    CREATE INDEX buyer_group_members_account ON buyer_group_members(account, type);
""")  # ---- v16 (RGY-028): see the module docstring

MIGRATIONS.append("""
    -- v17 (RGY-031): trader groups without chaining. ALL THREE TABLES ARE DERIVED and rebuilt together with `buyer_links` (rugyomi.buyers).
    CREATE TABLE buyer_wallets(
        account TEXT PRIMARY KEY,
        launches INTEGER NOT NULL,
        span_launches INTEGER NOT NULL,
        first_ns INTEGER NOT NULL,
        last_ns INTEGER NOT NULL,
        share_bps INTEGER NOT NULL,
        frequent INTEGER NOT NULL
    );
    CREATE TABLE buyer_pairs(
        a TEXT NOT NULL,
        b TEXT NOT NULL,
        launches INTEGER NOT NULL,
        first_ns INTEGER NOT NULL,
        last_ns INTEGER NOT NULL,
        min_gap_ms INTEGER NOT NULL,
        group_key TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (a, b)
    );
    CREATE INDEX buyer_pairs_b ON buyer_pairs(b);
    CREATE TABLE buyer_pair_launches(
        a TEXT NOT NULL,
        b TEXT NOT NULL,
        token TEXT NOT NULL,
        gap_ms INTEGER NOT NULL,
        a_ns INTEGER NOT NULL,
        a_tx TEXT,
        b_ns INTEGER NOT NULL,
        b_tx TEXT,
        PRIMARY KEY (a, b, token)
    );
    CREATE INDEX buyer_pair_launches_token ON buyer_pair_launches(token);
""")  # ---- v17 (RGY-031): see the module docstring

MIGRATIONS.append("""
    -- v18 (RGY-036): who holds the supply of a launch at T+5 / T+15 / T+60. BOTH TABLES ARE DERIVED and rebuildable (rugyomi.holdings; the CLI
    -- `rebuild-holdings` and the collector between blocks); the site only reads them. Not part of the cards, the rules or the rug label.
    CREATE TABLE launch_holdings(
        token TEXT NOT NULL,
        minute INTEGER NOT NULL,
        supply TEXT,                    -- net minted up to the cutoff (base units); NULL = not determinable (see supply_note)
        supply_note TEXT NOT NULL,      -- ok | no_mint (no mint event in the data) | below_holdings (the rows hold more than was minted)
        rows TEXT NOT NULL,             -- JSON [{key, wallets, held}] in priority order; held = base units, bought - sold, never negative
        extra TEXT NOT NULL DEFAULT '{}',   -- JSON: dev_transferred_out, early_wallets, ages_known, top10 / holders raw and bundle-adjusted
        computed_at TEXT NOT NULL,
        PRIMARY KEY (token, minute)
    );
    CREATE TABLE launch_holdings_done(
        token TEXT PRIMARY KEY,
        source TEXT NOT NULL,           -- live | history | none (no trades available)
        final INTEGER NOT NULL,
        config_hash TEXT NOT NULL,
        computed_at TEXT NOT NULL
    );
""")  # ---- v18 (RGY-036): see the module docstring

MIGRATIONS.append("""
    -- v19 (RGY-050): the as-of feature store. DERIVED and rebuildable (rugyomi.rules.features; the CLI `fill-features`); not part of the cards, the rules or the rug label.
    CREATE TABLE signal_features(
        token TEXT NOT NULL,
        signal TEXT NOT NULL,
        mkey TEXT NOT NULL,             -- features.feature_key(): signal / measure versions, declared settings, lists' versions, context settings
        measure TEXT NOT NULL,          -- JSON: the signal's raw as-of value (no points, no text)
        mark TEXT NOT NULL,             -- data mark when measured (features.data_mark)
        computed_at TEXT NOT NULL,
        PRIMARY KEY (signal, mkey, token)
    ) WITHOUT ROWID;
    CREATE INDEX signal_features_token ON signal_features(token);
""")  # ---- v19 (RGY-050): see the module docstring

MIGRATIONS.append("""
    -- v20 (RGY-043): the Live state block. BOTH TABLES ARE DERIVED and rebuildable (rugyomi.livestate); the site only reads them. Not part of the cards, the rules or the rug label.
    CREATE TABLE launch_live_state(
        token TEXT PRIMARY KEY,
        state TEXT NOT NULL,            -- ok | no_trading | history_gap
        number INTEGER,                 -- 0..100, NULL unless state = ok
        parts TEXT NOT NULL DEFAULT '{}',   -- JSON: the four parts (0..100) with their inputs
        facts TEXT NOT NULL DEFAULT '{}',   -- JSON: the facts "now" (price, profit, holdings, activity, last trade)
        asof_ns INTEGER NOT NULL,       -- the moment the numbers are as of (UTC ns, the last processed block)
        last_trade_ns INTEGER,
        version TEXT NOT NULL,          -- live_state.version
        config_hash TEXT NOT NULL,
        source TEXT NOT NULL,           -- live | history
        computed_at TEXT NOT NULL
    );
    CREATE INDEX launch_live_state_due ON launch_live_state(computed_at);
    CREATE TABLE launch_live_state_at(
        token TEXT NOT NULL,
        minute INTEGER NOT NULL,
        version TEXT NOT NULL,
        state TEXT NOT NULL,
        number INTEGER,
        parts TEXT NOT NULL DEFAULT '{}',
        facts TEXT NOT NULL DEFAULT '{}',
        config_hash TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (token, minute, version)
    ) WITHOUT ROWID;
""")  # ---- v20 (RGY-043): see the module docstring

MIGRATIONS.append("""
    -- v21 (RGY-056): the issued cards a later fix of the card path would have scored higher. DERIVED and rebuildable (rugyomi.cardfix; the CLI `rebuild-card-fixes`);
    -- the site only reads it. Never read by the publisher, the accuracy numbers or the rules; `cards` is not touched.
    CREATE TABLE card_fixes(
        token TEXT NOT NULL,
        fix TEXT NOT NULL,              -- cardfix.FIXES key
        deployed_at TEXT NOT NULL,      -- when the fix went live (UTC ISO); the card was issued before it
        issued_at TEXT NOT NULL,
        issued_score INTEGER NOT NULL,
        issued_band TEXT NOT NULL,
        points INTEGER NOT NULL,        -- what the fix's signals add as of the launch time (> 0)
        score INTEGER NOT NULL,         -- issued_score + points, capped at 100
        band TEXT NOT NULL,             -- the band of `score` under the card's config
        signals TEXT NOT NULL,          -- JSON [{id, issued, asof, reason, evidence}]: the fix's signals whose points differ
        profile TEXT NOT NULL,
        config_version TEXT NOT NULL,
        config_match INTEGER NOT NULL,  -- 1 = scored with the config whose hash the card carries
        computed_at TEXT NOT NULL,
        PRIMARY KEY (token, fix)
    );
""")  # ---- v21 (RGY-056): see rugyomi/cardfix.py

SCHEMA_VERSION = len(MIGRATIONS)
