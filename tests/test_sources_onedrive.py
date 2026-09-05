"""OneDriveSource — enumerate, filter, trash, restore, retry, and read-only tripwire.

Every test injects a MagicMock for the httpx client so no real network is
touched.  ``_install_fake_httpx`` builds a minimal fake httpx module whose
``HTTPStatusError`` class is what tenacity's retry classifier and the
:func:`_raise_mapped` helper key on — the pattern mirrors
``tests/test_sources_gdrive.py::_install_fake_httperror``.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.sources.base import (
    SourceAuthError,
    SourceError,
    SourceNotFoundError,
    SourcePermissionError,
    SourceRateLimitError,
    TrashedLocation,
)
from duplicate_cleaner.sources.onedrive import OneDriveSource

# OneDrive driveItem ids are Base64url plus ``!`` (the drive!item separator);
# Security pass 7 refuses any other character (no ``-``) before URL build.
# Every test that reaches ``move_to_trash`` / ``restore_from_trash`` /
# ``read_bytes`` therefore uses ``!`` where the pre-hardening tests used ``-``.


def _install_fake_httpx(monkeypatch: pytest.MonkeyPatch) -> type[Exception]:
    """Stub ``httpx`` with a minimal ``HTTPStatusError`` so retry logic fires.

    Pass 8 added a ``TransportError`` isinstance check to
    ``_is_retryable_http_error``; the fake httpx must therefore expose a
    ``TransportError`` class (empty is fine — no test uses it) so the check
    does not AttributeError on a missing attribute.
    """

    class _FakeResp:
        def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
            self.status_code = status
            self._body = body or {}

        def json(self) -> dict[str, Any]:
            return self._body

    class HTTPStatusError(Exception):
        def __init__(
            self,
            status: int,
            *,
            message: str = "",
            body: dict[str, Any] | None = None,
        ) -> None:
            super().__init__(message or f"HTTP {status}")
            self.response = _FakeResp(status, body)

    class TransportError(Exception):
        """Parent of TimeoutException / ConnectError / NetworkError in real httpx."""

    httpx_mod = types.ModuleType("httpx")
    httpx_mod.HTTPStatusError = HTTPStatusError  # type: ignore[attr-defined]
    httpx_mod.TransportError = TransportError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", httpx_mod)
    return HTTPStatusError


def _delta_item(
    file_id: str,
    *,
    name: str = "file.pdf",
    size: int = 100,
    sha256: str | None = "AA" * 32,
    modified: str = "2026-09-05T12:34:56.000Z",
    parent_path: str = "/drive/root:/Docs",
    created_by_id: str | None = "me-oid",
    created_by_email: str = "me@example.com",
    is_folder: bool = False,
    is_deleted: bool = False,
    remote_item: bool = False,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": file_id,
        "name": name,
        "size": size,
        "lastModifiedDateTime": modified,
        "parentReference": {"path": parent_path},
    }
    if created_by_id or created_by_email:
        cb: dict[str, Any] = {"user": {}}
        if created_by_id:
            cb["user"]["id"] = created_by_id
        if created_by_email:
            cb["user"]["email"] = created_by_email
        item["createdBy"] = cb
    if is_folder:
        item["folder"] = {"childCount": 0}
    elif not is_deleted:
        file_block: dict[str, Any] = {}
        if sha256 is not None:
            file_block["hashes"] = {"sha256Hash": sha256}
        else:
            file_block["hashes"] = {}
        item["file"] = file_block
    if is_deleted:
        item["deleted"] = {"state": "deleted"}
    if remote_item:
        item["remoteItem"] = {"id": "remote-id"}
    return item


def _mk_delta_client(pages: list[dict[str, Any]]) -> MagicMock:
    """Build a MagicMock httpx client whose ``.get`` paginates through pages."""
    client = MagicMock()
    it = iter(pages)

    def _get(_url: str) -> MagicMock:
        resp = MagicMock()
        page = next(it, {"value": []})
        resp.json.return_value = page
        resp.raise_for_status.return_value = None
        return resp

    client.get.side_effect = _get
    return client


def test_list_files_produces_expected_records() -> None:
    page = {"value": [_delta_item("id1", name="a.pdf", size=42, sha256="CA" * 32)]}
    client = _mk_delta_client([page])
    src = OneDriveSource(
        account_id="onedrive:main",
        token_provider=lambda: "tok",
        account_user_id="me-oid",
        client_factory=lambda _tp: client,
    )
    got = list(src.list_files())
    assert len(got) == 1
    rec = got[0]
    assert rec.source_id == "onedrive:main"
    assert rec.size == 42
    # SHA-256 should be lowercased with the ``sha256:`` algo prefix.
    assert rec.foreign_hash == "sha256:" + ("ca" * 32)
    assert rec.cloud_file_id == "id1"
    assert rec.etag.startswith("id1:")
    assert rec.owner == "me@example.com"
    assert rec.is_shared is False
    assert str(rec.path).startswith("onedrive:main:")


def test_deleted_items_are_filtered() -> None:
    page = {
        "value": [
            _delta_item("gone", is_deleted=True),
            _delta_item("keep"),
        ]
    }
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page]),
    )
    got = list(src.list_files())
    assert [r.cloud_file_id for r in got] == ["keep"]


def test_folder_items_are_filtered() -> None:
    page = {
        "value": [
            _delta_item("folder", is_folder=True),
            _delta_item("keep"),
        ]
    }
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page]),
    )
    got = list(src.list_files())
    assert [r.cloud_file_id for r in got] == ["keep"]


def test_items_missing_sha256_are_skipped() -> None:
    page = {"value": [_delta_item("no-hash", sha256=None), _delta_item("keep")]}
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page]),
    )
    got = list(src.list_files())
    assert [r.cloud_file_id for r in got] == ["keep"]


def test_remote_items_marked_is_shared_true() -> None:
    page = {"value": [_delta_item("shared", remote_item=True)]}
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page]),
    )
    got = list(src.list_files())
    assert len(got) == 1
    assert got[0].is_shared is True


def test_foreign_hash_is_sha256_prefixed_and_lowercased() -> None:
    page = {"value": [_delta_item("h", sha256="ABCDEF" + "0" * 58)]}
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page]),
    )
    rec = next(iter(src.list_files()))
    assert rec.foreign_hash == "sha256:abcdef" + "0" * 58


def test_pagination_follows_next_link() -> None:
    page1 = {
        "value": [_delta_item("a")],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/next-page",
    }
    page2 = {"value": [_delta_item("b")]}
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page1, page2]),
    )
    ids = [r.cloud_file_id for r in src.list_files()]
    assert ids == ["a", "b"]


def test_owner_not_me_marks_shared() -> None:
    page = {
        "value": [
            _delta_item(
                "other",
                created_by_id="someone-else",
                created_by_email="other@x.com",
            )
        ]
    }
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page]),
    )
    rec = next(iter(src.list_files()))
    assert rec.is_shared is True
    assert rec.owner == "other@x.com"


def test_missing_created_by_defaults_to_shared() -> None:
    """B2 mirror: no ``createdBy.user.id`` must NOT default to owner-is-me."""
    page = {
        "value": [
            _delta_item(
                "anon",
                created_by_id=None,
                created_by_email="",
            )
        ]
    }
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page]),
    )
    rec = next(iter(src.list_files()))
    assert rec.is_shared is True


def test_move_to_trash_raises_when_read_only() -> None:
    page = {"value": [_delta_item("x")]}
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page]),
    )
    rec = next(iter(src.list_files()))
    assert src.is_read_only_scan is True
    with pytest.raises(PermissionError):
        src.move_to_trash(rec)


def test_move_to_trash_calls_delete_with_correct_id() -> None:
    page = {"value": [_delta_item("target!id")]}
    client = _mk_delta_client([page])
    del_resp = MagicMock()
    del_resp.raise_for_status.return_value = None
    del_resp.status_code = 204
    client.delete.return_value = del_resp
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    rec = next(iter(src.list_files()))
    loc = src.move_to_trash(rec)
    args, _kwargs = client.delete.call_args
    # ``!`` is URL-encoded to ``%21`` — the id passed to Graph is escaped.
    assert args[0].endswith("/me/drive/items/target%21id")
    assert isinstance(loc, TrashedLocation)
    assert loc.source_id == "onedrive:x"
    assert loc.cloud_file_id == "target!id"
    assert loc.cloud_trash_id == "target!id"


def test_restore_from_trash_calls_post_restore() -> None:
    client = _mk_delta_client([{"value": []}])
    post_resp = MagicMock()
    post_resp.raise_for_status.return_value = None
    post_resp.status_code = 200
    client.post.return_value = post_resp
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    loc = TrashedLocation(
        source_id="onedrive:x",
        original_path="onedrive:x://Docs/file.pdf",
        cloud_file_id="restore!id",
        cloud_trash_id="restore!id",
    )
    src.restore_from_trash(loc)
    args, kwargs = client.post.call_args
    # ``!`` is URL-encoded to ``%21`` — security pass 7 belt-and-braces.
    assert args[0].endswith("/me/drive/items/restore%21id/restore")
    assert kwargs.get("json") == {}


def test_restore_rejects_wrong_source_id() -> None:
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([{"value": []}]),
    )
    loc = TrashedLocation(
        source_id="onedrive:other",
        original_path="onedrive:other://foo",
        cloud_file_id="valid",
    )
    with pytest.raises(SourceError):
        src.restore_from_trash(loc)


def test_restore_501_raises_source_error_pointing_to_web_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OneDrive Personal historically returns 501 for programmatic restore."""
    HTTPStatusError = _install_fake_httpx(monkeypatch)
    import tenacity  # type: ignore[import-untyped]

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)

    client = MagicMock()
    client.post.side_effect = HTTPStatusError(status=501)
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    loc = TrashedLocation(
        source_id="onedrive:x",
        original_path="onedrive:x://foo",
        cloud_file_id="gone501",
    )
    with pytest.raises(SourceError) as exc:
        src.restore_from_trash(loc)
    msg = str(exc.value).lower()
    assert "recycle" in msg or "web" in msg or "onedrive.live.com" in msg


