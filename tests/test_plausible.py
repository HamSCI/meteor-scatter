"""A decode whose grid meteor scatter cannot reach stays out of the sink.

B4 on its dummy load, 10-19: "MJ3XMF 1B0OCH R CI31", a grid in the open
Pacific.  Short-wave listeners report spots too, so the test reads the
grid, not the call."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from meteor_scatter.core.plausible import distance_km, grid_center, out_of_reach
from meteor_scatter.core.ch_tailer import ChTailer


class FakeWriter:
    is_noop = False
    health = "ok"

    def __init__(self) -> None:
        self.inserts: list = []

    def insert(self, rows) -> None:
        self.inserts.extend(rows)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

FALSE = "2026/10/19 18:10:30 -7 +8.50 50261200 & MJ3XMF 1B0OCH R CI31"
REAL = "2026/10/19 18:11:00 -3 +0.20 50260900 & AC0G K1ABC R EN37"


def test_grid_center_four_and_six_characters() -> None:
    assert grid_center("EM38") == (38.5, -93.0)
    lat, lon = grid_center("EM38ww")
    assert 38.9 < lat < 39.0 and -92.2 < lon < -92.0
    assert grid_center("em38") == (38.5, -93.0)
    assert grid_center("RR7") is None
    assert grid_center("") is None


def test_distance_is_great_circle() -> None:
    # One degree of latitude: about 111 km.
    assert distance_km((0.0, 0.0), (1.0, 0.0)) == pytest.approx(111.2, abs=0.2)


def test_the_b4_false_decode_is_out_of_reach() -> None:
    why = out_of_reach("CI31", "EM38ww")
    assert why is not None and "CI31" in why and "km" in why


@pytest.mark.parametrize("grid", ["EN37", "FN42", "DM79", "EL29"])
def test_meteor_scatter_distances_pass(grid) -> None:
    assert out_of_reach(grid, "EM38ww") is None


def test_no_grid_on_either_end_is_no_evidence() -> None:
    assert out_of_reach("", "EM38ww") is None
    assert out_of_reach("CI31", "") is None
    assert out_of_reach("RR73", "") is None


def test_the_tailer_keeps_the_false_decode_out_of_the_sink() -> None:
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "b4-msk144.log"
        log.write_text("")
        fake = FakeWriter()
        tailer = ChTailer(
            log_path=log, mode="msk144", radiod_id="test-rx888",
            host_call="AC0G", host_grid="EM38ww",
            processing_version="0.1.0+abc",
            writer_factory=lambda batch_rows: fake)
        tailer.start()
        try:
            with open(log, "a") as f:
                f.write(FALSE + "\n" + REAL + "\n")
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and not fake.inserts:
                time.sleep(0.1)
        finally:
            tailer.stop(timeout=2.0)
    assert [r["message"] for r in fake.inserts] == ["AC0G K1ABC R EN37"]
    assert tailer.rejected_out_of_reach == 1
