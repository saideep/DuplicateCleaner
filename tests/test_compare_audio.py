"""Audio near-duplicate tests — v0.8."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from duplicate_cleaner.compare.audio import (
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
    """shutil.which(fpcalc) → None → returns None without invoking acoustid."""
    audio = tmp_path / "x.mp3"
    audio.write_bytes(b"fake mp3 bytes")
    with patch("duplicate_cleaner.compare.audio.shutil.which", return_value=None):
        assert compute_audio_fingerprint(audio) is None


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

    with patch(
        "duplicate_cleaner.compare.audio.shutil.which",
        return_value="/opt/homebrew/bin/fpcalc",
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

    # Same durations + identical payloads → 1.0.
    c = "10.0:ZZZZZZZZZZ"
    d = "10.0:ZZZZZZZZZZ"
    assert fingerprint_similarity(c, d) == 1.0


def test_fingerprint_similarity_partial_overlap() -> None:
    """Character-position overlap produces the expected 0.0-1.0 similarity."""
    a = "10.0:ABCDEFGHIJ"
    b = "10.0:ABCDEXXXXX"  # 5 matching / 10 max
    assert fingerprint_similarity(a, b) == 0.5


def test_find_audio_near_duplicates_groups_re_encodes(tmp_path: Path) -> None:
    """Two mp3s that fingerprint identically end up in one group."""
    a = tmp_path / "song_320k.mp3"
    b = tmp_path / "song_192k.mp3"
    for p in (a, b):
        p.write_bytes(b"fake audio")

    records = [_hr(a, h="hash-a"), _hr(b, h="hash-b")]

    def _fake_fp(rec: HashedRecord, _store: Store | None) -> str | None:
        # Same duration + high-overlap fingerprint.
        if rec.path == a:
            return "180.0:ABCDEFGHIJKLMNOPQRST"
        return "180.5:ABCDEFGHIJKLMNOPQRSU"

    with patch(
        "duplicate_cleaner.compare.audio._load_or_compute_fingerprint",
        side_effect=_fake_fp,
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

    def _boom(_rec: HashedRecord, _store: Store | None) -> str | None:  # pragma: no cover
        raise AssertionError("fingerprint should not run below min_size")

    with patch(
        "duplicate_cleaner.compare.audio._load_or_compute_fingerprint",
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

    def _fake_fp(rec: HashedRecord, _store: Store | None) -> str:
        return "120.0:AAAAAAAAAAAAAAAAAAAA"

    with patch(
        "duplicate_cleaner.compare.audio._load_or_compute_fingerprint",
        side_effect=_fake_fp,
    ):
        assert find_audio_near_duplicates(records, similarity_threshold=0.90) == []


def test_find_audio_near_duplicates_ignores_non_audio(tmp_path: Path) -> None:
    """A .jpg record is never considered by the audio grouper."""
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg")
    song = tmp_path / "song.mp3"
    song.write_bytes(b"fake audio")

    calls: list[Path] = []

    def _fake_fp(rec: HashedRecord, _store: Store | None) -> str | None:
        calls.append(rec.path)
        return "60.0:AAAAAA"

    with patch(
        "duplicate_cleaner.compare.audio._load_or_compute_fingerprint",
        side_effect=_fake_fp,
    ):
        find_audio_near_duplicates(
            [_hr(photo, h="a"), _hr(song, h="b")],
            similarity_threshold=0.90,
        )
    assert calls == [song]
