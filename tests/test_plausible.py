"""Each MSK144 decode gets classified: spotted or not, and flagged when
unlikely (Michael, 10-20).  B4 on its dummy load, 10-19, decoded
"MJ3XMF 1B0OCH R CI31": a callsign from no ITU series, and a grid in the
open Pacific."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from meteor_scatter.core import plausible as P
from meteor_scatter.core.ch_tailer import ChTailer
from meteor_scatter.core.review import classify, sidecar_decodes, summary, to_csv

HOME = "EM38ww"
DAY = 24 * 3600.0


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


# ── grids and calls ─────────────────────────────────────────────────────────

def test_grid_center_four_and_six_characters() -> None:
    assert P.grid_center("EM38") == (38.5, -93.0)
    lat, lon = P.grid_center("EM38ww")
    assert 38.9 < lat < 39.0 and -92.2 < lon < -92.0
    assert P.grid_center("em38") == (38.5, -93.0)
    # B4's config carries a 10-character locator.
    assert P.grid_center("EM38ww40pk") == P.grid_center("EM38ww")
    assert P.Classifier("EM38ww40pk").host_grid_ok
    for bad in ("RR73", "RR7", "", "ZZ99"):
        assert P.grid_center(bad) is None


def test_distance_is_great_circle() -> None:
    assert P.distance_km((0.0, 0.0), (1.0, 0.0)) == pytest.approx(111.2, abs=0.2)


@pytest.mark.parametrize("call", ["K1ABC", "AC0G", "W9XYZ", "VE3/K1ABC", "K1ABC/P",
                                  "1A0KM", "9A1A", "3D2AG"])
def test_valid_transmitter_calls(call) -> None:
    assert P.tx_call_valid(call)


@pytest.mark.parametrize("call", ["1B0OCH", "Q1ABC", "0X1AB", "", "CQ", "<...>",
                                  "599", "73", "5NN"])
def test_invalid_transmitter_calls(call) -> None:
    assert not P.tx_call_valid(call)


# ── the classifier ──────────────────────────────────────────────────────────

def test_the_b4_false_decode_is_not_spotted() -> None:
    v = P.Classifier(HOME).classify(0, "1B0OCH", "CI31")
    assert not v.spot
    assert v.rejected == [P.REJECT_BAD_CALL]
    assert v.suspect == [P.SUSPECT_REACH]       # and its grid lies far off


def test_a_grid_in_the_message_spots() -> None:
    v = P.Classifier(HOME).classify(0, "K1ABC", "EN37")
    assert v.spot and v.grid == "EN37" and v.grid_source == "message"
    assert v.suspect == [] and v.distance_km < 1000


def test_a_later_message_takes_the_grid_sent_within_24_hours() -> None:
    c = P.Classifier(HOME)
    c.classify(0, "K1ABC", "EN37")
    v = c.classify(DAY - 1, "K1ABC", "")
    assert v.spot and v.grid == "EN37" and v.grid_source == "recent"
    late = c.classify(DAY + 1, "K1ABC", "")
    assert not late.spot and late.rejected == [P.REJECT_NO_GRID]


def test_no_grid_ever_is_not_spotted() -> None:
    v = P.Classifier(HOME).classify(0, "K1ABC", "")
    assert not v.spot and v.rejected == [P.REJECT_NO_GRID]


def test_an_invalid_call_teaches_no_grid() -> None:
    c = P.Classifier(HOME)
    c.classify(0, "1B0OCH", "EN37")
    assert c.classify(1, "1B0OCH", "").grid == ""


def test_a_grid_beyond_reach_spots_flagged() -> None:
    v = P.Classifier(HOME).classify(0, "JA1ABC", "PM95")
    assert v.spot and v.suspect == [P.SUSPECT_REACH] and v.distance_km > 9000


def test_no_receiver_grid_spots_nothing() -> None:
    c = P.Classifier("")
    assert not c.host_grid_ok
    v = c.classify(0, "K1ABC", "EN37")
    assert not v.spot and v.rejected == [P.REJECT_HOST_GRID]


# ── heard exchanges ─────────────────────────────────────────────────────────

def d(t, tx, rx, msg):
    return {"time": t, "tx_call": tx, "rx_call": rx, "message": msg}


def test_both_sides_with_an_acknowledgement_is_an_exchange() -> None:
    ds = [d(0, "K1ABC", "W9XYZ", "W9XYZ K1ABC FN42"),
          d(15, "W9XYZ", "K1ABC", "K1ABC W9XYZ R+05"),
          d(30, "N0CALL", "K1ABC", "K1ABC N0CALL EM48")]
    assert P.heard_exchanges(ds) == {0: "K1ABC<>W9XYZ", 1: "K1ABC<>W9XYZ"}


def test_one_side_or_no_acknowledgement_is_no_exchange() -> None:
    assert P.heard_exchanges([d(0, "K1ABC", "W9XYZ", "W9XYZ K1ABC R+05")]) == {}
    assert P.heard_exchanges([d(0, "K1ABC", "W9XYZ", "W9XYZ K1ABC FN42"),
                              d(15, "W9XYZ", "K1ABC", "K1ABC W9XYZ EN37")]) == {}
    assert P.heard_exchanges([d(0, "K1ABC", "W9XYZ", "W9XYZ K1ABC FN42"),
                              d(7200, "W9XYZ", "K1ABC", "K1ABC W9XYZ RRR")]) == {}


# ── the review of an archive ────────────────────────────────────────────────

def test_review_classifies_sidecars_in_time_order() -> None:
    side = {"slot_start_utc": "2026-10-19T18:10:30+00:00", "dial_freq_hz": 50260000,
            "band": "6m", "decodes": [
                {"snr": -7, "dt": 8.5, "audio_freq_hz": 1200, "message": "MJ3XMF 1B0OCH R CI31"},
                {"snr": -3, "dt": 1.0, "audio_freq_hz": 1500, "message": "AC0G K1ABC EN37"},
                {"snr": -2, "dt": 4.0, "audio_freq_hz": 1500, "message": "AC0G K1ABC RRR"}]}
    rows = classify(sidecar_decodes(side, "6m/20261019/261019_181030.wav"), HOME)
    assert [r["message"] for r in rows] == ["AC0G K1ABC EN37", "AC0G K1ABC RRR",
                                            "MJ3XMF 1B0OCH R CI31"]
    assert [r["spot"] for r in rows] == [True, True, False]
    assert rows[1]["grid_source"] == "recent"
    assert rows[0]["frequency_hz"] == 50261500
    assert summary(rows) == (3, 2, 0, 0)
    text = to_csv(rows)
    assert text.splitlines()[0].startswith("utc,band,recording")
    assert "transmitter callsign not ITU-allocated" in text


# ── the daemon ──────────────────────────────────────────────────────────────

def test_the_tailer_spots_only_what_passes() -> None:
    lines = ["2026/10/19 18:10:30 -7 +8.50 50261200 & MJ3XMF 1B0OCH R CI31",
             "2026/10/19 18:11:00 -3 +0.20 50260900 & AC0G K1ABC R EN37",
             "2026/10/19 18:11:15 -3 +0.20 50260900 & AC0G K1ABC RRR",
             "2026/10/19 18:11:30 -3 +0.20 50260900 & AC0G W9XYZ R+05",
             "2026/10/19 18:12:00 -9 +0.20 50260900 & AC0G JA1ABC PM95"]
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "b4-msk144.log"
        log.write_text("")
        fake = FakeWriter()
        tailer = ChTailer(
            log_path=log, mode="msk144", radiod_id="test-rx888",
            host_call="AC0G", host_grid=HOME, processing_version="0.1.0+abc",
            writer_factory=lambda batch_rows: fake)
        tailer.start()
        try:
            with open(log, "a") as f:
                f.write("\n".join(lines) + "\n")
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and len(fake.inserts) < 3:
                time.sleep(0.1)
        finally:
            tailer.stop(timeout=2.0)
    got = {r["message"]: r for r in fake.inserts}
    assert sorted(got) == ["AC0G JA1ABC PM95", "AC0G K1ABC R EN37", "AC0G K1ABC RRR"]
    assert got["AC0G K1ABC RRR"]["grid"] == "EN37"
    assert got["AC0G K1ABC RRR"]["grid_source"] == "recent"
    assert got["AC0G JA1ABC PM95"]["suspect"] == [P.SUSPECT_REACH]
    assert got["AC0G K1ABC R EN37"]["suspect"] == []
    assert tailer.rejected == {P.REJECT_BAD_CALL: 1, P.REJECT_NO_GRID: 1}


def test_a_30_s_station_repeated_across_two_slots_is_spotted_once() -> None:
    lines = [
        # K1ABC on 30 s periods: :00-:30 spans the :00 and :15 slots.
        "2026/10/19 18:11:00 -3 +2.00 50260900 & AC0G K1ABC EN37",
        "2026/10/19 18:11:15 -4 +6.00 50260900 & AC0G K1ABC EN37",
        # The same text again in the next period, :30, is a new transmission.
        "2026/10/19 18:11:30 -3 +1.00 50260900 & AC0G K1ABC EN37",
        # A 15 s station in the :45 slot, after a different message at :30.
        "2026/10/19 18:11:45 -5 +1.00 50260900 & AC0G W9XYZ EN52"]
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "b4-msk144.log"
        log.write_text("")
        fake = FakeWriter()
        tailer = ChTailer(
            log_path=log, mode="msk144", radiod_id="test-rx888",
            host_call="AC0G", host_grid=HOME, processing_version="0.1.0+abc",
            writer_factory=lambda batch_rows: fake)
        tailer.start()
        try:
            with open(log, "a") as f:
                f.write("\n".join(lines) + "\n")
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and len(fake.inserts) < 3:
                time.sleep(0.1)
        finally:
            tailer.stop(timeout=2.0)
    times = [r["time"].strftime("%M:%S") for r in fake.inserts]
    assert times == ["11:00", "11:30", "11:45"]
    assert tailer.repeats_skipped == 1


# ── the repeat filter (review, 10-20) ───────────────────────────────────────

def test_repeats_key_on_slot_time_not_arrival_order() -> None:
    f = P.RepeatFilter()
    a, b = P.channel_of(50260900), P.channel_of(28145900)
    assert not f.repeat(660, a, "AC0G K1ABC EN37")      # A at :00
    assert not f.repeat(690, b, "AC0G W9XYZ EN52")      # B at :30 lands first
    assert f.repeat(675, a, "AC0G K1ABC EN37")          # A's slow :15 still a repeat


def test_repeats_stay_on_their_own_channel() -> None:
    f = P.RepeatFilter()
    assert not f.repeat(660, P.channel_of(50260900), "CQ K1ABC EN37")
    assert not f.repeat(675, P.channel_of(144150900), "CQ K1ABC EN37")


def test_review_applies_the_daemons_repeat_rule() -> None:
    side = lambda start, dt: {"slot_start_utc": start, "dial_freq_hz": 50260000,
                              "band": "6m", "decodes": [
        {"snr": -3, "dt": dt, "audio_freq_hz": 1500, "message": "AC0G K1ABC EN37"}]}
    rows = classify(sidecar_decodes(side("2026-10-19T18:11:00+00:00", 2.0), "a.wav")
                    + sidecar_decodes(side("2026-10-19T18:11:15+00:00", 6.0), "b.wav"), HOME)
    assert [r["spot"] for r in rows] == [True, False]
    assert rows[1]["rejected"] == [P.REJECT_REPEAT]
