"""Project-tree aggregation tests — v0.4."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from duplicate_cleaner.cli import app
from duplicate_cleaner.compare.tree import (
    _score_keeper,
    aggregate_project_duplicates,
    detect_project_dirs,
    is_git_repo_dirty,
)
from duplicate_cleaner.hash.pipeline import HashedRecord


def _hr(path: Path, *, size: int = 100, mtime: float = 1000.0, h: str = "H") -> HashedRecord:
    return HashedRecord(
        path=path,
        size=size,
        mtime=mtime,
        inode=abs(hash(str(path))) & 0xFFFFFFFF,
        dev=1,
        nlink=1,
        full_hash=h,
    )


def _write_project(root: Path, layout: dict[str, bytes], marker_child: str) -> None:
    """Create a project tree at ``root`` with ``marker_child`` as the qualifying marker."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    marker_path = root / marker_child
    if marker_child == ".git":
        marker_path.mkdir(parents=True, exist_ok=True)
        (marker_path / "HEAD").write_text("ref: refs/heads/main\n")
    else:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        if not marker_path.exists():
            marker_path.write_text("marker\n")


def _hashed_records_for_project(project_root: Path) -> list[HashedRecord]:
    """Produce synthetic HashedRecords for every regular file in ``project_root``.

    Files are hashed by content-string so two projects with identical file
    contents produce identical hashes; two with differing content produce
    different hashes.
    """
    out: list[HashedRecord] = []
    for p in project_root.rglob("*"):
        if not p.is_file():
            continue
        content = p.read_bytes()
        # Use a stable content-based synthetic hash so identical bytes
        # collide and different bytes don't.  Full hex — a prior version
        # truncated to 20 chars and collided ``b"unique to A"`` with
        # ``b"unique to B"``.
        h = f"content:{content.hex()}:{len(content)}"
        out.append(_hr(p, size=len(content), h=h))
    return out


# --- detect_project_dirs --------------------------------------------------


def test_detect_project_dirs_finds_git_marker(tmp_path: Path) -> None:
    proj = tmp_path / "myrepo"
    _write_project(
        proj,
        {"a.txt": b"aa", "src/b.py": b"print(1)", "src/c.py": b"print(2)"},
        marker_child=".git",
    )
    records = _hashed_records_for_project(proj)
    result = detect_project_dirs(records)
    assert proj in result
    info = result[proj]
    assert info.marker == ".git"
    assert info.name == "myrepo"
    # 3 project files + 1 .git/HEAD marker file we wrote via _write_project.
    assert len(info.file_hashes) == 4


def test_detect_project_dirs_finds_python_marker(tmp_path: Path) -> None:
    proj = tmp_path / "pypkg"
    _write_project(
        proj,
        {"pyproject.toml": b"[project]\n", "pkg/__init__.py": b""},
        marker_child="pyproject.toml",
    )
    records = _hashed_records_for_project(proj)
    result = detect_project_dirs(records)
    assert proj in result
    assert result[proj].marker == "pyproject.toml"


def test_detect_project_dirs_finds_node_marker(tmp_path: Path) -> None:
    proj = tmp_path / "nodepkg"
    _write_project(
        proj,
        {"package.json": b"{}", "index.js": b"module.exports = {};"},
        marker_child="package.json",
    )
    records = _hashed_records_for_project(proj)
    result = detect_project_dirs(records)
    assert proj in result
    assert result[proj].marker == "package.json"


def test_detect_project_dirs_ignores_non_project_dir(tmp_path: Path) -> None:
    plain = tmp_path / "just_data"
    plain.mkdir()
    (plain / "photo1.jpg").write_bytes(b"jpeg data 1")
    (plain / "photo2.jpg").write_bytes(b"jpeg data 2")
    records = _hashed_records_for_project(plain)
    result = detect_project_dirs(records)
    assert plain not in result
    # And no ancestor either.
    assert tmp_path not in result


