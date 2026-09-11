"""v0.2 sub-milestone 5e — cross-source scoring rule tests.

Every scenario constructs a ``Group`` of synthetic ``HashedRecord``\\ s
and calls ``score_group`` directly.  No CLI wiring, no cloud sources —
the scorer is the unit under test.

Invariants under test:

* Local under an active home ALWAYS beats any cloud copy in the same
  group.  The ``cloud_when_local_exists`` signal (-3) stamps every cloud
  sibling with a penalty large enough to keep local as keeper.
* ``is_shared=True`` on a cloud member marks it informational — a shared
  file is NEVER a discard candidate (AUDIT_LOG invariant).
* When a group has no local member, the earliest source id listed in
  ``retained_cloud_order`` wins the keeper role.
* A cloud source id absent from ``retained_cloud_order`` sorts strictly
  below every listed source.
* The mixed 1xlocal + 1xcloud-shared + 1xcloud-owned matrix produces
  exactly one keeper (local), one informational (shared), and one
  discard candidate (owned) with the expected signals attached.
"""
from __future__ import annotations

from pathlib import Path

from duplicate_cleaner.compare.exact import Group
from duplicate_cleaner.config import DEFAULT_WEIGHTS, Config
from duplicate_cleaner.hash.pipeline import HashedRecord
from duplicate_cleaner.score.rules import score_group


def _hr(
    path: Path,
    *,
    size: int = 100,
    mtime: float = 1000.0,
    inode: int | None = None,
    source_id: str = "local",
    is_shared: bool = False,
) -> HashedRecord:
    """Small factory — every test tweaks only the fields it cares about."""
    return HashedRecord(
        path=path,
        size=size,
        mtime=mtime,
        inode=inode if inode is not None else (abs(hash(str(path))) & 0xFFFFFFFF),
        dev=1,
        nlink=1,
        full_hash="H" * 64,
        source_id=source_id,
        is_shared=is_shared,
    )


def test_local_wins_over_cloud_in_same_group(tmp_path: Path) -> None:
    """1 local + 1 gdrive with same hash → local is keeper, cloud has -3 signal."""
    active = tmp_path / "Users" / "me"
    (active / "Documents").mkdir(parents=True)
    local_path = active / "Documents" / "foo.bin"
    local_path.write_bytes(b"x")
    cloud_path = Path("gdrive:personal://Docs/foo.bin")

    group = Group(
        hash="H",
        size=1,
        members=[
            _hr(local_path, source_id="local"),
            _hr(cloud_path, source_id="gdrive:personal"),
        ],
    )
    cfg = Config(active_homes=[active])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_source = {m.source_id: m for m in members}

    assert by_source["local"].is_proposed_keeper
    assert not by_source["gdrive:personal"].is_proposed_keeper
    # -3 signal wire-check: the cloud member carries the penalty.
    cloud_signals = [name for name, _w in by_source["gdrive:personal"].signals]
    assert any("cloud entry when local copy exists" in s for s in cloud_signals)
    weights_map = dict(by_source["gdrive:personal"].signals)
    assert weights_map["cloud entry when local copy exists"] == -3.0


def test_gphotos_source_marked_informational(tmp_path: Path) -> None:
    """v0.6-patch M1: a gphotos: member never becomes a discard candidate.

    Google Photos is read-only in v0.6 (permanently until v0.6.1 escalates
    scope).  Without informational marking the scorer proposes the gphotos
    entry as a discard and ``dc apply --commit`` trips the read-only
    pre-flight tripwire, refusing the whole run.  This test locks in the
    fix — the gphotos member is marked informational so the local peer
    remains the sole keeper and no discard is emitted for the group.
    """
    active = tmp_path / "Users" / "me"
    (active / "Documents").mkdir(parents=True)
    local_path = active / "Documents" / "photo.HEIC"
    local_path.write_bytes(b"x")
    gphotos_path = Path("gphotos:personal://photo.HEIC")

    group = Group(
        hash="H",
        size=1,
        members=[
            _hr(local_path, source_id="local"),
            _hr(gphotos_path, source_id="gphotos:personal"),
        ],
    )
    cfg = Config(active_homes=[active])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_source = {m.source_id: m for m in members}

    assert by_source["gphotos:personal"].is_informational is True
    assert by_source["gphotos:personal"].is_proposed_keeper is False
    assert by_source["local"].is_proposed_keeper is True


