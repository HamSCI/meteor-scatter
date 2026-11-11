"""The unit's %i is the instance key (AI6VN-1) while the reporter is the
true id (AI6VN/1) -- one instance, never a mismatch (rob, 2026-11-11)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from meteor_scatter._reporter_id import systemd_instance, to_key  # noqa: E402


class InstanceKeyTests(unittest.TestCase):
    def test_every_spelling_has_one_key(self):
        for text in ("AI6VN/1", "AI6VN=1", "AI6VN\\x3d1", "ai6vn/1"):
            with self.subTest(text=text):
                self.assertEqual(to_key(text), "AI6VN-1")
        self.assertEqual(systemd_instance("AI6VN=1"), "AI6VN-1")
        self.assertEqual(systemd_instance("my-rx888"), "my-rx888")

    def test_msqp_accepts_the_key_against_the_true_id(self):
        try:
            from meteor_scatter.msqp_cli import _check_instance_matches, PackageRefused
        except Exception as exc:                     # noqa: BLE001
            self.skipTest(f"msqp_cli not importable here: {exc}")
        _check_instance_matches("AI6VN-1", "AI6VN/1", "config", per_instance=True)
        with self.assertRaises(PackageRefused):
            _check_instance_matches("AI6VN-2", "AI6VN/1", "config", per_instance=True)


if __name__ == "__main__":
    unittest.main()
