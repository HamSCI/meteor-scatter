import json, tempfile, unittest
from datetime import date
from pathlib import Path

from meteor_scatter.core.archive import Archive, band_label, slot_stem, slot_day

T0 = 1797206370.0          # 2026-12-13 23:59:30 UTC


class NamingTests(unittest.TestCase):
    def test_band_labels(self):
        self.assertEqual(band_label(28145000), "10m")
        self.assertEqual(band_label(50260000), "6m")
        self.assertEqual(band_label(144150000), "144150kHz")

    def test_stem_follows_wsjtx(self):
        self.assertEqual(slot_stem(T0), "261213_235930")

    def test_slot_near_midnight_belongs_to_its_start_day(self):
        self.assertEqual(slot_day(T0), date(2026, 12, 13))


class KeepAndPruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "archive"
        self.spool = Path(self.tmp.name) / "spool"
        self.spool.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _wav(self, name="x.wav", size=1000):
        p = self.spool / name
        p.write_bytes(b"\0" * size)
        return p

    def test_keep_moves_wav_and_writes_sidecar(self):
        a = Archive(self.root, max_days=90, max_gb=2)
        dest = a.keep(self._wav(), 28145000, T0, {"k": 1})
        self.assertEqual(dest, self.root / "10m" / "20261213" / "261213_235930.wav")
        self.assertTrue(dest.exists())
        self.assertEqual(json.loads(dest.with_suffix(".json").read_text()), {"k": 1})
        self.assertFalse((self.spool / "x.wav").exists())

    def test_prune_by_age(self):
        a = Archive(self.root, max_days=90, max_gb=2)
        for d in ("20260901", "20261213"):
            (self.root / "6m" / d).mkdir(parents=True)
            (self.root / "6m" / d / "a.wav").write_bytes(b"\0")
        removed = a.prune(date(2026, 12, 14))
        self.assertEqual(removed, ["6m/20260901"])
        self.assertTrue((self.root / "6m" / "20261213").exists())

    def test_prune_by_size_removes_oldest_day_first(self):
        a = Archive(self.root, max_days=90, max_gb=0.000002)   # 2000 bytes
        for d in ("20261211", "20261212", "20261213"):
            (self.root / "10m" / d).mkdir(parents=True)
            (self.root / "10m" / d / "a.wav").write_bytes(b"\0" * 1000)
        removed = a.prune(date(2026, 12, 13))
        self.assertEqual(removed, ["10m/20261211"])

    def test_recordings_in_range_by_start_day(self):
        a = Archive(self.root, max_days=90, max_gb=2)
        a.keep(self._wav("a.wav"), 50260000, T0, {})
        a.keep(self._wav("b.wav"), 50260000, T0 + 60, {})   # 2026-12-14 00:00:30
        got = a.recordings(date(2026, 12, 13), date(2026, 12, 13))
        self.assertEqual([p.name for _, p, _ in got], ["261213_235930.wav"])

    def test_usage_counts_bytes_oldest_and_today(self):
        a = Archive(self.root, max_days=90, max_gb=2)
        a.keep(self._wav(size=500), 28145000, T0, {})
        u = a.usage(date(2026, 12, 13))
        self.assertEqual(u["oldest_day"], "20261213")
        self.assertEqual(u["kept_today"], 1)
        self.assertGreaterEqual(u["bytes"], 500)
