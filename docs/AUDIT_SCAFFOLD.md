# Audit scaffold — high-standards review contract

Every audit agent (Code Review or Security) MUST follow this scaffold. It is loaded verbatim into every audit prompt going forward. It ensures the reviewer applies architectural memory, considers alternatives, spots overkill, traces regression surface, and enforces the project's coding standards — not just hunts for bugs.

## 0. Load context first

Before reading any source file, read in this order:

1. `docs/AUDIT_LOG.md` — invariants, rejected alternatives, prior-round findings + resolutions.
2. `/Users/vaannada/.claude/plans/i-want-to-create-precious-cloud.md` — plan file with locked-in decisions.
3. `docs/safety.md` — current safety-model documentation.
4. `docs/design/v*-*.md` if reviewing a feature that has a design doc.

If you find yourself questioning a decision that is listed in "Rejected alternatives" of `AUDIT_LOG.md`, DO NOT re-litigate it unless you have new information the previous decision didn't have.

## 1. Invariants — verify none are weakened

The invariants section of `AUDIT_LOG.md` lists load-bearing properties. Walk the diff and ask: does any changed code path weaken one of these? Report as a **BLOCKER** if yes, even if the change is locally correct.

## 2. Alternatives analysis

For each non-trivial change, name at least ONE alternative approach and say why the chosen path is (or isn't) better. If the alternative is clearly better, that's a finding: `category=simplification` with `verdict=CONFIRMED`.

Example format:

> Chosen: two-pass streaming via `scan_stage` SQLite table.
> Alternative: single-pass with in-memory `dict[int, list[FileRecord]]`.
> Verdict: chosen path is better for large trees (O(size-buckets) memory vs O(files)) BUT for typical scans <100k files the simpler in-memory path is warmer cache and no schema-migration risk. Not a blocker; note the trade-off.

## 3. Overkill check

For every added abstraction, config knob, feature branch: is complexity proportional to user value?

- Config knob nobody will tune → simpler default and drop the knob.
- Class hierarchy for one implementation → make it a function.
- Feature that solves a hypothetical problem → drop until real problem shows up.

Report as `category=simplification` with a specific fix proposal.

## 4. Regression surface

For each meaningful change, trace: what existing behavior does this touch, and how would we know if we broke it?

- Which existing tests exercise this path?
- Are there paths this change touches that have NO test coverage?
- Could a change in module A silently change behavior in module B via shared state (SQLite cache, in-memory globals, config)?

Report gaps as `category=test-coverage`.

## 5. Standards enforcement

Every changed source file must satisfy:

- Type hints on every public function signature (`mypy --strict` clean).
- Ruff clean (`ruff check .` — pyproject.toml has the config).
- No forbidden calls (see `tests/test_no_forbidden_calls.py` — check the greppable regexes are still sufficient for the new code).
- No `print()` in library code; use logging or return values. CLI layer uses `rich`.
- Docstrings on public functions ONLY, ONE LINE each, states WHY not WHAT.
- No comments describing what the code obviously does. Only comments where a subtle constraint / bug workaround / invariant that isn't self-evident.

Report violations as `category=simplification` or `category=correctness` as appropriate.

## 6. Report format

Return findings inline as JSON-in-markdown. For each finding:

```json
{
  "file": "src/path/file.py",
  "line": 123,
  "short_summary": "≤60 char claim, DATA-LOSS: prefix for data-loss risks",
  "summary": "One sentence stating the defect.",
  "failure_scenario": "Concrete inputs/state → wrong output/crash.",
  "category": "safety|correctness|test-coverage|simplification",
  "verdict": "CONFIRMED|PLAUSIBLE",
  "alternative": "One-sentence alternate approach (if applicable).",
  "regression_surface": "What breaks if we ship this as-is (if applicable)."
}
```

Rank most severe first. If a finding is a NEW DATA-LOSS risk, put `DATA-LOSS:` prefix in `short_summary` and mark ship-blocking.

## 7. Verdict statement

End the report with an explicit verdict:

- **BLOCK on ship** — if any DATA-LOSS or invariant-weakening finding.
- **Ready with must-fix follow-ups** — if only correctness/safety findings that should land in same release.
- **Ready with post-release notes** — if only simplification / test-coverage findings, acceptable to defer.

## 8. After sign-off

If your review clears the release, append a short entry to `docs/AUDIT_LOG.md` under the current milestone's "history" section: date, your verdict, count of findings by category. If you found DATA-LOSS or invariant-weakening bugs, list them.
