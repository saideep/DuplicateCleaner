"""Rule-based taxonomy classifier — maps SignalSets to (domain, subfolder, confidence).

Rules are declarative dataclasses in a module-level list.  Each rule owns:

* a **predicate** callable that returns a (score, matched-kinds) tuple,
* a **domain / subfolder template** that composes the destination path,
* a **precedence** integer used to break score ties deterministically,
* a **confidence_boost** additive value applied on top of the raw score.

The classifier selects the highest-scoring rule; the runner-ups become
``alternatives`` on the plan entry.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

from duplicate_cleaner.organize.signals import (
    KNOWN_BROKERAGES,
    KNOWN_INSTITUTIONS,
    KNOWN_VENDORS,
    SignalSet,
)

# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #


class PredicateResult(NamedTuple):
    """A predicate's score + the signal kinds it observed to score."""

    score: float
    matched_signal_kinds: tuple[str, ...]


Predicate = Callable[[SignalSet], PredicateResult]


@dataclass(frozen=True)
class TaxonomyRule:
    """One classification rule."""

    id: str
    predicate: Predicate
    domain: str
    subfolder_template: str
    precedence: int = 50
    confidence_boost: float = 0.0


@dataclass(frozen=True)
class Classification:
    """The output of :func:`classify` for one file."""

    domain: str
    subfolder: str
    confidence: float
    fired_rules: tuple[str, ...] = ()
    matched_signals: tuple[str, ...] = ()
    alternatives: tuple[tuple[str, str, float], ...] = ()


# --------------------------------------------------------------------------- #
# Predicate helpers                                                           #
# --------------------------------------------------------------------------- #


def _year_hint(s: SignalSet) -> str:
    """Return the best year for template substitution as a string."""
    for candidate in (s.pdf_year, s.exif_year, s.video_year, s.fname_year, s.mtime_year):
        if candidate:
            return str(candidate)
    return "Undated"


def _yyyy_mm(s: SignalSet) -> str:
    year = None
    month = None
    if s.exif_year and s.exif_month:
        year, month = s.exif_year, s.exif_month
    elif s.mtime_year and s.mtime_month:
        year, month = s.mtime_year, s.mtime_month
    if year and month:
        return f"{year:04d}-{month:02d}"
    if year:
        return str(year)
    return "Undated"


def _pdf_class_predicate(target: str, weight: float = 0.9) -> Predicate:
    def pred(s: SignalSet) -> PredicateResult:
        if s.pdf_class == target:
            return PredicateResult(weight * max(s.pdf_class_confidence, 0.7),
                                   ("pdf.class." + target,))
        return PredicateResult(0.0, ())
    return pred


def _fname_kw_predicate(keywords: tuple[str, ...], weight: float = 0.5) -> Predicate:
    kw_set = frozenset(k.lower() for k in keywords)

    def pred(s: SignalSet) -> PredicateResult:
        matched = [kw for kw in s.fname_keywords if kw.lower() in kw_set]
        if matched:
            return PredicateResult(weight, tuple(f"fname.keyword.{m}" for m in matched))
        return PredicateResult(0.0, ())
    return pred


def _or_predicate(*preds: Predicate) -> Predicate:
    def pred(s: SignalSet) -> PredicateResult:
        best_score = 0.0
        matched: tuple[str, ...] = ()
        for p in preds:
            r = p(s)
            if r.score > best_score:
                best_score = r.score
                matched = r.matched_signal_kinds
        return PredicateResult(best_score, matched)
    return pred


def _sum_predicate(*preds: Predicate) -> Predicate:
    def pred(s: SignalSet) -> PredicateResult:
        total = 0.0
        kinds: list[str] = []
        for p in preds:
            r = p(s)
            total += r.score
            kinds.extend(r.matched_signal_kinds)
        return PredicateResult(min(total, 1.0), tuple(kinds))
    return pred


def _mime_starts_predicate(prefix: str, weight: float = 0.7) -> Predicate:
    def pred(s: SignalSet) -> PredicateResult:
        if s.mime_top and s.mime_top.lower() == prefix:
            return PredicateResult(weight, (f"mime.{prefix}",))
        return PredicateResult(0.0, ())
    return pred


def _has_id3_predicate(weight: float = 0.9) -> Predicate:
    def pred(s: SignalSet) -> PredicateResult:
        artist = s.id3_albumartist or s.id3_artist
        if artist and s.id3_album:
            return PredicateResult(weight, ("id3.artist", "id3.album"))
        if s.id3_album or artist:
            return PredicateResult(weight * 0.4, ("id3.album",) if s.id3_album else ("id3.artist",))
        return PredicateResult(0.0, ())
    return pred


def _issuer_predicate(pool: tuple[str, ...], weight: float = 0.6) -> Predicate:
    lookup = frozenset(x.lower() for x in pool)

    def pred(s: SignalSet) -> PredicateResult:
        if s.fname_issuer and s.fname_issuer.lower() in lookup:
            return PredicateResult(weight, (f"fname.issuer.{s.fname_issuer}",))
        return PredicateResult(0.0, ())
    return pred


