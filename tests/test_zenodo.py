import json, tempfile, unittest
from pathlib import Path
from meteor_scatter.zenodo import Zenodo, ZenodoError


class FakeTransport:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, headers, data))
        return self.replies.pop(0)


class ZenodoTests(unittest.TestCase):
    def test_create_sends_bearer_token_and_json(self):
        t = FakeTransport([(201, json.dumps({"id": "abc", "links": {"self_html": "https://z/abc"}}).encode())])
        z = Zenodo("https://sandbox.zenodo.org", "TOK", transport=t)
        rec = z.create_draft({"metadata": {}})
        self.assertEqual(rec["id"], "abc")
        m, url, h, data = t.calls[0]
        self.assertEqual((m, url), ("POST", "https://sandbox.zenodo.org/api/records"))
        self.assertEqual(h["Authorization"], "Bearer TOK")

    def test_upload_is_init_content_commit(self):
        t = FakeTransport([(201, b"{}"), (200, b"{}"), (200, b"{}")])
        z = Zenodo("https://z", "T", transport=t)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.zip"; p.write_bytes(b"PK")
            z.upload("abc", p)
        self.assertEqual([c[0] for c in t.calls], ["POST", "PUT", "POST"])
        self.assertTrue(t.calls[1][1].endswith("/api/records/abc/draft/files/a.zip/content"))
        self.assertEqual(t.calls[1][3], b"PK")

    def test_upload_quotes_key_in_paths_but_not_in_init_body(self):
        t = FakeTransport([(201, b"{}"), (200, b"{}"), (200, b"{}")])
        z = Zenodo("https://z", "T", transport=t)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "my shower.zip"; p.write_bytes(b"PK")
            z.upload("abc", p)
        self.assertEqual(json.loads(t.calls[0][3]), [{"key": "my shower.zip"}])
        self.assertTrue(t.calls[1][1].endswith("/draft/files/my%20shower.zip/content"))
        self.assertTrue(t.calls[2][1].endswith("/draft/files/my%20shower.zip/commit"))

    def test_error_status_raises_with_body(self):
        t = FakeTransport([(403, b'{"message": "no"}')])
        with self.assertRaises(ZenodoError) as cm:
            Zenodo("https://z", "T", transport=t).create_draft({})
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(cm.exception.body, b'{"message": "no"}')

    def test_no_publish_or_submit_anywhere(self):
        import meteor_scatter.zenodo as zmod
        src = Path(zmod.__file__).read_text()
        self.assertNotIn("submit-review", src)
        self.assertNotIn("/actions/publish", src)
