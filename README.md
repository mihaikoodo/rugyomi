# rugyomi

Risk analytics for token launches on NEAR launchpads ([nearly.trade](https://nearly.trade), [justhoot.fun](https://justhoot.fun)).
For every new token it shows a 0-100 risk score with the reasons behind it, frozen at launch time, and later what actually happened to the buyers.

**Live site: [mihaikoodo.dev](https://mihaikoodo.dev)** · code: [github.com/mihaikoodo/rugyomi](https://github.com/mihaikoodo/rugyomi) · JSON API and badge: [see below](#json-api-and-badge) · built for the NEAR HACKATHON ([how this repository relates to it](#built-in-the-hackathon-window)).

<!-- facts:begin -->
**The numbers below come from the full database (as of 14 Nov 2026), not from the small demo set described further down.** 4,451 launches seen between 22 Sep 2026 and 14 Nov 2026 (297 on justhoot.fun (v1), 374 on justhoot.fun (v2), 3780 on nearly.trade); 186 risk cards frozen at launch time; 4,023 launches have a final outcome label, and 1,126 of them (28 %) are rugs by the label's definition; 190 groups of linked accounts and 32 funding clusters.

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
git clone https://github.com/mihaikoodo/rugyomi && cd rugyomi
pip install -e ".[web]"            # Python 3.12 or newer
python -m rugyomi.web --demo       # http://localhost:8000
```

| Do this | See this |
|---|---|
| Open the live site, [mihaikoodo.dev](https://mihaikoodo.dev) | The same pages on the current database, which follows the chain: new launches appear with their card within minutes. The demo below needs no network and no key. |
| Open `http://localhost:8000/` | The feed: launches with their score, risk band and what is known so far. A banner on every page says this is a demo subset. |
| Open the page of a launch: `/launch/refrax.nearlytrade.near` | The frozen risk card with each reason and its evidence, the 5 / 15 / 30 / 60 minute snapshots, the outcome label, who bought first and who holds the supply. A buyer wallet with a recorded label (set by hand, with its public reason, or derived from the account name by a published list, e.g. the sub-accounts of a trading bot) carries a tag. |
| Open the creator page: `/creator/thecoins.near` | The account's own numbers next to its group's: accounts linked to it, the funding trail that links them (each link with its transaction), and every launch of the group. Links are facts about accounts, never a claim that they are one person. |
| Open `/methodology` | The signals, points and thresholds of the rules profile that scores new launches, read from the same configuration the cards use; earlier profiles are marked as such. |
| Open `/accuracy` and `/rankings` | The frozen cards against their stored outcomes, and creators ranked by buyers' losses. In the demo they count over the demo subset only. |
| `python verify_ledger.py ledger/ledger.jsonl` | `OK`, the number of cards and the head hash, or the first broken line. Standard library only. The file in the repository is a copy of the journal as of 10 Nov 2026 (117 cards); the live journal has more, only grows at its end and verifies the same way: `curl -s https://mihaikoodo.dev/ledger.jsonl -o live-ledger.jsonl && python verify_ledger.py live-ledger.jsonl`. |

## Why NEAR

- The facts the score needs are all in blocks: account creation, who funded whom, token transfers and the launchpad contracts' own events. NEAR's named accounts and receipts make a funding trail readable straight from the blocks.
- The launch bot hides the creator on chain, but not the money: for `extract.nearlytrade.near` the creator is `launchbot.nearlytrade.near`, and the account that paid the launch bot for it is `mario12.near` (transaction `2YmcxF54YCdb7tDEruiYD6qUcJLVCh7LESRmbkwsQNLe`). The card is about that real owner.
- Named accounts can imitate each other (`name_imitation` compares a creator's account name with a published list of notable accounts), and money that came through NEAR Intents or an exchange does not show where it started (`fresh_obfuscated_funding`). Both are expert rules whose points are set by hand, listed on `/methodology`.
- Both launchpads run on NEAR and create a token every few minutes, with a window of minutes in which the creator can take money out: this is the time scale the score has to work on, and NEAR's block time of about a second makes that possible live.
- Everything is derived from blocks you can download yourself, so any card can be recomputed and any number checked.

## Limitations

- The score is a risk estimate from on-chain behaviour, not a verdict and not advice. It does not know what happens off chain, and a "Low" card is not a guarantee.
- The live score is not calibrated yet: part of its weights were fitted on history, part are set by hand. The first screen above says how the score did against a plain baseline; read it before relying on the score. The history that was held back to test it was read again when the profiles were compared, so it is a check, not a clean test.
- Only two launchpads, nearly.trade and justhoot.fun, are parsed. A third launchpad, or a changed contract, needs new parsing code.
- Launches before the collector started have no 5 / 15 / 30 / 60 minute snapshots, so they show less detail.
- The demo is a subset (see above). Nothing in the demo updates; the live site follows the chain.

## Built in the hackathon window

All code in this repository was written between 28 October 2026 and 14 November 2026, during the NEAR HACKATHON development window; the git history from the first commit shows it. The state that was submitted is the tag `hackathon-submission` ([that tag](https://github.com/mihaikoodo/rugyomi/tree/hackathon-submission), [all commits up to it](https://github.com/mihaikoodo/rugyomi/commits/hackathon-submission)). Commits after the tag are not part of the submission.

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
`http://localhost:8080`. The pages use no CDN, fonts or trackers; [htmx](https://htmx.org) is vendored in `rugyomi/web/static/` with its license.

## JSON API and badge

A read-only API on the same host as the site, for a dashboard or a bot. GET and HEAD only; there is no key, no account and no cookie, and nothing can be written.
Every body is JSON with `"api": "v1"`; an error is `{"api": "v1", "error": "..."}`. A scored answer is cached for 60 seconds (`Cache-Control: public, max-age=60`); an unknown launch, a launch that has no card yet and every error are not cached (`Cache-Control: no-store`). Answers may be read from any web page (`Access-Control-Allow-Origin: *`).
The score is the stored card, the same number the page of the launch shows; the API computes nothing of its own. Cloudflare in front of the site allows 60 requests per 10 seconds from one IP address; poll with `since` instead of repeating a full list.

| Endpoint | Answer |
|---|---|
| `/api/v1/launch/{token}` | `token`, `launchpad`, `symbol` (plain names only), `creator`, `owner`, `launched_at` (UTC), `coverage`, `quote_token` (null when the pool is paired with NEAR), `risk`, `outcome`, `site_url`. `risk` is `null` when the launch has no card yet. |
| `/api/v1/launches?since=&limit=` | The newest launches first: `token`, `launchpad`, `creator`, `launched_at`, `risk` (`kind`, `score`, `band`), `outcome`, `site_url`. `limit` is 1 to 200 (default 50); `since` is unix seconds or an ISO 8601 UTC date or date-time (launches at or after it). A bad `limit` or `since` is a 400 and is not echoed. |
| `/api/v1/account/{account}` | `labels`, `created`, `launches_as_creator` (`count` and `outcomes`: rug, no_rug, undefined, waiting, not_tracked) and `trader`: the summary of the account's rows as an early buyer (counts and the profit and loss total at T+60 min, no rows; the rows are on `rows_url`), or `null`. |
| `/api/v1/meta` | The rules profile in force, the three bands with their score ranges and colours, the recent rug rate with its one-line text, the disclaimer ("Analysis, not advice.") and the links to the methodology, the accuracy page and the ledger. |

`risk.kind` is `live` for a card issued at launch time: it has `issued_at`, `rules_version`, `config_version`, `config_hash`, the `ledger_hash` of its line in the public ledger and the `reasons` (`id`, `points`, `text`, `short`, and `evidence` with a link to each transaction or account). Any other value marks a card computed afterwards from the same facts: it is not a forecast published at the time, it carries `as_of`, `computed_at`, `in_sample` and a `note` saying so, and it has no ledger hash. `outcome` is `null` until the first label is stored; its `state` is `rug`, `no rug`, `undefined` or `waiting`, as on the pages, and a launch in the first 24 hours is `waiting` unless a rug has already been seen. An unknown or malformed id is a 404 `{"api": "v1", "error": "not found"}`.

**Badge.** `/badge/{token}.svg` is a small SVG for an `<img>` tag (no script, no cookie). `?style=a` (default) is one pill in the band colour, "Risk 45 · Elevated". `?style=b` is the lens of the logo with the number in the ring, the band word and the name beside it, always the same size (116 x 40); it has a dark tile by default and a light one with `?theme=light`. A card computed afterwards is drawn like a live one (the API tells them apart in `risk.kind`). A launch without a card is "pending", an unknown token answers 404 with a neutral "unknown" badge.

```html
<img src="https://mihaikoodo.dev/badge/TOKEN.svg" alt="rugyomi risk score">
<img src="https://mihaikoodo.dev/badge/TOKEN.svg?style=b" alt="rugyomi risk score">
<img src="https://mihaikoodo.dev/badge/TOKEN.svg?style=b&theme=light" alt="rugyomi risk score">
```

```
curl https://mihaikoodo.dev/api/v1/launch/TOKEN
curl "https://mihaikoodo.dev/api/v1/launches?since=2026-11-13T00:00:00Z&limit=20"
```

The score is a risk estimate from on-chain behaviour, not advice and not a verdict.

## Use cases

A trading dashboard or a bot can show the risk of a launch next to the token with one `<img>` tag. We prepared such an integration for [Sendler Dash](https://dash.sendler.xyz), a public NEAR dashboard; see [docs/badge.md](docs/badge.md) for the embed code, the styles, the limits and the score file for the past. An AI agent can read the same data through the MCP server below.

## Use it from an AI agent

`rugyomi-mcp` is a small [MCP](https://modelcontextprotocol.io) server (stdio) that lets an AI agent read the same data as the JSON API above. It is read-only by construction: four tools, all GET requests to `/api/v1`, no POST, no key, no other host.

| Tool | Answer |
|---|---|
| `get_launch_risk(token)` | The stored risk card of one launch, with the reasons and the evidence, and its outcome. |
| `recent_launches(since, limit)` | The newest launches with score, band and outcome (`limit` 1 to 200, default 50). |
| `get_account(account)` | An account as a creator (launches and their outcomes) and as an early buyer. |
| `get_meta()` | The rules profile in force, the bands, the recent rug rate, the disclaimer. |

The score is the risk of a rug within 24 hours: analysis, not advice. A tool returns the API JSON unchanged; a bad id or an unknown record is a readable error, never a stack trace.

Install with `pip install -e ".[mcp]"`. Test it against the live site first, with `--base-url`; if that is not possible (no network, or the site is down), use `--demo`, which answers from the demo database shipped with the repository and needs no network (it needs the `web` extra too: `pip install -e ".[mcp,web]"`). In demo mode `site_url` is a relative path.

```
python -m rugyomi.mcp --base-url https://mihaikoodo.dev   # the public site (also the default)
python -m rugyomi.mcp --base-url http://localhost:8000    # your own copy of the site
python -m rugyomi.mcp --demo                              # offline fallback, demo database
```

The address can also be set with the `RUGYOMI_API_BASE` environment variable; the flag wins. Register the server with an agent that speaks MCP over stdio. For Claude Code, in `.mcp.json` (or with `claude mcp add rugyomi -- python -m rugyomi.mcp --base-url https://mihaikoodo.dev`):

```
{
  "mcpServers": {
    "rugyomi": { "command": "python", "args": ["-m", "rugyomi.mcp", "--base-url", "https://mihaikoodo.dev"] }
  }
}
```

For Codex, in `~/.codex/config.toml` (the key is `mcp_servers`):

```
[mcp_servers.rugyomi]
command = "python"
args = ["-m", "rugyomi.mcp", "--base-url", "https://mihaikoodo.dev"]
```

Offline fallback: replace the two arguments `"--base-url", "https://mihaikoodo.dev"` with `"--demo"`. Then ask, for example: "List the five newest launches and explain the risk of the first one." The agent calls `recent_launches`, then `get_launch_risk`, and answers from the reasons and the transactions linked there.

## Post the cards to Telegram

`python -m rugyomi.publisher run` (a separate process; stdlib only) posts every **live** risk card to a Telegram channel and a daily summary at a configured UTC hour
(the 24 h outcome as a reply to the card is a setting, `publisher.outcome_replies`, and is off by default). It reads the collector's database read-only and writes only its own
`publisher.db` (`channel_posts`: one row per post, so a restart never posts twice). We do not edit posts. It is **off by default**. A token symbol chosen by a creator is shown only if it is plain (letters, digits, `$_-`, up to 24 characters); otherwise the first label of the token id is shown, so a symbol cannot carry a link or a claim into a post.

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
| `quote_price.py` | The NEAR price of a quote token from its own swaps with wNEAR (no look-ahead, no outside feed) and the all-or-nothing conversion of a launch's trades to NEAR. One function reads a price with a maximum age: outcomes and cards use at most 15 minutes, and without a provable price the launch stays undetermined; the trader pages show the last known price up to 24 hours old, marked «≈» with the price's age. |
| `quote_apply.py` | Read-only before / after of the quote conversion (outcomes of the launches not paired with wNEAR computed in memory next to the stored ones, examples with transactions, the issued cards the converted dev buy would score differently) and the rebuild of their snapshots and outcomes by the existing paths. |
| `accounts.py` | When and by whom an account was created. |
| `labels.py` | Manual account labels as a separate append-only layer; raw facts are never edited. |
| `web/` | The read-only site (FastAPI + Jinja2): feed, launch page, creator page, `/healthz`, `/robots.txt` (a crawl delay for ClaudeBot; `?all=`, the HTML fragments, the badges and the ledger files are off limits); one module for the SQL (`queries.py`) and one for formatting (`fmt.py`). `api.py` is the JSON API and `badge.py` the SVG badge; both read through the same query functions as the pages. |
| `risk_snapshot.py` | The score file for a developer who does not want to call the API for the past: every launch with a score as JSON, CSV and a small token-to-score file (`python -m rugyomi.risk_snapshot`); reads the database read-only, standard library only. |
| `mcp/` | The read-only MCP server (stdio) over `/api/v1`: four tools, GET only; `--demo` runs the real site code in process on the demo database. `backend.py` talks to the site or the demo, `server.py` defines the tools. |
| `rules/` | The rules module (and the accuracy table of the frozen cards against the stored outcomes, `rules/accuracy.py`): signals as of the launch time (no lookahead), the 0–100 risk score with reasons and evidence, frozen risk cards, outcome labels, the rug label (`rug_fast` / `rug_late`, `rules/rug.py`) stored by the collector in `launch_outcomes` (`rules/stored.py`), the methodology text. Defaults in `rules/default_config.json`; a private override can be merged over them. |
| `links.py` | Who is behind a launch (RGY-019): direct links between accounts that launched (funded, created, launch-bot owner, token transfer), linked accounts (connected groups) and funding clusters (a common narrow non-service source), rankings; derived tables `link_*` rebuilt by the collector. Service accounts are an explicit list. |
| `dossier.py` | Creator dossier and rankings over the whole history (RGY-023): derived tables `dossier_launch` / `dossier_flow` / `dossier_group` rebuilt by the collector (hourly, after a link rebuild, a few minutes after new final outcomes); the creator page and `/rankings` read them with plain SQL. Money in / out, buyers' loss, rugs per group — facts, not identities. |
| `publisher.py` | The Telegram publisher (card post, outcome reply, daily summary, `channel_posts`, retries, dry run); a separate process. |
| `buyers.py` | Who buys a launch (derived, rebuildable): the first buyers of every launch, and the recorded links between them (bundles, trader groups) - facts with transactions, never "one person". |
| `holdings.py` | Who holds the supply of a launch at T+5 / 15 / 60 minutes: the dev, the wallets the dev sent tokens to, early buyers, the top-10 holders (derived, read-only on the site). |
| `livestate.py` | The live state of a launch: what its trades show right now and one number from a fixed formula. Not a forecast and not part of the risk score. |
| `cardfix.py` | Cards issued before a fix: which issued cards a later fix would have scored higher, derived and shown as a note. An issued card is never changed. |
| `ledger.py` | The forecast ledger: every live card as one line of a hash chain; `verify_ledger.py` checks it with the standard library only. |
| `safe_text.py` | The plain-name rule for a creator-chosen token symbol in posts and reasons. |
| `history.py` | Launch index import, ingestion of the blocks around historical launches, `creator_launches(account)` (all launches of a creator, from every source), a report on the history. |

## Principles

- Raw facts are append-only and never rewritten; everything else (launches, snapshots) is derived and can be rebuilt.
- A gap is shown, not hidden: skipped heights, quarantined blocks and incomplete windows are part of the status.
- Launches found only in historical blocks (`coverage = launch_only`) get no snapshots, because the surrounding blocks are
  not stored.

## Libraries

- The collector, parser, rules, snapshots and publisher use the Python standard library only (Python 3.12 or newer).
- The site: [FastAPI](https://fastapi.tiangolo.com) 0.162.0, [Jinja2](https://jinja.palletsprojects.com) 3.1.6 (autoescape on), [Uvicorn](https://www.uvicorn.org) 0.54.0 - exact pins in `pyproject.toml`.
- Front end, vendored in `rugyomi/web/static/` with its license: [htmx](https://htmx.org). No CDN, fonts or trackers.
- The MCP server (`rugyomi/mcp/`, optional extra `mcp`): the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 2.3.0, exact pin in `pyproject.toml`.
- Tests: [pytest](https://pytest.org) 9.1.1 and [httpx](https://www.python-httpx.org) 0.28.1 (FastAPI's test client).
- Storage: SQLite (standard library `sqlite3`).

## License

[PolyForm Noncommercial License 1.0.0](LICENSE). Noncommercial use, study and modification are allowed; commercial use is
not.