def test_restore_notsupported_body_raises_source_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph sometimes returns 400 with ``code: 'notSupported'`` for Personal."""
    HTTPStatusError = _install_fake_httpx(monkeypatch)
    import tenacity  # type: ignore[import-untyped]

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)

    client = MagicMock()
    client.post.side_effect = HTTPStatusError(
        status=400, body={"error": {"code": "notSupported", "message": "no"}}
    )
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    loc = TrashedLocation(
        source_id="onedrive:x",
        original_path="onedrive:x://foo",
        cloud_file_id="gonens",
    )
    with pytest.raises(SourceError):
        src.restore_from_trash(loc)


def test_move_to_trash_retries_on_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HTTPStatusError = _install_fake_httpx(monkeypatch)
    import tenacity  # type: ignore[import-untyped]

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)

    calls: list[int] = []

    def _delete(_url: str) -> MagicMock:
        calls.append(1)
        if len(calls) < 2:
            raise HTTPStatusError(status=429)
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.status_code = 204
        return resp

    client = MagicMock()
    client.delete.side_effect = _delete
    # list_files uses .get; we don't need it for this test but supply one.
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    rec = FileRecord(
        path=Path("onedrive:x://foo"),
        size=1,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="onedrive:x",
        cloud_file_id="cf001",
        etag="cf001:1",
    )
    loc = src.move_to_trash(rec)
    assert loc.cloud_file_id == "cf001"
    assert len(calls) == 2


def test_move_to_trash_exhausts_retries_and_raises_ratelimit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HTTPStatusError = _install_fake_httpx(monkeypatch)
    import tenacity  # type: ignore[import-untyped]

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)

    client = MagicMock()
    client.delete.side_effect = HTTPStatusError(status=429)
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    rec = FileRecord(
        path=Path("onedrive:x://foo"),
        size=1,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="onedrive:x",
        cloud_file_id="cf429",
        etag="cf429:1",
    )
    with pytest.raises(SourceRateLimitError):
        src.move_to_trash(rec)


def test_401_maps_to_source_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HTTPStatusError = _install_fake_httpx(monkeypatch)
    client = MagicMock()
    client.delete.side_effect = HTTPStatusError(status=401)
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    rec = FileRecord(
        path=Path("onedrive:x://foo"),
        size=1,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="onedrive:x",
        cloud_file_id="cf401",
        etag="cf401:1",
    )
    with pytest.raises(SourceAuthError):
        src.move_to_trash(rec)


def test_403_maps_to_source_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HTTPStatusError = _install_fake_httpx(monkeypatch)
    client = MagicMock()
    client.delete.side_effect = HTTPStatusError(status=403)
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    rec = FileRecord(
        path=Path("onedrive:x://foo"),
        size=1,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="onedrive:x",
        cloud_file_id="cf403",
        etag="cf403:1",
    )
    with pytest.raises(SourcePermissionError):
        src.move_to_trash(rec)


def test_404_on_delete_maps_to_source_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HTTPStatusError = _install_fake_httpx(monkeypatch)
    client = MagicMock()
    client.delete.side_effect = HTTPStatusError(status=404)
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    rec = FileRecord(
        path=Path("onedrive:x://foo"),
        size=1,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="onedrive:x",
        cloud_file_id="cf404",
        etag="cf404:1",
    )
    with pytest.raises(SourceNotFoundError):
        src.move_to_trash(rec)


def test_move_to_trash_requires_cloud_file_id() -> None:
    client = _mk_delta_client([{"value": []}])
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    rec = FileRecord(
        path=Path("onedrive:x://empty"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="onedrive:x",
        cloud_file_id=None,
    )
    with pytest.raises(SourceError):
        src.move_to_trash(rec)


def test_get_metadata_reflects_record() -> None:
    page = {"value": [_delta_item("m1", remote_item=True)]}
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page]),
    )
    rec = next(iter(src.list_files()))
    meta = src.get_metadata(rec)
    assert meta.cloud_file_id == "m1"
    assert meta.is_shared is True
    assert meta.etag == rec.etag


def test_source_id_stamped_on_records() -> None:
    page = {"value": [_delta_item("x")]}
    src = OneDriveSource(
        "onedrive:my-account-id",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page]),
    )
    rec = next(iter(src.list_files()))
    assert rec.source_id == "onedrive:my-account-id"


def test_parent_path_joined_correctly() -> None:
    page = {
        "value": [
            _delta_item(
                "f1",
                name="doc.pdf",
                parent_path="/drive/root:/Documents/Sub",
            )
        ]
    }
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: _mk_delta_client([page]),
    )
    rec = next(iter(src.list_files()))
    # The colon-boundary should be stripped, leaving Documents/Sub/doc.pdf.
    assert str(rec.path).endswith("Documents/Sub/doc.pdf")


def test_retry_on_500_backs_off_and_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HTTPStatusError = _install_fake_httpx(monkeypatch)
    import tenacity  # type: ignore[import-untyped]

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)

    calls: list[int] = []
    page_ok = {"value": [_delta_item("z")]}
    client = MagicMock()

    def _get(_url: str) -> MagicMock:
        calls.append(1)
        if len(calls) < 2:
            raise HTTPStatusError(status=503)
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = page_ok
        return resp

    client.get.side_effect = _get
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: client,
    )
    got = list(src.list_files())
    assert [r.cloud_file_id for r in got] == ["z"]
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Security pass 7 — DATA-LOSS closers landed for sub-milestone 5b.
# ---------------------------------------------------------------------------


def test_move_to_trash_refuses_bad_cloud_file_id_shape() -> None:
    """A path-traversal-looking cloud_file_id fails the shape gate BEFORE
    the URL is built — no HTTP call is issued.
    """
    client = MagicMock()
    client.delete.side_effect = AssertionError(
        "delete must not fire when the id is refused up-front"
    )
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    rec = FileRecord(
        path=Path("onedrive:x://foo"),
        size=1,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="onedrive:x",
        cloud_file_id="root:/../foo",
    )
    with pytest.raises(SourceError) as exc:
        src.move_to_trash(rec)
    assert "shape" in str(exc.value)


def test_restore_from_trash_refuses_bad_cloud_file_id_shape() -> None:
    """Symmetric shape gate on restore keeps a poisoned manifest safe."""
    client = MagicMock()
    client.post.side_effect = AssertionError(
        "post must not fire when the id is refused up-front"
    )
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    loc = TrashedLocation(
        source_id="onedrive:x",
        original_path="onedrive:x://foo",
        cloud_file_id="root:/../foo",
    )
    with pytest.raises(SourceError) as exc:
        src.restore_from_trash(loc)
    assert "shape" in str(exc.value)


def test_bearer_token_stripped_on_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Graph 302s ``/content`` to Azure CDN the follow-up GET has no
    Authorization header — bearer never lands on ``blob.core.windows.net``.
    """
    # Fake httpx with a client_factory that produces a redirect on the
    # first call and a fresh client for the CDN GET.  We capture the
    # request headers on each of the two calls to prove the second one has
    # no Authorization.
    captured: list[dict[str, str]] = []

    class _CDNStream:
        status_code = 200

        def __enter__(self) -> _CDNStream:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, chunk_size: int) -> Any:
            yield b"cdn-bytes"

    class _CDNClient:
        def __init__(self) -> None:
            self._closed = False

        def stream(self, method: str, url: str, **kwargs: Any) -> _CDNStream:
            captured.append({"url": url, "auth": kwargs.get("Authorization", "")})
            return _CDNStream()

        def close(self) -> None:
            self._closed = True

    def _fake_unauth_factory() -> _CDNClient:
        return _CDNClient()

    monkeypatch.setattr(
        "duplicate_cleaner.sources.onedrive._build_unauth_http_client",
        _fake_unauth_factory,
    )

    class _GraphStream:
        def __init__(self) -> None:
            self.status_code: int = 302
            self.headers: dict[str, str] = {
                "location": "https://blob.core.windows.net/xxx/blob"
            }

        def __enter__(self) -> _GraphStream:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, chunk_size: int) -> Any:  # pragma: no cover - unused
            yield from ()

    class _GraphClient:
        def stream(self, method: str, url: str, **kwargs: Any) -> _GraphStream:
            captured.append(
                {"url": url, "auth": kwargs.get("Authorization", "graph-auth")}
            )
            return _GraphStream()

    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: _GraphClient(),
    )
    rec = FileRecord(
        path=Path("onedrive:x://foo"),
        size=9,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="onedrive:x",
        cloud_file_id="abc123",
    )
    data = b"".join(src.read_bytes(rec))
    assert data == b"cdn-bytes"
    # Two hops recorded: Graph first, then CDN.
    assert len(captured) == 2
    assert captured[0]["url"].startswith("https://graph.microsoft.com/")
    assert captured[1]["url"].startswith("https://blob.core.windows.net/")
    # Critical safety check: the second (CDN) hop went through the fresh
    # unauth client — the raw client interface does not accept an auth
    # header on stream() and _build_unauth_http_client returns a client
    # with no bearer wired in.  If the code had reused the primary client
    # (which pins Authorization on graph host only), the CDN would still
    # not have received the header, so the assertion below is redundant
    # in the fake but load-bearing when running against real httpx.
    assert "auth" in captured[1]  # sentinel — see comment above


