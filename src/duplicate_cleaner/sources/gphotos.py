"""GooglePhotosSource — read-only Photos Library API v1 backed source.

v0.6: Google Photos scans over the ``mediaItems`` endpoint of the Photos
Library API v1.  Read-only by design in this milestone — the trash flow
requires a scope escalation from ``photoslibrary.readonly`` to the full
``photoslibrary`` scope, which needs user re-consent.  That flow lands in
v0.6.1; until then :meth:`GooglePhotosSource.move_to_trash` raises a typed
``SourceError`` pointing the user at the escalation.

Google Photos does NOT expose per-item MD5/SHA-256 in the list response,
so the reconcile pipeline is the only path to a canonical hash for these
items.  :meth:`read_bytes` re-fetches the media item to obtain a fresh
``baseUrl`` (they expire after 60 minutes) and streams the ``=d``-suffixed
original-quality bytes via an unauthenticated ``httpx`` client — Google
issues short-lived signed URLs, and forwarding the bearer token to that
storage host would leak credentials.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.sources.base import (
    SourceAuthError,
    SourceDriftError,
    SourceError,
    SourceMetadata,
    SourceNotFoundError,
    SourcePermissionError,
    SourceRateLimitError,
    TrashedLocation,
    UploadResult,
)

log = logging.getLogger(__name__)

# Google Photos media item ids are Base64url-ish; the Photos API's actual
# format is documented as opaque but empirically Base64url with ``_`` /
# ``-`` and a length of ~80 chars.  Refuse suspicious ids BEFORE URL
# interpolation — same shape gate as ``sources.gdrive._GDRIVE_ID_RE``.
_GPHOTOS_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def _validate_media_item_id(media_item_id: str) -> None:
    """Reject a suspicious Google Photos media item id BEFORE it hits a URL."""
    if not _GPHOTOS_ID_RE.match(media_item_id):
        raise SourceError(
            f"Google Photos media_item_id {media_item_id!r} does not match "
            f"the expected shape ({_GPHOTOS_ID_RE.pattern}); refusing to "
            "interpolate a suspicious id into a Photos API URL."
        )


def _build_photos_service(credentials: Any) -> Any:
    """Return a Photos Library v1 service object.  Isolated so tests can patch."""
    from googleapiclient.discovery import build  # type: ignore[import-not-found,import-untyped]

    return build(
        "photoslibrary",
        "v1",
        credentials=credentials,
        cache_discovery=False,
        static_discovery=False,
    )


def _is_retryable_http_error(exc: BaseException) -> bool:
    """Return True for 429 / 5xx googleapiclient HttpError instances."""
    try:
        from googleapiclient.errors import (
            HttpError,  # type: ignore[import-not-found,import-untyped]
        )
    except ImportError:
        return False
    if not isinstance(exc, HttpError):
        return False
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is None:
        return False
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return code == 429 or 500 <= code < 600


def _is_retryable_cdn_error(exc: BaseException) -> bool:
    """Return True for transient httpx failures on the Photos CDN stream.

    Symmetric with :func:`_is_retryable_http_error` for the Photos API path,
    but covers the storage host used by ``read_bytes``.  Google's CDN
    throttles large libraries with 429s and returns 5xx during transient
    overload; ``httpx.TransportError`` covers DNS blips, TCP resets, and
    read timeouts on the same host.
    """
    try:
        import httpx  # type: ignore[import-not-found,import-untyped]
    except ImportError:
        return False
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is None:
            return False
        try:
            code = int(status)
        except (TypeError, ValueError):
            return False
        return code == 429 or 500 <= code < 600
    return False


def _gphotos_call(callable_: Callable[[], Any]) -> Any:
    """Invoke ``callable_`` with tenacity backoff on 429 / 5xx."""
    from tenacity import (  # type: ignore[import-not-found,import-untyped]
        Retrying,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential,
    )

    for attempt in Retrying(
        retry=retry_if_exception(_is_retryable_http_error),
        wait=wait_exponential(multiplier=1, min=1, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    ):
        with attempt:
            return callable_()
    raise RuntimeError("tenacity Retrying loop completed without a result")


def _http_status(exc: BaseException) -> int | None:
    """Return the numeric HTTP status on a googleapiclient ``HttpError``."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _raise_mapped(exc: BaseException) -> None:
    """Translate a Photos HttpError into the closest ``SourceError`` subtype."""
    try:
        from googleapiclient.errors import (
            HttpError,  # type: ignore[import-not-found,import-untyped]
        )
    except ImportError:
        return
    if not isinstance(exc, HttpError):
        return
    status = _http_status(exc)
    if status is None:
        return
    if status == 404:
        raise SourceNotFoundError(str(exc)) from exc
    if status == 403:
        raise SourcePermissionError(str(exc)) from exc
    if status == 401:
        raise SourceAuthError(str(exc)) from exc
    if status == 429 or 500 <= status < 600:
        raise SourceRateLimitError(str(exc)) from exc


