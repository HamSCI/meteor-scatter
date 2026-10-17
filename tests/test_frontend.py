import socket
import sys
import unittest
from unittest import mock

from meteor_scatter.core.frontend import (
    FrontendListener, FrontendLog, FrontendReading, configure_status_socket)

SR = 12000


def r(t, gain=10.0, atten=0.0, over=0):
    return FrontendReading(t=t, rf_gain=gain, rf_atten=atten, rf_agc=True,
                           if_power=-23.5, ad_over=over)


class WindowTests(unittest.TestCase):
    def test_steady_gain_has_no_steps(self):
        log = FrontendLog()
        for i in range(0, 40):
            log.add(r(1000.0 + i * 0.5))
        w = log.window(1005.0, 1015.0, SR)
        self.assertEqual(w["frontend_status"], "ok")
        self.assertEqual(w["steps"], [])
        self.assertEqual(w["start"]["rf_gain"], 10.0)

    def test_step_is_a_window_between_packets(self):
        log = FrontendLog()
        log.add(r(1000.0)); log.add(r(1004.5)); log.add(r(1005.0, gain=12.5)); log.add(r(1020.0, gain=12.5))
        w = log.window(1000.0, 1030.0, SR)
        self.assertEqual(len(w["steps"]), 1)
        s = w["steps"][0]
        self.assertEqual((s["from_db"], s["to_db"]), (10.0, 12.5))
        self.assertEqual(s["sample_window"], [int(4.5 * SR), int(5.0 * SR)])

    def test_gap_over_five_seconds_marks_missing(self):
        log = FrontendLog()
        log.add(r(1000.0)); log.add(r(1012.0))
        w = log.window(1000.0, 1030.0, SR)
        self.assertEqual(w["frontend_status"], "partial")
        self.assertIn([0, 12 * SR], w["missing_sample_windows"])

    def test_no_readings_at_all_is_missing(self):
        w = FrontendLog().window(1000.0, 1030.0, SR)
        self.assertEqual(w["frontend_status"], "missing")
        self.assertIsNone(w["start"])

    def test_ad_over_rise_marks_overload(self):
        log = FrontendLog()
        log.add(r(999.0, over=7)); log.add(r(1010.0, over=7)); log.add(r(1012.0, over=9))
        w = log.window(1000.0, 1015.0, SR)
        self.assertEqual(w["ad_over_delta"], 2)
        self.assertTrue(w["overloaded"])

    def test_log_keeps_only_changes_or_one_per_second(self):
        log = FrontendLog()
        for i in range(100):
            log.add(r(1000.0 + i * 0.01))        # 100 packets in one second, no change
        self.assertLessEqual(len(log._readings), 2)

    def test_only_stale_reading_before_window_is_missing(self):
        log = FrontendLog()
        log.add(r(500.0))
        w = log.window(1000.0, 1030.0, SR)
        self.assertEqual(w["frontend_status"], "missing")
        self.assertEqual(w["missing_sample_windows"], [[0, 30 * SR]])
        self.assertIsNone(w["start"])           # a 500 s old reading never stands in
        self.assertIsNone(w["end"])

    def test_stale_start_is_null_but_fresh_end_reported(self):
        log = FrontendLog()
        log.add(r(990.0)); log.add(r(1020.0, gain=12.5)); log.add(r(1028.0, gain=12.5))
        w = log.window(1000.0, 1030.0, SR)
        self.assertIsNone(w["start"])           # 10 s before t0: older than the 5 s gap
        self.assertEqual(w["end"]["t"], 1028.0)
        self.assertEqual(w["end"]["rf_gain"], 12.5)

    def test_reading_within_the_gap_before_t0_is_the_start(self):
        log = FrontendLog()
        log.add(r(996.0, gain=9.0)); log.add(r(1027.0, gain=9.0))
        w = log.window(1000.0, 1030.0, SR)
        self.assertEqual(w["start"]["t"], 996.0)
        self.assertEqual(w["start"]["rf_gain"], 9.0)

    def test_end_is_null_when_nothing_arrives_near_the_slot_end(self):
        log = FrontendLog()
        log.add(r(999.0)); log.add(r(1003.0)); log.add(r(1010.0))
        w = log.window(1000.0, 1030.0, SR)
        self.assertEqual(w["start"]["t"], 999.0)
        self.assertIsNone(w["end"])             # last reading 20 s before t1

    def test_first_reading_just_after_t0_is_the_start_without_a_before(self):
        log = FrontendLog()
        log.add(r(1002.0)); log.add(r(1029.0))
        w = log.window(1000.0, 1030.0, SR)
        self.assertEqual(w["start"]["t"], 1002.0)
        self.assertEqual(w["end"]["t"], 1029.0)

    def test_first_reading_long_after_t0_is_not_the_start(self):
        log = FrontendLog()
        log.add(r(1012.0)); log.add(r(1029.0))
        w = log.window(1000.0, 1030.0, SR)
        self.assertIsNone(w["start"])

    def test_ad_over_counter_reset_is_unknown_not_false(self):
        log = FrontendLog()
        log.add(r(999.0, over=500)); log.add(r(1005.0, over=500)); log.add(r(1010.0, over=3))
        w = log.window(1000.0, 1015.0, SR)
        self.assertIsNone(w["ad_over_delta"])
        self.assertIsNone(w["overloaded"])
        self.assertTrue(w["ad_over_reset"])

    def test_ad_over_reset_false_when_counter_rises(self):
        log = FrontendLog()
        log.add(r(999.0, over=7)); log.add(r(1010.0, over=9))
        self.assertFalse(log.window(1000.0, 1015.0, SR)["ad_over_reset"])


