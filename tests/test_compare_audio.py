"""Audio near-duplicate tests — v0.8."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from duplicate_cleaner.compare.audio import (
    _FPCALC_FALLBACK_PATH,
    _FPCALC_PRIMARY_PATH,
    AUDIO_EXTENSIONS,
    compute_audio_fingerprint,
    find_audio_near_duplicates,
    fingerprint_similarity,
    is_audio_path,
)
from duplicate_cleaner.hash.pipeline import HashedRecord
from duplicate_cleaner.store import Store


def _hr(
    path: Path,
    *,
    size: int = 250_000,
    mtime: float = 1000.0,
    h: str = "H",
) -> HashedRecord:
    return HashedRecord(
        path=path,
        size=size,
        mtime=mtime,
        inode=abs(hash(str(path))) & 0xFFFFFFFF,
        dev=1,
        nlink=1,
        full_hash=h,
    )


def test_is_audio_path_and_extensions() -> None:
    assert is_audio_path(Path("song.mp3"))
    assert is_audio_path(Path("SONG.MP3"))
    assert is_audio_path(Path("track.FLAC"))
    assert not is_audio_path(Path("photo.jpg"))
    # sanity check the exported set covers the expected formats
    assert {".mp3", ".m4a", ".flac", ".wav", ".ogg"} <= AUDIO_EXTENSIONS


def test_compute_audio_fingerprint_on_missing_binary(tmp_path: Path) -> None:
    """No hardcoded fpcalc path exists → returns None without invoking acoustid."""
    audio = tmp_path / "x.mp3"
    audio.write_bytes(b"fake mp3 bytes")
    with patch(
        "duplicate_cleaner.compare.audio.os.path.exists", return_value=False
    ):
        assert compute_audio_fingerprint(audio) is None


def test_fpcalc_path_safety_uses_hardcoded_absolute_path(tmp_path: Path) -> None:
    """H9-style rail: fpcalc is looked up only at hardcoded absolute paths.

    Locks in the audit pass 17 N1 fix — audio fingerprint resolution
    does NOT reach for ``shutil.which('fpcalc')``.  An attacker with
    write access to an early ``$PATH`` directory can otherwise inject a
    shim that pyacoustid runs with the scan process's UID.  Structural:
    the ``compare/audio`` module does not import ``shutil`` at all
    (verified below).
    """
    import duplicate_cleaner.compare.audio as amod

    # No shutil import → the module cannot resolve ``shutil.which`` at
    # runtime.  If a future refactor pulls shutil in, this assertion
    # fires and the reviewer must decide whether the guard is still intact.
    assert not hasattr(amod, "shutil"), (
        "compare/audio must not import shutil — the fpcalc binary lookup "
        "uses a hardcoded absolute-path resolver.  See H9 in AUDIT_LOG."
    )

    # ``_find_fpcalc`` consults ONLY the two hardcoded paths.
    checked: list[str] = []

    def _fake_exists(p: str) -> bool:
        checked.append(p)
        return False

    with patch(
        "duplicate_cleaner.compare.audio.os.path.exists", side_effect=_fake_exists
    ):
        assert amod._find_fpcalc() is None

    assert checked == [_FPCALC_PRIMARY_PATH, _FPCALC_FALLBACK_PATH], (
        f"_find_fpcalc should only check the hardcoded paths; got {checked!r}"
    )


def test_compute_audio_fingerprint_sets_fpcalc_env(tmp_path: Path) -> None:
    """pyacoustid picks up the FPCALC env var; the compute path must stamp it."""
    import os as _os

    audio = tmp_path / "x.mp3"
    audio.write_bytes(b"fake mp3 bytes")

    fake_acoustid = type("A", (), {})()

    def _fake_fingerprint(_path: str) -> tuple[float, bytes]:
        return (2.0, b"ABCDEF")

    fake_acoustid.fingerprint_file = _fake_fingerprint  # type: ignore[attr-defined]
    fake_acoustid.FingerprintGenerationError = RuntimeError  # type: ignore[attr-defined]

    prior = _os.environ.pop("FPCALC", None)
    try:
        with patch(
            "duplicate_cleaner.compare.audio.os.path.exists",
            side_effect=lambda p: p == _FPCALC_PRIMARY_PATH,
        ):
            import sys
            sys.modules["acoustid"] = fake_acoustid
            try:
                fp = compute_audio_fingerprint(audio)
            finally:
                sys.modules.pop("acoustid", None)
        assert fp is not None
        assert _os.environ.get("FPCALC") == _FPCALC_PRIMARY_PATH
    finally:
        if prior is not None:
            _os.environ["FPCALC"] = prior
        else:
            _os.environ.pop("FPCALC", None)


def test_compute_audio_fingerprint_caches(tmp_path: Path) -> None:
    """Second call with the same store + stable stats returns the cached fingerprint."""
    audio = tmp_path / "x.mp3"
    audio.write_bytes(b"fake mp3 bytes")
    store = Store(path=tmp_path / "cache.db")

    fake_acoustid = type("A", (), {})()

    def _fake_fingerprint(_path: str) -> tuple[float, bytes]:
        _fake_fingerprint.calls += 1  # type: ignore[attr-defined]
        return (3.5, b"ABC123DEF")

    _fake_fingerprint.calls = 0  # type: ignore[attr-defined]
    fake_acoustid.fingerprint_file = _fake_fingerprint  # type: ignore[attr-defined]
    fake_acoustid.FingerprintGenerationError = RuntimeError  # type: ignore[attr-defined]

    with patch(
        "duplicate_cleaner.compare.audio.os.path.exists",
        side_effect=lambda p: p == _FPCALC_PRIMARY_PATH,
    ):
        import sys
        sys.modules["acoustid"] = fake_acoustid
        try:
            fp1 = compute_audio_fingerprint(audio, store=store)
            fp2 = compute_audio_fingerprint(audio, store=store)
        finally:
            sys.modules.pop("acoustid", None)
        store.close()

    assert fp1 is not None
    assert fp1 == fp2
    # Second call should NOT re-invoke acoustid.fingerprint_file — cache hit.
    assert _fake_fingerprint.calls == 1  # type: ignore[attr-defined]
    # Duration prefix preserved in the cached blob.
    assert fp1.startswith("3.500:")


def test_fingerprint_similarity_duration_filter() -> None:
    """Durations 3+ seconds apart short-circuit to 0.0 regardless of payload match."""
    a = "10.0:AAAAAAAAAAA"
    b = "13.5:AAAAAAAAAAA"  # 3.5s apart → > 2s tolerance → 0.0
    assert fingerprint_similarity(a, b) == 0.0


def test_fingerprint_similarity_bit_hamming() -> None:
    """N4 fix: fingerprint compare is bit-level Hamming on decoded uint32 arrays.

    Two decoded fingerprints of matching length whose XOR flips a known
    number of bits produce the expected 0.0-1.0 similarity.  The decode
    step is patched so the test is deterministic and independent of the
    fpcalc / pyacoustid install.
    """
    # 4 uint32 samples per fingerprint = 128 bits total.
    # Fingerprint a: all zeros.  Fingerprint b: flips 32 bits (one full sample).
    # Expected similarity: 1.0 - 32/128 = 0.75.
    fake_a = [0, 0, 0, 0]
    fake_b = [0xFFFFFFFF, 0, 0, 0]

    def _fake_decode(fp: str) -> list[int] | None:
        if fp.startswith("A"):
            return fake_a
        if fp.startswith("B"):
            return fake_b
        return None

    with patch(
        "duplicate_cleaner.compare.audio._decode_fingerprint",
        side_effect=_fake_decode,
    ):
        sim = fingerprint_similarity("10.0:AXX", "10.0:BYY")
    assert abs(sim - 0.75) < 1e-9


def test_fingerprint_similarity_identical_payloads() -> None:
    """Identical decoded payloads → similarity 1.0."""
    fake = [0x12345678, 0xDEADBEEF, 0]

    with patch(
        "duplicate_cleaner.compare.audio._decode_fingerprint",
        return_value=fake,
    ):
        assert fingerprint_similarity("10.0:P", "10.0:P") == 1.0


def test_find_audio_near_duplicates_groups_re_encodes(tmp_path: Path) -> None:
    """Two mp3s that fingerprint identically end up in one group."""
    a = tmp_path / "song_320k.mp3"
    b = tmp_path / "song_192k.mp3"
    for p in (a, b):
        p.write_bytes(b"fake audio")

    records = [_hr(a, h="hash-a"), _hr(b, h="hash-b")]

    def _fake_fp(path: Path, *, store: Store | None = None) -> str | None:
        # Same duration + identical payload → similarity 1.0.
        if path == a:
            return "180.0:AAAAA"
        return "180.5:AAAAA"

    # Deterministic decoded payloads keep the compare independent of any
    # installed pyacoustid / chromaprint bindings.
    fake_bits = [0xABCD1234, 0x12345678, 0]

    with patch(
        "duplicate_cleaner.compare.audio.compute_audio_fingerprint",
        side_effect=_fake_fp,
    ), patch(
        "duplicate_cleaner.compare.audio._decode_fingerprint",
        return_value=fake_bits,
    ):
        groups = find_audio_near_duplicates(records, similarity_threshold=0.90)

    assert len(groups) == 1
    g = groups[0]
    assert {m.path for m in g.members} == {a, b}
    assert g.min_similarity >= 0.90
    # duration_range reflects the mocked durations.
    assert 179.9 <= g.duration_range[0] <= 180.5
    assert 179.9 <= g.duration_range[1] <= 180.5


def test_find_audio_near_duplicates_skips_below_min_size(tmp_path: Path) -> None:
    """A 50KB audio record is excluded before fingerprinting."""
    small = tmp_path / "clip.mp3"
    small.write_bytes(b"tiny")
    records = [_hr(small, size=50_000, h="hash-small")]

    def _boom(*_a: object, **_kw: object) -> str | None:  # pragma: no cover
        raise AssertionError("fingerprint should not run below min_size")

    with patch(
        "duplicate_cleaner.compare.audio.compute_audio_fingerprint",
        side_effect=_boom,
    ):
        assert find_audio_near_duplicates(records, min_size=100_000) == []


def test_find_audio_near_duplicates_skips_exact_dupes(tmp_path: Path) -> None:
    """If both members share the same BLAKE3 hash, the audio pass drops them —
    the exact-dup pass will surface the pair instead.
    """
    a = tmp_path / "x.mp3"
    b = tmp_path / "y.mp3"
    for p in (a, b):
        p.write_bytes(b"fake audio")
    records = [_hr(a, h="SAME"), _hr(b, h="SAME")]

    def _fake_fp(_path: Path, *, store: Store | None = None) -> str:
        return "120.0:AAAAAAAAAAAAAAAAAAAA"

    with patch(
        "duplicate_cleaner.compare.audio.compute_audio_fingerprint",
        side_effect=_fake_fp,
    ), patch(
        "duplicate_cleaner.compare.audio._decode_fingerprint",
        return_value=[0xDEADBEEF, 0],
    ):
        assert find_audio_near_duplicates(records, similarity_threshold=0.90) == []


def test_find_audio_near_duplicates_ignores_non_audio(tmp_path: Path) -> None:
    """A .jpg record is never considered by the audio grouper."""
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg")
    song = tmp_path / "song.mp3"
    song.write_bytes(b"fake audio")

    calls: list[Path] = []

    def _fake_fp(path: Path, *, store: Store | None = None) -> str | None:
        calls.append(path)
        return "60.0:AAAAAA"

    with patch(
        "duplicate_cleaner.compare.audio.compute_audio_fingerprint",
        side_effect=_fake_fp,
    ):
        find_audio_near_duplicates(
            [_hr(photo, h="a"), _hr(song, h="b")],
            similarity_threshold=0.90,
        )
    assert calls == [song]


# ---------------------------------------------------------------------------
# CLI end-to-end: dc scan emits audio_near_dup_groups
# ---------------------------------------------------------------------------


def _invoke_scan_with_config(
    tmp_path: Path,
    scan_roots: list[Path],
    report_dir: Path,
    active_homes: list[Path],
    extra_args: list[str] | None = None,
) -> object:
    """Run ``dc scan`` under a temp config + cache directory (mirrors the image test)."""
    from typer.testing import CliRunner

    from duplicate_cleaner.cli import app
    from duplicate_cleaner.config import load_config as real_load

    cfg_path = tmp_path / "cfg" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    homes_toml = ", ".join(f'"{h}"' for h in active_homes)
    cfg_path.write_text(f"active_homes = [{homes_toml}]\nmin_size_bytes = 1\n")
    cache_dir = tmp_path / "cache"

    runner = CliRunner()
    with patch("duplicate_cleaner.cli.load_config") as lc, patch(
        "duplicate_cleaner.cli.CONFIG_PATH", cfg_path
    ), patch("duplicate_cleaner.cli.CACHE_DIR", cache_dir):
        lc.side_effect = lambda: real_load(cfg_path)
        args: list[str] = [
            "scan",
            *(str(r) for r in scan_roots),
            "--report",
            str(report_dir),
            "--min-size",
            "0",
        ]
        if extra_args:
            args.extend(extra_args)
        return runner.invoke(app, args, catch_exceptions=False)


def test_cli_scan_emits_audio_near_dup_groups(tmp_path: Path) -> None:
    """dc scan on a pair of near-duplicate audio files emits an audio-near-dup group."""
    import json

    from duplicate_cleaner.compare.audio import AudioNearDupGroup

    scan_root = tmp_path / "music"
    scan_root.mkdir()
    a = scan_root / "song_320.mp3"
    b = scan_root / "song_192.mp3"
    a.write_bytes(b"\x00" * 300_000)
    b.write_bytes(b"\x01" * 250_000)

    def _fake_find(records: object, **_kw: object) -> list[AudioNearDupGroup]:
        rec_list = list(records)
        members = [
            r for r in rec_list if r.path.suffix.lower() == ".mp3"
        ]
        if len(members) < 2:
            return []
        # Make sure the members have DIFFERENT hashes so the exact-dup
        # skip does NOT drop this group.
        return [
            AudioNearDupGroup(
                members=members,
                min_similarity=0.98,
                duration_range=(180.0, 180.5),
                fingerprints=["180.0:FP-A", "180.5:FP-B"],
            )
        ]

    with patch(
        "duplicate_cleaner.cli.find_audio_near_duplicates",
        side_effect=_fake_find,
    ), patch(
        "duplicate_cleaner.cli.is_fpcalc_available",
        return_value=True,
    ):
        r = _invoke_scan_with_config(
            tmp_path,
            [scan_root],
            tmp_path / "report",
            [scan_root],
        )
    assert getattr(r, "exit_code", 1) == 0, getattr(r, "output", "")
    with (tmp_path / "report" / "report.json").open() as f:
        data = json.load(f)
    groups = data.get("audio_near_dup_groups", [])
    assert groups, f"Expected an audio-near-dup group; got: {data}"
    g = groups[0]
    assert g["kind"] == "audio-near-dup"
    assert len(g["members"]) == 2
    assert g.get("audio_near_dup") is not None
    assert g["audio_near_dup"]["min_similarity"] == 0.98


def test_store_purge_stale_audio_fingerprints(tmp_path: Path) -> None:
    """purge_stale_audio_fingerprints drops rows older than the cutoff.

    Mirrors test_store_purge_stale_phashes.  Audit pass 17 N2 wired the
    call in ``cli.py::scan``; without a store-level test the future CI
    could regress the purge without any red flag.
    """
    import time as _time
    from unittest.mock import patch as _patch

    store = Store(tmp_path / "cache.db")
    try:
        fake_now = 1_000_000_000.0
        with _patch.object(_time, "time", return_value=fake_now):
            store.put_audio_fingerprint(
                Path("/tmp/old.mp3"), 10, 1.0, "3.0:OLDBLOB"
            )
        with _patch.object(_time, "time", return_value=fake_now + 86400 * 30):
            dropped = store.purge_stale_audio_fingerprints(max_age_days=1.0)
        assert dropped == 1
        assert store.get_cached_audio_fingerprint(
            Path("/tmp/old.mp3"), 10, 1.0
        ) is None
    finally:
        store.close()


def test_store_purge_stale_video_signatures(tmp_path: Path) -> None:
    """purge_stale_video_signatures drops rows older than the cutoff.

    Symmetric to :func:`test_store_purge_stale_audio_fingerprints`; both
    were unwired until audit pass 17 N2.
    """
    import time as _time
    from unittest.mock import patch as _patch

    store = Store(tmp_path / "cache.db")
    try:
        fake_now = 1_000_000_000.0
        with _patch.object(_time, "time", return_value=fake_now):
            store.put_video_signature(
                Path("/tmp/old.mp4"), 20, 2.0, "60.0:aabb,ccdd"
            )
        with _patch.object(_time, "time", return_value=fake_now + 86400 * 30):
            dropped = store.purge_stale_video_signatures(max_age_days=1.0)
        assert dropped == 1
        assert store.get_cached_video_signature(
            Path("/tmp/old.mp4"), 20, 2.0
        ) is None
    finally:
        store.close()
