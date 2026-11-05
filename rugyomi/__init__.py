"""rugyomi — risk analytics for token launches on NEAR launchpads.

Every launch gets a risk score within seconds and a full breakdown after an hour. Every claim links to a
transaction; every prediction is scored against the outcome in public.

Package map:
  keys        where the FastNear API key comes from (KeySource; default: one FASTNEAR_API_KEY)
  neardata    final blocks from neardata (FastNear)
  parse       raw facts out of a block, each with a stable key (receipt, origin)
  store       SQLite schema, migrations, the per-block writer, cursor and gaps
  launches    launch detection from stored facts (creator, real owner behind launchbot)
  collector   the live follow loop and `python -m rugyomi.collector run|status`
"""
__version__ = "0.0.1"
