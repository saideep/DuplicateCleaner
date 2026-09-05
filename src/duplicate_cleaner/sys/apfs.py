"""APFS clone lineage detection via ``getattrlist(2)`` — macOS-only.

Two files sharing a clone lineage ID share physical storage on APFS. Trashing
one such file does not reclaim any space. The scorer uses this to mark clone
family members as informational (same treatment as hard-links).

The kernel call is invoked directly through ``ctypes`` — Python stdlib does
not expose ``getattrlist``. Every failure path returns ``None`` so the caller
falls back to ``st_nlink``-based hard-link logic.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import platform
import struct
from pathlib import Path

log = logging.getLogger(__name__)

# ``attribute_set_t`` bit that says "the commonattr words that follow use the
# extended layout, so ATTR_CMNEXT_* flags mean what they claim to mean". Any
# request touching ATTR_CMNEXT_* MUST set this bit — otherwise the kernel
# interprets the flags against the legacy layout and returns garbage.
_FSOPT_ATTR_CMN_EXTENDED = 0x00000020

# ATTR_CMNEXT_CLONEID: a 64-bit lineage identifier shared by every file
# descended from a common ``clonefile()`` ancestor on the same volume.
# Bit 0x100 per ``<sys/attr.h>``; older docs sometimes referenced 0x4 but
# that maps to ATTR_CMNEXT_RELPATH (a variable-length string) and would
# return misleading bytes.
_ATTR_CMNEXT_CLONEID = 0x00000100


class _AttrList(ctypes.Structure):
    """Mirror of ``struct attrlist`` from ``<sys/attr.h>``.

    Field order and types are load-bearing — the kernel reads the struct
    byte-for-byte, so a re-ordering silently returns wrong data.
    """

    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort),
        ("commonattr", ctypes.c_uint),
        ("volattr", ctypes.c_uint),
        ("dirattr", ctypes.c_uint),
        ("fileattr", ctypes.c_uint),
        ("forkattr", ctypes.c_uint),
    ]


_ATTR_BIT_MAP_COUNT = 5


def _load_libc() -> ctypes.CDLL | None:
    """Best-effort libc handle for ``getattrlist``."""
    name = ctypes.util.find_library("c")
    if not name:
        return None
    try:
        return ctypes.CDLL(name, use_errno=True)
    except OSError:
        return None


_LIBC: ctypes.CDLL | None = _load_libc() if platform.system() == "Darwin" else None


def _is_supported() -> bool:
    return _LIBC is not None


def get_clone_id(path: Path) -> int | None:
    """Return the APFS clone lineage id for ``path``, or ``None`` if unavailable.

    ``None`` means "cannot answer" — non-APFS volume, non-Darwin host, older
    kernel, permission error, or path missing. Callers MUST NOT interpret
    ``None`` as "no clone family"; they should fall through to the hard-link
    heuristic instead.
    """
    if _LIBC is None:
        return None
    try:
        raw = os.fsencode(str(path))
    except (UnicodeError, TypeError):
        return None

    attrs = _AttrList()
    attrs.bitmapcount = _ATTR_BIT_MAP_COUNT
    attrs.reserved = 0
    # H3: ATTR_CMNEXT_* bits go in ``forkattr`` when FSOPT_ATTR_CMN_EXTENDED
    # is set — see ``<sys/attr.h>`` and getattrlist(2). Prior code placed the
    # bit in ``commonattr`` where 0x100 aliases to ``ATTR_CMN_SCRIPT`` (a
    # 4-byte text encoding), which the ``length < 12`` guard silently rejected.
    attrs.commonattr = 0
    attrs.volattr = 0
    attrs.dirattr = 0
    attrs.fileattr = 0
    attrs.forkattr = _ATTR_CMNEXT_CLONEID

    # Kernel writes: 4-byte length prefix + 8-byte clone id, so 12 bytes is
    # plenty. Add slack in case a future macOS ships extra padding.
    buf = ctypes.create_string_buffer(64)

    try:
        rc = _LIBC.getattrlist(
            ctypes.c_char_p(raw),
            ctypes.byref(attrs),
            buf,
            ctypes.c_size_t(len(buf)),
            ctypes.c_ulong(_FSOPT_ATTR_CMN_EXTENDED),
        )
    except OSError:
        return None
    if rc != 0:
        errno = ctypes.get_errno()
        log.debug("getattrlist(%s) failed: errno=%d", path, errno)
        return None

    payload = buf.raw
    if len(payload) < 4:
        return None
    # First 4 bytes are the returned attribute length (little-endian). A
    # valid clone-id response is 12 bytes total (uint32 length + uint64
    # value). Anything smaller — including the 8-byte response returned by
    # older or non-supporting filesystems — is treated as "unknown" so we
    # never conflate distinct files into a false clone family.
    (length,) = struct.unpack_from("<I", payload, 0)
    if length < 12:
        return None
    (clone_id,) = struct.unpack_from("<Q", payload, 4)
    return int(clone_id)
