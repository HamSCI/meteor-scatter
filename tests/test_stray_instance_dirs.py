"""The stray AC0G\\x3dB4/ directories an older unit template built under
/var/lib/meteor-scatter go away on install, but only while they hold
nothing but empty mode directories (sigmond tasks/spec-reporter-id-v372.md
§5)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "remove-stray-instance-dirs.sh"
UNIT = REPO / "systemd" / "meteor-scatter@.service"
STATE_ROOT = "/var/lib/meteor-scatter"
MODE = "msk144"


class UnitTemplateTests(unittest.TestCase):

    def test_the_unit_builds_no_per_instance_directory(self):
        # %i keeps systemd's spelling, so this used to make AC0G\x3dB4/.
        self.assertNotIn(f"{STATE_ROOT}/%i", UNIT.read_text())


class StrayDirTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stray = self.root / "AC0G\\x3dB4"

    def _run(self, root=None):
        return subprocess.run(["bash", str(SCRIPT), str(root or self.root)],
                              capture_output=True, text=True)

    def test_empty_mode_dirs_are_removed(self):
        (self.stray / MODE).mkdir(parents=True)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.stray.exists())
        self.assertIn("removed empty stray directory", r.stdout)

    def test_a_dir_holding_a_file_stays_and_is_named(self):
        (self.stray / MODE).mkdir(parents=True)
        (self.stray / MODE / "slot.wav").write_bytes(b"")
        r = self._run()
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.stray / MODE / "slot.wav").exists())
        self.assertIn("slot.wav", r.stderr)

    def test_a_dir_with_nested_dirs_stays(self):
        (self.stray / MODE / "2026").mkdir(parents=True)
        self._run()
        self.assertTrue((self.stray / MODE / "2026").is_dir())

    def test_real_spool_dirs_are_never_touched(self):
        spool = self.root / "B4-100-rx888mk2-status.local" / MODE
        spool.mkdir(parents=True)
        plain = self.root / "AC0G=B4"
        plain.mkdir()
        self._run()
        self.assertTrue(spool.is_dir())
        self.assertTrue(plain.is_dir())

    def test_a_symlink_is_never_followed_or_removed(self):
        # Review Focus 4.
        target = self.root / "elsewhere"
        target.mkdir()
        os.symlink(target, self.stray)
        self._run()
        self.assertTrue(self.stray.is_symlink())
        self.assertTrue(target.is_dir())

    def test_a_missing_root_is_fine(self):
        self.assertEqual(self._run(self.root / "absent").returncode, 0)

    def test_install_runs_it_after_the_unit_reload(self):
        text = (REPO / "scripts" / "install.sh").read_text()
        call = 'bash "$REPO_SOURCE/scripts/remove-stray-instance-dirs.sh" "$SPOOL_DIR"'
        self.assertIn(call, text)
        self.assertGreater(text.index(call), text.index("systemctl daemon-reload"))


if __name__ == "__main__":
    unittest.main()
