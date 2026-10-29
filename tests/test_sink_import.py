"""The writer comes from hs_uploader; sigmond's paths stay optional in units."""
import re
import sys
from pathlib import Path

import pytest

UNITS = ["systemd/meteor-scatter@.service"]
ROOT = Path(__file__).resolve().parents[1]


def test_writer_comes_from_hs_uploader_without_sigmond(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "sigmond", None)
    monkeypatch.setitem(sys.modules, "sigmond.hamsci_sink", None)
    monkeypatch.setenv("SIGMOND_SQLITE_PATH", str(tmp_path / "sink.db"))
    from meteor_scatter.core.ch_tailer import _default_writer_factory
    writer = _default_writer_factory(10)
    assert writer is not None
    assert type(writer).__module__ == "hs_uploader.sink.writer"
    # is_noop is a plain attribute on this Writer, not a method.
    noop = writer.is_noop
    assert (noop() if callable(noop) else noop) is False


@pytest.mark.parametrize("unit", UNITS)
def test_unit_marks_sigmond_paths_optional(unit):
    found = 0
    for line in (ROOT / unit).read_text().splitlines():
        m = re.match(r"\s*ReadWritePaths=(.*)", line)
        if not m:
            continue
        for tok in m.group(1).split():
            if "/var/lib/sigmond" in tok or "/var/lib/hs-uploader" in tok:
                found += 1
                assert tok.startswith("-"), f"{unit}: {tok}"
    assert found, f"{unit}: no sigmond/hs-uploader ReadWritePaths entry"
