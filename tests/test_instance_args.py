"""The daemon receives --instance %i --radiod-id %i, and %i keeps systemd's
spelling (AC0G\\x3dB4).  cli._normalize_instance_args turns both back into
the storage form before anything reads them (sigmond
tasks/spec-reporter-id-v372.md §5, M1 and M7)."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from meteor_scatter.cli import _normalize_instance_args  # noqa: E402


class InstanceArgTests(unittest.TestCase):

    def test_systemd_spelling_becomes_storage(self):
        ns = SimpleNamespace(instance="AC0G\\x3dB4", radiod_id="AC0G\\x3dB4")
        _normalize_instance_args(ns)
        self.assertEqual((ns.instance, ns.radiod_id), ("AC0G=B4", "AC0G=B4"))

    def test_every_escape_is_decoded_not_only_x3d(self):
        ns = SimpleNamespace(instance="W1ABC\\x2d5", radiod_id=None)
        _normalize_instance_args(ns)
        self.assertEqual(ns.instance, "W1ABC-5")
        self.assertIsNone(ns.radiod_id)

    def test_plain_names_pass_through(self):
        ns = SimpleNamespace(instance="AC0G-B4",
                             radiod_id="B4-100-rx888mk2-status.local")
        _normalize_instance_args(ns)
        self.assertEqual((ns.instance, ns.radiod_id),
                         ("AC0G-B4", "B4-100-rx888mk2-status.local"))


if __name__ == "__main__":
    unittest.main()
