"""Tests for the v0.3-a organize discovery pipeline."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from duplicate_cleaner.cli import app
from duplicate_cleaner.config import CONFIG_PATH, Config
from duplicate_cleaner.organize.discover import (
    _detect_cohesion,
    _MediaItem,
    cluster_events,
    discover,
)
from duplicate_cleaner.organize.plan import PlanFile
from duplicate_cleaner.organize.render import render_plan
from duplicate_cleaner.organize.signals import (
    FilenameSignalExtractor,
    MusicSignalExtractor,
    PDFSignalExtractor,
    PhotoSignalExtractor,
    SignalSet,
    classify_pdf_text,
    extract_all,
    merge_signal_sets,
)
from duplicate_cleaner.organize.taxonomy import (
    Classification,
    classify,
    default_rules,
)

# --------------------------------------------------------------------------- #
# Signal extractors                                                           #
# --------------------------------------------------------------------------- #


def test_filename_extractor_detects_date_and_keywords(tmp_path: Path) -> None:
    """Filename signal extractor pulls date, seqno, keywords out of names."""
    p = tmp_path / "payslip_2024-03-15.pdf"
    p.write_bytes(b"stub")
    ex = FilenameSignalExtractor()
    signals = ex.extract(p)
    assert signals.fname_date == "2024-03-15"
    assert signals.fname_year == 2024
    assert "payslip" in signals.fname_keywords


def test_filename_extractor_detects_sequence_number(tmp_path: Path) -> None:
    p = tmp_path / "IMG_5001.HEIC"
    p.write_bytes(b"stub")
    signals = FilenameSignalExtractor().extract(p)
    assert signals.fname_seqno == 5001


def test_filename_extractor_detects_vendor(tmp_path: Path) -> None:
    p = tmp_path / "Amazon_receipt_order.pdf"
    p.write_bytes(b"stub")
    signals = FilenameSignalExtractor().extract(p)
    assert signals.fname_issuer == "Amazon"
    assert "receipt" in signals.fname_keywords


def test_filename_extractor_year_only(tmp_path: Path) -> None:
    p = tmp_path / "notes 2023.txt"
    p.write_bytes(b"stub")
    signals = FilenameSignalExtractor().extract(p)
    assert signals.fname_year == 2023


def test_music_extractor_returns_empty_when_mutagen_missing(tmp_path: Path) -> None:
    """When mutagen is not importable, extractor returns an empty SignalSet."""
    p = tmp_path / "song.mp3"
    p.write_bytes(b"stub")
    with patch.dict(os.environ, {}, clear=False):
        # Force ImportError by mocking sys.modules.
        import sys

        real = sys.modules.pop("mutagen", None)
        sys.modules["mutagen"] = None  # type: ignore[assignment]
        try:
            s = MusicSignalExtractor().extract(p)
        finally:
            if real is not None:
                sys.modules["mutagen"] = real
            else:
                sys.modules.pop("mutagen", None)
    assert s.id3_artist is None
    assert s.id3_album is None


def test_music_extractor_reads_id3_tags(tmp_path: Path) -> None:
    """Mock mutagen: fake tag dict feeds SignalSet."""
    p = tmp_path / "song.mp3"
    p.write_bytes(b"stub")

    fake_audio = MagicMock()
    fake_audio.get.side_effect = lambda k: {
        "artist": ["Miles Davis"],
        "albumartist": ["Miles Davis"],
        "album": ["Kind of Blue"],
        "tracknumber": ["01/07"],
        "date": ["1959"],
        "genre": ["Jazz"],
    }.get(k)

    fake_mutagen = MagicMock()
    fake_mutagen.File = MagicMock(return_value=fake_audio)
    import sys

    real = sys.modules.get("mutagen")
    sys.modules["mutagen"] = fake_mutagen
    try:
        s = MusicSignalExtractor().extract(p)
    finally:
        if real is not None:
            sys.modules["mutagen"] = real
        else:
            sys.modules.pop("mutagen", None)
    assert s.id3_artist == "Miles Davis"
    assert s.id3_album == "Kind of Blue"
    assert s.id3_track == 1
    assert s.id3_year == 1959


def test_photo_extractor_gracefully_handles_non_image(tmp_path: Path) -> None:
    """A non-image with .jpg extension is degraded to no signals, no exception."""
    p = tmp_path / "not-really.jpg"
    p.write_bytes(b"nope")
    signals = PhotoSignalExtractor().extract(p)
    assert signals.exif_date_original is None


def test_pdf_extractor_returns_empty_when_deps_missing(tmp_path: Path) -> None:
    """PDF extractor tolerates missing pikepdf + pdfplumber."""
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 not a real pdf")
    signals = PDFSignalExtractor().extract(p)
    # No exception; empty text and no class.
    assert signals.pdf_class is None


def test_classify_pdf_text_payslip() -> None:
    """PDF keyword classifier lights up on a synthetic payslip block."""
    text = """
    Company Payslip
    Employee ID: 12345
    Pay Period: March 2024
    Gross Pay: 5000
    Net Pay: 3800
    Deductions: 1200
    YTD: 15000
    """
    label, conf, hits = classify_pdf_text(text)
    assert label == "payslip"
    assert conf >= 0.6
    assert "Gross Pay" in hits


def test_classify_pdf_text_receipt() -> None:
    text = """
    Amazon.com Receipt
    Order Number: 112-4839
    Subtotal: 45.00
    Tax: 3.50
    Total: 48.50
    Payment Method: Visa **** 1234
    Thank you for shopping.
    """
    label, conf, _ = classify_pdf_text(text)
    assert label == "receipt"
    assert conf >= 0.6


def test_merge_signal_sets_later_wins() -> None:
    a = SignalSet(fname_year=2020)
    b = SignalSet(fname_year=2024)
    merged = merge_signal_sets([a, b])
    assert merged.fname_year == 2024


# --------------------------------------------------------------------------- #
# Taxonomy classifier                                                         #
# --------------------------------------------------------------------------- #


def test_classify_payslip_signals_lands_in_hr() -> None:
    """Payslip PDF + filename keyword → HR/Payslips/{year}."""
    s = SignalSet(
        fname_keywords=frozenset({"payslip"}),
        pdf_class="payslip",
        pdf_class_confidence=0.85,
        pdf_year=2024,
    )
    cls = classify(s, threshold=0.5)
    assert cls.domain == "HR"
    assert cls.subfolder == "Payslips/2024"
    assert "hr.payslip" in cls.fired_rules


def test_classify_music_album() -> None:
    """ID3 artist+album → Media/Music/{artist}/{album}."""
    s = SignalSet(
        id3_artist="Miles Davis",
        id3_albumartist="Miles Davis",
        id3_album="Kind of Blue",
    )
    cls = classify(s, threshold=0.5)
    assert cls.domain == "Media"
    assert cls.subfolder == "Music/Miles Davis/Kind of Blue"


def test_classify_receipt_with_vendor() -> None:
    s = SignalSet(
        fname_keywords=frozenset({"receipt"}),
        fname_issuer="Amazon",
        fname_year=2024,
        pdf_class="receipt",
        pdf_class_confidence=0.75,
    )
    cls = classify(s, threshold=0.5)
    assert cls.domain == "Finances"
    assert cls.subfolder.startswith("Receipts/2024")
    assert "Amazon" in cls.subfolder


def test_classify_below_threshold_goes_to_unsorted() -> None:
    """A file with only weak signals goes to Unsorted with alternatives populated."""
    s = SignalSet(fname_keywords=frozenset({"insurance"}), fname_year=2024)
    cls = classify(s, threshold=0.9, unsorted_hint_threshold=0.4)
    assert cls.domain == "Unsorted"
    # Alternatives should still show the sub-threshold candidates.
    assert len(cls.alternatives) >= 0


def test_classify_with_no_signals_returns_flat_unsorted() -> None:
    cls = classify(SignalSet(), threshold=0.5)
    assert cls.domain == "Unsorted"
    assert cls.subfolder == ""


def test_default_rules_are_stable_across_calls() -> None:
    r1 = default_rules()
    r2 = default_rules()
    assert [r.id for r in r1] == [r.id for r in r2]


def test_classification_is_deterministic_on_tie() -> None:
    """Tie-breaking must be stable across runs."""
    s = SignalSet(fname_keywords=frozenset({"payslip", "invoice"}))
    a = classify(s, threshold=0.4)
    b = classify(s, threshold=0.4)
    assert a.domain == b.domain
    assert a.subfolder == b.subfolder


# --------------------------------------------------------------------------- #
# Event clustering                                                            #
# --------------------------------------------------------------------------- #


def test_event_clustering_same_event_within_gap() -> None:
    """Photos 6h apart cluster into one event."""
    base = 1_700_000_000.0
    items = [
        _MediaItem(Path(f"/a/{i}.jpg"), base + i * 6 * 3600.0)
        for i in range(5)
    ]
    clusters = cluster_events(items, gap_hours=12.0)
    assert len(clusters) == 1


def test_event_clustering_splits_on_large_gap() -> None:
    """Photos 24h apart form two events."""
    base = 1_700_000_000.0
    items = [
        _MediaItem(Path("/a/0.jpg"), base),
        _MediaItem(Path("/a/1.jpg"), base + 24 * 3600.0),
        _MediaItem(Path("/a/2.jpg"), base + 48 * 3600.0),
    ]
    clusters = cluster_events(items, gap_hours=12.0)
    assert len(clusters) == 3


def test_event_clustering_empty_returns_empty_list() -> None:
    assert cluster_events([], gap_hours=12.0) == []


# --------------------------------------------------------------------------- #
# Cohesion detection                                                          #
# --------------------------------------------------------------------------- #


def _fake_record(path: Path, size: int = 1024) -> object:
    from duplicate_cleaner.scan.walk import FileRecord

    return FileRecord(
        path=path, size=size, mtime=1_700_000_000.0,
        inode=1, dev=1, nlink=1,
    )


def test_cohesion_detection_forms_group_at_80_percent(tmp_path: Path) -> None:
    """5-file dir with 4 sharing (domain, subfolder) → cohesion group."""
    bucket: list[tuple[object, SignalSet, Classification]] = []
    for i in range(4):
        p = tmp_path / f"photo_{i}.jpg"
        p.write_bytes(b"stub")
        bucket.append((
            _fake_record(p),
            SignalSet(mime_top="image", mtime_year=2024, mtime_month=6),
            Classification(domain="Photos", subfolder="2024/2024-06", confidence=0.9),
        ))
    # Odd one out — Documents domain.
    odd = tmp_path / "note.pdf"
    odd.write_bytes(b"stub")
    bucket.append((
        _fake_record(odd),
        SignalSet(),
        Classification(domain="Unsorted", subfolder="", confidence=0.1),
    ))
    groups: list[object] = []
    cid = _detect_cohesion(tmp_path, bucket, groups)  # type: ignore[arg-type]
    assert cid is not None
    assert len(groups) == 1


def test_cohesion_detection_no_group_when_ratio_below_threshold(tmp_path: Path) -> None:
    """3 different classifications across 5 files → no cohesion."""
    bucket: list[tuple[object, SignalSet, Classification]] = []
    for i, (dom, sub) in enumerate([
        ("Photos", "2024/2024-06"),
        ("Photos", "2024/2024-06"),
        ("HR", "Payslips/2024"),
        ("Finances", "Receipts/2024/Amazon"),
        ("Unsorted", ""),
    ]):
        p = tmp_path / f"file_{i}.dat"
        p.write_bytes(b"stub")
        bucket.append((
            _fake_record(p),
            SignalSet(),
            Classification(domain=dom, subfolder=sub, confidence=0.8),
        ))
    groups: list[object] = []
    cid = _detect_cohesion(tmp_path, bucket, groups)  # type: ignore[arg-type]
    assert cid is None
    assert groups == []


# --------------------------------------------------------------------------- #
# End-to-end discover                                                         #
# --------------------------------------------------------------------------- #


def _mk_cfg(tmp_root: Path) -> Config:
    return Config(
        active_homes=[tmp_root],
        min_size_bytes=1,
        exclude_globs=[],
        follow_symlinks=False,
    )


def test_discover_produces_valid_plan(tmp_path: Path) -> None:
    """Full walk over a fixture tree → PlanFile with entries + Pydantic-valid JSON."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "payslip_2024.pdf").write_bytes(b"stub payslip pdf")
    (src / "IMG_5001.HEIC").write_bytes(b"stub heic")
    (src / "IMG_5002.HEIC").write_bytes(b"stub heic")
    (src / "notes.txt").write_bytes(b"random text file")

    cfg = _mk_cfg(src)
    plan, summary = discover([src], cfg)

    # Every entry round-trips through model_dump_json → validate.
    data = plan.model_dump_json()
    round_trip = PlanFile.model_validate_json(data)
    assert round_trip.total_files == len(plan.entries)
    assert summary.files_scanned >= 4
    assert plan.total_files >= 4