def test_icloud_source_marked_informational(tmp_path: Path) -> None:
    """v0.6-patch M1: an icloud: member never becomes a discard candidate.

    iCloud Photos is permanently read-only — deletion goes via Photos.app,
    not this source.  Symmetric to the gphotos test above.
    """
    active = tmp_path / "Users" / "me"
    (active / "Pictures").mkdir(parents=True)
    local_path = active / "Pictures" / "photo.HEIC"
    local_path.write_bytes(b"y")
    icloud_path = Path("iclouddrive:icloud:personal://photo.HEIC")

    group = Group(
        hash="H",
        size=1,
        members=[
            _hr(local_path, source_id="local"),
            _hr(icloud_path, source_id="icloud:personal"),
        ],
    )
    cfg = Config(active_homes=[active])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_source = {m.source_id: m for m in members}

    assert by_source["icloud:personal"].is_informational is True
    assert by_source["icloud:personal"].is_proposed_keeper is False
    assert by_source["local"].is_proposed_keeper is True


def test_shared_cloud_file_is_informational(tmp_path: Path) -> None:
    """A cloud member with is_shared=True is informational, never a discard."""
    active = tmp_path / "Users" / "me"
    (active / "Documents").mkdir(parents=True)
    local_path = active / "Documents" / "foo.bin"
    local_path.write_bytes(b"x")
    shared_path = Path("gdrive:personal://Shared/foo.bin")

    group = Group(
        hash="H",
        size=1,
        members=[
            _hr(local_path, source_id="local"),
            _hr(shared_path, source_id="gdrive:personal", is_shared=True),
        ],
    )
    cfg = Config(active_homes=[active])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_source = {m.source_id: m for m in members}

    assert by_source["gdrive:personal"].is_informational
    assert not by_source["gdrive:personal"].is_proposed_keeper
    assert by_source["local"].is_proposed_keeper


def test_retained_cloud_order_when_no_local() -> None:
    """3 cloud members from different accounts + config-ordered preference.

    The earliest source listed in ``retained_cloud_order`` wins the
    keeper role.  All three members share size/mtime/path-depth so the
    ordering signal is what breaks the tie.
    """
    p_personal = Path("gdrive:personal://foo.bin")
    p_family = Path("gdrive:family://foo.bin")
    p_onedrive = Path("onedrive:main://foo.bin")

    group = Group(
        hash="H",
        size=1,
        members=[
            _hr(p_personal, source_id="gdrive:personal"),
            _hr(p_family, source_id="gdrive:family"),
            _hr(p_onedrive, source_id="onedrive:main"),
        ],
    )
    cfg = Config(
        retained_cloud_order=[
            "gdrive:personal",
            "gdrive:family",
            "onedrive:main",
        ]
    )
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_source = {m.source_id: m for m in members}
    assert by_source["gdrive:personal"].is_proposed_keeper
    assert not by_source["gdrive:family"].is_proposed_keeper
    assert not by_source["onedrive:main"].is_proposed_keeper
    assert by_source["gdrive:personal"].score > by_source["gdrive:family"].score
    assert by_source["gdrive:family"].score > by_source["onedrive:main"].score


def test_retained_cloud_order_unknown_account_sorts_last() -> None:
    """A source not in ``retained_cloud_order`` is deprioritised below listed ones."""
    p_personal = Path("gdrive:personal://foo.bin")
    p_onedrive = Path("onedrive:main://foo.bin")

    group = Group(
        hash="H",
        size=1,
        members=[
            _hr(p_personal, source_id="gdrive:personal"),
            _hr(p_onedrive, source_id="onedrive:main"),
        ],
    )
    # Only gdrive:personal listed — onedrive:main falls into the
    # "unlisted" bucket and must sort last.
    cfg = Config(retained_cloud_order=["gdrive:personal"])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_source = {m.source_id: m for m in members}
    assert by_source["gdrive:personal"].is_proposed_keeper
    assert not by_source["onedrive:main"].is_proposed_keeper
    unlisted_signal_names = [
        name for name, _w in by_source["onedrive:main"].signals
    ]
    assert any("retained_cloud_order[unlisted]" in s for s in unlisted_signal_names)


