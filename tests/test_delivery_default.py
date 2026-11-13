"""With METEOR_SCATTER_DELIVERY_MODE unset the daemon deposits (uniform routing)."""
import os
from unittest import mock

from meteor_scatter.core import recorder as r


def _rec():
    rec = r.MeteorScatterRecorder.__new__(r.MeteorScatterRecorder)
    rec._station = {"callsign": "AC0G", "grid_square": "EM38ww"}
    rec._paths, rec._radiod_id, rec._uploaders = {}, "test", []
    rx = mock.Mock()
    rec._receivers, rec._cycle_batcher = [rx], mock.Mock()
    return rec, rx


def _unset():
    env = dict(os.environ)
    env.pop("METEOR_SCATTER_DELIVERY_MODE", None)
    return mock.patch.dict(os.environ, env, clear=True)


def test_unset_mode_is_deposit():
    with _unset():
        assert r.MeteorScatterRecorder._delivery_mode() == "deposit"


def test_unset_mode_starts_no_sender_and_flags_rows_for_the_server():
    rec, rx = _rec()
    with _unset(), mock.patch.object(r, "HsPskReporterUploader") as sender:
        rec._start_uploaders()
        rec._start_all_ch_tailers()
    sender.assert_not_called()
    assert rx.start_ch_tailers.call_args.kwargs["forward_flag"] is True


def test_direct_stays_reachable_by_explicit_config():
    with mock.patch.dict(os.environ, {"METEOR_SCATTER_DELIVERY_MODE": "direct"}):
        assert r.MeteorScatterRecorder._delivery_mode() == "direct"