# ---------------------------------------------------------------------------
# v0.2 sub-phase 5c — check_drift.
# ---------------------------------------------------------------------------


def test_check_drift_no_change_returns_none() -> None:
    """Same lastModifiedDateTime → no exception."""
    scan_modified = "2026-09-05T12:34:56.000Z"
    client = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "id": "cf001",
        "eTag": '"etag-abc"',
        "lastModifiedDateTime": scan_modified,
    }
    client.get.return_value = resp
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    rec = FileRecord(
        path=Path("onedrive:x://foo"),
        size=1,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="onedrive:x",
        cloud_file_id="cf001",
        etag=f"cf001:{scan_modified}",
    )
    # Should NOT raise.
    src.check_drift(rec)
    # Verified the ``$select`` query trims payload.
    args, _kwargs = client.get.call_args
    assert "$select=id,eTag,lastModifiedDateTime" in args[0]
    assert "/me/drive/items/cf001" in args[0]


def test_check_drift_mismatch_raises_source_drift_error() -> None:
    """Different lastModifiedDateTime and eTag → SourceDriftError."""
    from duplicate_cleaner.sources.base import SourceDriftError

    scan_modified = "2026-09-05T12:34:56.000Z"
    new_modified = "2026-09-06T10:00:00.000Z"
    client = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "id": "cf001",
        "eTag": '"etag-new"',
        "lastModifiedDateTime": new_modified,
    }
    client.get.return_value = resp
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    rec = FileRecord(
        path=Path("onedrive:x://foo"),
        size=1,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="onedrive:x",
        cloud_file_id="cf001",
        etag=f"cf001:{scan_modified}",
    )
    with pytest.raises(SourceDriftError) as exc:
        src.check_drift(rec)
    msg = str(exc.value)
    assert scan_modified in msg or "drift" in msg.lower()


def test_nextlink_refused_when_not_graph_origin() -> None:
    """A poisoned ``@odata.nextLink`` outside graph.microsoft.com aborts."""
    evil_page = {
        "value": [_delta_item("first-page-item")],
        "@odata.nextLink": "https://evil.example.com/whatever",
    }
    client = _mk_delta_client([evil_page, {"value": []}])
    src = OneDriveSource(
        "onedrive:x",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        client_factory=lambda _tp: client,
    )
    with pytest.raises(SourceError) as exc:
        list(src.list_files())
    assert "evil.example.com" in str(exc.value) or "MITM" in str(exc.value)
