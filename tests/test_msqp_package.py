import unittest
from datetime import date
from meteor_scatter import msqp_package as P


class TitleTests(unittest.TestCase):
    def test_msqp_form_same_month(self):
        self.assertEqual(P.title("AC0G/B4", "em38ww40pk", date(2026, 12, 12), date(2026, 12, 15)),
                         "AC0G/B4 EM38WW 20261212-15 Meteor Scatter QSO Party")

    def test_single_day_and_cross_month(self):
        self.assertEqual(P.title("AC0G", "EM38ww", date(2026, 12, 13), date(2026, 12, 13)),
                         "AC0G EM38WW 20261213 Meteor Scatter QSO Party")
        self.assertEqual(P.title("AC0G", "EM38ww", date(2026, 12, 31), date(2027, 1, 2)),
                         "AC0G EM38WW 20261231-0102 Meteor Scatter QSO Party")


class AllTxtTests(unittest.TestCase):
    def test_wsjtx_layout(self):
        side = {"slot_start_utc": "2026-12-13T23:59:30+00:00", "dial_freq_hz": 50260000}
        d = {"snr": -2, "dt": 0.3, "audio_freq_hz": 1500, "message": "CQ W3USR FN21"}
        self.assertEqual(P.all_txt_line(side, d),
                         "261213_235930    50.260 Rx MSK144     -2  0.3 1500 CQ W3USR FN21\n")


class DescriptionTests(unittest.TestCase):
    CFG = {"operator": "Michael Hauan AC0G", "contact_email": "a@b.c", "antenna": "dipole", "comments": ""}

    def test_fields_in_msqp_order_with_monitor_values(self):
        f = dict(P.description_fields(self.CFG, "AC0G/B4", "EM38ww40pk", ["10m", "6m"], "v1"))
        self.assertEqual(f["Operation Mode"], "Receive Only")
        self.assertEqual(f["Transmitter Output Power"], "NA")
        self.assertEqual(f["Six Character Grid Square"], "EM38WW")
        self.assertEqual(f["Bands Operated"], "10m, 6m")
        self.assertIn("AGC", f["Additional Comments"])
        self.assertIn("v1", f["Additional Comments"])
        keys = [k for k, _ in P.description_fields(self.CFG, "AC0G/B4", "EM38ww40pk", ["6m"], "v1")]
        self.assertEqual(keys, P.MSQP_FIELD_ORDER)

    def test_metadata_never_asks_to_publish(self):
        md = P.record_metadata("t", "<p>x</p>", "Michael Hauan AC0G", date(2026, 12, 16))
        self.assertEqual(md["metadata"]["resource_type"], {"id": "dataset"})
        self.assertEqual(md["access"]["record"], "public")
        self.assertEqual(md["metadata"]["rights"], [{"id": "cc-by-4.0"}])
        self.assertNotIn("submit", str(md).lower())
