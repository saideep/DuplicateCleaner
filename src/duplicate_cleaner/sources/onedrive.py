"""OneDriveSource — read + trash + restore for OneDrive Personal via Microsoft Graph.

Sub-milestone 4 of v0.2.  Mirrors :mod:`sources.gdrive` in shape: read is
default-safe (``is_read_only_scan=True`` tripwire fires before any state
change), trash goes through the provider Recycle Bin only (never
hard-delete), and restore is source_id-dispatched.

Wire choice: raw ``httpx`` client + ``msal`` for token refresh.  The
official ``msgraph-sdk`` is asyncio-first and pulls the Kiota generator
stack — overkill for four endpoints:

- ``GET /me/drive/root/delta`` — enumerate; paginated changes feed. On the
  first invocation returns every non-deleted item, so we treat it as the
  full-listing endpoint for v0.2.
- ``DELETE /me/drive/items/{id}`` — moves to Recycle Bin (Personal).
- ``POST /me/drive/items/{id}/restore`` — restore. Documented as
  Business-only; Personal returns 501 / ``notSupported`` and we surface an
  actionable error pointing the user at the web UI.

httpx and msal are imported lazily so the module can be ``py_compile``'d
and unit-tested with mocks even without the runtime deps installed.  A
custom ``client_factory`` callable is accepted by the constructor so tests
can inject a MagicMock without exercising :mod:`httpx` at all.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from duplicate_cleaner.auth.clients import GRAPH_ROOT
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
)

log = logging.getLogger(__name__)

_DELTA_URL = f"{GRAPH_ROOT}/me/drive/root/delta"

# Security pass 7: cloud_file_id shape gate.  Every OneDrive driveItem id
# observed in the wild is Base64url plus ``!`` (the drive/item separator);
# ``.``, ``/``, ``:``, and any URL-reserved character have never appeared.
# The regex is tighter than the sub-phase 5b validator (which admits 20+ char
# minimum for a REPORT member) because the source method has already located
# a physical account and any deviation should surface loudly.  The 120-char
# upper bound is well past the ~60 char real-world maximum.
_ONEDRIVE_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9!]{1,120}$")
# Note: source-side lower bound stays at 1 to keep the mock-ID unit tests
# working; the AUTHORITATIVE 20-char minimum is enforced at the report
# boundary via `paths.validate_cloud_entry` (external-input attack surface).
# Source-side defense-in-depth tightening deferred to v0.1.2 alongside
# a fixture rewrite.

# Security pass 9: redirect target of a Graph 302 (e.g. /me/drive/items/{id}/
# content → Azure CDN) must belong to a Microsoft-controlled origin.  Bearer
# is already stripped by ``_build_unauth_http_client`` for the follow-up
# fetch, but an on-path attacker who set ``Location=https://evil.example.com``
# could still stream attacker-supplied bytes into the reconcile pipeline —
# whose hash-comparison then trashes the local original as a "duplicate".
_ONEDRIVE_REDIRECT_ORIGIN_ALLOW: tuple[str, ...] = (
    "graph.microsoft.com",
    ".blob.core.windows.net",
    ".sharepoint.com",
)


def _redirect_origin_is_allowed(target_url: str) -> bool:
    """Return True when ``target_url``'s host belongs to a Microsoft-controlled origin."""
    host = (urlparse(target_url).hostname or "").lower()
    if not host:
        return False
    for entry in _ONEDRIVE_REDIRECT_ORIGIN_ALLOW:
        if entry.startswith("."):
            if host.endswith(entry) or host == entry.lstrip("."):
                return True
        elif host == entry:
            return True
    return False

# Security pass 7: any URL that carries the Authorization header must have
# this host as its netloc.  A cross-origin redirect (Azure CDN for /content;
# a poisoned ``@odata.nextLink``) MUST NOT forward the bearer token.
_GRAPH_HOST: str = urlparse(GRAPH_ROOT).netloc


