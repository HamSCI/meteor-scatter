"""Tests for radiod channel-lifetime keep-alive (ka9q-python ≥3.13.0).

msk144-recorder opts into ka9q-python / radiod's LIFETIME tag so a crashed
or killed recorder can't leave channels lingering on radiod beyond
~`radiod_lifetime_frames / 50` seconds (≈2 min at the default).

Two surfaces under test:
  * config: ``processing.radiod_lifetime_frames`` defaults to 6000,
    validates non-negative int, sentinel 0 = "no LIFETIME tag".
  * keep-alive thread: refreshes every (frames/50/4) seconds against
    every (MultiStream, ssrc) the provisioner registered; survives
    individual `set_channel_lifetime` failures (radiod restart etc.).

The provisioning path (the actual `multi.add_channel(lifetime=...)`
plumbing) is exercised by integration smoke-tests against a live
radiod, not here — those are gated on a real ka9q-python install.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from msk144_recorder.config import DEFAULTS, load_config


class ConfigDefaultsTests(unittest.TestCase):

    def test_default_is_6000_frames(self):
        self.assertEqual(
            DEFAULTS["processing"]["radiod_lifetime_frames"], 6000,
        )

    def _write_config(self, body: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False,
        )
        tmp.write(body)
        tmp.flush()
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(path.unlink)
        return path

    def test_missing_section_falls_back_to_default(self):
        # No [processing] section at all — should default to 6000.
        path = self._write_config(
            '[paths]\nspool_dir = "/tmp/x"\n'
            '[[radiod]]\nstatus = "host"\n'
        )
        cfg = load_config(path)
        self.assertEqual(
            cfg["processing"]["radiod_lifetime_frames"], 6000,
        )

    def test_explicit_value_honored(self):
        path = self._write_config(
            '[processing]\nradiod_lifetime_frames = 3000\n'
            '[[radiod]]\nstatus = "host"\n'
        )
        cfg = load_config(path)
        self.assertEqual(
            cfg["processing"]["radiod_lifetime_frames"], 3000,
        )

    def test_zero_means_no_lifetime_tag(self):
        # 0 is the sentinel for "don't send LIFETIME, no keep-alive."
        path = self._write_config(
            '[processing]\nradiod_lifetime_frames = 0\n'
            '[[radiod]]\nstatus = "host"\n'
        )
        cfg = load_config(path)
        self.assertEqual(cfg["processing"]["radiod_lifetime_frames"], 0)

    def test_negative_rejected(self):
        path = self._write_config(
            '[processing]\nradiod_lifetime_frames = -1\n'
            '[[radiod]]\nstatus = "host"\n'
        )
        with self.assertRaisesRegex(ValueError, "radiod_lifetime_frames"):
            load_config(path)

    def test_non_int_rejected(self):
        path = self._write_config(
            '[processing]\nradiod_lifetime_frames = "many"\n'
            '[[radiod]]\nstatus = "host"\n'
        )
        with self.assertRaisesRegex(ValueError, "radiod_lifetime_frames"):
            load_config(path)


class _RecorderForKeepAliveTests:
    """Constructs Msk144Recorder with a stub config + manually populated
    private state so we can exercise the lifetime-keepalive paths
    without hitting `_provision_channels` (which imports ka9q).
    """

    @staticmethod
    def make(lifetime_frames: int):
        # Import is lazy because Msk144Recorder lives in core.recorder which
        # itself only does `from ka9q import ...` inside provisioning.
        from msk144_recorder.core.recorder import Msk144Recorder
        cfg = {
            "paths": {
                "spool_dir": "/tmp/psk-test", "log_dir": "/tmp/psk-test",
            },
            "station": {},
            "processing": {"radiod_lifetime_frames": lifetime_frames},
        }
        radiod = {"status": "host"}
        return Msk144Recorder(cfg, radiod)


class KeepAliveLoopTests(unittest.TestCase):
    """Verify the keepalive thread refreshes every (multi, ssrc) entry
    and tolerates individual set_channel_lifetime failures.
    """

    def test_no_thread_started_when_no_entries(self):
        rec = _RecorderForKeepAliveTests.make(6000)
        # No channels provisioned → no entries → no thread.
        rec._start_lifetime_keepalive()
        self.assertIsNone(rec._lifetime_thread)

    def test_thread_refreshes_all_entries(self):
        rec = _RecorderForKeepAliveTests.make(200)  # 200 frames = 4s
        m1, m2 = mock.MagicMock(), mock.MagicMock()
        rec._lifetime_entries = [(m1, 100), (m1, 101), (m2, 200)]
        rec._running = True

        # Drive the loop directly with a tight interval so the test
        # finishes quickly.  We don't go through _start_lifetime_keepalive
        # because that uses the natural cadence (frames/50/4).
        thread = threading.Thread(
            target=rec._lifetime_loop, args=(0.05,), daemon=True,
        )
        thread.start()
        # Give it long enough to fire at least twice across all entries.
        time.sleep(0.18)
        rec._running = False
        thread.join(timeout=1.0)

        # Each entry should have been refreshed at least once.
        m1.set_channel_lifetime.assert_any_call(100, 200)
        m1.set_channel_lifetime.assert_any_call(101, 200)
        m2.set_channel_lifetime.assert_any_call(200, 200)

    def test_failure_does_not_crash_loop(self):
        rec = _RecorderForKeepAliveTests.make(200)
        m_bad = mock.MagicMock()
        m_bad.set_channel_lifetime.side_effect = RuntimeError("radiod down")
        m_good = mock.MagicMock()
        rec._lifetime_entries = [(m_bad, 100), (m_good, 200)]
        rec._running = True

        thread = threading.Thread(
            target=rec._lifetime_loop, args=(0.05,), daemon=True,
        )
        thread.start()
        time.sleep(0.12)
        rec._running = False
        thread.join(timeout=1.0)

        # The good entry must still have been refreshed despite the bad
        # one raising every iteration.
        m_good.set_channel_lifetime.assert_any_call(200, 200)

    def test_zero_frames_means_no_provisioning(self):
        """When lifetime_frames=0, no entries should be appended at
        provisioning time.  The provisioning path itself is integration-
        only, but we can verify the recorder honors the sentinel by
        checking the configured value is read straight through.
        """
        rec = _RecorderForKeepAliveTests.make(0)
        self.assertEqual(rec._radiod_lifetime_frames, 0)


if __name__ == "__main__":
    unittest.main()
