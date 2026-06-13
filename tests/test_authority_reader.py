"""Tests for AuthorityReader (consumer side of hf-timestd authority.json
schema v1 — see METROLOGY.md §4.5.2)."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from msk144_recorder.core.authority_reader import AuthorityReader, AuthoritySnapshot


def _good(**overrides) -> dict:
    base = {
        "schema": "v1",
        "utc_published": "2026-04-23T12:00:00.000000Z",
        "a_level": "A1",
        "t_level_active": "T3",
        "t_level_available": ["T3", "T2"],
        "t_level_witnesses": ["T2"],
        "rtp_to_utc_offset_ns": 812_345,
        "sigma_ns": 940_000,
        "stations_contributing": ["WWV", "CHU"],
        "last_transition_utc": "2026-04-23T11:58:13.000000Z",
        "disagreement_flags": [],
    }
    base.update(overrides)
    return base


class TestAuthorityReader(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "authority.json"
        self.now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, payload: dict) -> None:
        with self.path.open("w") as f:
            json.dump(payload, f)

    def _reader(self, **kw) -> AuthorityReader:
        return AuthorityReader(path=self.path, now_fn=lambda: self.now, **kw)

    def test_happy_path(self) -> None:
        self._write(_good())
        s = self._reader().read()
        self.assertIsNotNone(s)
        self.assertTrue(s.offset_usable)
        self.assertAlmostEqual(s.offset_seconds, 812_345e-9, places=9)

    def test_offset_usable_false_when_no_active_level(self) -> None:
        self._write(_good(t_level_active=None, rtp_to_utc_offset_ns=None, sigma_ns=None))
        s = self._reader().read()
        self.assertFalse(s.offset_usable)

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(self._reader().read())

    def test_corrupt_json_returns_none(self) -> None:
        self.path.write_text("{garbage")
        self.assertIsNone(self._reader().read())

    def test_unknown_schema_returns_none(self) -> None:
        self._write(_good(schema="v2"))
        self.assertIsNone(self._reader().read())

    def test_stale_publication_returns_none(self) -> None:
        self.now = self.now + timedelta(minutes=5)
        self._write(_good())
        self.assertIsNone(self._reader(freshness_sec=60.0).read())

    def test_governor_radiod_parsed(self) -> None:
        self._write(_good(governor_radiod="bee1-hf-status.local"))
        s = self._reader().read()
        self.assertEqual(s.governor_radiod, "bee1-hf-status.local")

    def test_governor_radiod_none_when_absent(self) -> None:
        self._write(_good())
        s = self._reader().read()
        self.assertIsNone(s.governor_radiod)

    def test_negative_offset_handled(self) -> None:
        self._write(_good(rtp_to_utc_offset_ns=-1_234_567))
        s = self._reader().read()
        self.assertEqual(s.rtp_to_utc_offset_ns, -1_234_567)
        self.assertAlmostEqual(s.offset_seconds, -0.001234567, places=9)


if __name__ == "__main__":
    unittest.main()
