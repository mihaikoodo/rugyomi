"""Bins and rate-by-bin tables for the read-only research diagnostics (RGY-039), standard library only, pure: values in, labels and numbers out.

`runner_features_t15.py` (RGY-034) carries its own copy of the same binners inside the script; this module is where new diagnostics take them from.

  ranges((lo, hi), ...)          binner for integer counts; hi None = open
  tercile_binner(values, ...)    bins from the TRAIN inputs only: <= 0 (and < 0), then the terciles of the positive values
  maxx_bins / yesno / devsold_bins   fixed bins
  cell(rows, base, key)          (n, k, share text, 90 % Wilson interval text, lift text) of the rows whose `key` is truthy
  one_hot(prefix, binner, value) the feature id of the bin a value falls in
A binner is a function value -> (label, order) with `.labels` (all labels in order, 'n/a' last). Nothing here sees an outcome except `cell`, which is given the label key.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from .accuracy import wilson


def ranges(*pairs: tuple[int, int | None]) -> Callable[[Any], tuple[str, int]]:
    labels = [(f"{lo}" if lo == hi else f"{lo}+" if hi is None else f"{lo}-{hi}") for lo, hi in pairs]

    def f(v: Any) -> tuple[str, int]:
        if v is None:
            return "n/a", len(labels)
        for i, (lo, hi) in enumerate(pairs):
            if v >= lo and (hi is None or v <= hi):
                return labels[i], i
        return "n/a", len(labels)
    f.labels = labels + ["n/a"]  # type: ignore[attr-defined]
    return f


def tercile_binner(values: Iterable[float | None], zero_label: str = "0", fmt: str = "{:g}", negative: bool = False) -> Callable[[Any], tuple[str, int]]:
    """Bins from the TRAIN inputs only: 'n/a' for None, a bin for <= 0 (and one for < 0 when `negative`), and the terciles of the positive values."""
    pos = sorted(v for v in values if v is not None and v > 0)
    edges: list[float] = []
    if pos:
        for p in (1 / 3, 2 / 3):
            e = pos[min(len(pos) - 1, int(p * len(pos)))]
            if not edges or e > edges[-1]:
                edges.append(e)
    labels = (["< 0"] if negative else []) + [zero_label]
    lo = 0.0
    for e in edges:
        labels.append(f"({fmt.format(lo)}, {fmt.format(e)}]")
        lo = e
    labels.append(f"> {fmt.format(lo)}" if edges else "> 0")
    k0 = 1 if negative else 0

    def f(v: Any) -> tuple[str, int]:
        if v is None:
            return "n/a", len(labels)
        if negative and v < 0:
            return labels[0], 0
        if v <= 0:
            return labels[k0], k0
        for i, e in enumerate(edges):
            if v <= e:
                return labels[k0 + 1 + i], k0 + 1 + i
        return labels[-1], len(labels) - 1
    f.labels = labels + ["n/a"]  # type: ignore[attr-defined]
    f.edges = edges  # type: ignore[attr-defined]
    return f


def maxx_bins(v: float | None) -> tuple[str, int]:
    labels = ["no outside buyer yet", "< 1", "1 - 1.5", "1.5 - 2", "2 - 3", ">= 3"]
    if v is None:
        return labels[0], 0
    for i, e in enumerate((1, 1.5, 2, 3), 1):
        if v < e:
            return labels[i], i
    return labels[5], 5


maxx_bins.labels = ["no outside buyer yet", "< 1", "1 - 1.5", "1.5 - 2", "2 - 3", ">= 3", "n/a"]  # type: ignore[attr-defined]


def yesno(v: Any) -> tuple[str, int]:
    return ("n/a", 2) if v is None else (("yes", 1) if v else ("no", 0))


yesno.labels = ["no", "yes", "n/a"]  # type: ignore[attr-defined]


def devsold_bins(v: int | None) -> tuple[str, int]:
    if v is None:
        return "n/a", 3
    return ("0", 0) if v <= 0 else ("1 - 5000 bps", 1) if v <= 5000 else ("> 5000 bps", 2)


devsold_bins.labels = ["0", "1 - 5000 bps", "> 5000 bps", "n/a"]  # type: ignore[attr-defined]


def share_text(k: int, n: int) -> str:
    return "-" if not n else f"{100 * k / n:.1f}%"


def cell(rows: list[dict[str, Any]], base: float | None, key: str = "rug") -> tuple[int, int, str, str, str]:
    """n, k, share, 90 % interval, lift of the rows whose `key` is truthy; `base` is the rate of the same group and part."""
    n = len(rows)
    k = sum(1 for r in rows if r[key])
    ci = wilson(k, n)
    lift = "-" if not n or not base else f"x{(k / n) / base:.2f}"
    return n, k, share_text(k, n), (f"{100 * ci[0]:.1f}-{100 * ci[1]:.1f}%" if ci else "-"), lift


def one_hot(prefix: str, binner: Callable[[Any], tuple[str, int]], value: Any) -> str:
    """The id of the one-hot feature for the bin `value` falls in. No ':' in it (the fit names a feature `id:points`)."""
    return f"{prefix}={binner(value)[0]}".replace(":", "-")