def test_detect_project_dirs_ignores_archive_members(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    _write_project(proj, {"a.txt": b"a"}, marker_child="Cargo.toml")
    real = _hashed_records_for_project(proj)
    # Fabricate an archive-member record inside the project — it should
    # not be attributed to the project's file_hashes.
    virt = HashedRecord(
        path=proj / "vendored.zip::inner/x.txt",
        size=10,
        mtime=0,
        inode=0,
        dev=0,
        nlink=0,
        full_hash="ARCHIVEMEMBERHASH",
        is_archive_member=True,
    )
    result = detect_project_dirs([*real, virt])
    info = result[proj]
    # Only the two real files (a.txt + Cargo.toml).
    assert len(info.file_hashes) == 2
    assert "vendored.zip::inner/x.txt" not in info.file_hashes


# --- aggregate_project_duplicates ---------------------------------------


def test_aggregate_finds_full_duplicate(tmp_path: Path) -> None:
    a = tmp_path / "live" / "proj"
    b = tmp_path / "backup" / "proj"
    files = {"README": b"hello", "src/main.py": b"print(1)"}
    _write_project(a, files, marker_child=".git")
    _write_project(b, files, marker_child=".git")
    records = _hashed_records_for_project(a) + _hashed_records_for_project(b)
    projects = detect_project_dirs(records)
    groups = aggregate_project_duplicates(projects, threshold=0.90)
    assert len(groups) == 1
    g = groups[0]
    assert set(g.members) == {a, b}
    assert g.similarity == 1.0
    assert g.differing_files == []
    assert g.identical_files == len(files) + 1  # + .git/HEAD


def test_aggregate_finds_partial_duplicate(tmp_path: Path) -> None:
    a = tmp_path / "live" / "proj"
    b = tmp_path / "backup" / "proj"
    # 8 identical files (same content) + 2 different-content files each.
    base = {f"file_{i}.txt": f"data-{i}".encode() for i in range(8)}
    diverge_a = {"only_in_a.txt": b"unique to A"}
    diverge_b = {"only_in_b.txt": b"unique to B"}
    _write_project(a, {**base, **diverge_a}, marker_child="Cargo.toml")
    _write_project(b, {**base, **diverge_b}, marker_child="Cargo.toml")
    records = _hashed_records_for_project(a) + _hashed_records_for_project(b)
    projects = detect_project_dirs(records)

    # 9/10 union entries shared vs 8 file-content-hashes in intersection
    # gives Jaccard = 8 / (10) = 0.8.
    # Since Cargo.toml is identical between the two → 8 + 1 shared = 9.
    # Union = 8 + 1 + 2 = 11.  intersection/union = 9/11 ≈ 0.818.
    high = aggregate_project_duplicates(projects, threshold=0.90)
    assert high == []  # below 0.90

    low = aggregate_project_duplicates(projects, threshold=0.70)
    assert len(low) == 1
    g = low[0]
    assert set(g.members) == {a, b}
    assert 0.7 <= g.similarity < 0.9
    # differing files include the two divergent + no others.
    diff_rels = {d.relative_path for d in g.differing_files}
    assert "only_in_a.txt" in diff_rels
    assert "only_in_b.txt" in diff_rels


def _invoke_scan_with_config(
    tmp_path: Path,
    scan_roots: list[Path],
    report_dir: Path,
    active_homes: list[Path],
    extra_args: list[str] | None = None,
) -> object:
    """Helper: run ``dc scan`` with a temp config + cache in ``tmp_path``.

    Uses the ``patch("duplicate_cleaner.cli.load_config", ...)`` pattern
    the organize tests use so the CLI reads a temp config instead of the
    user's real one.
    """
    cfg_path = tmp_path / "cfg" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    homes_toml = ", ".join(f'"{h}"' for h in active_homes)
    cfg_path.write_text(f"active_homes = [{homes_toml}]\nmin_size_bytes = 1\n")
    cache_dir = tmp_path / "cache"

    from duplicate_cleaner.config import load_config as real_load

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


def test_aggregate_threshold_config_via_cli(tmp_path: Path) -> None:
    """CLI --min-project-similarity overrides the aggregate threshold."""
    a = tmp_path / "live" / "proj"
    b = tmp_path / "live" / "old" / "proj"
    # Content overlap ratio is small enough to be well below 0.9.
    shared = {f"f{i}.txt": f"c{i}".encode() for i in range(4)}
    _write_project(a, {**shared, "unique_a.txt": b"a"}, marker_child=".git")
    _write_project(
        b, {**shared, "u1.txt": b"1", "u2.txt": b"2", "u3.txt": b"3"},
        marker_child=".git",
    )

    scan_root = tmp_path / "live"

    report_default = tmp_path / "r_default"
    r1 = _invoke_scan_with_config(
        tmp_path, [scan_root], report_default, [scan_root]
    )
    assert r1.exit_code == 0, r1.output
    with (report_default / "report.json").open() as f:
        data_default = json.load(f)
    assert not [g for g in data_default["groups"] if g["kind"] == "tree"]

    report_low = tmp_path / "r_low"
    r2 = _invoke_scan_with_config(
        tmp_path,
        [scan_root],
        report_low,
        [scan_root],
        extra_args=["--min-project-similarity", "0.3"],
    )
    assert r2.exit_code == 0, r2.output
    with (report_low / "report.json").open() as f:
        data_low = json.load(f)
    tree_groups = [g for g in data_low["groups"] if g["kind"] == "tree"]
    assert tree_groups, "Expected a tree-aggregate group with lowered threshold."


# --- K2: connected-component aggregation --------------------------------


def test_aggregate_three_way_duplicate_emits_one_group(tmp_path: Path) -> None:
    """K2 (audit pass 14 blocker): three identical projects → 1 group of 3,
    not 3 pair-groups (which would yield duplicate discards on apply).
    """
    files = {"README": b"same content", "src/main.py": b"print(1)"}
    a = tmp_path / "live" / "proj"
    b = tmp_path / "backup" / "proj"
    c = tmp_path / "backup2" / "proj"
    _write_project(a, files, marker_child="Cargo.toml")
    _write_project(b, files, marker_child="Cargo.toml")
    _write_project(c, files, marker_child="Cargo.toml")

    records = (
        _hashed_records_for_project(a)
        + _hashed_records_for_project(b)
        + _hashed_records_for_project(c)
    )
    projects = detect_project_dirs(records)
    groups = aggregate_project_duplicates(projects, threshold=0.90)
    assert len(groups) == 1
    g = groups[0]
    assert set(g.members) == {a, b, c}
    # Reclaim = size of N-1 members, not the pair-sum overcount.
    per_member = projects[a].total_bytes
    assert g.total_bytes == 2 * per_member


def test_aggregate_partial_transitive_closure(tmp_path: Path) -> None:
    """K2: A~B, B~C, C~D each above threshold (A~D below) → one component of 4.

    Two pair-emitters used to produce 3 disjoint pair-groups here; the
    connected-component aggregator unifies them via transitivity.
    """
    # Build 4 projects such that adjacent pairs share MANY hashes; A vs D
    # share fewer (so their Jaccard alone drops below the threshold).
    # Every project keeps its own Cargo.toml as a common marker.
    def _project_hashes(shared_prefix: str, unique_tag: str) -> dict[str, bytes]:
        out: dict[str, bytes] = {
            f"shared/{shared_prefix}/f{i}.txt": f"c{i}".encode() for i in range(10)
        }
        out[f"unique_{unique_tag}.txt"] = unique_tag.encode()
        return out

    # AB share prefix "ab"; BC share prefix "bc"; CD share prefix "cd".
    a = tmp_path / "A"
    b = tmp_path / "B"
    c = tmp_path / "C"
    d = tmp_path / "D"
    _write_project(a, _project_hashes("ab", "A"), marker_child="Cargo.toml")
    _write_project(
        b,
        {**_project_hashes("ab", "B"), **_project_hashes("bc", "Bx")},
        marker_child="Cargo.toml",
    )
    _write_project(
        c,
        {**_project_hashes("bc", "C"), **_project_hashes("cd", "Cx")},
        marker_child="Cargo.toml",
    )
    _write_project(d, _project_hashes("cd", "D"), marker_child="Cargo.toml")

    records = (
        _hashed_records_for_project(a)
        + _hashed_records_for_project(b)
        + _hashed_records_for_project(c)
        + _hashed_records_for_project(d)
    )
    projects = detect_project_dirs(records)
    # Choose a threshold that unions each adjacent pair but leaves A~D
    # below the bar so we exercise transitivity, not direct similarity.
    groups = aggregate_project_duplicates(projects, threshold=0.45)
    assert len(groups) == 1
    g = groups[0]
    assert set(g.members) == {a, b, c, d}


# --- keeper scoring -----------------------------------------------------


def test_git_head_scoring(tmp_path: Path) -> None:
    """Two projects both with .git; newer HEAD wins."""
    a = tmp_path / "proj_new"
    b = tmp_path / "proj_old"
    _write_project(a, {"x": b"y"}, marker_child=".git")
    _write_project(b, {"x": b"y"}, marker_child=".git")

    ts_by_root: dict[Path, int] = {a: 1_720_000_000, b: 1_600_000_000}

    def _lookup(root: Path) -> int | None:
        return ts_by_root.get(root)

    idx, reason = _score_keeper([a, b], git_head_lookup=_lookup)
    assert idx == 0
    assert "newer git HEAD" in reason

    idx2, _ = _score_keeper([b, a], git_head_lookup=_lookup)
    assert idx2 == 1


def test_backup_folder_scoring(tmp_path: Path) -> None:
    """A project under an ``old/`` folder loses to one at a normal path."""
    live = tmp_path / "Users" / "me" / "Work" / "repos" / "foo"
    backup = tmp_path / "Volumes" / "OldMac" / "backup" / "repos" / "foo"
    _write_project(live, {"a": b"1"}, marker_child=".git")
    _write_project(backup, {"a": b"1"}, marker_child=".git")

    # No git_head_lookup — falls through to the backup-folder rule.
    idx, reason = _score_keeper([backup, live], git_head_lookup=lambda _p: None)
    assert idx == 1  # live wins
    assert "backup" in reason


# --- integration: report emits kind="tree" -----------------------------


def test_report_emits_tree_group(tmp_path: Path) -> None:
    """End-to-end CLI: report.json contains a kind='tree' group."""
    live = tmp_path / "live" / "proj"
    backup = tmp_path / "live" / "backup" / "proj"
    files = {"file.txt": b"content", "sub/nested.txt": b"nested"}
    _write_project(live, files, marker_child=".git")
    _write_project(backup, files, marker_child=".git")

    report_dir = tmp_path / "report"
    result = _invoke_scan_with_config(
        tmp_path, [tmp_path / "live"], report_dir, [tmp_path / "live"]
    )
    assert result.exit_code == 0, result.output
    with (report_dir / "report.json").open() as f:
        data = json.load(f)
    tree_groups = [g for g in data["groups"] if g["kind"] == "tree"]
    assert len(tree_groups) == 1
    tg = tree_groups[0]
    assert tg["similarity_pct"] == 100.0
    assert tg["identical_file_count"] >= len(files)
    # And per-file exact-duplicate groups for members inside the tree
    # should be gone — collapsed by the tree aggregate.
    exact_groups = [g for g in data["groups"] if g["kind"] == "exact"]
    for g in exact_groups:
        for m in g["members"]:
            assert not str(m["path"]).startswith(str(live)), m["path"]
            assert not str(m["path"]).startswith(str(backup)), m["path"]


# --- dirty-git safety net -----------------------------------------------


def test_is_git_repo_dirty_detects_uncommitted_changes(tmp_path: Path) -> None:
    """A real git repo with untracked changes reports dirty."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        env=env,
    )
    (repo / "a.txt").write_text("initial")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "seed"],
        check=True,
        env=env,
    )
    # Clean tree.
    assert not is_git_repo_dirty(repo)
    # Add an untracked file → dirty.
    (repo / "b.txt").write_text("uncommitted")
    assert is_git_repo_dirty(repo)


def test_non_git_dir_is_not_dirty(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("hi")
    assert not is_git_repo_dirty(plain)


def test_is_git_repo_dirty_treats_corrupted_repo_as_dirty(tmp_path: Path) -> None:
    """K5: any non-zero git exit → dirty (fail-closed on unknown state)."""
    from unittest.mock import patch

    repo = tmp_path / "corrupt_repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # marker present so is_git_repo_dirty inspects.

    class _FakeResult:
        def __init__(self) -> None:
            self.returncode = 42
            self.stdout = ""
            self.stderr = "corrupt object database"

    with patch(
        "duplicate_cleaner.compare.tree.subprocess.run",
        return_value=_FakeResult(),
    ):
        assert is_git_repo_dirty(repo)


def test_is_git_repo_dirty_treats_missing_git_binary_as_dirty(tmp_path: Path) -> None:
    """K5: missing ``git`` binary → dirty (subprocess raises FileNotFoundError)."""
    from unittest.mock import patch

    repo = tmp_path / "no_git_bin"
    repo.mkdir()
    (repo / ".git").mkdir()

    def _boom(*_args: object, **_kw: object) -> None:
        raise FileNotFoundError("git binary missing")

    with patch(
        "duplicate_cleaner.compare.tree.subprocess.run",
        side_effect=_boom,
    ):
        assert is_git_repo_dirty(repo)


# --- K4: config round-trip for min_project_similarity ------------------


def test_config_persists_min_project_similarity(tmp_path: Path) -> None:
    """K4 (audit pass 14 deferrable): ``dc scan --min-project-similarity``
    now round-trips through Config so users can pin the threshold in
    config.toml.  Writing + loading must preserve the float; the built-in
    default remains 0.90.
    """
    from duplicate_cleaner.config import Config, load_config

    # Default value.
    assert Config().min_project_similarity == 0.90

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'active_homes = ["{tmp_path}"]\n'
        "min_size_bytes = 1\n"
        "min_project_similarity = 0.75\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.min_project_similarity == 0.75