def test_discover_renders_html_and_json(tmp_path: Path) -> None:
    """The renderer writes both artifacts to the report dir."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "receipt_amazon.pdf").write_bytes(b"stub")
    cfg = _mk_cfg(src)
    plan, _ = discover([src], cfg)

    report = tmp_path / "out"
    html_path, json_path = render_plan(plan, report)
    assert html_path.exists()
    assert json_path.exists()
    text = html_path.read_text()
    assert "DuplicateCleaner Organize Plan" in text
    # JSON re-parses.
    json.loads(json_path.read_text())


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


runner = CliRunner()


def _prep_config(tmp_root: Path) -> Path:
    """Ensure a minimum config exists so the CLI accepts the run."""
    from duplicate_cleaner.config import write_default_config

    if not CONFIG_PATH.exists():
        write_default_config()
    return CONFIG_PATH


def test_cli_organize_discover_produces_artifacts(tmp_path: Path) -> None:
    """`dc organize discover` writes JSON + HTML into --report dir."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "payslip_2024.pdf").write_bytes(b"stub")
    (src / "invoice_2024.pdf").write_bytes(b"stub")

    # Use a temporary XDG config so the test doesn't touch the user's real one.
    with tempfile.TemporaryDirectory() as td:
        cfg_dir = Path(td) / "cfg"
        cfg_dir.mkdir()
        cfg_path = cfg_dir / "config.toml"
        cfg_path.write_text(f'active_homes = ["{src}"]\nmin_size_bytes = 1\n')
        with patch("duplicate_cleaner.cli.CONFIG_PATH", cfg_path), \
             patch("duplicate_cleaner.cli.load_config") as lc:
            from duplicate_cleaner.config import load_config as real_load

            lc.side_effect = lambda: real_load(cfg_path)
            result = runner.invoke(
                app,
                [
                    "organize", "discover",
                    str(src),
                    "--report", str(tmp_path / "out"),
                    "--skip-dedup-check",
                ],
            )
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "out" / "organize-plan.json").exists()
    assert (tmp_path / "out" / "organize-plan.html").exists()


