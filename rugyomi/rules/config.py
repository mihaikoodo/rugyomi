"""Rules configuration: public defaults + an optional private override (RGY-015).

The defaults are `default_config.json` in this package (public, provisional: "v0 — calibration in progress"). The
production values stay private: a JSON file named by `$RUGYOMI_RULES_CONFIG` is merged over the defaults (only the keys
it names; a leaf replaces the default leaf). Every card stores `config_version` (the `version` of the effective config)
and `config_hash` (a hash of the whole effective config), so a card is always tied to the numbers that produced it.

A broken override never silently falls back to the defaults on the server: `load_config` raises `ConfigError`, the
collector logs it and issues no card for that launch (the block itself still commits).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_PATH = HERE / "default_config.json"
ENV = "RUGYOMI_RULES_CONFIG"
# RGY-021: a rules PROFILE is a full public default file. "v0" is what the live collector issues; "v1" is the proposed one (history
# aware signals). A profile is picked by the caller (the backtest) or by $RUGYOMI_RULES_PROFILE — the live switch is a separate
# decision ("v1 confirmed"), so nothing changes while the variable is unset. A profile other than v0 never changes the v0 hash.
PROFILE_ENV = "RUGYOMI_RULES_PROFILE"
PROFILES = {"v0": DEFAULT_PATH, "v1": HERE / "default_config_v1.json"}


class ConfigError(ValueError):
    pass


def _profile(profile: str | None) -> str:
    p = profile or os.environ.get(PROFILE_ENV) or "v0"
    if p not in PROFILES:
        raise ConfigError(f"unknown rules profile {p!r} (known: {', '.join(sorted(PROFILES))})")
    return p


def _defaults(profile: str | None = None) -> dict[str, Any]:
    return json.loads(PROFILES[_profile(profile)].read_text(encoding="utf-8"))


def _merge(base: Any, over: Any, path: str = "") -> Any:
    """Deep merge `over` into a copy of `base`. Unknown keys and type changes are errors (a typo must not pass)."""
    if isinstance(base, dict):
        if not isinstance(over, dict):
            raise ConfigError(f"{path or 'config'}: expected an object")
        out = copy.deepcopy(base)
        for k, v in over.items():
            if k not in base:
                raise ConfigError(f"{path + '.' if path else ''}{k}: unknown key")
            out[k] = _merge(base[k], v, f"{path}.{k}" if path else k)
        return out
    if isinstance(base, list):
        if not isinstance(over, list) or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in over):
            raise ConfigError(f"{path}: expected a list of numbers")
        return list(over)
    if isinstance(base, bool):
        if not isinstance(over, bool):
            raise ConfigError(f"{path}: expected true / false")
        return over
    if isinstance(base, (int, float)):
        if isinstance(over, bool) or not isinstance(over, (int, float)):
            raise ConfigError(f"{path}: expected a number")
        return over
    if isinstance(base, str):
        if not isinstance(over, str):
            raise ConfigError(f"{path}: expected a string")
        return over
    return over


def effective(override: dict[str, Any] | None = None, profile: str | None = None) -> dict[str, Any]:
    base = _defaults(profile)
    cfg = base if not override else _merge(base, override)
    b = cfg["bands"]
    if not 0 < b["elevated"] < b["high"] <= 100:
        raise ConfigError("bands: need 0 < elevated < high <= 100")
    return cfg


LINK_SECTIONS = ("links", "clusters")           # RGY-019: parameters of the link tables, not of the cards


def config_hash(cfg: dict[str, Any]) -> str:
    """The hash a card (and a stored outcome) carries: the whole config EXCEPT the link sections — those parametrize
    the derived link tables (`links_config_hash`), and adding them must not change the hash of the numbers that the cards
    already issued were computed with."""
    body = {k: v for k, v in cfg.items() if k not in LINK_SECTIONS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


def links_config_hash(cfg: dict[str, Any]) -> str:
    body = {k: cfg[k] for k in LINK_SECTIONS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


_cache: dict[tuple, dict[str, Any]] = {}


def load_config(path: str | Path | None = None, profile: str | None = None) -> dict[str, Any]:
    """The effective config: the profile's defaults (v0 unless `profile` / $RUGYOMI_RULES_PROFILE says otherwise), merged with
    the override file (argument, else $RUGYOMI_RULES_CONFIG). Cached by (path, mtime, size, profile): a changed file takes effect
    on the next card without a restart."""
    p = path or os.environ.get(ENV)
    prof = _profile(profile)
    if not p:
        return effective(None, prof)
    p = Path(p)
    try:
        st = p.stat()
    except OSError as e:
        raise ConfigError(f"{p}: cannot read ({e.strerror})") from None
    key = (str(p), st.st_mtime_ns, st.st_size, prof)
    if key not in _cache:
        try:
            over = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ConfigError(f"{p}: not valid JSON ({type(e).__name__})") from None
        _cache.clear()
        _cache[key] = effective(over, prof)
    return copy.deepcopy(_cache[key])


def default_config(profile: str | None = None) -> dict[str, Any]:
    """The public defaults only — what /methodology shows. `profile`: "v0" (default) | "v1"."""
    return effective(None, profile)