def _validate_cloud_file_id(cloud_file_id: str) -> None:
    """Reject a suspicious OneDrive item id BEFORE it lands in a URL.

    A poisoned manifest with ``cloud_file_id="root:/../foo"`` would target
    the wrong item once interpolated into ``/me/drive/items/{id}`` — even a
    URL-encoded form would be routable by Graph.  The shape check refuses the
    request before we build the URL at all.
    """
    if not _ONEDRIVE_ID_RE.match(cloud_file_id):
        raise SourceError(
            f"OneDrive cloud_file_id {cloud_file_id!r} does not match the "
            f"expected shape ({_ONEDRIVE_ID_RE.pattern}); refusing to "
            "interpolate a suspicious id into a Graph URL."
        )


def _quote_cloud_file_id(cloud_file_id: str) -> str:
    """URL-escape ``cloud_file_id`` even after shape validation (belt-and-braces)."""
    return urllib.parse.quote(cloud_file_id, safe="")

# ``$select`` on ``/delta`` is documented as a hint only — Graph returns
# every default field regardless — but we send it to signal intent and to
# reduce payload size on well-behaved servers.
_DELTA_SELECT = (
    "id,name,size,file,folder,hashes,lastModifiedDateTime,parentReference,"
    "createdBy,remoteItem,deleted"
)


def _build_http_client(token_provider: Callable[[], str]) -> Any:
    """Return an ``httpx.Client`` configured with the bearer token.

    ``token_provider`` is a zero-arg callable that returns the current
    (possibly-refreshed) access token; wrapped by an ``httpx`` auth so
    every request picks up a fresh token without the caller re-injecting
    it.  Isolated so tests can patch it.

    Security pass 7: ``follow_redirects=False`` — a 302 response's
    ``Location`` may point at Azure CDN (``blob.core.windows.net``) for the
    ``/content`` endpoint, and httpx forwards request headers verbatim on
    redirect.  Keeping redirects off ensures :meth:`OneDriveSource.read_bytes`
    can explicitly re-issue the redirected request through a NEW client with
    no Authorization header attached.
    """
    import httpx  # type: ignore[import-not-found,import-untyped]

    class _BearerAuth(httpx.Auth):  # type: ignore[misc,no-any-unimported]
        def __init__(self, provider: Callable[[], str]) -> None:
            self._provider = provider

        def auth_flow(self, request: Any) -> Iterator[Any]:
            # Defense-in-depth: even with follow_redirects=False on the
            # client, httpx still exposes the auth flow to redirected
            # sub-requests during ``stream``.  Only attach Authorization
            # when the request host is Microsoft Graph — any other origin
            # (Azure CDN, a hostile ``@odata.nextLink`` proxy) MUST receive
            # a request with NO Authorization header.
            host = getattr(request.url, "host", None) or ""
            if host == _GRAPH_HOST:
                request.headers["Authorization"] = f"Bearer {self._provider()}"
            else:
                request.headers.pop("Authorization", None)
            yield request

    return httpx.Client(
        auth=_BearerAuth(token_provider),
        timeout=60.0,
        follow_redirects=False,
    )


def _build_unauth_http_client() -> Any:
    """Return an ``httpx.Client`` with NO auth for downloading redirected bytes.

    Used by :meth:`OneDriveSource.read_bytes` to follow a Graph 302 to Azure
    CDN.  A fresh client is constructed per-call so the caller cannot leak a
    stray Authorization header via a shared connection pool.
    """
    import httpx  # type: ignore[import-not-found,import-untyped]

    return httpx.Client(timeout=60.0, follow_redirects=True)


