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
                         "261213_235930    50.260 Rx MSK144  -2  0.3 1500 CQ W3USR FN21\n")


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


class ReadmeIqTests(unittest.TestCase):
    CFG = {"operator": "M", "contact_email": "a@b.c", "antenna": "d", "comments": ""}

    def test_usb_only_text_is_byte_for_byte_84c529e(self):
        import hashlib
        h = lambda t: hashlib.sha256(t.encode()).hexdigest()
        cfg = dict(self.CFG, operator="Michael Hauan AC0G", antenna="dipole")
        f = P.description_fields(cfg, "AC0G/B4", "EM38ww", ["6m"], "v1")
        self.assertEqual(h(P.readme_text("v1")),
                         "d58b350d05d18f29e7202e96ad9e697aeb94b93edc28b4a85a62d4226046a472")
        self.assertEqual(h(P.description_text(f)),
                         "e16ebf6fc58d5092e2d924fef389d904cec62982af68614b400afe0ad5280d38")
        self.assertEqual(h(P.description_html(f)),
                         "9a3e386d46def60cc75116c00446120c170f8295e99016d1a0023033bd154d88")

    def test_package_readme_explains_iq(self):
        r = P.readme_text("v1", has_iq=True)
        self.assertTrue(r.startswith(P.readme_text("v1")))      # the usb text is kept whole
        for want in ("in-phase part", "quadrature part", "phase and frequency of a ping",
                     "real part, I, is the USB audio", "little-endian IEEE 32-bit float",
                     "12 kHz", "iq_center_hz", "meteor-scatter msqp wav",
                     "https://github.com/HamSCI/meteor-scatter", "<stem>.s16.wav",
                     "slot_start_utc_exact", "first_sample_rtp", "label_anchor_utc",
                     "first_sample_offset", "utc(sample i) = label_anchor_utc",
                     "gain_chain", "scale_db", "wav_gain_db", "channel_reported",
                     '"usb"', '"iq"', "contradicts the sidecar"):
            self.assertIn(want, r)
        self.assertLess(r.index("numpy code"), r.index("meteor-scatter msqp wav"))  # numpy leads
        self.assertLessEqual(max(len(ln) for ln in r.splitlines()), 100)

    def test_description_names_format_and_wav_route(self):
        text = P.description_text(P.description_fields(self.CFG, "AC0G", "EM38ww", ["6m"], "v1",
                                                       has_iq=True))
        for want in ("two-channel IEEE float", "I then Q", "msqp wav", "16-bit"):
            self.assertIn(want, text)
        plain = P.description_text(P.description_fields(self.CFG, "AC0G", "EM38ww", ["6m"], "v1"))
        self.assertNotIn("I/Q", plain)
