import json
import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

from meteor_scatter.core.archive import Archive
from meteor_scatter.core.frontend import FrontendLog, FrontendReading
from meteor_scatter.core.msqp import MsqpChannel, MsqpRuntime
from meteor_scatter.core.noise_watch import NoiseWatch
from meteor_scatter.core.wav import WavStats

T0 = 1797206370.0


class MsqpChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fe = FrontendLog()
        self.fe.add(FrontendReading(T0 - 1, 10.0, 0.0, True, -23.5, 0))
        self.ch = MsqpChannel(archive=Archive(root / "a", 90, 2), frontend=self.fe,
                              freq_hz=50260000, reporter_id="AC0G/B4", scale_db=None,
                              noise=NoiseWatch(None), version="test")
        self.wav = root / "x.wav"
        self.wav.write_bytes(b"\0" * 64)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sidecar_carries_what_the_spec_lists(self):
        st = WavStats(gain_db=41.2, clipped_samples=3, noise_dbfs=-88.0)
        decodes = [{"snr": -5, "dt": 0.2, "audio_freq_hz": 1500, "sync": "&", "message": "CQ AC0G EM38"}]
        self.assertTrue(self.ch.on_decoded(self.wav, T0, 12000, st, decodes))
        side = json.loads((Path(self.tmp.name) / "a/6m/20261213/261213_235930.json").read_text())
        self.assertEqual(side["reporter_id"], "AC0G/B4")
        self.assertEqual(side["dial_freq_hz"], 50260000)
        self.assertEqual(side["scale"], "uncalibrated")
        self.assertEqual(side["wav_gain_db"], 41.2)
        self.assertEqual(side["clipped_samples"], 3)
        self.assertEqual(side["decodes"], decodes)
        self.assertEqual(side["channel_requested"]["agc_enable"], 0)
        self.assertIn("frontend_status", side["frontend"])
        self.assertEqual(side["software_version"], "test")

    def test_channel_requested_preset_comes_from_the_config(self):
        ch = MsqpChannel(archive=self.ch._archive, frontend=self.fe, freq_hz=50260000,
                         reporter_id="AC0G/B4", scale_db=None, noise=NoiseWatch(None),
                         version="test", preset="iq")
        ch.on_decoded(self.wav, T0, 12000, WavStats(0.0, 0, -80.0), [{"message": "x"}])
        side = json.loads((Path(self.tmp.name) / "a/6m/20261213/261213_235930.json").read_text())
        self.assertEqual(side["channel_requested"]["preset"], "iq")

    def test_channel_reported_from_the_listener(self):
        self.ch.ssrc = 9
        self.fe.add_channel(50260000.0, ssrc=9, preset="usb", agc_enable=True, gain_db=6.0,
                            encoding="F32LE", t=T0)
        # another client's channel on the same frequency must not replace ours (N2)
        self.fe.add_channel(50260000.0, ssrc=10, preset="am", agc_enable=False, gain_db=0.0,
                            encoding="S16LE", t=T0 + 1)
        self.ch.on_decoded(self.wav, T0, 12000, WavStats(0.0, 0, -80.0), [{"message": "x"}])
        side = json.loads((Path(self.tmp.name) / "a/6m/20261213/261213_235930.json").read_text())
        self.assertEqual(side["channel_reported"]["agc_enable"], True)
        self.assertEqual(side["channel_reported"]["gain_db"], 6.0)
        self.assertEqual(side["channel_reported"]["preset"], "usb")
        self.assertEqual(side["channel_reported"]["encoding"], "F32LE")

    def test_channel_reported_null_when_never_seen(self):
        # a report on our frequency but an unknown SSRC is not ours
        self.fe.add_channel(50260000.0, ssrc=10, preset="usb", agc_enable=True, gain_db=6.0,
                            encoding="F32LE", t=T0)
        self.ch.on_decoded(self.wav, T0, 12000, WavStats(0.0, 0, -80.0), [{"message": "x"}])
        side = json.loads((Path(self.tmp.name) / "a/6m/20261213/261213_235930.json").read_text())
        self.assertIn("channel_reported", side)
        self.assertIsNone(side["channel_reported"])

    def test_non_oserror_warns_once_counts_and_recovers(self):
        st = WavStats(0.0, 0, -80.0)
        with self.assertLogs("meteor_scatter.core.msqp", level="WARNING") as logs:
            with unittest.mock.patch.object(self.ch._archive, "keep",
                                            side_effect=TypeError("not serialisable")):
                for i in range(3):
                    self.assertFalse(self.ch.on_decoded(self.wav, T0 + 30 * i, 12000, st,
                                                        [{"message": "x"}]))
        self.assertEqual(sum("archiving stopped" in m for m in logs.output), 1)
        self.assertFalse(any("Traceback" in m for m in logs.output[1:]))
        self.assertEqual(self.ch.state()["archive_failures"], 3)
        self.assertFalse(self.ch.state()["archiving"])
        with self.assertLogs("meteor_scatter.core.msqp", level="INFO") as logs:
            self.assertTrue(self.ch.on_decoded(self.wav, T0 + 120, 12000, st, [{"message": "x"}]))
        self.assertEqual(sum("archiving resumed" in m for m in logs.output), 1)

    def test_disk_full_counts_warns_once_and_recovers(self):
        st = WavStats(0.0, 0, -80.0)
        with self.assertLogs("meteor_scatter.core.msqp", level="WARNING") as logs:
            with unittest.mock.patch.object(self.ch._archive, "keep", side_effect=OSError(28, "No space")):
                self.assertFalse(self.ch.on_decoded(self.wav, T0, 12000, st, [{"message": "x"}]))
                self.assertFalse(self.ch.on_decoded(self.wav, T0 + 30, 12000, st, [{"message": "x"}]))
        self.assertEqual(self.ch.archive_failures, 2)
        self.assertEqual(sum("archiving stopped" in m for m in logs.output), 1)
        self.wav.write_bytes(b"\0" * 64)
        with self.assertLogs("meteor_scatter.core.msqp", level="INFO") as logs:
            self.assertTrue(self.ch.on_decoded(self.wav, T0 + 60, 12000, st, [{"message": "x"}]))
        self.assertTrue(any("archiving resumed" in m for m in logs.output))

    def test_frontend_window_spans_the_configured_cadence(self):
        ch = MsqpChannel(archive=self.ch._archive, frontend=self.fe, freq_hz=50260000,
                         reporter_id="AC0G/B4", scale_db=None, noise=NoiseWatch(None),
                         version="test", cadence_sec=15.0)
        with unittest.mock.patch.object(self.fe, "window", wraps=self.fe.window) as win:
            ch.on_decoded(self.wav, T0, 12000, WavStats(0.0, 0, -80.0), [{"message": "x"}])
        win.assert_called_once_with(T0, T0 + 15.0, 12000)

    def test_calibrated_band_says_so(self):
        ch = MsqpChannel(archive=self.ch._archive, frontend=self.fe, freq_hz=28145000,
                         reporter_id="AC0G/B4", scale_db=12.5, noise=NoiseWatch(None),
                         version="test")
        ch.on_decoded(self.wav, T0, 12000, WavStats(12.5, 0, -80.0), [{"message": "x"}])
        side = json.loads((Path(self.tmp.name) / "a/10m/20261213/261213_235930.json").read_text())
        self.assertEqual(side["scale"], "calibrated")

    def test_on_slot_feeds_the_noise_watch(self):
        noise = unittest.mock.MagicMock()
        noise.observe.return_value = "noise floor drifted"
        ch = MsqpChannel(archive=self.ch._archive, frontend=self.fe, freq_hz=50260000,
                         reporter_id="AC0G/B4", scale_db=None, noise=noise, version="test")
        with self.assertLogs("meteor_scatter.core.msqp", level="WARNING") as logs:
            ch.on_slot(T0, WavStats(0.0, 0, -70.0))
        noise.observe.assert_called_once_with(T0, -70.0, 10.0)
        self.assertTrue(any("6m: noise floor drifted" in m for m in logs.output))


class NoiseLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "noise-50260000.txt"
        self.ch = MsqpChannel(archive=Archive(self.root / "a", 90, 2), frontend=FrontendLog(),
                              freq_hz=50260000, reporter_id="AC0G/B4", scale_db=None,
                              noise=NoiseWatch(None), version="test", noise_log=self.log)

    def tearDown(self):
        self.tmp.cleanup()

    def test_appends_slot_and_floor_and_skips_none(self):
        self.ch.on_slot(T0, WavStats(0.0, 0, -81.25))
        self.ch.on_slot(T0 + 30, WavStats(0.0, 0, None))
        self.assertEqual(self.log.read_text().splitlines(), [f"{int(T0)} -81.2"])

    def test_keeps_an_hour_of_15_s_slots(self):
        for i in range(250):
            self.ch.on_slot(T0 + 15 * i, WavStats(0.0, 0, -80.0 - i * 0.01))
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 240)
        self.assertEqual(lines[0].split()[0], str(int(T0 + 15 * 10)))
        self.assertEqual(lines[-1].split()[0], str(int(T0 + 15 * 249)))

    def test_oserror_never_escapes_and_warns_once(self):
        self.ch._noise_log = self.root / "no-such-dir" / "deeper" / "n.txt"
        with unittest.mock.patch("pathlib.Path.mkdir", side_effect=OSError(30, "read-only")), \
                self.assertLogs("meteor_scatter.core.msqp", level="WARNING") as logs:
            self.ch.on_slot(T0, WavStats(0.0, 0, -80.0))
            self.ch.on_slot(T0 + 30, WavStats(0.0, 0, -80.0))
        self.assertEqual(sum("noise log" in m for m in logs.output), 1)

    def test_damaged_log_heals_and_never_raises(self):
        self.log.write_bytes(b"\xff\xfe garbage\n" + f"{int(T0) - 30} -80.0\n".encode())
        self.ch.on_slot(T0, WavStats(0.0, 0, -81.0))
        self.assertEqual(self.log.read_text().splitlines(),
                         [f"{int(T0) - 30} -80.0", f"{int(T0)} -81.0"])

    def test_non_oserror_warns_once_counts_and_recovers(self):
        with unittest.mock.patch("os.replace", side_effect=RuntimeError("odd")), \
                self.assertLogs("meteor_scatter.core.msqp", level="WARNING") as logs:
            for i in range(3):
                self.ch.on_slot(T0 + 30 * i, WavStats(0.0, 0, -80.0))
        self.assertEqual(sum("noise log stopped" in m for m in logs.output), 1)
        self.assertEqual(self.ch.state()["noise_log_failures"], 3)
        with self.assertLogs("meteor_scatter.core.msqp", level="INFO") as logs:
            self.ch.on_slot(T0 + 120, WavStats(0.0, 0, -80.0))
        self.assertEqual(sum("noise log resumed" in m for m in logs.output), 1)

    def test_runtime_names_the_log_per_band_under_spool_root(self):
        rt = MsqpRuntime({"archive_dir": str(self.root / "arc"), "archive_max_days": 90,
                          "archive_max_gb": 2.0}, "t", "AC0G-B4", spool_root=self.root / "sp")
        ch = rt.channel(50260000, reporter_id="x", scale_db=None, noise_cal_dbfs=None)
        self.assertEqual(ch._noise_log, self.root / "sp" / "noise-50260000.txt")


class MsqpRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rt = MsqpRuntime({"archive_dir": str(self.root / "a"), "archive_max_days": 90,
                               "archive_max_gb": 2.0}, version="v-test", instance_id="AC0G/B4")

    def tearDown(self):
        self.tmp.cleanup()

    def _channel(self, freq=50260000):
        return self.rt.channel(freq, reporter_id="AC0G/B4", scale_db=None,
                               noise_cal_dbfs=-90.0, cadence_sec=30.0)

    def test_channel_takes_the_configured_preset(self):
        ch = self.rt.channel(50260000, reporter_id="AC0G/B4", scale_db=None,
                             noise_cal_dbfs=None, preset="usb")
        self.assertEqual(ch.preset, "usb")

    def test_state_names_the_archive_root(self):
        path = self.root / "msqp-state.json"
        self.rt.write_state(path)
        state = json.loads(path.read_text())
        self.assertEqual(state["archive_root"], str(self.root / "a" / "AC0G-B4"))

    def test_rebuild_reuses_the_band_channel(self):
        # ReceiverManager.reset_source re-runs provision_channels with the same runtime.
        a = self._channel()
        b = self._channel()
        self.assertIs(a, b)
        self.assertEqual(len(self.rt.channels), 1)

    def test_write_state_is_atomic_json(self):
        self._channel()
        self._channel(28145000)
        path = self.root / "msqp-state.json"
        self.rt.write_state(path)
        state = json.loads(path.read_text())
        self.assertEqual([c["band"] for c in state["channels"]], ["6m", "10m"])
        self.assertIn("archive", state)
        self.assertIsNone(state["frontend_last_packet_age_sec"])
        self.assertFalse(path.with_suffix(".json.part").exists())

    def test_prune_delegates_to_the_archive(self):
        self.assertEqual(self.rt.archive.root, self.root / "a" / "AC0G-B4")
        old = self.rt.archive.root / "6m" / "20200101"
        old.mkdir(parents=True)
        (old / "200101_000000.wav").write_bytes(b"\0")
        removed = self.rt.prune(date(2026, 10, 17))
        self.assertTrue(removed)
        self.assertFalse(old.exists())

    def test_listener_failure_is_logged_not_raised(self):
        with unittest.mock.patch("meteor_scatter.core.frontend.FrontendListener") as fl:
            fl.return_value.start.side_effect = OSError("no route")
            with self.assertLogs("meteor_scatter.core.msqp", level="WARNING"):
                self.rt.start_listener("rx.local")
        self.rt.stop_listener()        # nothing running: must not raise

    def test_listener_start_and_stop(self):
        with unittest.mock.patch("meteor_scatter.core.frontend.FrontendListener") as fl:
            self.rt.start_listener("rx.local")
            fl.assert_called_once_with("rx.local", self.rt.frontend)
            fl.return_value.start.assert_called_once_with()
            self.rt.stop_listener()
            fl.return_value.stop.assert_called_once_with()


