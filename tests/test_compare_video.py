"""Video near-duplicate tests — v0.8."""
from __future__ import annotations

import random
from pathlib import Path
from unittest.mock import MagicMock, patch

from duplicate_cleaner.compare.video import (
    _FFMPEG_FALLBACK_PATH,
    _FFMPEG_PRIMARY_PATH,
    _PHASH_BIT_BUDGET,
    _PHASH_HASH_SIZE,
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


def _phash_hex(seed: int) -> str:
    """Return a 256-bit random-looking pHash (64 hex chars) for tests."""
    r = random.Random(seed)
    return "".join(r.choice("0123456789abcdef") for _ in range(64))


def test_signature_similarity_duration_filter() -> None:
    """Durations 5+ seconds apart → similarity 0."""
    p = _phash_hex(1)
    a = f"60.0:{p},{p},{p},{p},{p}"
    b = f"70.0:{p},{p},{p},{p},{p}"  # 10s apart → > 5s tolerance
    assert signature_similarity(a, b) == 0.0


def test_signature_similarity_identical_phashes() -> None:
    """Identical duration + identical pHashes → similarity 1.0."""
    p = _phash_hex(2)
    a = f"60.0:{p},{p},{p},{p},{p}"
    assert signature_similarity(a, a) == 1.0


def test_signature_similarity_bounded_and_monotone() -> None:
    """1.0 - avg_distance/256; expected within [0.0, 1.0] and closer bytes → higher."""
    p = "0" * 64
    q = "0" * 63 + "1"  # 1 bit difference in one 256-bit pHash
    a = f"60.0:{p},{p},{p}"
    b = f"60.0:{p},{q},{p}"
    sim = signature_similarity(a, b)
    assert 0.0 <= sim <= 1.0
    # avg distance = 1/3, /256 → sim ≈ 0.9987
    assert sim > 0.99


def test_signature_similarity_malformed() -> None:
    p = _phash_hex(3)
    assert signature_similarity("", f"10.0:{p}") == 0.0
    assert signature_similarity("bogus", f"10.0:{p}") == 0.0
    # phash count mismatch → 0
    assert signature_similarity(f"10.0:{p},{p}", f"10.0:{p}") == 0.0


def test_video_similarity_bit_scale_matches_hash_size() -> None:
    """Two random 256-bit pHashes score ~0.5, not 0.87.

    Locks in the fix for audit pass 17 N3: the similarity normalises by
    ``_PHASH_BIT_BUDGET`` (== ``_PHASH_HASH_SIZE * _PHASH_HASH_SIZE`` =
    256 for hash_size=16).  Two random pHashes flip ~half of the bits on
    average, so the normalised similarity sits near 0.5 — not the 0.87
    the old ``avg / 256`` + 64-bit-hash mismatch produced.  Threshold
    0.90 now has real bite instead of admitting any random pair.
    """
    assert _PHASH_HASH_SIZE == 16
    assert _PHASH_BIT_BUDGET == 256

    def _rand_phash(rng: random.Random) -> str:
        return "".join(rng.choice("0123456789abcdef") for _ in range(64))

    trials = 32
    total = 0.0
    for seed in range(trials):
        rng_a = random.Random(seed * 2)
        rng_b = random.Random(seed * 2 + 1)
        a = "60.0:" + ",".join(_rand_phash(rng_a) for _ in range(5))
        b = "60.0:" + ",".join(_rand_phash(rng_b) for _ in range(5))
        total += signature_similarity(a, b)
    avg_sim = total / trials
    # Random pHashes → ~50% bits flipped → similarity ≈ 0.5.  A small
    # tolerance handles the RNG variance without letting the assertion
    # slide back toward the old ~0.87 broken regime.
    assert 0.40 <= avg_sim <= 0.60, (
        f"Random-input similarity {avg_sim:.3f} not near 0.5; the "
        "video similarity normaliser drifted away from the actual bit budget."
    )


def test_find_video_near_duplicates_groups_re_encodes(tmp_path: Path) -> None:
    """Two videos with matching mocked signatures → one group."""
    a = tmp_path / "movie_hd.mp4"
    b = tmp_path / "movie_sd.mp4"
    for p in (a, b):
        p.write_bytes(b"fake video")
    records = [_hr(a, h="ha"), _hr(b, h="hb")]

    phash = _phash_hex(11)

    def _fake_sig(path: Path, *, store: object = None) -> str | None:
        prefix = "600.0" if path == a else "601.0"
        return f"{prefix}:{phash},{phash},{phash},{phash},{phash}"

    with patch(
        "duplicate_cleaner.compare.video.compute_video_signature",
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
        "duplicate_cleaner.compare.video.compute_video_signature",
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

    phash = _phash_hex(12)

    def _fake_sig(_path: Path, *, store: object = None) -> str:
        return f"600.0:{phash},{phash},{phash},{phash},{phash}"

    with patch(
        "duplicate_cleaner.compare.video.compute_video_signature",
        side_effect=_fake_sig,
    ):
        assert (
            find_video_near_duplicates(records, similarity_threshold=0.90) == []
        )


def test_find_video_near_duplicates_below_threshold_produces_nothing(
    tmp_path: Path,
) -> None:
    """Two visually-unrelated videos → no group at the default threshold.

    Audit pass 17 N3: with hash_size=16 → 256-bit budget, all-zeros vs
    all-ones pHashes score similarity 0.0 (all bits differ).  The old
    /256 + 64-bit-hash mismatch scored the same pair at 0.75 — a
    conservative default threshold could not reject it.  With the
    corrected normaliser the default 0.90 threshold naturally rejects
    unrelated pairs; no ``0.999`` workaround needed.
    """
    a = tmp_path / "movie_a.mp4"
    b = tmp_path / "movie_b.mp4"
    for p in (a, b):
        p.write_bytes(b"fake video")
    records = [_hr(a, h="ha"), _hr(b, h="hb")]

    def _fake_sig(path: Path, *, store: object = None) -> str:
        payload = "0" * 64 if path == a else "f" * 64
        return f"600.0:{payload},{payload},{payload},{payload},{payload}"

    with patch(
        "duplicate_cleaner.compare.video.compute_video_signature",
        side_effect=_fake_sig,
    ):
        assert (
            find_video_near_duplicates(records, similarity_threshold=0.90) == []
        )


# ---------------------------------------------------------------------------
# CLI end-to-end: dc scan emits video_near_dup_groups
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


def test_cli_scan_emits_video_near_dup_groups(tmp_path: Path) -> None:
    """dc scan on a pair of near-duplicate videos emits a video-near-dup group."""
    import json

    from duplicate_cleaner.compare.video import VideoNearDupGroup

    scan_root = tmp_path / "movies"
    scan_root.mkdir()
    a = scan_root / "clip_hd.mp4"
    b = scan_root / "clip_sd.mp4"
    a.write_bytes(b"\x00" * 2_500_000)
    b.write_bytes(b"\x01" * 2_000_000)

    def _fake_find(records: object, **_kw: object) -> list[VideoNearDupGroup]:
        rec_list = list(records)
        members = [
            r for r in rec_list if r.path.suffix.lower() == ".mp4"
        ]
        if len(members) < 2:
            return []
        return [
            VideoNearDupGroup(
                members=members,
                min_similarity=0.94,
                duration_range=(600.0, 600.5),
                signatures=[
                    f"600.0:{'a' * 64},{'b' * 64}",
                    f"600.5:{'a' * 64},{'b' * 64}",
                ],
            )
        ]

    with patch(
        "duplicate_cleaner.cli.find_video_near_duplicates",
        side_effect=_fake_find,
    ), patch(
        "duplicate_cleaner.cli.is_ffmpeg_available",
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
    groups = data.get("video_near_dup_groups", [])
    assert groups, f"Expected a video-near-dup group; got: {data}"
    g = groups[0]
    assert g["kind"] == "video-near-dup"
    assert len(g["members"]) == 2
    assert g.get("video_near_dup") is not None
    assert g["video_near_dup"]["min_similarity"] == 0.94


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
