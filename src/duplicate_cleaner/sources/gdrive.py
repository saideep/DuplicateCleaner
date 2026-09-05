"""GoogleDriveSource — read + metadata only in sub-phase 2; trash lands in sub-phase 3.

Wraps ``google-api-python-client`` behind the :class:`Source` protocol.  A
freshly-constructed source defaults to ``is_read_only_scan=True`` so a bug
in the scan pipeline cannot trash a cloud file — sub-phase 3 will construct
the source with the flag off inside the mover.

The Drive REST client is imported lazily so the module can be imported (and
``py_compile``'d, and unit-tested with mocks) without ``google-api-python-client``
installed.  The retry helper does the same for :mod:`tenacity`.
"""
from __future__ import annotations

import io
import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.sources.base import (
    SourceAuthError,
    SourceError,
    SourceMetadata,
    SourceNotFoundError,
    SourcePermissionError,
    SourceRateLimitError,
    TrashedLocation,
)

log = logging.getLogger(__name__)

_GOOGLE_NATIVE_MIME_PREFIX = "application/vnd.google-apps."

_LIST_FIELDS = (
    "nextPageToken,"
    "files(id,name,size,md5Checksum,modifiedTime,parents,owners,"
    "shared,trashed,mimeType)"
)

_PARENT_FIELDS = "id,name,parents"


def _build_drive_service(credentials: Any) -> Any:
    """Return a Drive v3 service object.  Isolated so tests can patch it."""
    from googleapiclient.discovery import build  # type: ignore[import-not-found,import-untyped]

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _is_retryable_http_error(exc: BaseException) -> bool:
    """Return True for 429 and 5xx googleapiclient ``HttpError`` responses."""
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