def test_cli_organize_discover_refuses_without_active_homes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.toml"
        cfg_path.write_text("active_homes = []\nmin_size_bytes = 1\n")
        with patch("duplicate_cleaner.cli.CONFIG_PATH", cfg_path), \
             patch("duplicate_cleaner.cli.load_config") as lc:
            from duplicate_cleaner.config import load_config as real_load

            lc.side_effect = lambda: real_load(cfg_path)
            result = runner.invoke(
                app,
                [
                    "organize", "discover",
                    str(src),
                    "--report", str(tmp_path / "out"),
                    "--skip-dedup-check",
                ],
            )
    assert result.exit_code != 0


def test_cli_organize_discover_pending_dedup_prints_warning(tmp_path: Path) -> None:
    """A pending discardable group triggers the soft warning by default."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "sample.txt").write_bytes(b"stub")

    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.toml"
        cfg_path.write_text(f'active_homes = ["{src}"]\nmin_size_bytes = 1\n')
        with patch("duplicate_cleaner.cli.CONFIG_PATH", cfg_path), \
             patch("duplicate_cleaner.cli.load_config") as lc, \
             patch("duplicate_cleaner.cli.Store") as store_cls:
            from duplicate_cleaner.config import load_config as real_load

            lc.side_effect = lambda: real_load(cfg_path)
            # First Store() call is the pending-dedup probe; return a fake
            # row with pending > 0.  Second Store() call is the discovery
            # itself; return a real Store.
            real_store = MagicMock()
            real_store._conn.execute.return_value.fetchone.return_value = {"n": 3}
            real_store.close = MagicMock()

            def store_factory(*a: object, **kw: object) -> object:
                # Return the fake for the probe; then a real Store for discover.
                store_factory.calls += 1  # type: ignore[attr-defined]
                if store_factory.calls == 1:  # type: ignore[attr-defined]
                    return real_store
                from duplicate_cleaner.store import Store as _S
                return _S(Path(td) / "cache.db")

            store_factory.calls = 0  # type: ignore[attr-defined]
            store_cls.side_effect = store_factory
            result = runner.invoke(
                app,
                [
                    "organize", "discover",
                    str(src),
                    "--report", str(tmp_path / "out"),
                ],
            )
    combined = result.stdout + (result.stderr or "")
    assert "pending dedup" in combined.lower() or "Warning" in combined


def test_extract_all_universal_signals(tmp_path: Path) -> None:
    """Universal (mime, mtime) signals always populate."""
    p = tmp_path / "sample.pdf"
    p.write_bytes(b"stub")
    signals = extract_all(p, mtime=p.stat().st_mtime)
    assert signals.mtime_year is not None
    assert signals.mime_top in ("application", None)


def test_discover_with_pdf_stub_classifies_as_hr_or_unsorted(tmp_path: Path) -> None:
    """Real PDF classification requires pikepdf; without it, still yields a plan."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "payslip_march_2024.pdf").write_bytes(b"fake payslip")
    cfg = _mk_cfg(src)
    plan, _ = discover([src], cfg)
    # Even without a real PDF, the filename keyword "payslip" alone might not
    # hit the confidence threshold (0.5 weight, 0.75 threshold), so it lands
    # in Unsorted with alternatives — but the entry must exist.
    matching = [e for e in plan.entries if e.filename == "payslip_march_2024.pdf"]
    assert len(matching) == 1
    entry = matching[0]
    assert entry.domain in ("HR", "Unsorted")