def test_reconciled_cross_algo_group_scores_local_keeper(tmp_path: Path) -> None:
    """v0.2.1: after ``reconcile_cross_source`` folds a local BLAKE3 record
    and a cloud MD5 record into the same duplicate group, the scorer must
    still pick the local under an active home as keeper.  Wires the whole
    pipeline (reconciliation → group_by_hash → score_group) so a regression
    that drops reconciliation would leave the two members in separate
    groups and this test would find zero cross-source groups.
    """
    import blake3  # type: ignore[import-untyped]

    from duplicate_cleaner.compare.exact import group_by_hash
    from duplicate_cleaner.hash.reconciliation import (
        make_budget,
        reconcile_cross_source,
    )
    from duplicate_cleaner.store import Store

    payload = b"reconciled-cross-algo-payload"
    canonical = str(blake3.blake3(payload).hexdigest())

    active = tmp_path / "Users" / "me"
    (active / "Documents").mkdir(parents=True)
    local_path = active / "Documents" / "foo.bin"
    local_path.write_bytes(payload)
    cloud_path = Path("gdrive:personal://Docs/foo.bin")

    records = [
        HashedRecord(
            path=local_path,
            size=len(payload),
            mtime=local_path.stat().st_mtime,
            inode=1,
            dev=1,
            nlink=1,
            full_hash=canonical,
            source_id="local",
        ),
        HashedRecord(
            path=cloud_path,
            size=len(payload),
            mtime=0.0,
            inode=0,
            dev=0,
            nlink=1,
            full_hash="md5:opaque",  # foreign — won't match local until reconcile
            source_id="gdrive:personal",
            foreign_hash="md5:opaque",
            etag="etag-x",
            cloud_file_id="cid-x",
        ),
    ]

    class _Src:
        id = "gdrive:personal"
        is_read_only_scan = True

        def read_bytes(
            self, _r: HashedRecord, chunk_size: int = 1 << 20
        ) -> list[bytes]:
            return [payload]

    class _LocalSrc:
        id = "local"
        is_read_only_scan = True

        def read_bytes(
            self, _r: HashedRecord, chunk_size: int = 1 << 20
        ) -> list[bytes]:
            raise AssertionError("local should not be re-read during reconcile")

    store = Store(path=tmp_path / "cache.db")
    reconciled, not_yet = reconcile_cross_source(
        records,
        {"local": _LocalSrc(), "gdrive:personal": _Src()},  # type: ignore[dict-item]
        store,
        make_budget(1000.0),
    )
    store.close()
    assert not_yet == []

    groups = list(group_by_hash(iter(reconciled)))
    assert len(groups) == 1
    cfg = Config(active_homes=[active])
    scored = score_group(groups[0], cfg, DEFAULT_WEIGHTS)
    by_source = {m.source_id: m for m in scored}
    assert by_source["local"].is_proposed_keeper
    assert not by_source["gdrive:personal"].is_proposed_keeper
    assert by_source["gdrive:personal"].reconciled is True


def test_mixed_local_cloud_shared_scoring_matrix(tmp_path: Path) -> None:
    """Big group: 1 local + 1 gdrive shared + 1 onedrive owned.

    Expected outcome:
    * ``local`` wins as the keeper (under active home, no cloud penalty).
    * ``gdrive:personal`` (shared) is informational — never a discard.
    * ``onedrive:main`` (owned, not shared) is the sole discard candidate
      and carries the ``cloud_when_local_exists`` -3 signal.
    """
    active = tmp_path / "Users" / "me"
    (active / "Documents").mkdir(parents=True)
    local_path = active / "Documents" / "foo.bin"
    local_path.write_bytes(b"x")
    shared_path = Path("gdrive:personal://Shared/foo.bin")
    owned_path = Path("onedrive:main://MyDocs/foo.bin")

    group = Group(
        hash="H",
        size=1,
        members=[
            _hr(local_path, source_id="local"),
            _hr(shared_path, source_id="gdrive:personal", is_shared=True),
            _hr(owned_path, source_id="onedrive:main"),
        ],
    )
    cfg = Config(active_homes=[active])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_source = {m.source_id: m for m in members}

    assert by_source["local"].is_proposed_keeper
    assert not by_source["local"].is_informational

    assert by_source["gdrive:personal"].is_informational
    assert not by_source["gdrive:personal"].is_proposed_keeper

    assert not by_source["onedrive:main"].is_informational
    assert not by_source["onedrive:main"].is_proposed_keeper
    owned_signal_names = [name for name, _w in by_source["onedrive:main"].signals]
    assert any(
        "cloud entry when local copy exists" in s for s in owned_signal_names
    )
