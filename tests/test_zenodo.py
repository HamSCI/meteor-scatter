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


class ZenodoRobustnessTests(unittest.TestCase):
    def test_trailing_slash_base_url(self):
        t = FakeTransport([(201, b"{}")])
        Zenodo("https://z/", "T", transport=t).create_draft({})
        self.assertEqual(t.calls[0][1], "https://z/api/records")

    def test_urlerror_becomes_zenodo_error(self):
        import urllib.error

        def boom(method, url, headers, data):
            raise urllib.error.URLError("connection refused")
        with self.assertRaises(ZenodoError) as cm:
            Zenodo("https://z", "T", transport=boom).create_draft({})
        self.assertEqual(cm.exception.status, 0)
        self.assertIn(b"connection refused", cm.exception.body)

    def test_socket_timeout_becomes_zenodo_error(self):
        def boom(method, url, headers, data):
            raise TimeoutError("timed out")
        with self.assertRaises(ZenodoError):
            Zenodo("https://z", "T", transport=boom).get_draft("abc")

    def test_upload_put_is_octet_stream(self):
        t = FakeTransport([(201, b"{}"), (200, b"{}"), (200, b"{}")])
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.zip"; p.write_bytes(b"PK")
            Zenodo("https://z", "T", transport=t).upload("abc", p)
        self.assertEqual(t.calls[1][2]["Content-Type"], "application/octet-stream")
        self.assertEqual(t.calls[0][2]["Content-Type"], "application/json")

    def test_non_json_2xx_raises_zenodo_error(self):
        t = FakeTransport([(200, b"<html>maintenance</html>")])
        with self.assertRaises(ZenodoError) as cm:
            Zenodo("https://z", "T", transport=t).get_draft("abc")
        self.assertEqual(cm.exception.status, 200)

    def test_record_id_is_quoted(self):
        t = FakeTransport([(200, b"{}"), (200, b"{}"), (200, b"{}"), (200, b"{}"), (200, b"{}")])
        z = Zenodo("https://z", "T", transport=t)
        z.get_draft("a/b c"); z.committed_files("a/b c"); z.reserve_doi("a/b c")
        z.request_community("a/b c", "comm")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.zip"; p.write_bytes(b"PK")
            t.replies[:] = [(200, b"{}")] * 3
            z.upload("a/b c", p)
        for _, url, _, _ in t.calls:
            self.assertIn("/api/records/a%2Fb%20c/", url)

    def test_cross_host_redirect_drops_authorization(self):
        import urllib.request
        from meteor_scatter.zenodo import _SameHostRedirect
        req = urllib.request.Request("https://z/api/records/x/draft/files",
                                     headers={"Authorization": "Bearer TOK"}, method="GET")
        h = _SameHostRedirect()
        other = h.redirect_request(req, None, 302, "Found", {}, "https://s3.example/blob")
        self.assertNotIn("authorization", {k.lower() for k in other.headers})
        same = h.redirect_request(req, None, 302, "Found", {}, "https://z/elsewhere")
        self.assertIn("authorization", {k.lower() for k in same.headers})
