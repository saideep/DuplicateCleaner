"""Image near-duplicate detection tests — v0.7."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from duplicate_cleaner.compare.image import (
    IMAGE_EXTENSIONS,
    ImageNearDupGroup,
    compute_phash,
    find_image_near_duplicates,
    hamming_distance,
    is_image_path,
)
from duplicate_cleaner.hash.pipeline import HashedRecord
from duplicate_cleaner.store import Store

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hr(
    path: Path,
    *,
    size: int | None = None,
    mtime: float = 1000.0,
    full_hash: str | None = None,
) -> HashedRecord:
    """Build a HashedRecord for a real on-disk file with plausible defaults."""
    st = path.stat()
    return HashedRecord(
        path=path,
        size=size if size is not None else st.st_size,
        mtime=mtime,
        inode=st.st_ino,
        dev=st.st_dev,
        nlink=1,
        full_hash=full_hash if full_hash is not None else f"H:{path.name}",
    )


def _write_gradient(dst: Path, *, width: int = 32, height: int = 32, seed: int = 0) -> None:
    """Write a small deterministic gradient image so pHash is stable across runs."""
    im = Image.new("RGB", (width, height))
    for y in range(height):
        for x in range(width):
            im.putpixel(
                (x, y),
                (
                    (x * 8 + seed) % 256,
                    (y * 8 + seed) % 256,
                    ((x + y) * 4 + seed) % 256,
                ),
            )
    im.save(dst, format="PNG")


def _write_near_dup(src: Path, dst: Path) -> None:
    """Save ``src`` as a 90%-quality JPEG to ``dst`` — a byte-different near-dup."""
    with Image.open(src) as im:
        im.save(dst, format="JPEG", quality=90)


def _write_photo_image(dst: Path, *, size: tuple[int, int] = (128, 128), seed: int = 7) -> None:
    """Write an image varied enough that its pHash isn't trivially all-zeros.

    A flat gradient pHash tends to be dominated by low-frequency components,
    which can collapse many hashes to identical values. This helper mixes in
    per-pixel high-frequency noise so the DCT captures real structure.
    """
    w, h = size
    im = Image.new("RGB", size)
    for y in range(h):
        for x in range(w):
            r = (x * 3 + y * 5 + seed * 11) % 256
            g = (x * 7 + seed * 13) % 256
            b = (y * 11 + seed * 17) % 256
            # Sprinkle a checkerboard so the high frequencies survive.
            checker = 40 if (x // 4 + y // 4) % 2 == 0 else 0
            im.putpixel((x, y), ((r + checker) % 256, (g + checker) % 256, b))
    im.save(dst, format="PNG")


# ---------------------------------------------------------------------------
# compute_phash / hamming_distance / is_image_path
# ---------------------------------------------------------------------------


def test_compute_phash_on_real_image(tmp_path: Path) -> None:
    """compute_phash returns a stable 64-hex-char pHash on a Pillow-writable image."""
    p = tmp_path / "photo.png"
    _write_photo_image(p, seed=1)
    h = compute_phash(p)
    assert h is not None
    assert len(h) == 64
    # Deterministic when re-computed on the same bytes.
    assert compute_phash(p) == h


def test_compute_phash_on_non_image(tmp_path: Path) -> None:
    """A non-image file (or unreadable bytes) yields None instead of raising."""
    p = tmp_path / "not-an-image.png"
    p.write_bytes(b"totally not a png")
    assert compute_phash(p) is None


def test_hamming_distance() -> None:
    """Trivial hex cases lock in the popcount semantics."""
    assert hamming_distance("00", "00") == 0
    assert hamming_distance("00", "01") == 1
    assert hamming_distance("00", "ff") == 8
    assert hamming_distance("ff", "ff") == 0
    assert hamming_distance("0f", "f0") == 8


def test_hamming_distance_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        hamming_distance("00", "0000")


def test_is_image_path_case_insensitive() -> None:
    assert is_image_path(Path("foo.JPG"))
    assert is_image_path(Path("bar.Heic"))
    assert not is_image_path(Path("script.py"))
    assert ".png" in IMAGE_EXTENSIONS


# ---------------------------------------------------------------------------
# find_image_near_duplicates
# ---------------------------------------------------------------------------


def test_find_image_near_duplicates_groups_similar(tmp_path: Path) -> None:
    """A PNG + a 90%-quality JPEG re-encode of it should cluster together."""
    a = tmp_path / "orig.png"
    b = tmp_path / "reencoded.jpg"
    _write_photo_image(a, seed=3)
    _write_near_dup(a, b)
    # Ensure both are above the min-size intake threshold.
    ba = a.read_bytes()
    if len(ba) < 10_000:
        # Bulk the PNG up with a metadata comment via Pillow-independent bytes.
        with Image.open(a) as im:
            im.save(a, format="PNG", pnginfo=None)
    records = [
        _hr(a, size=max(a.stat().st_size, 20_000), full_hash="HA"),
        _hr(b, size=max(b.stat().st_size, 20_000), full_hash="HB"),
    ]
    groups = find_image_near_duplicates(
        records, distance_threshold=8, min_size=len(ba) - 1
    )
    # Skip the assertion if the encoder happened to shift the pHash beyond
    # threshold — but on this deterministic gradient it should cluster.
    assert len(groups) == 1
    g = groups[0]
    assert {rec.path for rec in g.members} == {a, b}
    assert g.max_distance <= 8


def test_find_image_near_duplicates_respects_threshold(tmp_path: Path) -> None:
    """A tighter threshold (distance=0) refuses a real re-encode pair."""
    a = tmp_path / "orig.png"
    b = tmp_path / "reencoded.jpg"
    _write_photo_image(a, seed=5)
    _write_near_dup(a, b)
    records = [
        _hr(a, size=20_000, full_hash="HA"),
        _hr(b, size=20_000, full_hash="HB"),
    ]
    # Only the impossible-to-hit distance=0 threshold can reliably force a
    # miss regardless of the encoder quirks — even 1 bit can slip through.
    # Compute the real distance and pick threshold=distance-1.
    pha = compute_phash(a)
    phb = compute_phash(b)
    assert pha is not None and phb is not None
    real = hamming_distance(pha, phb)
    if real == 0:
        # If somehow the pHashes match exactly, force a mismatch by using a
        # different image for b.
        _write_photo_image(b, seed=99)
        phb = compute_phash(b)
        assert phb is not None
        real = hamming_distance(pha, phb)
    tighter = max(real - 1, 0)
    groups = find_image_near_duplicates(
        records, distance_threshold=tighter, min_size=1_000
    )
    assert groups == []


def test_find_image_near_duplicates_respects_min_size(tmp_path: Path) -> None:
    """Records under min_size are excluded — no near-dup group emitted."""
    a = tmp_path / "tiny.png"
    b = tmp_path / "tiny2.png"
    # Any dimensions — the size we pass in is the record's declared size.
    Image.new("RGB", (8, 8), color=(1, 2, 3)).save(a, format="PNG")
    Image.new("RGB", (8, 8), color=(1, 2, 3)).save(b, format="PNG")
    records = [
        _hr(a, size=100, full_hash="HA"),
        _hr(b, size=100, full_hash="HB"),
    ]
    groups = find_image_near_duplicates(records, min_size=10_000)
    assert groups == []


def test_find_image_near_duplicates_union_find_for_3way(tmp_path: Path) -> None:
    """Three near-dup images collapse to one 3-member group, not 3 pair groups."""
    a = tmp_path / "a.png"
    b = tmp_path / "b.jpg"
    c = tmp_path / "c.jpg"
    _write_photo_image(a, seed=11)
    _write_near_dup(a, b)
    _write_near_dup(a, c)
    records = [
        _hr(a, size=20_000, full_hash="HA"),
        _hr(b, size=20_000, full_hash="HB"),
        _hr(c, size=20_000, full_hash="HC"),
    ]
    groups = find_image_near_duplicates(records, distance_threshold=8, min_size=1_000)
    assert len(groups) == 1
    g = groups[0]
    assert len(g.members) == 3
    assert {rec.path for rec in g.members} == {a, b, c}


def test_image_near_dup_skipped_when_exact_dup_exists(tmp_path: Path) -> None:
    """A near-dup component where every member shares the same BLAKE3 hash is dropped."""
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    _write_photo_image(a, seed=17)
    _write_photo_image(b, seed=17)  # same content → same pHash + same BLAKE3
    records = [
        _hr(a, size=20_000, full_hash="SAMEHASH"),
        _hr(b, size=20_000, full_hash="SAMEHASH"),
    ]
    groups = find_image_near_duplicates(records, distance_threshold=8, min_size=1_000)
    # Component exists (pHashes match) but is skipped because BLAKE3 is
    # identical — those files are already an exact-duplicate group.
    assert groups == []


def test_phash_cache_hit(tmp_path: Path) -> None:
    """Second scan of the same file reuses the cached pHash instead of recomputing."""
    p = tmp_path / "cached.png"
    _write_photo_image(p, seed=23)
    store = Store(tmp_path / "cache.db")
    try:
        rec = _hr(p, size=20_000, full_hash="H1")
        # First call populates the cache — read it back.
        h1 = compute_phash(rec.path)
        assert h1 is not None
        store.put_phash(rec.path, rec.size, rec.mtime, h1)
        cached = store.get_cached_phash(rec.path, rec.size, rec.mtime)
        assert cached == h1
        # A subsequent call to find_image_near_duplicates with the store
        # should hit the cache — the file's pHash is used without needing
        # to re-open the bytes.  Break the file to prove cache-hit — the
        # near-dup grouper must NOT re-decode it.
        p.write_bytes(b"totally corrupted now")
        # But we keep the record's ``size`` + ``mtime`` frozen so the
        # cache key still matches.
        rec2 = _hr(p, size=rec.size, mtime=rec.mtime, full_hash="H1")
        rec2 = HashedRecord(
            path=rec2.path,
            size=rec.size,
            mtime=rec.mtime,
            inode=rec2.inode,
            dev=rec2.dev,
            nlink=1,
            full_hash="H1",
        )
        # A second same-image record with a different BLAKE3 hash so the
        # exact-dup skip doesn't fire.
        q = tmp_path / "sibling.png"
        _write_photo_image(q, seed=23)
        store.put_phash(q, 20_000, 1000.0, h1)
        rec_q = _hr(q, size=20_000, full_hash="H2")
        groups = find_image_near_duplicates(
            [rec2, rec_q], distance_threshold=8, min_size=1_000, store=store
        )
        # Cache hit means we still cluster them despite the corrupted p.
        assert len(groups) == 1
    finally:
        store.close()


def test_find_image_near_duplicates_skips_cloud_records(tmp_path: Path) -> None:
    """Cloud (source_id != 'local') records are skipped — v0.7 is local-only."""
    a = tmp_path / "a.png"
    _write_photo_image(a, seed=29)
    b = Path("gdrive:personal:///b.png")  # opaque cloud path
    records = [
        _hr(a, size=20_000, full_hash="HA"),
        HashedRecord(
            path=b,
            size=20_000,
            mtime=1000.0,
            inode=0,
            dev=0,
            nlink=1,
            full_hash="HB",
            source_id="gdrive:personal",
        ),
    ]
    # Cloud is filtered at intake regardless of whether it could be pHashed.
    groups = find_image_near_duplicates(records, distance_threshold=8, min_size=1_000)
    assert groups == []


def test_find_image_near_duplicates_non_image_extensions_ignored(tmp_path: Path) -> None:
    """Records with non-image extensions never enter the pHash decode path."""
    p = tmp_path / "not-image.txt"
    p.write_bytes(b"some text bytes of adequate size" * 400)
    records = [_hr(p, size=20_000, full_hash="HX")]
    groups = find_image_near_duplicates(records, min_size=1_000)
    assert groups == []


def test_find_image_near_duplicates_result_shape(tmp_path: Path) -> None:
    """ImageNearDupGroup carries max_distance, size_range, and per-member pHashes."""
    a = tmp_path / "a.png"
    b = tmp_path / "b.jpg"
    _write_photo_image(a, seed=31)
    _write_near_dup(a, b)
    records = [
        _hr(a, size=25_000, full_hash="HA"),
        _hr(b, size=15_000, full_hash="HB"),
    ]
    groups = find_image_near_duplicates(records, distance_threshold=8, min_size=1_000)
    assert len(groups) == 1
    g = groups[0]
    assert isinstance(g, ImageNearDupGroup)
    assert g.size_range == (15_000, 25_000)
    assert g.max_distance >= 0
    assert len(g.phashes) == len(g.members) == 2
    assert all(isinstance(h, str) and len(h) == 64 for h in g.phashes)


# ---------------------------------------------------------------------------
# Store integration
# ---------------------------------------------------------------------------


def test_store_get_cached_phash_returns_none_on_miss(tmp_path: Path) -> None:
    store = Store(tmp_path / "cache.db")
    try:
        assert store.get_cached_phash(Path("/nonexistent"), 100, 1.0) is None
    finally:
        store.close()


def test_store_put_and_get_phash_roundtrip(tmp_path: Path) -> None:
    store = Store(tmp_path / "cache.db")
    try:
        p = tmp_path / "foo.png"
        p.write_bytes(b"x" * 100)
        store.put_phash(p, 100, 42.0, "deadbeef")
        assert store.get_cached_phash(p, 100, 42.0) == "deadbeef"
        # Stat mismatch → miss.
        assert store.get_cached_phash(p, 100, 999.0) is None
        assert store.get_cached_phash(p, 200, 42.0) is None
    finally:
        store.close()


def test_store_purge_stale_phashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """purge_stale_phashes drops rows whose computed_ts is older than the cutoff."""
    import time as _time

    store = Store(tmp_path / "cache.db")
    try:
        # Freeze time backward so the row is stamped in the past.
        fake_now = 1_000_000_000.0
        monkeypatch.setattr(_time, "time", lambda: fake_now)
        store.put_phash(Path("/tmp/old.png"), 10, 1.0, "abcd")
        # Sweep with a zero-day TTL — should reap the row.
        monkeypatch.setattr(_time, "time", lambda: fake_now + 86400 * 30)
        dropped = store.purge_stale_phashes(max_age_days=1.0)
        assert dropped == 1
        assert store.get_cached_phash(Path("/tmp/old.png"), 10, 1.0) is None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# hash/pipeline.compute_phash wrapper
# ---------------------------------------------------------------------------


def test_hash_pipeline_compute_phash_wrapper(tmp_path: Path) -> None:
    """The hash-pipeline entry point delegates to the compare.image implementation."""
    from duplicate_cleaner.hash.pipeline import compute_phash as pipeline_phash

    p = tmp_path / "wrap.png"
    _write_photo_image(p, seed=37)
    direct = compute_phash(p)
    via_pipeline = pipeline_phash(p)
    assert direct == via_pipeline
    assert direct is not None


# ---------------------------------------------------------------------------
# CLI end-to-end: dc scan emits image_near_dup_groups
# ---------------------------------------------------------------------------


def _invoke_scan_with_config(
    tmp_path: Path,
    scan_roots: list[Path],
    report_dir: Path,
    active_homes: list[Path],
    extra_args: list[str] | None = None,
) -> object:
    """Run ``dc scan`` under a temp config + cache directory.

    Mirrors ``tests/test_compare_tree.py``'s helper so behaviour matches
    the v0.4 CLI tests bit-for-bit.
    """
    from unittest.mock import patch

    from typer.testing import CliRunner

    from duplicate_cleaner.cli import app
    from duplicate_cleaner.config import load_config as real_load

    cfg_path = tmp_path / "cfg" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    homes_toml = ", ".join(f'"{h}"' for h in active_homes)
    cfg_path.write_text(f"active_homes = [{homes_toml}]\nmin_size_bytes = 1\n")
    cache_dir = tmp_path / "cache"

    runner = CliRunner()
    with patch("duplicate_cleaner.cli.load_config") as lc, \
         patch("duplicate_cleaner.cli.CONFIG_PATH", cfg_path), \
         patch("duplicate_cleaner.cli.CACHE_DIR", cache_dir):
        lc.side_effect = lambda: real_load(cfg_path)
        args: list[str] = ["scan"] + [str(r) for r in scan_roots] + [
            "--report",
            str(report_dir),
            "--min-size",
            "0",
        ]
        if extra_args:
            args.extend(extra_args)
        return runner.invoke(app, args, catch_exceptions=False)


def test_cli_scan_emits_image_near_dup_groups(tmp_path: Path) -> None:
    """dc scan on a tree with two near-duplicate images emits an image_near_dup_group."""
    import json

    scan_root = tmp_path / "photos"
    scan_root.mkdir()
    a = scan_root / "orig.png"
    b = scan_root / "reencoded.jpg"
    _write_photo_image(a, seed=41)
    _write_near_dup(a, b)

    r = _invoke_scan_with_config(
        tmp_path,
        [scan_root],
        tmp_path / "report",
        [scan_root],
        extra_args=["--image-near-dup-min-size", "100"],
    )
    assert getattr(r, "exit_code", 1) == 0, getattr(r, "output", "")
    with (tmp_path / "report" / "report.json").open() as f:
        data = json.load(f)
    groups = data.get("image_near_dup_groups", [])
    assert groups, f"Expected an image-near-dup group; got: {data.get('groups')}"
    g = groups[0]
    assert g["kind"] == "image-near-dup"
    assert len(g["members"]) == 2
    assert g.get("image_near_dup") is not None
    assert g["image_near_dup"]["hash_bits"] == 256


def test_cli_scan_no_include_image_near_dup_flag_disables_it(tmp_path: Path) -> None:
    """--no-include-image-near-dup keeps the near-dup rail dark."""
    import json

    scan_root = tmp_path / "photos"
    scan_root.mkdir()
    a = scan_root / "orig.png"
    b = scan_root / "reencoded.jpg"
    _write_photo_image(a, seed=43)
    _write_near_dup(a, b)

    r = _invoke_scan_with_config(
        tmp_path,
        [scan_root],
        tmp_path / "report",
        [scan_root],
        extra_args=[
            "--no-include-image-near-dup",
            "--image-near-dup-min-size",
            "100",
        ],
    )
    assert getattr(r, "exit_code", 1) == 0, getattr(r, "output", "")
    with (tmp_path / "report" / "report.json").open() as f:
        data = json.load(f)
    assert data.get("image_near_dup_groups", []) == []
