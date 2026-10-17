"""Just enough of Zenodo's InvenioRDM API to leave an MSQP draft for its operator.

This module can create a draft, upload files, reserve a DOI and attach the community's review
request.  It has no way to submit the request or publish: a person does that on Zenodo, because a
published record can never change.

The endpoint paths follow InvenioRDM as the capture plan lists them; they stay provisional until
the operator's Zenodo sandbox probe (deferred) confirms them.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class ZenodoError(Exception):
    def __init__(self, status, body, what):
        super().__init__(f"Zenodo {what}: HTTP {status}: {body[:300]!r}")
        self.status, self.body = status, body


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never send the bearer token to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(
                req.full_url).netloc:
            for name in list(new.headers):
                if name.lower() == "authorization":
                    del new.headers[name]
            for name in list(new.unredirected_hdrs):
                if name.lower() == "authorization":
                    del new.unredirected_hdrs[name]
        return new


def _urllib_transport(method, url, headers, data):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(_SameHostRedirect)
    try:
        with opener.open(req, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Zenodo:
    def __init__(self, base_url: str, token: str, transport=None):
        self._base = base_url.rstrip("/")
        self._token = token
        self._t = transport or _urllib_transport

    def _call(self, what, method, path, body=None, raw: bytes = None,
              content_type="application/json"):
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = content_type
        try:
            status, resp = self._t(method, self._base + path, headers, data)
        except OSError as e:  # URLError, socket timeout, connection reset
            raise ZenodoError(0, str(e).encode(), what) from e
        if status >= 300:
            raise ZenodoError(status, resp, what)
        try:
            return json.loads(resp) if resp else {}
        except ValueError as e:
            raise ZenodoError(status, resp, f"{what} (reply is not JSON)") from e

    @staticmethod
    def _id(record_id: str) -> str:
        return urllib.parse.quote(str(record_id), safe="")

    def create_draft(self, metadata: dict) -> dict:
        return self._call("create draft", "POST", "/api/records", metadata)

    @staticmethod
    def draft_url(record: dict) -> str:
        return record.get("links", {}).get("self_html", "")

    def get_draft(self, record_id: str) -> dict:
        return self._call("read draft", "GET", f"/api/records/{self._id(record_id)}/draft")

    def committed_files(self, record_id: str) -> set:
        r = self._call("list files", "GET", f"/api/records/{self._id(record_id)}/draft/files")
        return {e["key"] for e in r.get("entries", []) if e.get("status") == "completed"}

    def upload(self, record_id: str, path: Path) -> None:
        self.upload_bytes(record_id, path.name, path.read_bytes())

    def upload_bytes(self, record_id: str, key: str, data: bytes) -> None:
        """Upload `data` as the draft file `key`: start, send the content, commit.  The caller
        reads the bytes (`msqp draft` reads them through a directory fd)."""
        base = f"/api/records/{self._id(record_id)}/draft/files"
        self._call("start upload", "POST", base, [{"key": key}])
        key = urllib.parse.quote(key)
        self._call("upload", "PUT", f"{base}/{key}/content", raw=data,
                   content_type="application/octet-stream")
        self._call("commit upload", "POST", f"{base}/{key}/commit")

    def reserve_doi(self, record_id: str) -> None:
        self._call("reserve DOI", "POST", f"/api/records/{self._id(record_id)}/draft/pids/doi")

    def request_community(self, record_id: str, community_id: str) -> None:
        self._call("attach community", "PUT", f"/api/records/{self._id(record_id)}/draft/review",
                   {"receiver": {"community": community_id}, "type": "community-submission"})
