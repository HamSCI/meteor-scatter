"""This client's copy of the reporter-id rules, run against the shared table.

tests/data/reporter_id_vectors.json must stay a byte-identical copy of
sigmond's tests/data/reporter_id_vectors.json; sigmond's
tests/test_reporter_id_drift.py fails when they differ.  Refresh it with:

    cp ../sigmond/tests/data/reporter_id_vectors.json tests/data/
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from meteor_scatter._reporter_id import (  # noqa: E402
    display, systemd_instance, to_storage, unescape_systemd_instance,
)

VECTORS = json.loads(
    (REPO / "tests" / "data" / "reporter_id_vectors.json").read_text())


class VectorTableTests(unittest.TestCase):

    def test_reporter_ids(self):
        for v in VECTORS["reporter_ids"]:
            with self.subTest(input=v["input"]):
                self.assertEqual(to_storage(v["input"]), v["storage"])
                # Units and per-instance files carry the key (AC0G-B4).
                self.assertEqual(systemd_instance(v["storage"]), v["systemd"])
                # The pre-key unit spelling (AC0G\\x3dB4) still reads back.
                self.assertEqual(unescape_systemd_instance(v["legacy_systemd"]), v["storage"])
                self.assertEqual(to_storage(v["legacy_systemd"]), v["storage"])
                self.assertEqual(display(v["storage"]), v["display"])
                self.assertEqual(display(v["legacy_systemd"]), v["display"])

    def test_unit_names_escape_only_what_systemd_must(self):
        for v in VECTORS["escape_only"]:
            with self.subTest(name=v["name"]):
                self.assertEqual(systemd_instance(v["name"]), v["systemd"])

    def test_invalid_text_is_refused(self):
        for text in VECTORS["invalid"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                to_storage(text)

    def test_display_never_shows_an_escape(self):
        for v in VECTORS["display_only"]:
            with self.subTest(input=v["input"]):
                self.assertEqual(display(v["input"]), v["display"])


if __name__ == "__main__":
    unittest.main()