class WindowBoundaryTests(unittest.TestCase):
    def test_step_straddling_t0_is_clamped_to_start(self):
        log = FrontendLog()
        log.add(r(995.0)); log.add(r(1002.0, gain=12.5)); log.add(r(1006.0, gain=12.5))
        w = log.window(1000.0, 1010.0, SR)
        self.assertEqual(len(w["steps"]), 1)
        self.assertEqual(w["steps"][0]["sample_window"], [0, 2 * SR])

    def test_step_straddling_t1_is_clamped_to_end(self):
        log = FrontendLog()
        log.add(r(1005.0)); log.add(r(1012.0, gain=12.5))
        w = log.window(1000.0, 1010.0, SR)
        self.assertEqual(len(w["steps"]), 1)
        self.assertEqual(w["steps"][0]["sample_window"], [5 * SR, 10 * SR])

    def test_reading_exactly_at_t0_is_the_start_state(self):
        log = FrontendLog()
        log.add(r(999.0, gain=8.0)); log.add(r(1000.0, gain=10.0)); log.add(r(1004.0, gain=10.0))
        w = log.window(1000.0, 1008.0, SR)
        self.assertEqual(w["start"]["rf_gain"], 10.0)
        self.assertEqual(w["start"]["t"], 1000.0)
        self.assertEqual(w["steps"], [])

    def test_reading_exactly_at_t1_is_inside(self):
        log = FrontendLog()
        log.add(r(1000.0)); log.add(r(1010.0, gain=12.5))
        w = log.window(1000.0, 1010.0, SR)
        self.assertEqual(w["end"]["t"], 1010.0)
        self.assertEqual(w["end"]["rf_gain"], 12.5)
        self.assertEqual(w["steps"][0]["sample_window"], [0, 10 * SR])


class FakeSock:
    def __init__(self):
        self.calls = []

    def setsockopt(self, level, opt, val):
        self.calls.append(("setsockopt", level, opt, val))

    def bind(self, addr):
        self.calls.append(("bind", addr))

    def settimeout(self, t):
        self.calls.append(("settimeout", t))


