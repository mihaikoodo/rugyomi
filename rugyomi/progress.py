"""Progress lines for the long runs (the rebuilds, the feature fill): one line per 500 launches, not one per commit (a run over thousands of launches used to print
a line every 25-50 and bury the output). `tick(done)` may be called as often as convenient; it logs only when `done` has passed the next multiple of `step`, and the line
says that multiple ("features 500/4405" = at least 500 done)."""
from __future__ import annotations

from typing import Callable

STEP = 500


def ticker(log: Callable[[str], None], label: str, total: int, step: int = STEP) -> Callable[[int], None]:
    state = {"next": step}

    def tick(done: int) -> None:
        if done >= state["next"]:
            mark = done // step * step
            log(f"{label} {mark}/{total}")
            state["next"] = mark + step
    return tick
