"""Signal extractors — pull structured metadata from files for taxonomy classification.

Each extractor implements :class:`SignalExtractor` and returns a
:class:`SignalSet`.  Heavy third-party imports (``mutagen``, ``pikepdf``,
``pdfplumber``, ``hachoir``, ``PIL``) are done lazily inside the extract
methods so unit tests can import this module without the optional deps
present, and so importing ``organize.signals`` at CLI startup has zero cost.

Read-only by contract: extractors NEVER write to the filesystem; they may
open files for read and may spawn subprocesses only via read-only APIs.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Data carrier — every extractor returns one of these.                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SignalSet:
    """Union of every signal kind an extractor can produce for one file.

    Every field is ``None``/empty for extractors that do not apply.  Callers
    merge multiple ``SignalSet``s via :func:`merge_signal_sets`.
    """

    # Universal (filename / stat)
    mime_top: str | None = None
    mime_sub: str | None = None
    mtime_year: int | None = None
    mtime_month: int | None = None
    path_tokens: tuple[str, ...] = ()

    # Filename patterns
    fname_date: str | None = None
    fname_year: int | None = None
    fname_issuer: str | None = None
    fname_seqno: int | None = None
    fname_keywords: frozenset[str] = frozenset()

    # Music (mutagen / ID3)
    id3_artist: str | None = None
    id3_albumartist: str | None = None
    id3_album: str | None = None
    id3_track: int | None = None
    id3_year: int | None = None
    id3_genre: str | None = None

    # Photo (Pillow EXIF)
    exif_date_original: str | None = None
    exif_year: int | None = None
    exif_month: int | None = None
    exif_make: str | None = None
    exif_model: str | None = None
    exif_gps_lat: float | None = None
    exif_gps_lon: float | None = None
    exif_ts_epoch: float | None = None  # seconds since epoch for clustering

    # Video (hachoir)
    video_create_date: str | None = None
    video_year: int | None = None
    video_ts_epoch: float | None = None
    video_gps_lat: float | None = None
    video_gps_lon: float | None = None
    video_duration_s: float | None = None

    # PDF (pikepdf + pdfplumber)
    pdf_title: str | None = None
    pdf_author: str | None = None
    pdf_creation_date: str | None = None
    pdf_year: int | None = None
    pdf_keywords: str | None = None
    pdf_first_page_text: str | None = None
    pdf_class: str | None = None
    pdf_class_confidence: float = 0.0

    # Fired keyword tags collected during PDF classification
    pdf_keyword_hits: frozenset[str] = frozenset()

    def merged_with(self, other: SignalSet) -> SignalSet:
        """Return a SignalSet where fields from ``other`` win when non-null."""
        d = self.__dict__.copy()
        for k, v in other.__dict__.items():
            if v is None or v == () or v == frozenset() or v == 0.0:
                continue
            # ``d`` may hold a non-null default; ``other``'s explicit non-null
            # value wins.
            d[k] = v
        return SignalSet(**d)


def merge_signal_sets(sets: list[SignalSet]) -> SignalSet:
    """Merge a list of SignalSets left-to-right; later non-null wins."""
    out = SignalSet()
    for s in sets:
        out = out.merged_with(s)
    return out


# --------------------------------------------------------------------------- #
# Protocol                                                                    #
# --------------------------------------------------------------------------- #


@runtime_checkable
class SignalExtractor(Protocol):
    """Every extractor consumes a path and returns a SignalSet."""

    applies_to_suffixes: frozenset[str]

    def extract(self, path: Path) -> SignalSet: ...


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

MUSIC_SUFFIXES: frozenset[str] = frozenset(
    (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac")
)
PHOTO_SUFFIXES: frozenset[str] = frozenset(
    (".jpg", ".jpeg", ".heic", ".png", ".tif", ".tiff", ".cr2", ".nef",
     ".arw", ".dng", ".raw")
)
VIDEO_SUFFIXES: frozenset[str] = frozenset(
    (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv")
)
PDF_SUFFIXES: frozenset[str] = frozenset((".pdf",))

# Filename patterns.  ``_`` is a word char in Python ``re``, so ``\b`` after
# an underscore does NOT match — we use ``(?<!\d)`` / ``(?!\d)`` to bound
# on digit sequences instead so ``payslip_2024-03-15.pdf`` still matches.
_DATE_RE = re.compile(
    r"(?<!\d)(?P<y>(?:19|20)\d{2})[-_./](?P<m>0[1-9]|1[0-2])[-_./](?P<d>0[1-9]|[12]\d|3[01])(?!\d)"
)
_YEAR_ONLY_RE = re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})(?!\d)")
_SEQNO_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:IMG|DSC|MVI|VID|PXL)_?(?P<n>\d{2,6})",
    re.IGNORECASE,
)
_TOKEN_SPLIT_RE = re.compile(r"[/_\-.\s]+")

# Keyword tags — case-insensitive substrings.  Order irrelevant; matched set
# is stored on ``SignalSet.fname_keywords``.
FILENAME_KEYWORDS: tuple[str, ...] = (
    "payslip", "salary", "paystub",
    "invoice", "bill",
    "receipt",
    "statement",
    "tax", "w2", "w-2", "1099", "form16", "form-16", "itr", "1040",
    "offer",
    "insurance", "policy",
    "passport", "license", "aadhaar", "pan",
    "contract", "agreement",
    "resume", "cv",
)

# Static vendor / institution lexicon — used both for filename hints and PDF
# first-page matching in the taxonomy layer.
KNOWN_VENDORS: tuple[str, ...] = (
    "Amazon", "Uber", "DoorDash", "Whole Foods", "Home Depot", "Costco",
    "Target", "Walmart", "Apple", "Best Buy",
)
KNOWN_INSTITUTIONS: tuple[str, ...] = (
    "Chase", "Wells Fargo", "Bank of America", "Citi", "Discover", "HDFC",
    "ICICI", "SBI", "Amex", "Capital One",
)
KNOWN_BROKERAGES: tuple[str, ...] = (
    "Vanguard", "Fidelity", "Charles Schwab", "Robinhood", "E*TRADE",
    "Merrill", "TD Ameritrade",
)


# --------------------------------------------------------------------------- #
# Universal signals                                                           #
# --------------------------------------------------------------------------- #


def _universal_signals(path: Path, mtime: float) -> SignalSet:
    """Fill in mime + mtime + path-token signals; never fails."""
    import mimetypes

    mime, _ = mimetypes.guess_type(str(path))
    top, sub = (None, None)
    if mime and "/" in mime:
        top, sub = mime.split("/", 1)
    try:
        dt = datetime.fromtimestamp(mtime)
        year = dt.year
        month = dt.month
    except (OSError, ValueError, OverflowError):
        year = None
        month = None
    tokens = tuple(
        t.lower() for t in _TOKEN_SPLIT_RE.split(path.stem) if t
    )
    return SignalSet(
        mime_top=top,
        mime_sub=sub,
        mtime_year=year,
        mtime_month=month,
        path_tokens=tokens,
    )


# --------------------------------------------------------------------------- #
# Filename                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class FilenameSignalExtractor:
    """Regex-only extractor — always applies, never fails on read."""

    applies_to_suffixes: frozenset[str] = field(default_factory=frozenset)

    def extract(self, path: Path) -> SignalSet:
        stem = path.stem
        full = path.name

        fname_date = None
        fname_year = None
        m = _DATE_RE.search(full)
        if m:
            y, mo, d = m.group("y"), m.group("m"), m.group("d")
            fname_date = f"{y}-{mo}-{d}"
            try:
                fname_year = int(y)
            except ValueError:
                fname_year = None
        else:
            ym = _YEAR_ONLY_RE.search(full)
            if ym:
                try:
                    fname_year = int(ym.group("y"))
                except ValueError:
                    fname_year = None

        fname_seqno = None
        sm = _SEQNO_RE.search(full)
        if sm:
            try:
                fname_seqno = int(sm.group("n"))
            except ValueError:
                fname_seqno = None

        lower = full.lower()
        hits: list[str] = []
        for kw in FILENAME_KEYWORDS:
            if kw.lower() in lower:
                hits.append(kw)

        # Issuer detection: substring match against static vendors,
        # institutions, brokerages — first hit wins.
        fname_issuer = None
        stem_lower = stem.lower().replace("_", " ").replace("-", " ")
        for candidate in (*KNOWN_VENDORS, *KNOWN_INSTITUTIONS, *KNOWN_BROKERAGES):
            if candidate.lower() in stem_lower:
                fname_issuer = candidate
                break

        return SignalSet(
            fname_date=fname_date,
            fname_year=fname_year,
            fname_issuer=fname_issuer,
            fname_seqno=fname_seqno,
            fname_keywords=frozenset(hits),
        )


# --------------------------------------------------------------------------- #
# Music                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class MusicSignalExtractor:
    """ID3 / Vorbis tag reader via ``mutagen``.

    ``mutagen`` is imported lazily so ``organize.signals`` remains importable
    on a bare install.  A missing dep degrades cleanly to no signals.
    """

    applies_to_suffixes: frozenset[str] = MUSIC_SUFFIXES

    def extract(self, path: Path) -> SignalSet:
        try:
            import mutagen  # type: ignore[import-not-found,import-untyped]
        except ImportError:
            log.debug("mutagen not installed — skipping music signals for %s", path)
            return SignalSet()

        try:
            audio = mutagen.File(str(path), easy=True)  # type: ignore[attr-defined]
        except Exception as exc:
            log.debug("mutagen failed on %s: %s", path, exc)
            return SignalSet()
        if audio is None:
            return SignalSet()

        def _first(key: str) -> str | None:
            v = audio.get(key) if hasattr(audio, "get") else None
            if not v:
                return None
            if isinstance(v, list) and v:
                return str(v[0])
            return str(v)

        artist = _first("artist")
        albumartist = _first("albumartist") or artist
        album = _first("album")
        genre = _first("genre")
        raw_track = _first("tracknumber")
        raw_year = _first("date") or _first("year")

        track: int | None = None
        if raw_track:
            head = raw_track.split("/")[0].strip()
            if head.isdigit():
                track = int(head)

        year: int | None = None
        if raw_year:
            m = _YEAR_ONLY_RE.search(raw_year)
            if m:
                try:
                    year = int(m.group("y"))
                except ValueError:
                    year = None

        return SignalSet(
            id3_artist=artist,
            id3_albumartist=albumartist,
            id3_album=album,
            id3_genre=genre,
            id3_track=track,
            id3_year=year,
        )


# --------------------------------------------------------------------------- #
# Photo                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class PhotoSignalExtractor:
    """EXIF reader via Pillow; optional GPS fallback via ``piexif``."""

    applies_to_suffixes: frozenset[str] = PHOTO_SUFFIXES

    def extract(self, path: Path) -> SignalSet:
        try:
            from PIL import ExifTags, Image  # type: ignore[import-not-found,import-untyped]
        except ImportError:
            log.debug("Pillow not installed — skipping photo signals for %s", path)
            return SignalSet()

        exif_data: dict[int, object] = {}
        try:
            with Image.open(str(path)) as img:
                raw = img.getexif()
                if raw:
                    for tag_id, value in raw.items():
                        exif_data[int(tag_id)] = value
        except Exception as exc:
            log.debug("Pillow EXIF failed on %s: %s", path, exc)
            return SignalSet()

        # Map to human-readable names.
        try:
            name_for = {v: k for k, v in ExifTags.TAGS.items()}
        except AttributeError:
            name_for = {}
        by_name: dict[str, object] = {}
        for tag_id, value in exif_data.items():
            name = ExifTags.TAGS.get(int(tag_id))
            if isinstance(name, str):
                by_name[name] = value

        date_original = _stringy(by_name.get("DateTimeOriginal") or by_name.get("DateTime"))
        year: int | None = None
        month: int | None = None
        ts_epoch: float | None = None
        if date_original:
            # EXIF format: "YYYY:MM:DD HH:MM:SS"
            m = re.match(
                r"(?P<y>\d{4})[:\-](?P<mo>\d{2})[:\-](?P<d>\d{2})"
                r"[ T](?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2})",
                date_original.strip(),
            )
            if m:
                try:
                    year = int(m.group("y"))
                    month = int(m.group("mo"))
                    dt = datetime(
                        year, month, int(m.group("d")),
                        int(m.group("H")), int(m.group("M")), int(m.group("S")),
                    )
                    ts_epoch = dt.timestamp()
                except (ValueError, OverflowError):
                    year = None
                    month = None

        make = _stringy(by_name.get("Make"))
        model = _stringy(by_name.get("Model"))

        # GPS via Pillow's IFD.  Missing keys are fine.
        lat = lon = None
        try:
            with Image.open(str(path)) as img:
                exif = img.getexif()
                gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo) if hasattr(ExifTags, "IFD") else None
                if gps_ifd:
                    lat, lon = _gps_from_ifd(gps_ifd)
        except Exception as exc:
            log.debug("Pillow GPS extraction failed on %s: %s", path, exc)

        _ = name_for  # kept for future GPSTAGS lookup
        return SignalSet(
            exif_date_original=date_original,
            exif_year=year,
            exif_month=month,
            exif_ts_epoch=ts_epoch,
            exif_make=make,
            exif_model=model,
            exif_gps_lat=lat,
            exif_gps_lon=lon,
        )


def _stringy(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace").strip("\x00 ")
        except (UnicodeDecodeError, AttributeError):
            return None
    s = str(value).strip("\x00 ")
    return s or None


def _rational_to_deg(triple: object) -> float | None:
    """Convert (deg, min, sec) rationals to decimal degrees; tolerant of shape."""
    parts: tuple[object, object, object]
    try:
        parts = tuple(triple)  # type: ignore[arg-type,assignment]
    except TypeError:
        return None
    if len(parts) < 3:
        return None
    d_raw, m_raw, s_raw = parts[0], parts[1], parts[2]
    try:
        return float(d_raw) + float(m_raw) / 60.0 + float(s_raw) / 3600.0  # type: ignore[arg-type]
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _gps_from_ifd(gps: object) -> tuple[float | None, float | None]:
    """Extract (lat, lon) from a Pillow GPSInfo IFD; returns (None, None) on any failure.

    GPS tags per EXIF spec: 1=LatitudeRef, 2=Latitude, 3=LongitudeRef, 4=Longitude.
    """
    try:
        get = gps.get  # type: ignore[attr-defined]
    except AttributeError:
        return None, None
    lat_ref = get(1)
    lat = get(2)
    lon_ref = get(3)
    lon = get(4)
    lat_deg = _rational_to_deg(lat) if lat is not None else None
    lon_deg = _rational_to_deg(lon) if lon is not None else None
    if lat_deg is not None and isinstance(lat_ref, str) and lat_ref.upper() == "S":
        lat_deg = -lat_deg
    if lon_deg is not None and isinstance(lon_ref, str) and lon_ref.upper() == "W":
        lon_deg = -lon_deg
    return lat_deg, lon_deg


# --------------------------------------------------------------------------- #
# Video                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class VideoSignalExtractor:
    """Video metadata via ``hachoir`` (pure-Python, no ffmpeg dep)."""

    applies_to_suffixes: frozenset[str] = VIDEO_SUFFIXES

    def extract(self, path: Path) -> SignalSet:
        try:
            from hachoir.metadata import (
                extractMetadata,  # type: ignore[import-not-found,import-untyped]
            )
            from hachoir.parser import createParser  # type: ignore[import-not-found,import-untyped]
        except ImportError:
            log.debug("hachoir not installed — skipping video signals for %s", path)
            return SignalSet()

        try:
            parser = createParser(str(path))
        except Exception as exc:
            log.debug("hachoir parser failed on %s: %s", path, exc)
            return SignalSet()
        if parser is None:
            return SignalSet()

        try:
            with parser:
                meta = extractMetadata(parser)
        except Exception as exc:
            log.debug("hachoir metadata extract failed on %s: %s", path, exc)
            return SignalSet()
        if meta is None:
            return SignalSet()

        create_iso: str | None = None
        year: int | None = None
        ts_epoch: float | None = None
        duration_s: float | None = None
        try:
            creation = meta.get("creation_date")
        except (ValueError, KeyError):
            creation = None
        if creation is not None:
            try:
                create_iso = creation.isoformat()
                year = creation.year
                ts_epoch = creation.timestamp()
            except (AttributeError, OSError, OverflowError, ValueError):
                pass
        try:
            duration = meta.get("duration")
        except (ValueError, KeyError):
            duration = None
        if duration is not None:
            import contextlib

            with contextlib.suppress(AttributeError, TypeError):
                duration_s = float(duration.total_seconds())

        return SignalSet(
            video_create_date=create_iso,
            video_year=year,
            video_ts_epoch=ts_epoch,
            video_duration_s=duration_s,
        )


# --------------------------------------------------------------------------- #
# PDF                                                                         #
# --------------------------------------------------------------------------- #


# Keyword lexicon — copied from the design doc §5.1.
PDF_CLASSES: dict[str, dict[str, float]] = {
    "payslip": {
        "Gross Pay": 3, "Net Pay": 3, "Employee ID": 2, "YTD": 1,
        "Basic": 1, "HRA": 1, "Deductions": 2, "Pay Period": 2,
    },
    "receipt": {
        "Receipt": 2, "Order #": 2, "Order Number": 2, "Subtotal": 1,
        "Tax": 1, "Total": 1, "Payment Method": 2, "Thank you": 1,
    },
    "bank_statement": {
        "Statement Period": 3, "Opening Balance": 3, "Closing Balance": 3,
        "Account Number": 2, "IFSC": 1, "Routing": 1,
    },
    "tax_form": {
        "W-2": 4, "1099": 4, "Form 16": 4, "ITR": 3, "Assessment Year": 2,
        "Total Tax": 2, "Employer Identification": 2, "1040": 4,
    },
    "invoice": {
        "Invoice #": 3, "Invoice Number": 3, "Bill To": 2, "Ship To": 1,
        "Due Date": 2, "Terms": 1, "PO Number": 1,
    },
    "offer_letter": {
        "Offer": 2, "Position": 2, "Salary": 2, "Start Date": 2,
        "pleased to offer": 3, "terms of employment": 3, "CTC": 2,
    },
    "insurance": {
        "Policy Number": 3, "Premium": 2, "Coverage": 2, "Beneficiary": 2,
        "Insured": 1, "Deductible": 1,
    },
    "investment": {
        "Portfolio": 2, "Holdings": 2, "Dividend": 2, "NAV": 2,
        "Cost Basis": 2, "Unrealized": 1, "Broker": 1,
    },
    "id_document": {
        "Passport": 4, "Driving License": 3, "PAN": 3, "Aadhaar": 3,
        "Social Security": 3, "Date of Birth": 1, "Nationality": 2,
    },
    "legal": {
        "WHEREAS": 3, "hereby agree": 2, "NOTARY": 2,
        "party of the first part": 3, "in witness whereof": 2,
    },
}


def classify_pdf_text(text: str) -> tuple[str | None, float, frozenset[str]]:
    """Return (best-class-label, confidence, hit-keywords).

    Confidence = weighted-score / max-possible-weight for the class.  A
    threshold check is left to callers.
    """
    if not text:
        return None, 0.0, frozenset()
    lower_text = text.lower()
    best_label: str | None = None
    best_conf: float = 0.0
    best_hits: frozenset[str] = frozenset()
    for label, kws in PDF_CLASSES.items():
        max_score = sum(kws.values())
        if max_score <= 0:
            continue
        raw = 0.0
        hits: list[str] = []
        for kw, weight in kws.items():
            if kw.lower() in lower_text:
                raw += float(weight)
                hits.append(kw)
        conf = raw / float(max_score)
        if conf > best_conf:
            best_conf = conf
            best_label = label
            best_hits = frozenset(hits)
    return best_label, best_conf, best_hits


@dataclass
class PDFSignalExtractor:
    """PDF Info dict via pikepdf + first-page text via pdfplumber."""

    applies_to_suffixes: frozenset[str] = PDF_SUFFIXES
    first_page_char_cap: int = 5000
    class_threshold: float = 0.6

    def extract(self, path: Path) -> SignalSet:
        title = author = keywords = creation_raw = None
        try:
            import pikepdf  # type: ignore[import-not-found,import-untyped]

            with pikepdf.open(str(path)) as pdf:
                docinfo = pdf.docinfo
                if docinfo is not None:
                    title = _pdf_str(docinfo.get("/Title"))
                    author = _pdf_str(docinfo.get("/Author"))
                    keywords = _pdf_str(docinfo.get("/Keywords"))
                    creation_raw = _pdf_str(docinfo.get("/CreationDate"))
        except ImportError:
            log.debug("pikepdf not installed — no PDF info for %s", path)
        except Exception as exc:
            log.debug("pikepdf failed on %s: %s", path, exc)

        first_page_text: str | None = None
        try:
            import pdfplumber  # type: ignore[import-not-found,import-untyped]

            with pdfplumber.open(str(path)) as doc:
                pages = getattr(doc, "pages", [])
                if pages:
                    txt = pages[0].extract_text() or ""
                    first_page_text = txt[: self.first_page_char_cap]
        except ImportError:
            log.debug("pdfplumber not installed — no first-page text for %s", path)
        except Exception as exc:
            log.debug("pdfplumber failed on %s: %s", path, exc)

        year: int | None = None
        if creation_raw:
            # PDF /CreationDate format: "D:YYYYMMDDHHmmSS..."
            m = re.search(r"D:(?P<y>(?:19|20)\d{2})", creation_raw)
            if m:
                try:
                    year = int(m.group("y"))
                except ValueError:
                    year = None

        klass, conf, hits = classify_pdf_text(first_page_text or "")
        if conf < self.class_threshold:
            klass = None

        return SignalSet(
            pdf_title=title,
            pdf_author=author or None,
            pdf_creation_date=creation_raw,
            pdf_year=year,
            pdf_keywords=keywords,
            pdf_first_page_text=first_page_text,
            pdf_class=klass,
            pdf_class_confidence=conf,
            pdf_keyword_hits=hits,
        )


def _pdf_str(value: object) -> str | None:
    if value is None:
        return None
    try:
        s = str(value)
    except (TypeError, ValueError):
        return None
    s = s.strip("\x00 ")
    return s or None


# --------------------------------------------------------------------------- #
# Orchestrator                                                                #
# --------------------------------------------------------------------------- #


def _default_extractors() -> tuple[SignalExtractor, ...]:
    """Build the default extractor tuple; typed as the abstract Protocol so
    downstream code can substitute any Protocol-conforming extractor."""
    ex: list[SignalExtractor] = [
        FilenameSignalExtractor(),
        MusicSignalExtractor(),
        PhotoSignalExtractor(),
        VideoSignalExtractor(),
        PDFSignalExtractor(),
    ]
    return tuple(ex)


DEFAULT_EXTRACTORS: tuple[SignalExtractor, ...] = _default_extractors()


def extract_all(
    path: Path,
    mtime: float,
    extractors: tuple[SignalExtractor, ...] = DEFAULT_EXTRACTORS,
) -> SignalSet:
    """Run every applicable extractor and merge the results.

    ``mtime`` is passed explicitly so callers can pull it from a stat cache
    rather than re-stat the file.
    """
    parts: list[SignalSet] = [_universal_signals(path, mtime)]
    suffix = path.suffix.lower()
    for ex in extractors:
        # FilenameSignalExtractor advertises no suffixes -> always applies.
        if not ex.applies_to_suffixes or suffix in ex.applies_to_suffixes:
            try:
                parts.append(ex.extract(path))
            except Exception as exc:
                log.warning("Extractor %s failed on %s: %s", type(ex).__name__, path, exc)
    return merge_signal_sets(parts)
