"""New meteor-scatter instances start with no in-process sender.

`off` starts no sender of its own and marks each row forward_to_pskreporter
false, so the station's hs-uploader daemon -- the one pskreporter sender per
set of rows -- posts MSK144 under the site sink switch
(sigmond tasks/plan-sink-control.md §10.2 step 1, §10.3 item 4).
"""
import os
import tomllib
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _seed() -> dict:
    with open(REPO / "deploy.toml", "rb") as f:
        return tomllib.load(f)["contract"]["instance_env"]


def test_new_instances_are_seeded_off():
    assert _seed()["METEOR_SCATTER_DELIVERY_MODE"] == "off"


def test_the_seed_starts_no_sender_and_leaves_rows_to_the_daemon():
    # What the seed must DO, not only what it reads: no in-process sender,
    # and rows flagged false, which the daemon's psk-pskreporter selects.
    from meteor_scatter.core import recorder as r
    rec = r.MeteorScatterRecorder.__new__(r.MeteorScatterRecorder)
    rec._station = {"callsign": "AC0G", "grid_square": "EM38ww"}
    rec._paths, rec._radiod_id, rec._uploaders = {}, "test", []
    rx = mock.Mock()
    rec._receivers, rec._cycle_batcher = [rx], mock.Mock()
    env = {"METEOR_SCATTER_DELIVERY_MODE": _seed()["METEOR_SCATTER_DELIVERY_MODE"]}
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(r, "HsPskReporterUploader") as sender:
        rec._start_uploaders()
        rec._start_all_ch_tailers()
    sender.assert_not_called()
    assert rx.start_ch_tailers.call_args.kwargs["forward_flag"] is False