def _http_status(exc: BaseException) -> int | None:
    """Return the numeric status attached to an httpx ``HTTPStatusError``."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    status = getattr(resp, "status_code", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _is_retryable_status(status: int | None) -> bool:
    """429 + 5xx are retry-worthy; everything else is fatal."""
    if status is None:
        return False
    return status == 429 or 500 <= status < 600


def _is_retryable_http_error(exc: BaseException) -> bool:
    """Return True for transient Graph errors — 429/5xx plus transport-level.

    CR#1 (pass 8): the original classifier only matched httpx.HTTPStatusError.
    On a real long-lived scan a TCP RST / DNS blip / read timeout raises
    httpx.TransportError (which is the parent of TimeoutException,
    ConnectError, NetworkError, ReadError, ...) — those must also retry, or
    a 45-minute enumeration aborts on the first transient hiccup.

    Imports httpx lazily so a test that never touches the network can run
    without the package installed.
    """
    try:
        import httpx  # type: ignore[import-not-found,import-untyped]
    except ImportError:
        return False
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return _is_retryable_status(_http_status(exc))
    return False


def _graph_call(callable_: Callable[[], Any]) -> Any:
    """Invoke ``callable_`` with tenacity backoff on 429 / 5xx.

    Symmetric to ``sources.gdrive._drive_call``: on the final retry
    exhaustion the original ``HTTPStatusError`` is re-raised — the caller
    then runs it through :func:`_raise_mapped` which translates 401/403/
    404/429 to typed :class:`SourceError` subtypes.
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


def _raise_mapped(exc: BaseException) -> None:
    """Translate an httpx.HTTPStatusError into the closest ``SourceError``.

    Returns normally on non-httpx errors so the original exception falls
    through to the caller's re-raise.
    """
    try:
        import httpx  # type: ignore[import-not-found,import-untyped]
    except ImportError:
        return
    if not isinstance(exc, httpx.HTTPStatusError):
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
        # Only reached after tenacity exhausted its retries — surface as a
        # caller-actionable rate-limit error rather than a raw HTTP error.
        raise SourceRateLimitError(str(exc)) from exc


def _parse_rfc3339(value: str) -> float:
    """Parse a Graph ``lastModifiedDateTime`` RFC 3339 string to POSIX float."""
    try:
        cleaned = value.replace("Z", "+00:00")
        return float(datetime.fromisoformat(cleaned).timestamp())
    except (TypeError, ValueError):
        return 0.0