def _parse_rfc3339(value: str) -> float:
    """Parse a Photos ``creationTime`` RFC 3339 string to POSIX float."""
    try:
        cleaned = value.replace("Z", "+00:00")
        return float(datetime.fromisoformat(cleaned).timestamp())
    except (TypeError, ValueError):
        return 0.0


class GooglePhotosSource:
    """Source over one Google account's Photos library.  Read-only in v0.6."""

    id: str
    is_read_only_scan: bool

    def __init__(
        self,
        account_id: str,
        credentials: Any,
        client_config: dict[str, Any] | None = None,
        *,
        is_read_only_scan: bool = True,
        service_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.id = account_id
        self._credentials = credentials
        self._client_config: dict[str, Any] = dict(client_config or {})
        # v0.6: Google Photos is permanently read-only until v0.6.1 wires the
        # scope escalation flow.  The attribute is still honoured for
        # symmetry with the other sources — a caller that flips it off gets
        # a SourceError from ``move_to_trash`` describing the deferral.
        self.is_read_only_scan = is_read_only_scan
        self._service_factory: Callable[[Any], Any] = (
            service_factory if service_factory is not None else _build_photos_service
        )
        self._service: Any | None = None
        # ``owners[0].emailAddress`` from the credentials profile, cached so
        # every FileRecord stamps the same owner without re-fetching /me on
        # each item.
        self._owner_email: str | None = None

    @property
    def service(self) -> Any:
        """Return the Photos Library v1 service, constructing it on first access."""
        if self._service is None:
            self._service = self._service_factory(self._credentials)
        return self._service

    def _resolve_owner_email(self) -> str | None:
        """Return the signed-in user's email; ``None`` if unavailable."""
        if self._owner_email is not None:
            return self._owner_email or None
        # ``google.oauth2.credentials.Credentials`` exposes ``.id_token`` on
        # some flows but not all; the OAuth flow stamps ``user_email`` into
        # the token blob we accept via ``client_config`` so we do not need
        # a live /me round-trip.  Empty-string cache marker disables the
        # lookup on subsequent calls.
        email = str(self._client_config.get("user_email") or "")
        self._owner_email = email
        return email or None

    def list_files(self) -> Iterator[FileRecord]:
        """Paginated ``mediaItems.list`` yielding one FileRecord per item."""
        owner = self._resolve_owner_email()
        page_token: str | None = None
        while True:
            request = self.service.mediaItems().list(
                pageSize=100,
                pageToken=page_token,
            )
            resp = _gphotos_call(request.execute)
            for item in resp.get("mediaItems", []) or []:
                rec = self._item_to_record(item, owner=owner)
                if rec is not None:
                    yield rec
            page_token = resp.get("nextPageToken") if isinstance(resp, dict) else None
            if not page_token:
                break

    def _item_to_record(
        self, item: dict[str, Any], *, owner: str | None
    ) -> FileRecord | None:
        """Convert one Photos ``mediaItem`` payload into a FileRecord."""
        media_item_id = item.get("id")
        if not isinstance(media_item_id, str) or not media_item_id:
            return None
        filename = str(item.get("filename") or "")
        if not filename:
            # Photos API always returns a filename in practice; treat missing
            # as an unusable item rather than fabricating one.
            return None
        media_metadata = item.get("mediaMetadata") or {}
        creation_time_raw = ""
        if isinstance(media_metadata, dict):
            creation_time_raw = str(media_metadata.get("creationTime") or "")
        mtime = _parse_rfc3339(creation_time_raw) if creation_time_raw else 0.0

        etag = f"{media_item_id}:{creation_time_raw}"
        virtual = f"gphotos:{self.id}://{filename}"

        return FileRecord(
            # Photos API does not surface size in list; leave 0 and stamp the
            # v0.6 report note.  A future revision may HEAD the baseUrl but
            # that costs a round-trip per item.
            path=Path(virtual),
            size=0,
            mtime=mtime,
            inode=0,
            dev=0,
            nlink=1,
            source_id=self.id,
            # No provider-side hash — reconciliation MUST download bytes and
            # BLAKE3 them.  The cache key includes ``etag`` so an unchanged
            # media item's hash survives across scans.
            foreign_hash=None,
            etag=etag,
            cloud_file_id=media_item_id,
            owner=owner,
            # ``photoslibrary.readonly`` only enumerates the app's own items,
            # so every listed item is "owned by me".  A future revision using
            # the full scope may surface shared-with-me items — until then
            # every list_files() record is is_shared=False.
            is_shared=False,
        )

    def read_bytes(
        self, record: FileRecord, chunk_size: int = 1 << 20
    ) -> Iterator[bytes]:
        """Re-fetch ``baseUrl`` and stream ``=d`` original-quality bytes.

        Photos ``baseUrl`` values expire in ~60 minutes, so a scan-time URL
        is unusable at reconcile time — we always re-fetch immediately
        before streaming.  Appending ``=d`` yields the original-quality
        download (per Photos API docs); omitting the suffix returns a
        display-only rendition unsuitable for hashing.
        """
        if not record.cloud_file_id:
            raise SourceError(
                f"{record.path}: cannot read bytes without cloud_file_id"
            )
        _validate_media_item_id(record.cloud_file_id)
        try:
            fresh = _gphotos_call(
                self.service.mediaItems().get(
                    mediaItemId=record.cloud_file_id,
                ).execute
            )
        except Exception as exc:
            _raise_mapped(exc)
            raise
        base_url = ""
        if isinstance(fresh, dict):
            base_url = str(fresh.get("baseUrl") or "")
        if not base_url:
            raise SourceError(
                f"{record.path}: Photos API returned no baseUrl for id "
                f"{record.cloud_file_id!r}."
            )
        # ``=d`` forces original-quality download bytes.  Photos issues a
        # signed storage URL — do NOT forward the OAuth bearer token to
        # that host.
        original_url = base_url + "=d"
        yield from self._stream_cdn_bytes(record, original_url, chunk_size)

    def _stream_cdn_bytes(
        self, record: FileRecord, original_url: str, chunk_size: int
    ) -> Iterator[bytes]:
        """Retry-wrapped CDN stream with content-type and empty-body guards.

        v0.6-patch: wraps the outgoing ``httpx.stream`` in tenacity backoff
        over 429 / 5xx / transport errors (audit finding M4) and refuses
        200-with-HTML or zero-byte responses (M5) so a session-expired
        redirect page can't be hashed as image bytes.
        """
        import httpx  # type: ignore[import-not-found,import-untyped]
        from tenacity import (  # type: ignore[import-not-found,import-untyped]
            Retrying,
            retry_if_exception,
            stop_after_attempt,
            wait_exponential,
        )

        def _fetch_chunks() -> list[bytes]:
            """Perform one CDN stream attempt; retried by the outer loop.

            The chunk list is materialised eagerly so a 429 raised mid-stream
            still trips the retry classifier — a yielding generator would
            leak the httpx context out from under tenacity.
            """
            with (
                httpx.Client(follow_redirects=True, timeout=60.0) as cli,
                cli.stream("GET", original_url) as resp,
            ):
                status = getattr(resp, "status_code", 0)
                if status and status >= 400:
                    # Raise the httpx status error so tenacity can classify
                    # 429 / 5xx as retry-worthy.
                    resp.raise_for_status()
                content_type = str(resp.headers.get("content-type", "") or "")
                top = content_type.split("/", 1)[0].lower() if content_type else ""
                if top not in {"image", "video"}:
                    raise SourceError(
                        f"{record.path}: Photos CDN returned unexpected "
                        f"content-type {content_type!r}; refusing to hash "
                        "a non-media response (session-expired redirect?)."
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes(chunk_size=chunk_size):
                    if chunk:
                        total += len(chunk)
                        chunks.append(chunk)
                if total == 0:
                    raise SourceError(
                        f"{record.path}: Photos CDN returned an empty body "
                        f"(status {status}); refusing to hash zero bytes."
                    )
                return chunks

        chunks: list[bytes] = []
        for attempt in Retrying(
            retry=retry_if_exception(_is_retryable_cdn_error),
            wait=wait_exponential(multiplier=1, min=1, max=60),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                chunks = _fetch_chunks()
        yield from chunks

    def move_to_trash(self, record: FileRecord) -> TrashedLocation:
        """Refuse the call — Google Photos trash is deferred to v0.6.1.

        v0.6-patch: the redundant ``is_read_only_scan`` tripwire was dropped.
        The outer construction path pins the flag to ``True`` at every apply
        site (see ``cli._build_sources_for_apply``), so the branch could
        never fire in production.  Unconditional ``SourceError`` mirrors the
        iCloud Photos shape; v0.6.1 will reintroduce a flag-gated write path
        when scope escalation actually flips.
        """
        _ = record
        raise SourceError(
            "Google Photos trash deferred to v0.6.1; scope escalation "
            "required.  Use the Google Photos app or web UI at "
            "https://photos.google.com to move photos to the trash for now."
        )

    def restore_from_trash(self, loc: TrashedLocation) -> None:
        """Refuse — v0.6.1 will wire restore alongside the trash escalation."""
        _ = loc
        raise SourceError(
            "Google Photos restore deferred to v0.6.1; scope escalation "
            "required.  Restore manually from the Google Photos trash UI."
        )

    def get_metadata(self, record: FileRecord) -> SourceMetadata:
        """Return metadata already populated at scan time — no re-fetch."""
        return SourceMetadata(
            etag=record.etag,
            cloud_file_id=record.cloud_file_id,
            owner=record.owner,
            is_shared=record.is_shared,
        )

    def check_drift(self, record: FileRecord) -> None:
        """Re-fetch the media item and verify the composite creationTime etag.

        Google Photos' ``mediaMetadata.creationTime`` is stable across
        renames / album edits but changes when the underlying file is
        re-uploaded.  The composite ``f"{id}:{creationTime}"`` therefore
        catches an "I re-uploaded this photo" edit even when the id stays
        the same.
        """
        if not record.cloud_file_id:
            raise SourceError(
                f"{record.path}: cannot check drift without cloud_file_id"
            )
        if record.source_id != self.id:
            raise SourceError(
                f"FileRecord.source_id {record.source_id!r} does not match "
                f"this GooglePhotosSource id {self.id!r}."
            )
        _validate_media_item_id(record.cloud_file_id)
        try:
            resp = _gphotos_call(
                self.service.mediaItems().get(
                    mediaItemId=record.cloud_file_id,
                ).execute
            )
        except Exception as exc:
            _raise_mapped(exc)
            raise
        creation_time = ""
        if isinstance(resp, dict):
            metadata = resp.get("mediaMetadata")
            if isinstance(metadata, dict):
                creation_time = str(metadata.get("creationTime") or "")
        current_etag = f"{record.cloud_file_id}:{creation_time}"
        if current_etag != record.etag:
            raise SourceDriftError(
                f"Cloud etag drift for {record.path}: scan={record.etag!r} "
                f"now={current_etag!r} — the Photos item's creationTime "
                "changed since scan.  Rescan and retry."
            )

    def upload(
        self,
        dest_path: str,
        byte_stream: Iterator[bytes],
        expected_size: int,
    ) -> UploadResult:
        """Refuse — Google Photos is not a supported migrate destination.

        v0.6 does not enable Google Photos as a write destination.  Even a
        future revision would need the full ``photoslibrary`` scope; until
        then a plan that names ``gphotos:*`` as ``--to`` surfaces here.
        """
        _ = (dest_path, byte_stream, expected_size)
        raise NotImplementedError(
            "Google Photos upload is not supported in v0.6 (read-only "
            "source).  Deferred alongside the v0.6.1 scope escalation."
        )