# --------------------------------------------------------------------------- #
# Template rendering                                                          #
# --------------------------------------------------------------------------- #

_TEMPLATE_TOKEN_RE = re.compile(r"\{(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\}")


def _render_subfolder(template: str, s: SignalSet) -> str:
    """Fill in ``{year}``, ``{yyyy_mm}``, ``{artist}``, ``{album}``, ``{vendor}``,
    ``{institution}``, ``{brokerage}``, ``{employer}``, ``{issuer}`` tokens."""

    def _lookup(name: str) -> str:
        if name == "year":
            return _year_hint(s)
        if name == "yyyy_mm":
            return _yyyy_mm(s)
        if name == "artist":
            return _clean(s.id3_albumartist or s.id3_artist) or "UnknownArtist"
        if name == "album":
            return _clean(s.id3_album) or "UnknownAlbum"
        if name == "vendor":
            return _issuer_within(s.fname_issuer, KNOWN_VENDORS) or "UnknownVendor"
        if name == "institution":
            return _issuer_within(s.fname_issuer, KNOWN_INSTITUTIONS) or "UnknownInstitution"
        if name == "brokerage":
            return _issuer_within(s.fname_issuer, KNOWN_BROKERAGES) or "UnknownBrokerage"
        if name == "employer":
            return _clean(s.pdf_author) or "UnknownEmployer"
        if name == "issuer":
            return _clean(s.fname_issuer) or "Unknown"
        return "Unknown"

    def _sub(m: re.Match[str]) -> str:
        return _lookup(m.group("name"))

    rendered = _TEMPLATE_TOKEN_RE.sub(_sub, template)
    return rendered.strip("/")


def _clean(v: str | None) -> str | None:
    if not v:
        return None
    # Remove filesystem-unsafe characters conservatively.
    out = re.sub(r"[\\/:*?\"<>|]", " ", v).strip()
    return out or None


def _issuer_within(candidate: str | None, pool: tuple[str, ...]) -> str | None:
    if not candidate:
        return None
    lower = candidate.lower()
    for p in pool:
        if p.lower() == lower:
            return p
    return None


# --------------------------------------------------------------------------- #
# Default rule catalog                                                        #
# --------------------------------------------------------------------------- #


def default_rules() -> tuple[TaxonomyRule, ...]:
    """Build the v0.3 initial rule set — one construction call per invocation.

    A function rather than a module constant so tests can rebuild a fresh
    copy in isolation and future revisions can accept configuration.
    """
    return (
        # HR
        TaxonomyRule(
            id="hr.payslip",
            predicate=_or_predicate(
                _pdf_class_predicate("payslip", 0.9),
                _fname_kw_predicate(("payslip", "salary", "paystub"), 0.5),
            ),
            domain="HR",
            subfolder_template="Payslips/{year}",
            precedence=90,
        ),
        TaxonomyRule(
            id="hr.offer",
            predicate=_or_predicate(
                _pdf_class_predicate("offer_letter", 0.9),
                _fname_kw_predicate(("offer",), 0.4),
            ),
            domain="HR",
            subfolder_template="OfferLetters/{year}",
            precedence=85,
        ),
        TaxonomyRule(
            id="hr.tax",
            predicate=_or_predicate(
                _pdf_class_predicate("tax_form", 0.9),
                _fname_kw_predicate(
                    ("w2", "w-2", "1099", "form16", "form-16", "itr", "1040", "tax"),
                    0.7,
                ),
            ),
            domain="HR",
            subfolder_template="Tax/{year}",
            precedence=88,
        ),
        # Personal
        TaxonomyRule(
            id="personal.ids",
            predicate=_or_predicate(
                _pdf_class_predicate("id_document", 0.9),
                _fname_kw_predicate(("passport", "license", "aadhaar", "pan"), 0.7),
            ),
            domain="Personal",
            subfolder_template="IDs",
            precedence=85,
        ),
        TaxonomyRule(
            id="personal.insurance",
            predicate=_or_predicate(
                _pdf_class_predicate("insurance", 0.85),
                _fname_kw_predicate(("insurance", "policy"), 0.5),
            ),
            domain="Personal",
            subfolder_template="Insurance/{year}",
            precedence=80,
        ),
        # Finances
        TaxonomyRule(
            id="fin.receipt",
            predicate=_or_predicate(
                _pdf_class_predicate("receipt", 0.9),
                _fname_kw_predicate(("receipt",), 0.6),
                _issuer_predicate(KNOWN_VENDORS, 0.55),
            ),
            domain="Finances",
            subfolder_template="Receipts/{year}/{vendor}",
            precedence=82,
        ),
        TaxonomyRule(
            id="fin.statement",
            predicate=_or_predicate(
                _pdf_class_predicate("bank_statement", 0.9),
                _fname_kw_predicate(("statement",), 0.5),
                _issuer_predicate(KNOWN_INSTITUTIONS, 0.6),
            ),
            domain="Finances",
            subfolder_template="Statements/{year}/{institution}",
            precedence=82,
        ),
        TaxonomyRule(
            id="fin.invoice",
            predicate=_or_predicate(
                _pdf_class_predicate("invoice", 0.85),
                _fname_kw_predicate(("invoice", "bill"), 0.6),
            ),
            domain="Finances",
            subfolder_template="Invoices/{year}",
            precedence=78,
        ),
        TaxonomyRule(
            id="fin.investment",
            predicate=_or_predicate(
                _pdf_class_predicate("investment", 0.85),
                _issuer_predicate(KNOWN_BROKERAGES, 0.65),
            ),
            domain="Finances",
            subfolder_template="Investments/{year}",
            precedence=78,
        ),
        # Photos / Videos
        TaxonomyRule(
            id="photo.event",
            predicate=_mime_starts_predicate("image", 0.7),
            domain="Photos",
            subfolder_template="{year}/{yyyy_mm}",
            precedence=60,
        ),
        TaxonomyRule(
            id="video.event",
            predicate=_mime_starts_predicate("video", 0.7),
            domain="Videos",
            subfolder_template="{year}/{yyyy_mm}",
            precedence=60,
        ),
        # Media (music, books)
        TaxonomyRule(
            id="media.music",
            predicate=_has_id3_predicate(0.9),
            domain="Media",
            subfolder_template="Music/{artist}/{album}",
            precedence=95,
        ),
        TaxonomyRule(
            id="media.books",
            predicate=_or_predicate(
                _fname_kw_predicate(("book", "ebook", "epub"), 0.4),
            ),
            domain="Media",
            subfolder_template="Books",
            precedence=55,
        ),
        # Work / Projects — very coarse fallbacks
        TaxonomyRule(
            id="work.docs",
            predicate=_fname_kw_predicate(("resume", "cv", "proposal", "deck"), 0.4),
            domain="Work",
            subfolder_template="{year}",
            precedence=50,
        ),
    )


