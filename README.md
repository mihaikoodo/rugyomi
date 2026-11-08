# rugyomi

Risk analytics for token launches on NEAR launchpads ([nearly.trade](https://nearly.trade), [justhoot.fun](https://justhoot.fun)).

**The problem.** On a launchpad a token appears every few minutes, and most of them are over within the hour: the creator
buys, sells into the first buyers and leaves. The facts that tell these launches apart from the rest (who created the
token, how it was funded, what the creator did in the first minutes) are all on chain, but nobody has them in one place.

**What this repository does.** It follows the NEAR chain block by block, stores every relevant on-chain fact in a local
SQLite database, detects launches on both launchpads, and measures what happened to each launch 5 / 15 / 30 / 60 minutes
after it started. Every number can be traced back to a transaction.

## Status: work in progress

| Works now | Not built yet |
|---|---|
| Live collector (neardata blocks → raw facts → launches), restart-safe, one SQLite transaction per block | Calibrated score weights (the v0 weights are provisional) |
| Parsing of nearly.trade, justhoot v1 and v2 launches; real owner of launches made through the nearly launch bot | Public website and Telegram channel |
| Launch snapshots at T+5 / 15 / 30 / 60 min and T+24 h (dev, insiders, buyers, price and its peak, holders) | Public API / MCP server |
| Import of the history of all earlier launches (launch index + the blocks around each launch) | Public accuracy table |
| Quarantine of a block that cannot be processed, honest status (`status`), manual account labels | |
| Risk card per live launch: 0–100 score, reasons with evidence, frozen at launch time; a read-only site with a methodology page | |
| The rug label (the score's target): the insiders sold into the other buyers, within 60 min (`rug_fast`) or 24 h (`rug_late`) | |

The project is a hackathon entry, so the interface is a command line for now.

## Install and test

Python 3.12 or newer. The collector uses only the standard library; the site needs the `web` extra.

```
pip install -e ".[test,web]"
python -m pytest -q
```

The tests are offline: they run on real mainnet blocks stored in `tests/fixtures/` (public chain data) and on a temporary
database.

## Run the collector

You need one FastNear API key (set `FASTNEAR_API_KEY`). The collector reads blocks from `mainnet.neardata.xyz`.

```
export FASTNEAR_API_KEY=...
python -m rugyomi.collector run --db ./rugyomi.db        # follows the chain until you stop it (Ctrl-C)
python -m rugyomi.collector status --db ./rugyomi.db     # in another shell: last block, lag, launches, errors (JSON)
```

On an empty database the collector starts at the current final block and records that height as its start. After a
stop or a crash it continues from the first block it has not stored yet; a block is never skipped silently.

Other commands (`python -m rugyomi.collector --help`): `rebuild-launches`, `rebuild-snapshots`, `rebuild-outcomes` (recompute the stored outcome labels with the config in force), `rebuild-links` (recompute the link tables: linked accounts and funding clusters), `extend-start`,
`recheck-nulls`, `reprocess-quarantine`, `import-launch-index`, `ingest-blocks`, and `score --token T` (the risk card of a launch as of its launch time; writes nothing).

## Run the site

A read-only site over the same database: the latest launches with their risk score, one page per launch and per creator, and `/methodology` (generated from the rules registry). It is a separate
process, opens the database read-only, has no write endpoints, and listens on the local address only.

```
pip install -e ".[web]"
python -m rugyomi.web --db ./rugyomi.db            # http://localhost:8000
```

To read it on a server, forward the port instead of opening it: `ssh -L 8080:localhost:8000 <server>`, then open
`http://localhost:8080`. The pages use no CDN, fonts or trackers; [Pico CSS](https://picocss.com) and
[htmx](https://htmx.org) are vendored in `rugyomi/web/static/` with their licenses.

## Post the cards to Telegram

`python -m rugyomi.publisher run` (a separate process; stdlib only) posts every **live** risk card to a Telegram channel, the 24 h outcome as
a reply to that post, and a daily summary at a configured UTC hour. It reads the collector's database read-only and writes only its own
`publisher.db` (`channel_posts`: one row per post, so a restart never posts twice). Posts are never edited. It is **off by default**.

| Setting | Where | Meaning |
|---|---|---|
| `TG_ALERT_BOT_TOKEN` or `TELEGRAM_BOT_TOKEN` | environment / `.env` | the bot (added to the channel as an admin) |
| `TG_ALERT_CHAT_ID` or `TELEGRAM_ALLOWED_IDS` | environment / `.env` | admin chat(s): where **dry-run** posts go |
| `TG_CHANNEL_ID` | environment / `.env` | the channel (`-100…` or `@name`): where **real** posts go |
| `publisher.enabled` / `publisher.dry_run` | `publisher.json` (defaults) + a JSON override named by `RUGYOMI_PUBLISHER_CONFIG` | off → dry run (admin chat, marked `[dry run]`) → real channel |
| `site.base_url` | same | empty = no site link in the posts |

Only cards issued after the mode was first switched on are posted. Preview without sending: `python -m rugyomi.publisher preview <token>`.

## Modules (`rugyomi/`)

| Module | What it does |
|---|---|
| `neardata.py` | Client for neardata block downloads (key on every request, rate limit, redirects counted). |
| `keys.py` | Where the API key comes from (`FASTNEAR_API_KEY`; a different key source can be plugged in). |
| `parse/facts.py` | One block → raw facts (transfers, token transfers, account creation, launch events). The single source of the fact format and of what is stored. |
| `store/` | SQLite schema with additive migrations, the per-block writer, the cursor and gap detection. |
| `collector.py` | The live loop and the command line; retries network errors forever, quarantines a block whose processing keeps failing. |
| `launches.py` | Detects launches (nearly.trade, justhoot v1 / v2) and the real owner behind the launch bot. |
| `snapshots.py` | The T+5 / 15 / 30 / 60 minute and T+24 h view of a launch (dev, insiders, buyers, price peak, holders), computed from stored facts only; skipped when the block coverage of the window is incomplete. |
| `accounts.py` | When and by whom an account was created. |
| `labels.py` | Manual account labels as a separate append-only layer; raw facts are never edited. |
| `web/` | The read-only site (FastAPI + Jinja2): feed, launch page, creator page, `/healthz`; one module for the SQL (`queries.py`) and one for formatting (`fmt.py`). |
| `rules/` | The rules module (and the accuracy table of the frozen cards against the stored outcomes, `rules/accuracy.py`): signals as of the launch time (no lookahead), the 0–100 risk score with reasons and evidence, frozen risk cards, outcome labels, the rug label (`rug_fast` / `rug_late`, `rules/rug.py`) stored by the collector in `launch_outcomes` (`rules/stored.py`), the methodology text. Defaults in `rules/default_config.json`; a private override can be merged over them. |
| `links.py` | Who is behind a launch (RGY-019): direct links between accounts that launched (funded, created, launch-bot owner, token transfer), linked accounts (connected groups) and funding clusters (a common narrow non-service source), rankings; derived tables `link_*` rebuilt by the collector. Service accounts are an explicit list. |
| `dossier.py` | Creator dossier and rankings over the whole history (RGY-023): derived tables `dossier_launch` / `dossier_flow` / `dossier_group` rebuilt by the collector (hourly, after a link rebuild, a few minutes after new final outcomes); the creator page and `/rankings` read them with plain SQL. Money in / out, buyers' loss, rugs per group — facts, not identities. |
| `publisher.py` | The Telegram publisher (card post, outcome reply, daily summary, `channel_posts`, retries, dry run); a separate process. |
| `history.py` | Launch index import, ingestion of the blocks around historical launches, `creator_launches(account)` (all launches of a creator, from every source), a report on the history. |

## Principles

- Raw facts are append-only and never rewritten; everything else (launches, snapshots) is derived and can be rebuilt.
- A gap is shown, not hidden: skipped heights, quarantined blocks and incomplete windows are part of the status.
- Launches found only in historical blocks (`coverage = launch_only`) get no snapshots, because the surrounding blocks are
  not stored.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE). Noncommercial use, study and modification are allowed; commercial use is
not.
