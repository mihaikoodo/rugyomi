# rugyomi

Risk analytics for token launches on NEAR launchpads ([nearly.trade](https://nearly.trade), [justhoot.fun](https://justhoot.fun)).
For every new token it shows a 0-100 risk score with the reasons behind it, frozen at launch time, and later what actually happened to the buyers.

<!-- facts:begin -->
**The numbers below come from the full database (as of 12 Nov 2026), not from the small demo set described further down.** 4,424 launches seen between 22 Sep 2026 and 12 Nov 2026 (297 on justhoot.fun (v1), 366 on justhoot.fun (v2), 3761 on nearly.trade); 159 risk cards frozen at launch time; 4,274 launches have a final outcome label, and 1,110 of them (26 %) are rugs by the label's definition; 190 groups of linked accounts and 32 funding clusters.

**Does the score work?** Walk-forward check of 2026-11-11: each of 6 weeks (2026-W41 to 2026-W46, 1676 launches) was scored by models fitted only on launches whose outcome was already known when the week began, and compared with one fixed yardstick, the plain rug rate of the previous 2 weeks. A model counts as better only if it beat that rate in more than half of the compared weeks and on average. The best model, 2w/v1, beat that rate in 3 of 5 weeks but lost on average; we do not count that as better. The AUC of the launch-time score ranged from 0.47 to 0.58 by week (0.50 = a coin flip). The weekly rug rate in those weeks ran from 6 % to 47 %. The launch-time score does not yet beat the recent rug rate of the market.

Calibration on history understates risk when the market shifts: in 2026-W43 a model fitted on all earlier launches predicted rug rates of 11 / 14 / 35 % for Low / Elevated / High, and 45 / 38 / 56 % happened.
<!-- facts:end -->

**The problem.** On a launchpad a token appears every few minutes, and most of them are over within the hour: the creator
buys, sells into the first buyers and leaves. The facts that tell these launches apart from the rest (who created the
token, how it was funded, what the creator did in the first minutes) are all on chain, but nobody has them in one place.

**What this repository does.** It follows the NEAR chain block by block, stores every relevant on-chain fact in a local
SQLite database, detects launches on both launchpads, scores each one at the moment it starts, and measures what happened
5 / 15 / 30 / 60 minutes and 24 hours later. Every number can be traced back to a transaction. A score is a card that is
written once and never edited; a public hash-chained ledger of the cards lets anyone check that none was changed afterwards.

## Try it in two minutes (no key, no chain access)

The repository ships a small demo database, so the site runs offline.

<!-- demo:begin -->
**Demo subset.** `demo/rugyomi_demo.db.gz` holds 405 of the 4,424 launches of the full database, copied on 12 Nov 2026. They were chosen by a fixed rule (seed rugyomi-demo-1): the reference cases and the 29.10 chain, the launches of their linked accounts and funding clusters (and of the 3 largest funding clusters), every launch of the last 2 full days, and a sample stratified by launchpad and week. The tables on the Accuracy and Rankings pages are computed on this subset only; the walk-forward summary is a stored result computed on the full database. Every page of the site works on it; pages that count over all launches (Accuracy, Rankings) count over this subset only. The numbers above are not computed from it.
<!-- demo:end -->

```
git clone <this repository> && cd rugyomi
pip install -e ".[web]"            # Python 3.12 or newer
python -m rugyomi.web --demo       # http://localhost:8000
```

| Do this | See this |
|---|---|
| Open `http://localhost:8000/` | The feed: launches with their score, risk band and what is known so far. A banner on every page says this is a demo subset. |
| Open the page of a launch: `/launch/refrax.nearlytrade.near` | The frozen risk card with each reason and its evidence, the 5 / 15 / 30 / 60 minute snapshots, the outcome label, who bought first and who holds the supply. |
| Open the creator page: `/creator/thecoins.near` | The account's own numbers next to its group's: accounts linked to it, the funding trail that links them (each link with its transaction), and every launch of the group. Links are facts about accounts, never a claim that they are one person. |
| Open `/creator/coinisland.near` | A manually set account label, shown with its public reason. |
| Open `/methodology` | Every rule, weight and threshold, generated from the same code that scores launches. |
| Open `/accuracy` and `/rankings` | The frozen cards against their stored outcomes, and creators ranked by buyers' losses. In the demo they count over the demo subset only. |
| `python verify_ledger.py ledger/ledger.jsonl` (the ledger copy, once it is added to `ledger/`) | `OK` and the head hash, or the first broken line. Standard library only. |

## Why NEAR

- The facts the score needs are all in blocks: account creation, who funded whom, token transfers and the launchpad contracts' own events. NEAR's named accounts and receipts make a funding trail readable straight from the blocks.
- Both launchpads run on NEAR and create a token every few minutes, with a window of minutes in which the creator can take money out: this is the time scale the score has to work on, and NEAR's block time of about a second makes that possible live.
- Everything is derived from blocks you can download yourself, so any card can be recomputed and any number checked.

## Limitations

- The score is a risk estimate from on-chain behaviour, not a verdict and not advice. It does not know what happens off chain, and a "Low" card is not a guarantee.
- The weights are provisional. The first screen above says how the score did against a plain baseline; read it before relying on the score.
- Only two launchpads, nearly.trade and justhoot.fun, are parsed. A third launchpad, or a changed contract, needs new parsing code.
- Launches before the collector started have no 5 / 15 / 30 / 60 minute snapshots, so they show less detail.
- The demo is a subset (see above). Nothing in the demo updates; the live site follows the chain.

## Built with AI

This project was built together with Claude, Anthropic's AI assistant. The author set the goals and made the decisions; Claude wrote code and tests together with the author and helped analyse the results. The offline tests (`python -m pytest -q`) and the open ledger are there so that you do not have to take either of them on trust.

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

## Libraries

- The collector, parser, rules, snapshots and publisher use the Python standard library only (Python 3.12 or newer).
- The site: [FastAPI](https://fastapi.tiangolo.com) 0.162.0, [Jinja2](https://jinja.palletsprojects.com) 3.1.6 (autoescape on), [Uvicorn](https://www.uvicorn.org) 0.54.0 - exact pins in `pyproject.toml`.
- Front end, vendored in `rugyomi/web/static/` with their licenses: [Pico CSS](https://picocss.com) and [htmx](https://htmx.org). No CDN, fonts or trackers.
- Tests: [pytest](https://pytest.org) 9.1.1 and [httpx](https://www.python-httpx.org) 0.28.1 (FastAPI's test client).
- Storage: SQLite (standard library `sqlite3`).

## License

[PolyForm Noncommercial License 1.0.0](LICENSE). Noncommercial use, study and modification are allowed; commercial use is
not.