def _drive_call(callable_: Callable[[], Any]) -> Any:
    """Invoke ``callable_`` with tenacity backoff on 429 / 5xx.

    Imported lazily so unit tests can exercise the module without ``tenacity``
    installed.  A test that wants to bypass retry logic entirely patches
    :func:`_drive_call` directly.  On the final retry exhaustion, the original
    ``HttpError`` is re-raised — ``_raise_mapped`` (used by
    ``move_to_trash`` / ``restore_from_trash``) then translates it to a
    :class:`SourceRateLimitError` or other typed error so callers can react.
    """
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
    """Return the numeric HTTP status on a ``googleapiclient.HttpError``."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _raise_mapped(exc: BaseException) -> None:
    """Translate a Drive HttpError into the closest ``SourceError`` subtype.

    Called from within ``except`` blocks; returns normally on unrecognised
    errors so the original exception falls through to the caller's re-raise.
    """
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
        # Reached only after tenacity exhausted its retry budget — surface
        # a caller-actionable rate-limit error rather than a raw HttpError.
        raise SourceRateLimitError(str(exc)) from exc


class GoogleDriveSource:
    """Source over one Google account's My Drive.  Read-only in sub-phase 2."""

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
        self.is_read_only_scan = is_read_only_scan
        self._service_factory: Callable[[Any], Any] = (
            service_factory if service_factory is not None else _build_drive_service
        )
        self._service: Any | None = None
        # (folder_id -> (name, parents)) cached across list_files() for O(N)
        # parent-chain resolution instead of a repeated round-trip per file.
        self._folder_cache: dict[str, tuple[str, list[str]]] = {}

    @property
    def service(self) -> Any:
        """Return the Drive v3 service, constructing it on first access."""
        if self._service is None:
            self._service = self._service_factory(self._credentials)
        return self._service

    def list_files(self) -> Iterator[FileRecord]:
        """Paginated ``files.list`` yielding one :class:`FileRecord` per eligible item."""
        page_token: str | None = None
        while True:
            request = self.service.files().list(
                q="trashed = false",
                pageSize=1000,
                fields=_LIST_FIELDS,
                pageToken=page_token,
                spaces="drive",
            )
            resp = _drive_call(request.execute)
            for item in resp.get("files", []):
                rec = self._item_to_record(item)
                if rec is not None:
                    yield rec
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def _item_to_record(self, item: dict[str, Any]) -> FileRecord | None:
        """Filter and convert one Drive files.list item into a FileRecord."""
        mime = str(item.get("mimeType", ""))
        if mime.startswith(_GOOGLE_NATIVE_MIME_PREFIX):
            return None
        if item.get("trashed"):
            return None
        cloud_file_id = item.get("id")
        if not cloud_file_id:
            return None
        raw_size = item.get("size")
        try:
            size = int(raw_size) if raw_size is not None else 0
        except (TypeError, ValueError):
            return None

        md5 = item.get("md5Checksum")
        foreign_hash = f"md5:{md5}" if isinstance(md5, str) and md5 else None

        modified = item.get("modifiedTime")
        mtime = _parse_rfc3339(modified) if isinstance(modified, str) else 0.0

        owners = item.get("owners") or []
        owner_email: str | None = None
        # B2: default owner_is_me=False.  When Drive omits ``owners[0].me`` we
        # cannot prove the file is owned by us — safer to treat as shared so
        # the "shared cloud files informational-only" invariant is not
        # violated by a missing-field response.
        owner_is_me = False
        if isinstance(owners, list) and owners:
            first = owners[0] or {}
            if isinstance(first, dict):
                owner_email = first.get("emailAddress")
                owner_is_me = bool(first.get("me", False))

        is_shared = bool(item.get("shared")) or (not owner_is_me)

        etag_source = str(modified or "")
        etag = f"{cloud_file_id}:{etag_source}"

        drive_path = self._resolve_drive_path(item)
        virtual = f"{self.id}://{drive_path}"

        return FileRecord(
            path=Path(virtual),
            size=size,
            mtime=mtime,
            inode=0,
            dev=0,
            nlink=1,
            source_id=self.id,
            foreign_hash=foreign_hash,
            etag=etag,
            cloud_file_id=str(cloud_file_id),
            owner=owner_email,
            is_shared=is_shared,
        )

    def _resolve_drive_path(self, item: dict[str, Any]) -> str:
        """Walk ``parents`` up to a root/orphan, joining folder names with '/'.

        Bounded depth so a corrupted parent chain cannot loop forever; if the
        chain terminates before we hit a root the partial path is returned
        (that is enough for the human-readable ``FileRecord.path``).
        """
        name = str(item.get("name", ""))
        parents = item.get("parents") or []
        if not isinstance(parents, list) or not parents:
            return name
        segments: list[str] = [name]
        cur = str(parents[0])
        for _ in range(50):
            folder = self._folder_cache.get(cur)
            if folder is None:
                folder = self._fetch_folder(cur)
                self._folder_cache[cur] = folder
            fname, fparents = folder
            if fname:
                segments.append(fname)
            if not fparents:
                break
            cur = fparents[0]
        return "/".join(reversed(segments))

    def _fetch_folder(self, folder_id: str) -> tuple[str, list[str]]:
        """Return ``(name, parents)`` for a folder id via ``files.get``."""
        try:
            resp = _drive_call(
                self.service.files().get(
                    fileId=folder_id, fields=_PARENT_FIELDS
                ).execute
            )
        except Exception as exc:
            log.debug("Failed to fetch parent folder %s: %s", folder_id, exc)
            return ("", [])
        parents_raw = resp.get("parents") or []
        parents = [str(p) for p in parents_raw if isinstance(p, str)]
        return (str(resp.get("name", "")), parents)

    def read_bytes(
        self, record: FileRecord, chunk_size: int = 1 << 20
    ) -> Iterator[bytes]:
        """Stream bytes for ``record`` via ``files.get_media`` + MediaIoBaseDownload."""
        if not record.cloud_file_id:
            raise SourceError(
                f"{record.path}: cannot read bytes without cloud_file_id"
            )
        from googleapiclient.http import (
            MediaIoBaseDownload,  # type: ignore[import-not-found,import-untyped]
        )

        request = self.service.files().get_media(fileId=record.cloud_file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request, chunksize=chunk_size)
        done = False
        while not done:
            _, done = _drive_call(downloader.next_chunk)
            data = buf.getvalue()
            if data:
                yield data
                buf.seek(0)
                buf.truncate(0)

    def move_to_trash(self, record: FileRecord) -> TrashedLocation:
        """Set ``trashed=true`` on the Drive file — Drive keeps the file id.

        Returns a :class:`TrashedLocation` with ``cloud_file_id = cloud_trash_id``
        because Google Drive does not mint a separate trash id: the same file id
        is used to un-trash the file later.  Sub-phase 5 wires the mover to
        this method for real cloud discards; sub-phase 3 exercises it via unit
        tests only.
        """
        if self.is_read_only_scan:
            raise PermissionError(
                f"GoogleDriveSource(id={self.id!r}) is read-only during scan; "
                "construct with is_read_only_scan=False to enable trashing."
            )
        if not record.cloud_file_id:
            raise SourceError(
                f"{record.path}: cannot trash without cloud_file_id"
            )
        try:
            _drive_call(
                self.service.files().update(
                    fileId=record.cloud_file_id,
                    body={"trashed": True},
                    fields="id,trashed",
                ).execute
            )
        except Exception as exc:
            _raise_mapped(exc)
            raise
        return TrashedLocation(
            source_id=self.id,
            original_path=str(record.path),
            cloud_file_id=record.cloud_file_id,
            cloud_trash_id=record.cloud_file_id,
        )

    def restore_from_trash(self, loc: TrashedLocation) -> None:
        """Un-trash the Drive file whose id is recorded on ``loc``.

        Rejects a mismatched ``source_id`` so a poisoned manifest cannot
        dispatch a Google entry to a different source implementation.
        Raises :class:`SourceNotFoundError` when the trashed object has
        been permanently deleted (trash emptied).
        """
        if loc.source_id != self.id:
            raise SourceError(
                f"TrashedLocation source_id {loc.source_id!r} does not match "
                f"this GoogleDriveSource id {self.id!r}."
            )
        if not loc.cloud_file_id:
            raise SourceError(
                f"TrashedLocation has no cloud_file_id: cannot restore ({loc})"
            )
        try:
            _drive_call(
                self.service.files().update(
                    fileId=loc.cloud_file_id,
                    body={"trashed": False},
                    fields="id,trashed",
                ).execute
            )
        except Exception as exc:
            _raise_mapped(exc)
            raise

    def get_metadata(self, record: FileRecord) -> SourceMetadata:
        """Return metadata already populated at scan time — no re-fetch."""
        return SourceMetadata(
            etag=record.etag,
            cloud_file_id=record.cloud_file_id,
            owner=record.owner,
            is_shared=record.is_shared,
        )


def _parse_rfc3339(value: str) -> float:
    """Parse a Drive ``modifiedTime`` RFC 3339 string to a POSIX float."""
    try:
        cleaned = value.replace("Z", "+00:00")
        return float(datetime.fromisoformat(cleaned).timestamp())
    except (TypeError, ValueError):
        return 0.0
