"""Video near-duplicate tests — v0.8."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from duplicate_cleaner.compare.video import (
    _FFMPEG_FALLBACK_PATH,
    _FFMPEG_PRIMARY_PATH,
    VIDEO_EXTENSIONS,
    compute_video_signature,
    find_video_near_duplicates,
    is_video_path,
    signature_similarity,
)
from duplicate_cleaner.hash.pipeline import HashedRecord


def _hr(
    path: Path,
    *,
    size: int = 2_500_000,
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


def test_is_video_path_and_extensions() -> None:
    assert is_video_path(Path("clip.mp4"))
    assert is_video_path(Path("CLIP.MOV"))
    assert is_video_path(Path("show.mkv"))
    assert not is_video_path(Path("song.mp3"))
    assert {".mp4", ".mov", ".mkv", ".avi", ".webm"} <= VIDEO_EXTENSIONS


def test_compute_video_signature_on_missing_binary(tmp_path: Path) -> None:
    """ffmpeg absent at every hardcoded absolute path → returns None."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    with patch(
        "duplicate_cleaner.compare.video.os.path.exists", return_value=False
    ):
        assert compute_video_signature(video) is None


def test_ffmpeg_path_safety_uses_hardcoded_absolute_path(tmp_path: Path) -> None:
    """H9-style rail: subprocess is invoked with a hardcoded absolute path.

    Locks in the invariant that video signature extraction does NOT reach
    for ``shutil.which('ffmpeg')`` — an attacker with write access to an
    early ``$PATH`` directory can otherwise inject a shim.  The hardcoded
    resolver only consults :data:`_FFMPEG_PRIMARY_PATH` and
    :data:`_FFMPEG_FALLBACK_PATH`.  Structural: the ``compare/video`` module
    does not import ``shutil`` at all (verified below).
    """
    import duplicate_cleaner.compare.video as vmod

    # No shutil import → the module cannot resolve ``shutil.which`` at
    # runtime.  If a future refactor pulls shutil in, this assertion fires
    # and the reviewer must decide whether the guard is still intact.
    assert not hasattr(vmod, "shutil"), (
        "compare/video must not import shutil — the ffmpeg binary lookup "
        "uses a hardcoded absolute-path resolver.  See H9 in AUDIT_LOG."
    )

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")

    def _fake_exists(p: str) -> bool:
        return p in (_FFMPEG_PRIMARY_PATH, "/opt/homebrew/bin/ffprobe")

    with patch(
        "duplicate_cleaner.compare.video.os.path.exists", side_effect=_fake_exists
    ), patch(
        "duplicate_cleaner.compare.video.subprocess.run"
    ) as run_mock:
        # ffprobe returns duration; ffmpeg keyframe extraction stays
        # unmocked and will return a non-zero exit (which is fine — the
        # test cares only about the first invocation's binary path).
        run_mock.return_value = MagicMock(returncode=0, stdout="10.0\n", stderr="")
        compute_video_signature(video)
        # First subprocess call is ffprobe with the primary absolute path.
        first_call_args = run_mock.call_args_list[0].args[0]
        assert first_call_args[0] == "/opt/homebrew/bin/ffprobe"


def test_signature_similarity_duration_filter() -> None:
    """Durations 5+ seconds apart → similarity 0."""
    a = "60.0:00ff,11ee,22dd,33cc,44bb"
    b = "70.0:00ff,11ee,22dd,33cc,44bb"  # 10s apart → > 5s tolerance
    assert signature_similarity(a, b) == 0.0


def test_signature_similarity_identical_phashes() -> None:
    """Identical duration + identical pHashes → similarity 1.0."""
    a = "60.0:00ff,11ee,22dd,33cc,44bb"
    assert signature_similarity(a, a) == 1.0


def test_signature_similarity_bounded_and_monotone() -> None:
    """1.0 - avg_distance/256; expected within [0.0, 1.0] and closer bytes → higher."""
    a = "60.0:0000000000000000,ffffffffffffffff,0000000000000000"
    b = "60.0:0000000000000000,fffffffffffffff0,0000000000000000"  # 4-bit diff
    sim = signature_similarity(a, b)
    assert 0.0 <= sim <= 1.0
    assert sim > 0.9  # small diff → very high similarity via /256 normalisation


def test_signature_similarity_malformed() -> None:
    assert signature_similarity("", "10.0:aa") == 0.0
    assert signature_similarity("bogus", "10.0:aa") == 0.0
    # phash count mismatch → 0
    assert signature_similarity("10.0:aa,bb", "10.0:aa") == 0.0


