"""SQLite cache for hashes and decisions log."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CACHE_DIR = Path.home() / ".cache" / "duplicate_cleaner"
CACHE_DB = CACHE_DIR / "cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    inode INTEGER,
    dev INTEGER,
    partial_hash TEXT,
    full_hash TEXT,
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_full_hash ON files(full_hash);
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);

-- G3: ``scan_stage`` is per-scan. Two concurrent ``dc scan`` invocations
-- must not clobber each other's staged rows. The ``scan_id`` column
-- namespaces each scan; every read and write is filtered on it, so
-- concurrent scans interleave safely. On successful completion the owning
-- scan deletes its own rows; on start every ``hash_records`` call also
-- prunes rows older than ``_STAGE_TTL_SECONDS`` to sweep leaked state
-- from prior crashes.
CREATE TABLE IF NOT EXISTS scan_stage (
    scan_id TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    inode INTEGER,
    dev INTEGER,
    nlink INTEGER,
    staged_ts REAL NOT NULL,
    PRIMARY KEY (scan_id, path)
);
CREATE INDEX IF NOT EXISTS idx_scan_stage_scan_id_size
    ON scan_stage(scan_id, size);
CREATE INDEX IF NOT EXISTS idx_scan_stage_staged_ts
    ON scan_stage(staged_ts);

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    created_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    score REAL NOT NULL,
    is_proposed_keeper INTEGER NOT NULL,
    is_informational INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, path),
    FOREIGN KEY (group_id) REFERENCES groups(id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,
    kept_path TEXT NOT NULL,
    discarded_paths TEXT NOT NULL,
    feature_vector TEXT,
    user_overrode INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL
);
"""

# Tolerate float rounding on filesystem mtimes: fs precision may differ from
# what we read back through SQLite REAL.
_MTIME_EPS = 1e-6

# G3: rows staged more than this many seconds ago are swept on the next
# ``hash_records`` start. Covers leaks from prior crashed scans.
_STAGE_TTL_SECONDS = 24 * 60 * 60