class InstanceArchiveRootTests(unittest.TestCase):
    def test_sources_with_different_reporter_ids_get_different_roots(self):
        from meteor_scatter.core.receiver_manager import ReceiverManager
        with tempfile.TemporaryDirectory() as td:
            cfg = {"paths": {}, "station": {}, "processing": {},
                   "msqp": {"archive_dir": f"{td}/msqp"}}
            rxs = [ReceiverManager(config=cfg, radiod_block={"status": "same.local"},
                                   spool_root=Path(td), log_dir=Path(td),
                                   radiod_lifetime_frames=0, reporter_id=rid)
                   for rid in ("AC0G/B4", "AC0G/ND")]
            roots = [MsqpRuntime({"archive_dir": f"{td}/msqp", "archive_max_days": 90,
                                  "archive_max_gb": 2.0}, "v", rx.msqp_instance_id).archive.root
                     for rx in rxs]
            self.assertEqual(roots, [Path(td) / "msqp" / "AC0G-B4", Path(td) / "msqp" / "AC0G-ND"])

    def test_band_channel_gets_the_configured_preset(self):
        from meteor_scatter.core.receiver_manager import ReceiverManager
        rx = ReceiverManager(config={"paths": {}, "station": {}, "processing": {}},
                             radiod_block={"status": "rx.local"}, spool_root=Path("/tmp/x"),
                             log_dir=Path("/tmp/x"), radiod_lifetime_frames=0)
        rt = unittest.mock.MagicMock()
        rx._msqp_channel(rt, 50260000, "AC0G/B4", 30.0, "usb2")
        self.assertEqual(rt.channel.call_args.kwargs["preset"], "usb2")

    def test_instance_id_falls_back_to_config_then_radiod(self):
        from meteor_scatter.core.receiver_manager import ReceiverManager
        mk = lambda cfg: ReceiverManager(config=cfg, radiod_block={"status": "rx.local"},
                                         spool_root=Path("/tmp/x"), log_dir=Path("/tmp/x"),
                                         radiod_lifetime_frames=0)
        self.assertEqual(mk({"instance": {"reporter_id": "AC0G/B4"}}).msqp_instance_id, "AC0G/B4")
        self.assertEqual(mk({}).msqp_instance_id, "rx.local")


