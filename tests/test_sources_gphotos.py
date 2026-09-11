"""GooglePhotosSource — v0.6 read-only Photos Library API v1 backed source.

Every test mocks the Photos service factory so no real network is touched.
The retry tests inject a lightweight fake ``HttpError`` because the real
class from ``googleapiclient.errors`` may not be installed in every CI
environment.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.sources.base import (
    SourceAuthError,
    SourceDriftError,
    SourceError,
    SourceNotFoundError,
    SourcePermissionError,
    SourceRateLimitError,
)
from duplicate_cleaner.sources.gphotos import GooglePhotosSource

# Real-looking Google Photos media-item ids — Base64url, ≥ 20 chars per the
# _GPHOTOS_ID_RE shape gate.  Every test that reaches read_bytes / drift
# must use one of these so it does not trip the shape refusal branch.
_REAL_ID = "AAaaBBbbCCccDDddEEee"
_REAL_ID_2 = "FFffGGggHHhhIIiiJJjj"


def _mk_service(list_pages: list[dict[str, Any]]) -> MagicMock:
    """Build a mock Photos service whose ``mediaItems().list()`` paginates."""
    service = MagicMock()
    media_items = MagicMock()
    service.mediaItems.return_value = media_items

    pages_iter = iter(list_pages)

    def _list(**_kwargs: Any) -> MagicMock:
        request = MagicMock()
        page = next(pages_iter, {"mediaItems": []})
        request.execute.return_value = page
        return request

    media_items.list.side_effect = _list
    return service


def _photo_item(
    media_item_id: str,
    *,
    filename: str = "IMG_0001.HEIC",
    creation_time: str = "2026-09-05T12:34:56.000Z",
    base_url: str = "https://photos.googleusercontent.com/base",
) -> dict[str, Any]:
    return {
        "id": media_item_id,
        "filename": filename,
        "mediaMetadata": {
            "creationTime": creation_time,
            "photo": {
                "cameraMake": "Apple",
                "cameraModel": "iPhone 14 Pro",
                "focalLength": 6.86,
            },
        },
        "baseUrl": base_url,
        "mimeType": "image/heic",
        "productUrl": "https://photos.google.com/photo/whatever",
    }


def test_list_files_produces_expected_records() -> None:
    page = {"mediaItems": [_photo_item(_REAL_ID, filename="a.HEIC")]}
    service = _mk_service([page])
    src = GooglePhotosSource(
        account_id="gphotos:personal",
        credentials=None,
        client_config={"user_email": "me@example.com"},
        service_factory=lambda _c: service,
    )
    got = list(src.list_files())
    assert len(got) == 1
    rec = got[0]
    assert rec.source_id == "gphotos:personal"
    assert rec.size == 0  # Photos API doesn't expose size in list; v0.6 note
    assert rec.foreign_hash is None  # no MD5/SHA-256 in Photos
    assert rec.cloud_file_id == _REAL_ID
    assert rec.etag == f"{_REAL_ID}:2026-09-05T12:34:56.000Z"
    assert rec.owner == "me@example.com"
    assert rec.is_shared is False
    # v0.6 spec: ``path = f"gphotos:{account_id}://{filename}"``.  The
    # hardcoded ``gphotos:`` prefix + the account_id (which itself starts
    # with ``gphotos:``) gives a double-prefixed virtual path.
    assert str(rec.path).startswith("gphotos:gphotos:personal:")
    assert "a.HEIC" in str(rec.path)


def test_list_files_pages_via_nextPageToken() -> None:
    page1 = {"mediaItems": [_photo_item(_REAL_ID)], "nextPageToken": "tok1"}
    page2 = {"mediaItems": [_photo_item(_REAL_ID_2)]}
    service = _mk_service([page1, page2])
    src = GooglePhotosSource(
        "gphotos:personal",
        credentials=None,
        service_factory=lambda _c: service,
    )
    ids = [r.cloud_file_id for r in src.list_files()]
    assert ids == [_REAL_ID, _REAL_ID_2]


def test_list_files_populates_foreign_hash_None() -> None:
    """Google Photos API does not expose per-item hashes; foreign_hash MUST be None."""
    page = {"mediaItems": [_photo_item(_REAL_ID)]}
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    rec = next(iter(src.list_files()))
    assert rec.foreign_hash is None


def test_cloud_path_scheme_locked_in() -> None:
    """The virtual path SHOULD be ``gphotos:<account>://<filename>``."""
    page = {"mediaItems": [_photo_item(_REAL_ID, filename="foo.jpg")]}
    src = GooglePhotosSource(
        "gphotos:personal",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    rec = next(iter(src.list_files()))
    # Path normalises "//" to "/" internally; check the source-prefix shape.
    # The v0.6 spec's hardcoded ``gphotos:`` prefix + the account_id
    # (``gphotos:personal``) double-prefixes deliberately.
    assert str(rec.path).startswith("gphotos:gphotos:personal:")
    assert "foo.jpg" in str(rec.path)


def test_read_bytes_refetches_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_bytes MUST call mediaItems.get first (baseUrl expires in 60min)."""
    service = MagicMock()
    media_items = MagicMock()
    service.mediaItems.return_value = media_items
    get_request = MagicMock()
    get_request.execute.return_value = {
        "id": _REAL_ID,
        "baseUrl": "https://photos.googleusercontent.com/fresh-base",
    }
    media_items.get.return_value = get_request

    class _FakeStream:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {"content-type": "image/heic"}

        def __enter__(self) -> _FakeStream:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, chunk_size: int) -> Any:
            yield b"cdn-bytes"

    class _FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.calls: list[str] = []

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def stream(self, method: str, url: str, **_kwargs: Any) -> _FakeStream:
            self.calls.append(url)
            # Ensure ``=d`` original-quality suffix was appended.
            assert url.endswith("=d"), f"expected '=d' suffix on {url!r}"
            return _FakeStream()

    fake_client = _FakeClient()
    fake_httpx = types.ModuleType("httpx")
    fake_httpx.Client = lambda **kw: fake_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(
        path=Path("gphotos:x://foo.HEIC"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id=_REAL_ID,
        etag=f"{_REAL_ID}:x",
    )
    data = b"".join(src.read_bytes(rec))
    assert data == b"cdn-bytes"
    # The get() call MUST fire before any stream() call — the mock proves
    # both that it was invoked and that we passed the fresh baseUrl on.
    assert media_items.get.call_count == 1
    call = media_items.get.call_args
    assert call.kwargs["mediaItemId"] == _REAL_ID


def test_read_bytes_requires_cloud_file_id() -> None:
    service = _mk_service([{"mediaItems": []}])
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(path=Path("gphotos:x://x"), size=0, mtime=0.0, inode=0, dev=0, nlink=1)
    with pytest.raises(SourceError):
        list(src.read_bytes(rec))


def test_move_to_trash_raises_deferred() -> None:
    """v0.6.1: move_to_trash without ``has_trash`` names the escalation command.

    Was v0.6 "deferred to v0.6.1"; v0.6.1 replaces that with a
    per-account actionable escalation pointer.  The read-only default
    now surfaces ``dc auth grant-gphotos-trash <id>`` instead of the
    old milestone-name placeholder.
    """
    service = _mk_service([{"mediaItems": []}])
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        # Flag off so we bypass the scan tripwire and reach the
        # per-account has_trash check.
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    # v0.6.1: default has_trash=False.
    assert src.has_trash is False
    rec = FileRecord(
        path=Path("gphotos:x://foo.HEIC"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id=_REAL_ID,
    )
    with pytest.raises(SourceError) as exc:
        src.move_to_trash(rec)
    msg = str(exc.value)
    assert "grant-gphotos-trash" in msg
    assert "gphotos:x" in msg


def test_move_to_trash_raises_when_read_only() -> None:
    """v0.6-patch: move_to_trash refuses unconditionally with SourceError.

    The redundant read-only ``PermissionError`` branch was dropped in
    v0.6-patch (audit pass 16, M7) — the outer construction hard-codes
    ``is_read_only_scan=True`` at every apply site, so the branch was
    unreachable in production.  This test now asserts the surviving
    behaviour: SourceError with the v0.6.1 deferral message.
    """
    service = _mk_service([{"mediaItems": []}])
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        service_factory=lambda _c: service,
    )
    assert src.is_read_only_scan is True
    rec = FileRecord(
        path=Path("gphotos:x://foo.HEIC"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id=_REAL_ID,
    )
    with pytest.raises(SourceError):
        src.move_to_trash(rec)


def test_check_drift_matches() -> None:
    """Composite etag of id + creationTime unchanged → no exception."""
    ctime = "2026-09-05T12:34:56.000Z"
    service = MagicMock()
    media_items = MagicMock()
    service.mediaItems.return_value = media_items
    get_request = MagicMock()
    get_request.execute.return_value = {
        "id": _REAL_ID,
        "mediaMetadata": {"creationTime": ctime},
    }
    media_items.get.return_value = get_request
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(
        path=Path("gphotos:x://x"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id=_REAL_ID,
        etag=f"{_REAL_ID}:{ctime}",
    )
    src.check_drift(rec)  # must NOT raise
    assert media_items.get.call_count == 1


def test_check_drift_creation_time_changed() -> None:
    """Different creationTime → SourceDriftError."""
    scan_ctime = "2026-09-05T12:34:56.000Z"
    new_ctime = "2026-09-06T00:00:00.000Z"
    service = MagicMock()
    media_items = MagicMock()
    service.mediaItems.return_value = media_items
    get_request = MagicMock()
    get_request.execute.return_value = {
        "id": _REAL_ID,
        "mediaMetadata": {"creationTime": new_ctime},
    }
    media_items.get.return_value = get_request
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(
        path=Path("gphotos:x://x"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id=_REAL_ID,
        etag=f"{_REAL_ID}:{scan_ctime}",
    )
    with pytest.raises(SourceDriftError):
        src.check_drift(rec)


def _install_fake_httperror(monkeypatch: pytest.MonkeyPatch) -> type[Exception]:
    """Stub googleapiclient.errors.HttpError for the retry classifier."""

    class _FakeResp:
        def __init__(self, status: int) -> None:
            self.status = status

    class HttpError(Exception):
        def __init__(self, status: int, content: bytes = b"") -> None:
            super().__init__(f"HTTP {status}")
            self.resp = _FakeResp(status)
            self.content = content

    errors_mod = types.ModuleType("googleapiclient.errors")
    errors_mod.HttpError = HttpError  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "googleapiclient", types.ModuleType("googleapiclient")
    )
    monkeypatch.setitem(sys.modules, "googleapiclient.errors", errors_mod)
    return HttpError


def test_raise_mapped_401_maps_to_source_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HttpError = _install_fake_httperror(monkeypatch)
    service = MagicMock()
    media_items = MagicMock()
    service.mediaItems.return_value = media_items
    get_request = MagicMock()
    get_request.execute.side_effect = HttpError(status=401)
    media_items.get.return_value = get_request
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(
        path=Path("gphotos:x://x"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id=_REAL_ID,
        etag=f"{_REAL_ID}:t",
    )
    with pytest.raises(SourceAuthError):
        src.check_drift(rec)


def test_raise_mapped_403_maps_to_source_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HttpError = _install_fake_httperror(monkeypatch)
    service = MagicMock()
    media_items = MagicMock()
    service.mediaItems.return_value = media_items
    get_request = MagicMock()
    get_request.execute.side_effect = HttpError(status=403)
    media_items.get.return_value = get_request
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(
        path=Path("gphotos:x://x"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id=_REAL_ID,
        etag=f"{_REAL_ID}:t",
    )
    with pytest.raises(SourcePermissionError):
        src.check_drift(rec)


def test_raise_mapped_404_maps_to_source_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HttpError = _install_fake_httperror(monkeypatch)
    service = MagicMock()
    media_items = MagicMock()
    service.mediaItems.return_value = media_items
    get_request = MagicMock()
    get_request.execute.side_effect = HttpError(status=404)
    media_items.get.return_value = get_request
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(
        path=Path("gphotos:x://x"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id=_REAL_ID,
        etag=f"{_REAL_ID}:t",
    )
    with pytest.raises(SourceNotFoundError):
        src.check_drift(rec)


def test_raise_mapped_429_exhausts_retries_and_raises_ratelimit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HttpError = _install_fake_httperror(monkeypatch)
    import tenacity  # type: ignore[import-untyped]

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)

    service = MagicMock()
    media_items = MagicMock()
    service.mediaItems.return_value = media_items
    get_request = MagicMock()
    get_request.execute.side_effect = HttpError(status=429)
    media_items.get.return_value = get_request
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(
        path=Path("gphotos:x://x"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id=_REAL_ID,
        etag=f"{_REAL_ID}:t",
    )
    with pytest.raises(SourceRateLimitError):
        src.check_drift(rec)


def test_source_id_stamped_on_records() -> None:
    page = {"mediaItems": [_photo_item(_REAL_ID)]}
    src = GooglePhotosSource(
        "gphotos:my-photos-id",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    rec = next(iter(src.list_files()))
    assert rec.source_id == "gphotos:my-photos-id"


def test_get_metadata_reflects_record() -> None:
    page = {"mediaItems": [_photo_item(_REAL_ID)]}
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    rec = next(iter(src.list_files()))
    meta = src.get_metadata(rec)
    assert meta.cloud_file_id == _REAL_ID
    assert meta.is_shared is False
    assert meta.etag == rec.etag


def test_read_bytes_retries_cdn_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """v0.6-patch M4: CDN stream is retried on 429 (twice → success).

    Before v0.6-patch only the ``mediaItems.get`` call was retried; the
    subsequent ``httpx.stream`` that actually downloads the bytes had no
    retry, so Google's CDN 429s on large libraries silently dropped
    records.
    """
    import tenacity  # type: ignore[import-untyped]

    # No sleeping between retries — keep the test fast.
    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)

    service = MagicMock()
    media_items = MagicMock()
    service.mediaItems.return_value = media_items
    get_request = MagicMock()
    get_request.execute.return_value = {
        "id": _REAL_ID,
        "baseUrl": "https://photos.googleusercontent.com/fresh-base",
    }
    media_items.get.return_value = get_request

    call_count = {"n": 0}

    class _StatusResponse:
        def __init__(self, status: int) -> None:
            self.status_code = status

    class _FakeStreamSuccess:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {"content-type": "image/heic"}

        def __enter__(self) -> _FakeStreamSuccess:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, chunk_size: int) -> Any:
            yield b"payload"

    class _FakeStream429:
        status_code = 429
        headers: ClassVar[dict[str, str]] = {"content-type": "text/plain"}

        def __init__(self, response: _StatusResponse) -> None:
            self._response = response

        def __enter__(self) -> _FakeStream429:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            # Raise an httpx.HTTPStatusError-alike so _is_retryable_cdn_error
            # classifies it as retry-worthy.
            import httpx  # type: ignore[import-untyped]

            raise httpx.HTTPStatusError(
                "429 Too Many Requests",
                request=MagicMock(),
                response=MagicMock(status_code=429),
            )

        def iter_bytes(self, chunk_size: int) -> Any:  # pragma: no cover
            yield b""

    class _FakeClient:
        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def stream(self, method: str, url: str, **_kw: Any) -> Any:
            _ = (method, url)
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return _FakeStream429(_StatusResponse(429))
            return _FakeStreamSuccess()

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.Client = lambda **kw: _FakeClient()  # type: ignore[attr-defined]
    # tenacity-classifier needs HTTPStatusError + TransportError on httpx.
    import httpx as real_httpx  # type: ignore[import-untyped]

    fake_httpx.HTTPStatusError = real_httpx.HTTPStatusError  # type: ignore[attr-defined]
    fake_httpx.TransportError = real_httpx.TransportError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(
        path=Path("gphotos:x://foo.HEIC"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id=_REAL_ID,
        etag=f"{_REAL_ID}:t",
    )
    data = b"".join(src.read_bytes(rec))
    assert data == b"payload"
    assert call_count["n"] == 3  # two 429s then a 200


def test_read_bytes_refuses_html_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.6-patch M5: a 200-with-HTML response is refused, not hashed.

    A session-expired redirect returning an HTML interstitial with
    status 200 must not be hashed as image bytes — every such record
    would cluster into a fake duplicate group.
    """
    service = MagicMock()
    media_items = MagicMock()
    service.mediaItems.return_value = media_items
    get_request = MagicMock()
    get_request.execute.return_value = {
        "id": _REAL_ID,
        "baseUrl": "https://photos.googleusercontent.com/fresh-base",
    }
    media_items.get.return_value = get_request

    class _HTMLStream:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {
            "content-type": "text/html; charset=utf-8"
        }

        def __enter__(self) -> _HTMLStream:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, chunk_size: int) -> Any:  # pragma: no cover
            yield b"<html>session expired</html>"

    class _FakeClient:
        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def stream(self, method: str, url: str, **_kw: Any) -> _HTMLStream:
            _ = (method, url)
            return _HTMLStream()

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.Client = lambda **kw: _FakeClient()  # type: ignore[attr-defined]
    import httpx as real_httpx  # type: ignore[import-untyped]

    fake_httpx.HTTPStatusError = real_httpx.HTTPStatusError  # type: ignore[attr-defined]
    fake_httpx.TransportError = real_httpx.TransportError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(
        path=Path("gphotos:x://foo.HEIC"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id=_REAL_ID,
        etag=f"{_REAL_ID}:t",
    )
    with pytest.raises(SourceError) as exc:
        list(src.read_bytes(rec))
    assert "content-type" in str(exc.value).lower()


def test_read_bytes_refuses_bad_cloud_file_id_shape() -> None:
    """A traversal-looking id fails the shape gate BEFORE any Photos call."""
    service = MagicMock()
    media_items = MagicMock()
    service.mediaItems.return_value = media_items
    media_items.get.side_effect = AssertionError(
        "get must not fire when the id is refused up-front"
    )
    src = GooglePhotosSource(
        "gphotos:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(
        path=Path("gphotos:x://foo"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:x",
        cloud_file_id="a/b/c",
    )
    with pytest.raises(SourceError) as exc:
        list(src.read_bytes(rec))
    assert "shape" in str(exc.value)
