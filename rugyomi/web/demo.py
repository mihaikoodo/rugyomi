"""RGY-048: the demo database. The repository ships `demo/rugyomi_demo.db.gz` (a fixed sample taken from the live database, no secrets);
`python -m rugyomi.web --demo` unpacks it once into a cache directory and serves it read-only. No network, no keys."""
from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]                 # the directory that holds rugyomi/ and demo/
DEMO_GZ = REPO / "demo" / "rugyomi_demo.db.gz"


def unpack(gz: Path | None = None, cache: Path | None = None) -> Path:
    """The path of the unpacked database; unpacked only when the cache has no copy of THIS file (named by the hash of the .gz)."""
    gz = Path(gz) if gz else DEMO_GZ
    if not gz.is_file():
        raise FileNotFoundError(f"no demo database at {gz} (run from a full clone of the repository)")
    h = hashlib.sha256()
    with open(gz, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    cache = Path(cache) if cache else Path(tempfile.gettempdir()) / "rugyomi-demo"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"rugyomi_demo-{h.hexdigest()[:16]}.db"
    if out.is_file():
        return out
    tmp = out.with_suffix(f".tmp{os.getpid()}")
    with gzip.open(gz, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp, out)
    return out