# Cached default rules — regenerated per call for isolation-friendliness.
_DEFAULT_RULES: tuple[TaxonomyRule, ...] = default_rules()


# --------------------------------------------------------------------------- #
# Classifier                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _RuleScore:
    rule: TaxonomyRule
    score: float
    matched: tuple[str, ...] = ()


def _score_all(
    s: SignalSet, rules: tuple[TaxonomyRule, ...]
) -> list[_RuleScore]:
    out: list[_RuleScore] = []
    for r in rules:
        result = r.predicate(s)
        raw = result.score + r.confidence_boost
        if raw > 0.0:
            out.append(_RuleScore(rule=r, score=raw, matched=result.matched_signal_kinds))
    return out


def classify(
    signals: SignalSet,
    *,
    rules: tuple[TaxonomyRule, ...] | None = None,
    threshold: float = 0.75,
    unsorted_hint_threshold: float = 0.4,
) -> Classification:
    """Pick the best rule for a SignalSet; below-threshold falls to Unsorted.

    Alternatives (2nd- and 3rd-best rules) are attached so review UIs can
    surface them.  Ties broken by precedence, then template specificity, then
    rule id alphabetically — deterministic across runs.
    """
    rules = rules or _DEFAULT_RULES
    scored = _score_all(signals, rules)
    if not scored:
        return Classification(
            domain="Unsorted",
            subfolder="",
            confidence=0.0,
        )

    def _sort_key(rs: _RuleScore) -> tuple[float, int, int, str]:
        # Descending: score, precedence, template specificity (placeholder count),
        # then ascending rule id.  Python's ``sorted`` is stable and ascending;
        # negate the descending fields.
        placeholder_count = len(_TEMPLATE_TOKEN_RE.findall(rs.rule.subfolder_template))
        return (-rs.score, -rs.rule.precedence, -placeholder_count, rs.rule.id)

    scored.sort(key=_sort_key)
    best = scored[0]
    subfolder = _render_subfolder(best.rule.subfolder_template, signals)
    confidence = min(best.score, 1.0)

    alt_tuples: list[tuple[str, str, float]] = []
    for r in scored[1:4]:
        alt_sub = _render_subfolder(r.rule.subfolder_template, signals)
        alt_tuples.append((r.rule.domain, alt_sub, min(r.score, 1.0)))

    if confidence < threshold:
        if confidence >= unsorted_hint_threshold:
            return Classification(
                domain="Unsorted",
                subfolder=best.rule.domain,
                confidence=confidence,
                fired_rules=(best.rule.id,),
                matched_signals=best.matched,
                alternatives=tuple(alt_tuples),
            )
        return Classification(
            domain="Unsorted",
            subfolder="",
            confidence=confidence,
            fired_rules=(),
            matched_signals=(),
            alternatives=tuple(alt_tuples),
        )

    return Classification(
        domain=best.rule.domain,
        subfolder=subfolder,
        confidence=confidence,
        fired_rules=(best.rule.id,),
        matched_signals=best.matched,
        alternatives=tuple(alt_tuples),
    )


# Kept public for tests that want to peek at the raw list.
__all__ = [
    "Classification",
    "Predicate",
    "PredicateResult",
    "TaxonomyRule",
    "classify",
    "default_rules",
]