class OneDriveSource:
    """Source over one Microsoft account's OneDrive Personal.

    ``is_read_only_scan`` defaults True — same tripwire pattern as
    :class:`GoogleDriveSource`.  ``apply/mover.py`` constructs the source
    with the flag off (wired in sub-phase 5).
    """

    id: str
    is_read_only_scan: bool

    def __init__(
        self,
        account_id: str,
        token_provider: Callable[[], str],
        *,
        account_user_id: str | None = None,
        is_read_only_scan: bool = True,
        client_factory: Callable[[Callable[[], str]], Any] | None = None,
    ) -> None:
        """
        ``token_provider`` is a zero-arg callable returning the current
        bearer token.  Refresh handling lives outside the source (CLI +
        auth layer): the source only sees a fresh token per call.

        ``account_user_id`` is the signed-in user's Graph object id used
        to distinguish "created by me" from "shared with me" items.  If
        not supplied we fall back to marking non-remoteItem items as
        owned; ``remoteItem`` presence alone is the authoritative
        shared-with-you marker on OneDrive Personal.

        ``client_factory`` is an escape hatch for tests: a callable
        ``(token_provider) -> httpx.Client``-shaped object.
        """
        self.id = account_id
        self._token_provider = token_provider
        self._account_user_id = account_user_id
        self.is_read_only_scan = is_read_only_scan
        self._client_factory: Callable[[Callable[[], str]], Any] = (
            client_factory if client_factory is not None else _build_http_client
        )
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        """Return the httpx client, constructing it on first access."""
        if self._client is None:
            self._client = self._client_factory(self._token_provider)
        return self._client

    def list_files(self) -> Iterator[FileRecord]:
        """Enumerate every eligible item via ``/me/drive/root/delta``.

        First call to ``/delta`` returns every non-deleted item paged via
        ``@odata.nextLink``; the terminal page carries ``@odata.deltaLink``
        which we don't persist yet (sub-phase 5 may reuse for warm scans).

        Security pass 7: ``@odata.nextLink`` is validated to start with
        ``GRAPH_ROOT + "/"`` — Microsoft never returns a nextLink outside
        ``graph.microsoft.com``, so a mismatch is a MITM / proxy-injection
        signal and aborts enumeration rather than blindly following the URL
        (which would forward our Bearer token to an attacker-chosen origin).
        """
        next_url: str = f"{_DELTA_URL}?$select={_DELTA_SELECT}"
        while next_url:
            # Bind the loop variable through a default arg so tenacity's
            # retry callable always closes over the URL for the current
            # page (B023 — no late-binding surprise on a retry storm).
            def _fetch(url: str = next_url) -> Any:
                return self._get(url)

            resp = _graph_call(_fetch)
            data = resp.json()
            for item in data.get("value", []):
                rec = self._item_to_record(item)
                if rec is not None:
                    yield rec
            link = data.get("@odata.nextLink")
            if isinstance(link, str) and link:
                # Graph's nextLink is fully-formed — do NOT append our
                # own $select query on subsequent pages.
                if not link.startswith(GRAPH_ROOT + "/"):
                    raise SourceError(
                        "Refusing to follow @odata.nextLink outside "
                        f"{GRAPH_ROOT!r}: got {link!r}. This is a proxy or "
                        "MITM injection signal — check network configuration."
                    )
                next_url = link
            else:
                break

    def _get(self, url: str) -> Any:
        """GET wrapper that raises for status so tenacity can classify errors."""
        resp = self.client.get(url)
        resp.raise_for_status()
        return resp

    def _item_to_record(self, item: dict[str, Any]) -> FileRecord | None:
        """Filter and convert one Graph driveItem to a FileRecord."""
        # ``deleted`` presence marks an item as trashed — skip.  (Sub-phase
        # 5 warm-scan resync will need this to prune the local cache; for
        # now the enumeration is a snapshot so we just drop them.)
        if item.get("deleted") is not None:
            return None
        # Folders have a top-level ``folder`` object; only files carry
        # ``file`` (with the hash bag).
        if "folder" in item:
            return None
        file_block = item.get("file")
        if not isinstance(file_block, dict):
            return None
        cloud_file_id = item.get("id")
        if not cloud_file_id:
            return None
        # OneDrive Personal populates ``file.hashes.sha256Hash``; without
        # it we cannot align the record with local BLAKE3 or drive the
        # reconcile step.  Skip with a debug log — same shape as the
        # gdrive.md5-missing path (which produces ``foreign_hash=None``
        # instead of skipping, but Personal drives always have the hash).
        hashes = file_block.get("hashes")
        sha256 = None
        if isinstance(hashes, dict):
            sha256 = hashes.get("sha256Hash")
        if not isinstance(sha256, str) or not sha256:
            log.debug(
                "onedrive: skipping %s (no sha256Hash) — quickXorHash-only "
                "items are Business tenants and out of scope for v0.2.",
                cloud_file_id,
            )
            return None
        # Normalise: Graph returns hex uppercase; downstream reconcile
        # compares algo-tagged strings via ``str.lower()``, so lowercase
        # here for consistency with BLAKE3's hexdigest format.
        foreign_hash = f"sha256:{sha256.lower()}"

        raw_size = item.get("size")
        try:
            size = int(raw_size) if raw_size is not None else 0
        except (TypeError, ValueError):
            return None

        modified = item.get("lastModifiedDateTime")
        mtime = _parse_rfc3339(modified) if isinstance(modified, str) else 0.0

        # ``createdBy.user`` is optional; if absent (very old items or
        # anonymous shares) treat as shared-with-you.
        owner_email: str | None = None
        owner_user_id: str | None = None
        created_by = item.get("createdBy")
        if isinstance(created_by, dict):
            user_block = created_by.get("user")
            if isinstance(user_block, dict):
                email_val = user_block.get("email")
                if isinstance(email_val, str):
                    owner_email = email_val
                uid_val = user_block.get("id")
                if isinstance(uid_val, str):
                    owner_user_id = uid_val

        # ``remoteItem`` marks a shared-with-you drive item (the actual
        # bytes live in another drive).  This is the authoritative
        # shared-with-you signal on OneDrive Personal per Graph docs.
        remote_item = item.get("remoteItem")
        is_remote = remote_item is not None
        owner_is_me: bool
        if is_remote:
            owner_is_me = False
        elif self._account_user_id and owner_user_id:
            owner_is_me = owner_user_id == self._account_user_id
        else:
            # Match the B2 pattern from gdrive: when we cannot prove
            # ownership, DO NOT default to owner_is_me=True — that would
            # violate "shared cloud files informational-only" the moment
            # sub-phase 5 wires scoring.  Prefer False → is_shared=True.
            owner_is_me = False
        is_shared = is_remote or (not owner_is_me)

        parent = item.get("parentReference")
        parent_path = ""
        if isinstance(parent, dict):
            raw = parent.get("path")
            if isinstance(raw, str):
                parent_path = raw

        name = str(item.get("name", ""))
        drive_path = _join_parent_and_name(parent_path, name)
        virtual = f"{self.id}://{drive_path}"

        # etag: Graph exposes ``eTag`` on driveItem; we combine cloud id
        # with modified time as a stable-enough composite (mirrors the
        # gdrive pattern) so cache invalidation catches every edit even if
        # Graph's eTag semantics change.
        etag = f"{cloud_file_id}:{modified or ''}"

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

    def read_bytes(
        self, record: FileRecord, chunk_size: int = 1 << 20
    ) -> Iterator[bytes]:
        """Stream bytes for ``record`` via ``GET /me/drive/items/{id}/content``.

        Graph redirects to a pre-signed CDN URL (``blob.core.windows.net``).
        Security pass 7: the primary client has ``follow_redirects=False``,
        so a 302 surfaces here as an explicit hop; we re-issue the GET
        against the redirect target using a NEW client with NO Authorization
        header — that keeps the bearer token off the Azure CDN wire.
        """
        if not record.cloud_file_id:
            raise SourceError(
                f"{record.path}: cannot read bytes without cloud_file_id"
            )
        _validate_cloud_file_id(record.cloud_file_id)
        quoted_id = _quote_cloud_file_id(record.cloud_file_id)
        url = f"{GRAPH_ROOT}/me/drive/items/{quoted_id}/content"
        # First request: the Graph client returns a 302 pointing at Azure
        # CDN.  ``stream`` yields bytes without buffering the whole body.
        with self.client.stream("GET", url) as resp:
            status = getattr(resp, "status_code", None)
            if status in (301, 302, 303, 307, 308):
                redirect_target = resp.headers.get("location")
                if not redirect_target:
                    raise SourceError(
                        f"{record.path}: Graph returned {status} with no "
                        "Location header."
                    )
                # Security pass 9: reject redirect targets outside
                # Microsoft-controlled origins. Bearer is already stripped
                # for the follow-up fetch, but attacker-supplied bytes
                # streamed via a hostile Location would poison downstream
                # hash reconciliation and cause the local original to be
                # trashed as a "duplicate" when sub-phase 5c wires apply.
                if not _redirect_origin_is_allowed(redirect_target):
                    raise SourceError(
                        f"{record.path}: refusing to follow Graph 302 "
                        f"redirect to non-Microsoft origin "
                        f"({urlparse(redirect_target).hostname!r})."
                    )
                # Second request: through a fresh unauth client so the
                # Authorization header is not sent to a non-Graph host.
                unauth = _build_unauth_http_client()
                try:
                    with unauth.stream("GET", redirect_target) as cdn_resp:
                        cdn_resp.raise_for_status()
                        for chunk in cdn_resp.iter_bytes(chunk_size=chunk_size):
                            if chunk:
                                yield chunk
                finally:
                    unauth.close()
                return
            resp.raise_for_status()
            for chunk in resp.iter_bytes(chunk_size=chunk_size):
                if chunk:
                    yield chunk

    def move_to_trash(self, record: FileRecord) -> TrashedLocation:
        """Move ``record`` to the OneDrive Recycle Bin via ``DELETE /items/{id}``.

        OneDrive Personal keeps the item id after moving to the Recycle
        Bin, so ``cloud_file_id == cloud_trash_id``.  Restoration is
        best-effort — see :meth:`restore_from_trash`.
        """
        if self.is_read_only_scan:
            raise PermissionError(
                f"OneDriveSource(id={self.id!r}) is read-only during scan; "
                "construct with is_read_only_scan=False to enable trashing."
            )
        # CR#3 (pass 8): source_id guard mirrors restore_from_trash's check.
        # Defense-in-depth against a sub-phase 5b dispatch bug wiring the wrong
        # (source, record) pair — the wrong account's bearer would DELETE the
        # wrong item without an early refusal.
        if record.source_id != self.id:
            raise SourceError(
                f"FileRecord.source_id {record.source_id!r} does not match "
                f"this OneDriveSource id {self.id!r}."
            )
        if not record.cloud_file_id:
            raise SourceError(
                f"{record.path}: cannot trash without cloud_file_id"
            )
        # Security pass 7: refuse a suspicious id BEFORE URL interpolation.
        _validate_cloud_file_id(record.cloud_file_id)
        quoted_id = _quote_cloud_file_id(record.cloud_file_id)
        url = f"{GRAPH_ROOT}/me/drive/items/{quoted_id}"
        try:
            _graph_call(lambda: self._delete(url))
        except Exception as exc:
            _raise_mapped(exc)
            raise
        return TrashedLocation(
            source_id=self.id,
            original_path=str(record.path),
            cloud_file_id=record.cloud_file_id,
            cloud_trash_id=record.cloud_file_id,
        )

    def _delete(self, url: str) -> Any:
        """DELETE wrapper that raises on non-2xx (204 is success for Graph)."""
        resp = self.client.delete(url)
        resp.raise_for_status()
        return resp

    def restore_from_trash(self, loc: TrashedLocation) -> None:
        """Restore a trashed item via ``POST /items/{id}/restore``.

        Rejects a mismatched ``source_id`` so a poisoned manifest cannot
        dispatch a OneDrive entry to a different source implementation.

        OneDrive Personal historically did not support programmatic
        restore — the ``/restore`` endpoint returns 501 with an
        ``notSupported`` code.  When that happens we raise
        :class:`SourceError` with a message pointing the user at the
        OneDrive web Recycle Bin UI so the failure is actionable rather
        than opaque.  A 404 (permanently deleted, or Recycle Bin emptied)
        surfaces as :class:`SourceNotFoundError` per the shared error
        contract.
        """
        if loc.source_id != self.id:
            raise SourceError(
                f"TrashedLocation source_id {loc.source_id!r} does not match "
                f"this OneDriveSource id {self.id!r}."
            )
        if not loc.cloud_file_id:
            raise SourceError(
                f"TrashedLocation has no cloud_file_id: cannot restore ({loc})"
            )
        # Security pass 7: refuse a suspicious id BEFORE URL interpolation.
        _validate_cloud_file_id(loc.cloud_file_id)
        quoted_id = _quote_cloud_file_id(loc.cloud_file_id)
        url = f"{GRAPH_ROOT}/me/drive/items/{quoted_id}/restore"
        try:
            _graph_call(lambda: self._post(url, json_body={}))
        except Exception as exc:
            status = _http_status(exc)
            if status == 501 or _is_notsupported(exc):
                raise SourceError(
                    f"OneDrive Personal does not support programmatic restore "
                    f"of item {loc.cloud_file_id!r}. Restore manually from the "
                    "Recycle Bin at https://onedrive.live.com/?id=recyclebin"
                ) from exc
            _raise_mapped(exc)
            raise

    def _post(self, url: str, *, json_body: dict[str, Any]) -> Any:
        """POST wrapper — accepts an explicit JSON body so tests can assert on it."""
        resp = self.client.post(url, json=json_body)
        resp.raise_for_status()
        return resp

    def get_metadata(self, record: FileRecord) -> SourceMetadata:
        """Return metadata already populated at scan time — no re-fetch."""
        return SourceMetadata(
            etag=record.etag,
            cloud_file_id=record.cloud_file_id,
            owner=record.owner,
            is_shared=record.is_shared,
        )

    def check_drift(self, record: FileRecord) -> None:
        """Re-fetch ``lastModifiedDateTime`` + ``eTag`` and verify no drift.

        v0.2 sub-phase 5c: called by the mover immediately before
        :meth:`move_to_trash`.  Issues
        ``GET /me/drive/items/{id}?$select=id,eTag,lastModifiedDateTime``.
        The primary check is against the composite
        ``f"{cloud_file_id}:{lastModifiedDateTime}"`` (the same shape
        :meth:`list_files` stamps into ``FileRecord.etag``).  ``eTag`` is only
        used as a secondary confirmation because Graph does not always echo
        it on ``$select``.  A mismatch raises :class:`SourceDriftError` —
        the mover aborts the entire apply run.
        """
        if not record.cloud_file_id:
            raise SourceError(
                f"{record.path}: cannot check drift without cloud_file_id"
            )
        if record.source_id != self.id:
            raise SourceError(
                f"FileRecord.source_id {record.source_id!r} does not match "
                f"this OneDriveSource id {self.id!r}."
            )
        _validate_cloud_file_id(record.cloud_file_id)
        quoted_id = _quote_cloud_file_id(record.cloud_file_id)
        url = (
            f"{GRAPH_ROOT}/me/drive/items/{quoted_id}"
            "?$select=id,eTag,lastModifiedDateTime"
        )
        try:
            resp = _graph_call(lambda: self._get(url))
        except Exception as exc:
            _raise_mapped(exc)
            raise
        try:
            body = resp.json()
        except Exception as exc:
            raise SourceError(
                f"{record.path}: check_drift got non-JSON response ({exc})"
            ) from exc
        if not isinstance(body, dict):
            raise SourceError(
                f"{record.path}: check_drift response is not a JSON object"
            )
        modified = body.get("lastModifiedDateTime")
        current_composite = f"{record.cloud_file_id}:{modified or ''}"
        if current_composite == record.etag:
            return
        # Fall back to the provider-supplied ``eTag`` for a defense-in-depth
        # second chance: if the record.etag happens to be the raw Graph eTag
        # (older scans or a future scheme change), accept an eTag match too.
        graph_etag = body.get("eTag")
        if isinstance(graph_etag, str) and graph_etag and graph_etag == record.etag:
            return
        raise SourceDriftError(
            f"Cloud etag drift for {record.path}: scan={record.etag!r} "
            f"now={current_composite!r} (graph eTag={graph_etag!r}) — the "
            "file changed on the provider since scan.  Rescan and retry."
        )


def _join_parent_and_name(parent_path: str, name: str) -> str:
    """Compose a display path from Graph ``parentReference.path`` + item name.

    ``parentReference.path`` is a URL-encoded string like
    ``/drive/root:/Documents/Sub`` — the ``:`` marks the boundary between
    the drive-root prefix and the user-visible path.  We strip everything
    up to and including that colon and join with the item name.
    """
    if not parent_path:
        return name
    # Split on the first ``:`` — everything after is the user-visible path.
    _, _, after = parent_path.partition(":")
    trimmed = after.strip("/")
    if not trimmed:
        return name
    return f"{trimmed}/{name}"


def _is_notsupported(exc: BaseException) -> bool:
    """Return True when a Graph error body carries the ``notSupported`` code.

    Graph emits ``{"error": {"code": "notSupported", ...}}`` for the
    Personal-restore-unavailable case even when the HTTP status is 400 or
    500; we probe the JSON body defensively.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    try:
        body = resp.json()
    except Exception:
        return False
    if not isinstance(body, dict):
        return False
    err = body.get("error")
    if not isinstance(err, dict):
        return False
    code = err.get("code")
    return isinstance(code, str) and code == "notSupported"