if __name__ == "__main__":
    unittest.main()


class RecorderHousekeepingTests(unittest.TestCase):
    """msqp-state.json every stats tick, prune once an hour; neither may kill the stats thread."""

    def setUp(self):
        from meteor_scatter.core.recorder import MeteorScatterRecorder
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = Path(self.tmp.name)
        cfg = {"paths": {"spool_dir": str(self.spool), "log_dir": str(self.spool / "log")},
               "station": {}, "processing": {"radiod_lifetime_frames": 0}}
        self.rec = MeteorScatterRecorder(cfg, {"status": "rx.local"})
        self.rx = self.rec.receivers[0]
        self.rt = unittest.mock.MagicMock()
        self.rt.prune.return_value = []
        self.rec._msqp_runtimes = {self.rx: self.rt}

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_every_tick_prune_hourly(self):
        self.rec._msqp_housekeeping(1000.0)
        self.rec._msqp_housekeeping(1060.0)
        self.rec._msqp_housekeeping(1000.0 + 3600.0)
        self.assertEqual(self.rt.write_state.call_count, 3)
        self.rt.write_state.assert_called_with(self.spool / "rx.local" / "msqp-state.json")
        self.assertEqual(self.rt.prune.call_count, 2)

    def test_failures_are_swallowed(self):
        self.rt.write_state.side_effect = OSError("ro fs")
        self.rt.prune.side_effect = RuntimeError("boom")
        self.rec._msqp_housekeeping(1000.0)      # must not raise

    def test_provision_passes_each_receiver_its_runtime(self):
        from meteor_scatter.core.recorder import MeteorScatterRecorder
        cfg = {"paths": {"spool_dir": str(self.spool), "log_dir": str(self.spool / "log")},
               "station": {}, "processing": {"radiod_lifetime_frames": 0},
               "msqp": {"archive_dir": str(self.spool / "msqp")}}
        rec = MeteorScatterRecorder(cfg, [{"status": "a.local"}, {"status": "b.local"}])
        for rx in rec.receivers:
            rx.provision_channels = unittest.mock.MagicMock()
        rec._provision_all_receivers()
        got = [rx.provision_channels.call_args.kwargs["msqp_runtime"] for rx in rec.receivers]
        self.assertIsInstance(got[0], MsqpRuntime)
        self.assertIsNot(got[0], got[1])
        # No reporter id anywhere: each source archives under its radiod id.
        self.assertEqual([g.archive.root for g in got],
                         [self.spool / "msqp" / "a.local", self.spool / "msqp" / "b.local"])
        self.assertNotIn("keep_wav", rec.receivers[0].provision_channels.call_args.kwargs)


class BadMsqpBlockTests(unittest.TestCase):
    def test_bad_block_disables_the_archive_and_the_daemon_runs_on(self):
        from meteor_scatter.core.recorder import MeteorScatterRecorder
        with tempfile.TemporaryDirectory() as td:
            cfg = {"paths": {"spool_dir": td, "log_dir": td}, "station": {},
                   "processing": {"radiod_lifetime_frames": 0},
                   "msqp": {"archive_max_gb": "2"}}
            rec = MeteorScatterRecorder(cfg, {"status": "rx.local"})
            with self.assertLogs("meteor_scatter.core.recorder", "ERROR") as cm:
                rec._build_msqp_runtimes()
            self.assertEqual(rec._msqp_runtimes, {})
            self.assertIn("MSQP archive disabled", cm.output[0])
