# The forecast ledger

Every live risk card rugyomi issues is one line of `ledger.jsonl`, in the order the cards were issued. Each line carries the hash of the line before it, so a card
added, removed or changed later breaks every hash after it. Retro cards of history launches are not forecasts and are not here.

* `ledger.jsonl` — the chain. One canonical JSON object per line: `token`, `issued_at`, `issued_height`, `launch_ts_ns`, `score`, `band`, `reasons`, `signals`,
  `dev_account`, `config_version`, `config_hash`, `rules_version`, `computed_ms`, `prev_hash`, `hash`. The first line starts from 64 zeros (the genesis hash); `hash` is
  the sha256 of the line's canonical JSON without the `hash` key.
* `outcomes.jsonl` — the outcome labels, one line per card, keyed by token and `card_hash`, with the label version. They are not in the chain: an outcome is not
  known at launch and changes until it is final (24 hours).
* The same files are served live at `/ledger.jsonl`, `/ledger/head` and `/ledger/outcomes.jsonl` of the site; this directory is a copy made when the repository is
  updated (it only ever grows at its end).

Canonical form: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)` — keys sorted, no spaces, non-ASCII escaped, integers as
integers, floats as Python prints them.

**Anchor.** Once a day the channel gets a post `anchor COUNT:HASH`: the number of cards and the head hash at the end of the previous UTC day. Our policy is that we do not edit
channel posts, and a post's date is the platform's: an anchor lets anyone compare the chain with what we posted at that time. It is not a guarantee that a post cannot be changed, and it
does not replace checking the chain itself.

```
python verify_ledger.py ledger/ledger.jsonl --anchor 113:<hash from the post>
```

prints `OK` and the head, or `BROKEN at line N: …`. The first card is the first live card; cards issued before the ledger existed were chained once, in the order they
were issued.