class Store:
    """Wraps the SQLite cache DB — hashes, groups, decisions."""

    def __init__(self, path: Path = CACHE_DB) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        # G3: if a cache from a pre-scan_id schema exists, drop the old
        # ``scan_stage`` table so the CREATE below installs the new shape.
        # The table is scratch space; dropping it never loses committed data.
        self._migrate_scan_stage()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _migrate_scan_stage(self) -> None:
        """Drop legacy ``scan_stage`` schema (no ``scan_id`` column)."""
        try:
            cols = [
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(scan_stage)"
                ).fetchall()
            ]
        except sqlite3.DatabaseError:
            return
        if cols and "scan_id" not in cols:
            self._conn.execute("DROP TABLE IF EXISTS scan_stage")
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_cached_hash(
        self, path: Path, size: int, mtime: float
    ) -> tuple[str | None, str | None]:
        """Return (partial_hash, full_hash) if cached for the stat, else (None, None).

        G9: uses the same ``_MTIME_EPS`` tolerance as :meth:`upsert_file`.
        SQLite REAL round-trip can lose the last bit of a float; without the
        eps rule ``get_cached_hash`` would return miss and force a rehash
        even though ``upsert_file`` treats the stat as unchanged. Correctness
        was safe (rehash gives same result) but the cache-hit rate collapsed
        silently.
        """
        cur = self._conn.execute(
            "SELECT partial_hash, full_hash FROM files "
            "WHERE path = ? AND size = ? AND ABS(mtime - ?) <= ?",
            (str(path), size, mtime, _MTIME_EPS),
        )
        row = cur.fetchone()
        if row is None:
            return None, None
        partial = row["partial_hash"]
        full = row["full_hash"]
        return (
            partial if isinstance(partial, str) else None,
            full if isinstance(full, str) else None,
        )

    def upsert_file(
        self,
        path: Path,
        *,
        size: int,
        mtime: float,
        inode: int,
        dev: int,
        partial_hash: str | None,
        full_hash: str | None,
    ) -> None:
        """Insert or update a file row.

        Stat change semantics: if ``(size, mtime)`` differ from the existing
        row, the entire cache entry is invalidated — partial_hash and
        full_hash are overwritten with the new values (which may be ``None``).
        This prevents stale hashes from surviving a file rewrite.

        Stat match semantics: ``partial_hash`` and ``full_hash`` are
        ``COALESCE``-ed so a partial-hash-only update does not clobber the
        already-computed full hash.
        """
        row = self._conn.execute(
            "SELECT size, mtime FROM files WHERE path = ?", (str(path),)
        ).fetchone()

        stats_match = (
            row is not None
            and row["size"] == size
            and abs(float(row["mtime"]) - mtime) <= _MTIME_EPS
        )

        if stats_match:
            self._conn.execute(
                """
                UPDATE files SET
                    inode = ?,
                    dev = ?,
                    partial_hash = COALESCE(?, partial_hash),
                    full_hash = COALESCE(?, full_hash),
                    last_seen = ?
                WHERE path = ?
                """,
                (inode, dev, partial_hash, full_hash, time.time(), str(path)),
            )
        else:
            # Stat mismatch — replace the row so stale hashes cannot survive
            # a rewrite. Any hash the caller did not supply lands as NULL.
            self._conn.execute(
                """
                INSERT INTO files (path, size, mtime, inode, dev,
                                   partial_hash, full_hash, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size,
                    mtime = excluded.mtime,
                    inode = excluded.inode,
                    dev = excluded.dev,
                    partial_hash = excluded.partial_hash,
                    full_hash = excluded.full_hash,
                    last_seen = excluded.last_seen
                """,
                (
                    str(path),
                    size,
                    mtime,
                    inode,
                    dev,
                    partial_hash,
                    full_hash,
                    time.time(),
                ),
            )
        self._conn.commit()

    def new_scan_id(self) -> str:
        """Return a fresh scan id — a hex UUID, opaque to callers."""
        return uuid.uuid4().hex

    def sweep_stale_scan_stage(
        self, ttl_seconds: float = _STAGE_TTL_SECONDS
    ) -> int:
        """Delete scan_stage rows older than ``ttl_seconds``.

        Returns the number of rows deleted. Called by ``hash_records`` on
        every start to reap leaked state from prior crashed scans (G3).
        """
        cutoff = time.time() - ttl_seconds
        cur = self._conn.execute(
            "DELETE FROM scan_stage WHERE staged_ts < ?", (cutoff,)
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def clear_scan_stage(self, scan_id: str | None = None) -> None:
        """Delete staged rows.

        If ``scan_id`` is given, only that scan's rows are removed — this
        is the normal per-scan cleanup path. If it is ``None``, every row
        is deleted (used only by ``clear_cache`` for a full reset).
        """
        if scan_id is None:
            self._conn.execute("DELETE FROM scan_stage")
        else:
            self._conn.execute(
                "DELETE FROM scan_stage WHERE scan_id = ?", (scan_id,)
            )
        self._conn.commit()

    def stage_record(
        self,
        path: Path,
        *,
        scan_id: str,
        size: int,
        mtime: float,
        inode: int,
        dev: int,
        nlink: int,
    ) -> None:
        """Persist a walker output row for later size-bucket grouping.

        Every write is namespaced by ``scan_id`` so two concurrent scans
        cannot clobber each other (G3).
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO scan_stage "
            "(scan_id, path, size, mtime, inode, dev, nlink, staged_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_id, str(path), size, mtime, inode, dev, nlink, time.time()),
        )

    def commit(self) -> None:
        """Public commit — used by batched staging."""
        self._conn.commit()

    def iter_duplicate_size_buckets(
        self, scan_id: str
    ) -> Iterator[list[tuple[str, int, float, int, int, int]]]:
        """Yield one list per size-bucket for THIS scan that has >1 member.

        Emits ``(path, size, mtime, inode, dev, nlink)`` tuples so the caller
        can rebuild FileRecords without importing them here.
        """
        sizes = [
            int(row["size"])
            for row in self._conn.execute(
                "SELECT size FROM scan_stage WHERE scan_id = ? "
                "GROUP BY size HAVING COUNT(*) > 1 ORDER BY size",
                (scan_id,),
            ).fetchall()
        ]
        for size in sizes:
            rows = self._conn.execute(
                "SELECT path, size, mtime, inode, dev, nlink "
                "FROM scan_stage WHERE scan_id = ? AND size = ?",
                (scan_id, size),
            ).fetchall()
            yield [
                (
                    str(r["path"]),
                    int(r["size"]),
                    float(r["mtime"]),
                    int(r["inode"] or 0),
                    int(r["dev"] or 0),
                    int(r["nlink"] or 0),
                )
                for r in rows
            ]

    def record_group(
        self,
        kind: str,
        members: list[tuple[Path, float, bool, bool]],
    ) -> int:
        """Persist a duplicate group and its scored members."""
        cur = self._conn.execute(
            "INSERT INTO groups (kind, created_ts) VALUES (?, ?)",
            (kind, time.time()),
        )
        group_id = int(cur.lastrowid or 0)
        self._conn.executemany(
            "INSERT INTO group_members "
            "(group_id, path, score, is_proposed_keeper, is_informational) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (group_id, str(p), score, int(keeper), int(info))
                for p, score, keeper, info in members
            ],
        )
        self._conn.commit()
        return group_id

    def log_decision(
        self,
        group_id: int | None,
        kept_path: Path,
        discarded_paths: list[Path],
        feature_vector: dict[str, Any] | None,
        user_overrode: bool,
    ) -> None:
        """Append a decision row for the future weight-adaptation loop."""
        self._conn.execute(
            """
            INSERT INTO decisions
                (group_id, kept_path, discarded_paths, feature_vector,
                 user_overrode, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                str(kept_path),
                json.dumps([str(p) for p in discarded_paths]),
                json.dumps(feature_vector) if feature_vector is not None else None,
                int(user_overrode),
                time.time(),
            ),
        )
        self._conn.commit()

    def cache_stats(self) -> dict[str, int]:
        """Return counts for cli `cache stats`."""
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM files")
        row = cur.fetchone()
        total = int(row["n"]) if row else 0
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE full_hash IS NOT NULL"
        )
        row = cur.fetchone()
        hashed = int(row["n"]) if row else 0
        return {"files": total, "with_full_hash": hashed}

    def clear_cache(self) -> None:
        """Truncate all cache tables."""
        self._conn.executescript(
            "DELETE FROM files; DELETE FROM groups; DELETE FROM group_members; "
            "DELETE FROM scan_stage;"
        )
        self._conn.commit()
