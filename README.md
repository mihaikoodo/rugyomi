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
| Live collector (neardata blocks → raw facts → launches), restart-safe, one SQLite transaction per block | Risk score and its published methodology |
| Parsing of nearly.trade, justhoot v1 and v2 launches; real owner of launches made through the nearly launch bot | Website and Telegram channel |
| Launch snapshots at T+5 / 15 / 30 / 60 min (dev, buyers, price, holders) | Public API / MCP server |
| Import of the history of all earlier launches (launch index + the blocks around each launch) | Public accuracy table |
| Quarantine of a block that cannot be processed, honest status (`status`), manual account labels | |

The project is a hackathon entry, so the interface is a command line for now.

## Install and test

Python 3.12 or newer, no third-party runtime dependencies.

```
pip install -e ".[test]"
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

Other commands (`python -m rugyomi.collector --help`): `rebuild-launches`, `rebuild-snapshots`, `extend-start`,
`recheck-nulls`, `reprocess-quarantine`, `import-launch-index`, `ingest-blocks`.

## Modules (`rugyomi/`)

| Module | What it does |
|---|---|
| `neardata.py` | Client for neardata block downloads (key on every request, rate limit, redirects counted). |
| `keys.py` | Where the API key comes from (`FASTNEAR_API_KEY`; a different key source can be plugged in). |
| `parse/facts.py` | One block → raw facts (transfers, token transfers, account creation, launch events). The single source of the fact format and of what is stored. |
| `store/` | SQLite schema with additive migrations, the per-block writer, the cursor and gap detection. |
| `collector.py` | The live loop and the command line; retries network errors forever, quarantines a block whose processing keeps failing. |
| `launches.py` | Detects launches (nearly.trade, justhoot v1 / v2) and the real owner behind the launch bot. |
| `snapshots.py` | The T+5 / 15 / 30 / 60 minute view of a launch, computed from stored facts only; skipped when the block coverage of the window is incomplete. |
| `accounts.py` | When and by whom an account was created. |
| `labels.py` | Manual account labels as a separate append-only layer; raw facts are never edited. |
| `history.py` | Launch index import, ingestion of the blocks around historical launches, `creator_launches(account)` (all launches of a creator, from every source), a report on the history. |

## Principles

- Raw facts are append-only and never rewritten; everything else (launches, snapshots) is derived and can be rebuilt.
- A gap is shown, not hidden: skipped heights, quarantined blocks and incomplete windows are part of the status.
- Launches found only in historical blocks (`coverage = launch_only`) get no snapshots, because the surrounding blocks are
  not stored.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE). Noncommercial use, study and modification are allowed; commercial use is
not.