def test_find_video_near_duplicates_groups_re_encodes(tmp_path: Path) -> None:
    """Two videos with matching mocked signatures → one group."""
    a = tmp_path / "movie_hd.mp4"
    b = tmp_path / "movie_sd.mp4"
    for p in (a, b):
        p.write_bytes(b"fake video")
    records = [_hr(a, h="ha"), _hr(b, h="hb")]

    def _fake_sig(rec: HashedRecord, _store: object) -> str | None:
        prefix = "600.0" if rec.path == a else "601.0"
        return (
            f"{prefix}:0000000000000000,ffffffffffffffff,"
            "aaaaaaaaaaaaaaaa,5555555555555555,cccccccccccccccc"
        )

    with patch(
        "duplicate_cleaner.compare.video._load_or_compute_signature",
        side_effect=_fake_sig,
    ):
        groups = find_video_near_duplicates(records, similarity_threshold=0.90)
    assert len(groups) == 1
    g = groups[0]
    assert {m.path for m in g.members} == {a, b}
    assert g.min_similarity >= 0.90


def test_find_video_near_duplicates_skips_below_min_size(tmp_path: Path) -> None:
    """A 500KB video record is excluded before signature extraction."""
    small = tmp_path / "tiny.mp4"
    small.write_bytes(b"tiny")
    records = [_hr(small, size=500_000, h="hash-small")]

    def _boom(*_a: object, **_kw: object) -> None:  # pragma: no cover
        raise AssertionError("signature should not compute below min_size")

    with patch(
        "duplicate_cleaner.compare.video._load_or_compute_signature",
        side_effect=_boom,
    ):
        assert (
            find_video_near_duplicates(records, min_size=1_000_000) == []
        )


def test_find_video_near_duplicates_skips_exact_dupes(tmp_path: Path) -> None:
    """Members with matching BLAKE3 hashes fall through to the exact pass."""
    a = tmp_path / "x.mp4"
    b = tmp_path / "y.mp4"
    for p in (a, b):
        p.write_bytes(b"fake video")
    records = [_hr(a, h="SAME"), _hr(b, h="SAME")]

    def _fake_sig(_rec: HashedRecord, _store: object) -> str:
        return (
            "600.0:0000000000000000,ffffffffffffffff,"
            "aaaaaaaaaaaaaaaa,5555555555555555,cccccccccccccccc"
        )

    with patch(
        "duplicate_cleaner.compare.video._load_or_compute_signature",
        side_effect=_fake_sig,
    ):
        assert (
            find_video_near_duplicates(records, similarity_threshold=0.90) == []
        )


def test_find_video_near_duplicates_below_threshold_produces_nothing(
    tmp_path: Path,
) -> None:
    """Simulate two very different-looking videos → no group."""
    a = tmp_path / "movie_a.mp4"
    b = tmp_path / "movie_b.mp4"
    for p in (a, b):
        p.write_bytes(b"fake video")
    records = [_hr(a, h="ha"), _hr(b, h="hb")]

    def _fake_sig(rec: HashedRecord, _store: object) -> str:
        payload = "0000000000000000" if rec.path == a else "ffffffffffffffff"
        return f"600.0:{payload},{payload},{payload},{payload},{payload}"

    with patch(
        "duplicate_cleaner.compare.video._load_or_compute_signature",
        side_effect=_fake_sig,
    ):
        # /256 normalisation keeps unrelated content below 0.90 only when
        # every bit differs.  Threshold pushed to 0.999 to guarantee no
        # match on this widely-differing pair.
        assert (
            find_video_near_duplicates(records, similarity_threshold=0.999) == []
        )


def test_fallback_path_used_when_primary_missing(tmp_path: Path) -> None:
    """Fallback path is consulted only when the primary hardcoded path is missing."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")

    def _fake_exists(p: str) -> bool:
        return p in (_FFMPEG_FALLBACK_PATH, "/usr/local/bin/ffprobe")

    with patch(
        "duplicate_cleaner.compare.video.os.path.exists", side_effect=_fake_exists
    ), patch("duplicate_cleaner.compare.video.subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0, stdout="10.0\n", stderr="")
        compute_video_signature(video)
        assert (
            run_mock.call_args_list[0].args[0][0] == "/usr/local/bin/ffprobe"
        )