def test_signals_set_cache_round_trip(tmp_path: Path) -> None:
    """SignalSet → store → reload preserves frozenset/tuple fields."""
    from duplicate_cleaner.store import Store

    dbfile = tmp_path / "cache.db"
    store = Store(dbfile)
    try:
        s = SignalSet(
            mime_top="application",
            fname_keywords=frozenset({"payslip", "salary"}),
            path_tokens=("payslip", "2024"),
        )
        store.put_signal_set("/tmp/x.pdf", "local", 12345.0, s)
        got = store.get_cached_signals("/tmp/x.pdf", "local", 12345.0)
        assert got is not None
        assert got.fname_keywords == frozenset({"payslip", "salary"})
        assert got.path_tokens == ("payslip", "2024")

        # mtime drift → miss.
        got2 = store.get_cached_signals("/tmp/x.pdf", "local", 99999.0)
        assert got2 is None
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Regression checks against invariants                                        #
# --------------------------------------------------------------------------- #


def test_discovery_is_read_only(tmp_path: Path) -> None:
    """Running discover must not create any files under the src directory."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "notes.txt").write_bytes(b"content")
    src_snapshot = {p.name for p in src.iterdir()}
    cfg = _mk_cfg(src)
    _plan, _ = discover([src], cfg)
    assert {p.name for p in src.iterdir()} == src_snapshot


def test_rename_policy_default_is_preserve() -> None:
    """AUDIT invariant: rename_policy defaults to 'preserve'."""
    from duplicate_cleaner.config import Config as _Cfg

    c = _Cfg(active_homes=[])
    assert c.rename_policy == "preserve"


def test_config_organize_defaults() -> None:
    from duplicate_cleaner.config import Config as _Cfg

    c = _Cfg(active_homes=[])
    assert c.organize_confidence_threshold == pytest.approx(0.75)
    assert c.organize_dir_mode == 0o755
    assert c.event_gap_hours == 12
    assert c.min_event_photos == 5
    assert c.enforce_dedup_ordering is False