class ListenerSocketTests(unittest.TestCase):
    def test_binds_to_group_not_wildcard(self):
        s = FakeSock()
        configure_status_socket(s, "239.1.2.3", 5006)
        self.assertIn(("bind", ("239.1.2.3", 5006)), s.calls)
        self.assertNotIn(("bind", ("0.0.0.0", 5006)), s.calls)

    def test_joins_group(self):
        s = FakeSock()
        configure_status_socket(s, "239.1.2.3", 5006)
        adds = [c for c in s.calls if c[0] == "setsockopt" and c[2] == socket.IP_ADD_MEMBERSHIP]
        self.assertEqual(len(adds), 1)
        self.assertTrue(adds[0][3].startswith(socket.inet_aton("239.1.2.3")))

    def test_multicast_all_off_on_linux(self):
        s = FakeSock()
        with mock.patch.object(sys, "platform", "linux"):
            configure_status_socket(s, "239.1.2.3", 5006)
        self.assertIn(("setsockopt", socket.IPPROTO_IP, 49, 0), s.calls)

    def test_multicast_all_skipped_off_linux(self):
        s = FakeSock()
        with mock.patch.object(sys, "platform", "darwin"):
            configure_status_socket(s, "239.1.2.3", 5006)
        self.assertFalse([c for c in s.calls if c[0] == "setsockopt" and c[2] == 49])

    def test_multicast_all_oserror_is_tolerated(self):
        class Refusing(FakeSock):
            def setsockopt(self, level, opt, val):
                if opt == 49:
                    raise OSError("nope")
                super().setsockopt(level, opt, val)
        with mock.patch.object(sys, "platform", "linux"):
            configure_status_socket(Refusing(), "239.1.2.3", 5006)

    def test_start_twice_opens_one_socket(self):
        opened = []
        lst = FrontendListener("x.local", FrontendLog())
        lst._open = lambda: (opened.append(1), FakeSock())[1]
        lst._loop = lambda: None
        lst.start(); lst.start()
        self.assertEqual(len(opened), 1)

    def test_error_log_is_throttled(self):
        lst = FrontendListener("x.local", FrontendLog())
        with self.assertLogs("meteor_scatter.core.frontend", "WARNING") as cm:
            for now in (0.0, 1.0, 2.0, 59.0, 61.0):
                lst._warn(OSError("down"), now)
        self.assertEqual(len(cm.records), 2)


class ChannelStatusTests(unittest.TestCase):
    def test_latest_report_per_frequency(self):
        log = FrontendLog()
        log.add_channel(50260000.0, ssrc=50260, preset="usb", agc_enable=False, gain_db=0.0,
                        encoding="F32LE", t=1000.0)
        log.add_channel(50260000.0, ssrc=50260, preset="usb", agc_enable=True, gain_db=3.0,
                        encoding="F32LE", t=1001.0)
        rep = log.channel_reported(50260000)
        self.assertEqual(rep, {"ssrc": 50260, "preset": "usb", "agc_enable": True,
                               "gain_db": 3.0, "encoding": "F32LE", "received_at": 1001.0})

    def test_never_seen_is_none(self):
        log = FrontendLog()
        log.add_channel(28145000.0, ssrc=1, preset="usb", agc_enable=False, gain_db=0.0,
                        encoding="F32LE", t=1000.0)
        self.assertIsNone(log.channel_reported(50260000))

    def test_listener_records_the_channel_from_the_same_packet(self):
        from types import SimpleNamespace
        log = FrontendLog()
        lst = FrontendListener("x.local", log)
        st = SimpleNamespace(
            ssrc=77, frequency=50260000.0, preset="usb", agc_enable=False, gain=0.0,
            encoding_name="F32LE",
            frontend=SimpleNamespace(rf_gain=None, rf_atten=None, rf_agc=None,
                                     if_power=None, ad_over=None))
        lst._ingest(st, 1000.0)
        self.assertEqual(log.channel_reported(50260000)["ssrc"], 77)
        self.assertIsNone(log.latest())          # no front-end fields: no reading
