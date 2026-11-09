"""scripts/compare-capture.py: slot-by-slot comparison of two decode logs (F32 plan, Task 4)."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "compare_capture", Path(__file__).resolve().parents[1] / "scripts" / "compare-capture.py")
cc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cc)

OLD = """\
2026/11/08 09:00:00 -3 +4.10 50261451 & CQ W4IMD EM84
2026/11/08 09:00:00 -1 +6.50 50261451 & CQ W4IMD EM84
2026/11/08 09:00:15 2 +1.00 28146000 & K1ABC W2XYZ FN20
2026/11/08 09:01:00 0 +2.00 50261000 & CQ OLDONLY FN31
not a decode line
"""
NEW = """\
2026/11/08 09:00:00 1 +4.10 50261451 & CQ W4IMD EM84
2026/11/08 09:00:15 2 +1.00 28146000 & K1ABC W2XYZ FN20
2026/11/08 09:00:15 -5 +3.00 28146100 & CQ NEWMSG EM10
2026/11/08 09:02:30 4 +2.00 50260900 & CQ NEWSLOT FN42
"""


def _logs(tmp_path):
    o, n = tmp_path / "old.log", tmp_path / "new.log"
    o.write_text(OLD)
    n.write_text(NEW)
    return o, n


def test_best_snr_per_message_per_slot(tmp_path):
    o, _ = _logs(tmp_path)
    old = cc.load(o)
    assert old[("2026/11/08 09:00:00", 50)] == {"CQ W4IMD EM84": -1}
    assert len(old) == 3


def test_tally_and_snr_deltas(tmp_path):
    o, n = _logs(tmp_path)
    tally, deltas, rows = cc.compare(cc.load(o), cc.load(n))
    assert tally == {"slots_both": 2, "slots_old_only": 1, "slots_new_only": 1,
                     "msgs_both": 2, "msgs_old_only": 1, "msgs_new_only": 2}
    assert sorted(d for _k, _m, d in deltas) == [0, 2]   # W4IMD: 1 - (-1); K1ABC: 2 - 2
    assert ("2026/11/08 09:00:00", 50) in [k for k, _m, _d in deltas]
    assert ("old only", "CQ OLDONLY FN31") in [(r[1], r[2]) for r in rows]
    assert ("new only", "CQ NEWSLOT FN42") in [(r[1], r[2]) for r in rows]


def test_window_bounds_are_inclusive(tmp_path):
    o, _ = _logs(tmp_path)
    old = cc.load(o, "2026/11/08 09:00:15", "2026/11/08 09:00:15")
    assert list(old) == [("2026/11/08 09:00:15", 28)]


def test_main_prints_totals(tmp_path, capsys):
    o, n = _logs(tmp_path)
    assert cc.main([str(o), str(n), "--from", "2026/11/08 09:00", "--to", "2026/11/08 09:01"]) == 0
    out = capsys.readouterr().out
    assert "slots: both 2, old only 1, new only 0" in out
    assert "better in 1" in out


def test_main_prints_a_row_for_every_shared_message(tmp_path, capsys):
    o, n = _logs(tmp_path)
    cc.main([str(o), str(n)])
    out = capsys.readouterr().out
    both = [ln for ln in out.splitlines() if " both " in ln and "old=" in ln]
    assert len(both) == 2
    w4 = next(ln for ln in both if "CQ W4IMD EM84" in ln)
    assert "2026/11/08 09:00:00" in w4 and "50 MHz" in w4
    assert all(t in w4.split() for t in ("old=-1", "new=+1", "d=+2"))
    k1 = next(ln for ln in both if "K1ABC W2XYZ FN20" in ln)
    assert all(t in k1.split() for t in ("old=+2", "new=+2", "d=+0"))
