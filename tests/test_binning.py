"""`rugyomi.rules.binning`: the bins and the table cell shared by the research diagnostics."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rugyomi.rules import binning as BN  # noqa: E402


@pytest.mark.core
def test_the_bins_are_the_bins_of_the_t15_diagnostic():
    r = BN.ranges((0, 0), (1, 1), (2, 3), (4, None))
    assert [r(v)[0] for v in (0, 1, 2, 3, 4, 99, None)] == ["0", "1", "2-3", "2-3", "4+", "4+", "n/a"] and r.labels[-1] == "n/a"
    t = BN.tercile_binner([None, -1, 0, 1, 2, 3, 4, 5, 6], zero_label="0")
    assert t(None)[0] == "n/a" and t(0)[0] == "0" and t(-5)[0] == "0" and t(100)[0].startswith("> ") and len(t.edges) == 2
    t = BN.tercile_binner([-2, 1, 2, 3, 4, 5, 6], zero_label="= 0", negative=True)
    assert t(-1)[0] == "< 0" and t(0)[0] == "= 0"
    assert BN.maxx_bins(None)[0] == "no outside buyer yet" and BN.maxx_bins(0.5)[0] == "< 1" and BN.maxx_bins(3.0)[0] == ">= 3" and BN.maxx_bins(2.99)[0] == "2 - 3"
    assert BN.yesno(None)[0] == "n/a" and BN.yesno(0)[0] == "no" and BN.yesno(True)[0] == "yes"
    assert BN.devsold_bins(0)[0] == "0" and BN.devsold_bins(5000)[0] == "1 - 5000 bps" and BN.devsold_bins(5001)[0] == "> 5000 bps" and BN.devsold_bins(None)[0] == "n/a"
    n, k, sh, ci, lift = BN.cell([{"rug": 1}, {"rug": 0}, {"rug": 0}, {"rug": 1}], 0.25)
    assert (n, k, sh, lift) == (4, 2, "50.0%", "x2.00") and ci.endswith("%")
    assert BN.cell([], 0.25)[:3] == (0, 0, "-") and BN.one_hot("score", BN.ranges((0, 19)), 5) == "score=0-19" and ":" not in BN.one_hot("a:b", lambda v: ("x:y", 0), 1)


