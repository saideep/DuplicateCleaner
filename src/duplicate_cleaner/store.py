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

-- v0.2 additions.  ``schema_meta`` records the on-disk schema version so
-- ``_migrate_to_v2`` (below) is idempotent.  ``cloud_hash_cache`` will be
-- populated by ``sources/gdrive.py`` and ``sources/onedrive.py`` in later
-- sub-phases; the table is created eagerly so the migration is a single
-- one-shot event.
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cloud_hash_cache (
    source_id     TEXT NOT NULL,
    cloud_file_id TEXT NOT NULL,
    etag          TEXT NOT NULL,
    blake3_hash   TEXT NOT NULL,
    computed_ts   REAL NOT NULL,
    size          INTEGER NOT NULL,
    PRIMARY KEY (source_id, cloud_file_id, etag)
);
CREATE INDEX IF NOT EXISTS idx_cloud_hash_cache_hash
    ON cloud_hash_cache(blake3_hash);

-- v0.3 sub-milestone 5.3-a: extracted signal cache for the organizer.
-- One row per (path, source_id, signal_kind).  ``signal_value`` is TEXT so
-- numeric values are stringified.  Rerunning ``dc organize discover`` reuses
-- rows whose ``stored_mtime`` still matches the file's current mtime; a
-- stat change invalidates every row for the path via the discover
-- orchestrator's mtime tolerance check.
CREATE TABLE IF NOT EXISTS file_signals (
    path         TEXT NOT NULL,
    source_id    TEXT NOT NULL DEFAULT 'local',
    signal_kind  TEXT NOT NULL,
    signal_value TEXT NOT NULL,
    confidence   REAL,
    stored_mtime REAL NOT NULL DEFAULT 0,
    extracted_ts REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (path, source_id, signal_kind)
);
CREATE INDEX IF NOT EXISTS idx_file_signals_path
    ON file_signals(source_id, path);

-- v0.7 image near-duplicate detection.  Perceptual hashes (imagehash pHash
-- at hash_size=16 → 256 bits → 64 hex chars) are cached per local path so
-- a repeat scan on an unchanged image reuses the hash rather than re-
-- opening the file and re-running the DCT.  Keyed on (path, size, mtime)
-- to invalidate the same way ``files`` does.  TTL sweep at scan start
-- mirrors ``cloud_hash_cache``.
CREATE TABLE IF NOT EXISTS image_phash_cache (
    path         TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    phash        TEXT NOT NULL,
    computed_ts  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_phash_cache_computed_ts
    ON image_phash_cache(computed_ts);

-- v0.8 audio near-duplicate detection.  Chromaprint fingerprints (via
-- pyacoustid / the ``fpcalc`` binary) are stored as ``"duration:fingerprint"``
-- text so downstream comparators can duration-filter before running the
-- more expensive fingerprint compare.  Keyed on (path, size, mtime); TTL
-- sweep mirrors ``cloud_hash_cache`` / ``image_phash_cache``.
CREATE TABLE IF NOT EXISTS audio_fingerprint_cache (
    path         TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    fingerprint  TEXT NOT NULL,
    computed_ts  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audio_fingerprint_cache_computed_ts
    ON audio_fingerprint_cache(computed_ts);

-- v0.8 video near-duplicate detection.  N keyframe pHashes joined into
-- ``"duration:phash1,phash2,..."`` — captured via ffmpeg + imagehash.
-- Cached per local path with (size, mtime) invalidation like the audio
-- table.  TTL sweep at scan start.
CREATE TABLE IF NOT EXISTS video_signature_cache (
    path         TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    signature    TEXT NOT NULL,
    computed_ts  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_signature_cache_computed_ts
    ON video_signature_cache(computed_ts);
"""

# v0.2 schema version — bumped whenever ``files`` picks up new columns or
# a new table is added that the migration must guarantee.
_SCHEMA_VERSION = 2

# ALTER statements applied to the v0.1 ``files`` table on first v0.2 open.
# Each is idempotent (ADD COLUMN of an already-present column raises a
# specific OperationalError which the migration catches).
_V2_FILES_ALTERS: tuple[str, ...] = (
    "ALTER TABLE files ADD COLUMN source_id TEXT NOT NULL DEFAULT 'local'",
    "ALTER TABLE files ADD COLUMN foreign_hash TEXT",
    "ALTER TABLE files ADD COLUMN etag TEXT",
    "ALTER TABLE files ADD COLUMN cloud_file_id TEXT",
    "ALTER TABLE files ADD COLUMN owner TEXT",
    "ALTER TABLE files ADD COLUMN is_shared INTEGER NOT NULL DEFAULT 0",
)

_V2_FILES_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_files_source_hash "
    "ON files(source_id, full_hash)",
    "CREATE INDEX IF NOT EXISTS idx_files_source_foreign "
    "ON files(source_id, foreign_hash)",
)

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
        # v0.2 — extend the v0.1 ``files`` table with cloud columns.
        self._migrate_to_v2()

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

    def _migrate_to_v2(self) -> None:
        """Add cloud-source columns to ``files``; idempotent via schema_meta.

        Reads ``schema_meta.version`` (missing = "1"); if < 2 applies the
        ALTER TABLE statements and marks the DB as v2.  Each ADD COLUMN is
        wrapped so a partial prior run (crash between ALTERs) reconverges on
        re-open — SQLite raises a specific ``OperationalError`` for
        duplicate columns, which we swallow and continue.
        """
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        version = int(row["value"]) if row is not None else 1
        if version >= _SCHEMA_VERSION:
            return
        for stmt in _V2_FILES_ALTERS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        for stmt in _V2_FILES_INDEXES:
            self._conn.execute(stmt)
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES "
            "('version', ?)",
            (str(_SCHEMA_VERSION),),
        )
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

    def iter_singleton_stage_records(
        self, scan_id: str
    ) -> Iterator[tuple[str, int, float, int, int, int]]:
        """Yield staged rows whose size bucket has exactly one member.

        Cheap: driven purely by ``COUNT(*)`` on the ``size`` group-by, so
        no hashing is required. Callers use these rows to build the
        singleton section of the report at "essentially free" cost.
        """
        rows = self._conn.execute(
            "SELECT path, size, mtime, inode, dev, nlink FROM scan_stage "
            "WHERE scan_id = ? AND size IN ("
            "  SELECT size FROM scan_stage WHERE scan_id = ? "
            "  GROUP BY size HAVING COUNT(*) = 1"
            ")",
            (scan_id, scan_id),
        ).fetchall()
        for r in rows:
            yield (
                str(r["path"]),
                int(r["size"]),
                float(r["mtime"]),
                int(r["inode"] or 0),
                int(r["dev"] or 0),
                int(r["nlink"] or 0),
            )

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
            "DELETE FROM scan_stage; DELETE FROM cloud_hash_cache; "
            "DELETE FROM image_phash_cache; "
            "DELETE FROM audio_fingerprint_cache; "
            "DELETE FROM video_signature_cache;"
        )
        self._conn.commit()

    def get_cloud_hash(
        self, source_id: str, cloud_file_id: str, etag: str
    ) -> str | None:
        """Return a cached BLAKE3 for a cloud file, keyed by (source, id, etag).

        A hit skips a byte-for-byte re-download during reconcile.  Misses are
        expected whenever etag changes — the row for the old etag remains on
        disk but is never read again.
        """
        cur = self._conn.execute(
            "SELECT blake3_hash FROM cloud_hash_cache "
            "WHERE source_id = ? AND cloud_file_id = ? AND etag = ?",
            (source_id, cloud_file_id, etag),
        )
        row = cur.fetchone()
        if row is None:
            return None
        h = row["blake3_hash"]
        return h if isinstance(h, str) else None

    def put_cloud_hash(
        self,
        source_id: str,
        cloud_file_id: str,
        etag: str,
        blake3_hash: str,
        size: int,
    ) -> None:
        """Persist a computed BLAKE3 for a cloud file.  Idempotent."""
        self._conn.execute(
            "INSERT OR REPLACE INTO cloud_hash_cache "
            "(source_id, cloud_file_id, etag, blake3_hash, computed_ts, size) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source_id, cloud_file_id, etag, blake3_hash, time.time(), size),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # v0.3 organize discovery — signal cache.                            #
    # ------------------------------------------------------------------ #

    def get_cached_signals(
        self, path: str, source_id: str, mtime: float
    ) -> Any:
        """Return a cached SignalSet dict, or None on miss / mtime drift.

        Discover rerun cost is dominated by PDF text extraction; caching
        keeps a stable-corpus repeat scan close to walker cost.
        """
        cur = self._conn.execute(
            "SELECT signal_value, stored_mtime FROM file_signals "
            "WHERE path = ? AND source_id = ? AND signal_kind = ?",
            (path, source_id, "__signalset__"),
        )
        row = cur.fetchone()
        if row is None:
            return None
        stored_mtime = float(row["stored_mtime"])
        if abs(stored_mtime - mtime) > _MTIME_EPS:
            return None
        raw = row["signal_value"]
        if not isinstance(raw, str) or not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        # Import here to avoid a circular import at module load.
        from duplicate_cleaner.organize.signals import SignalSet

        try:
            return SignalSet(**_signal_set_from_json(data))
        except (TypeError, ValueError):
            return None

    def put_signal_set(
        self,
        path: str,
        source_id: str,
        mtime: float,
        signal_set: Any,
    ) -> None:
        """Persist a SignalSet as a JSON blob keyed on path+source+mtime."""
        try:
            payload = _signal_set_to_json(signal_set)
        except (TypeError, ValueError):
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO file_signals "
            "(path, source_id, signal_kind, signal_value, confidence, "
            " stored_mtime, extracted_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                path,
                source_id,
                "__signalset__",
                json.dumps(payload),
                None,
                mtime,
                time.time(),
            ),
        )
        self._conn.commit()

    def get_cached_phash(
        self, path: Path, size: int, mtime: float
    ) -> str | None:
        """Return the cached perceptual hash for ``path`` if stats match, else None.

        v0.7 image near-duplicate detection.  Same eps-tolerant mtime match
        as :meth:`get_cached_hash` — SQLite REAL round-trip can lose the
        last bit of a float, so an exact match check would silently drop
        cache hits.
        """
        cur = self._conn.execute(
            "SELECT phash FROM image_phash_cache "
            "WHERE path = ? AND size = ? AND ABS(mtime - ?) <= ?",
            (str(path), size, mtime, _MTIME_EPS),
        )
        row = cur.fetchone()
        if row is None:
            return None
        h = row["phash"]
        return h if isinstance(h, str) else None

    def put_phash(
        self, path: Path, size: int, mtime: float, phash: str
    ) -> None:
        """Persist a computed perceptual hash for a local image path.  Idempotent."""
        self._conn.execute(
            "INSERT OR REPLACE INTO image_phash_cache "
            "(path, size, mtime, phash, computed_ts) VALUES (?, ?, ?, ?, ?)",
            (str(path), size, mtime, phash, time.time()),
        )
        self._conn.commit()

    def purge_stale_phashes(self, max_age_days: float = 90.0) -> int:
        """Drop image_phash_cache rows older than ``max_age_days``.

        Mirror of :meth:`purge_stale_cloud_hashes` — cheap disk hygiene at
        scan start so the cache DB never grows without bound.  Rows for
        still-live images are re-populated on the next scan.
        """
        cutoff = time.time() - (max_age_days * 86400.0)
        cur = self._conn.execute(
            "DELETE FROM image_phash_cache WHERE computed_ts < ?", (cutoff,)
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def purge_stale_cloud_hashes(self, max_age_days: float = 90.0) -> int:
        """Drop cloud_hash_cache rows older than ``max_age_days``.

        Cheap disk-hygiene sweep — called at scan start so a long-running
        cache DB never grows without bound.  Cache rows for still-live files
        are re-populated on the next scan.
        """
        cutoff = time.time() - (max_age_days * 86400.0)
        cur = self._conn.execute(
            "DELETE FROM cloud_hash_cache WHERE computed_ts < ?", (cutoff,)
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ #
    # v0.8 — audio + video near-duplicate caches.                        #
    # ------------------------------------------------------------------ #

    def get_cached_audio_fingerprint(
        self, path: Path, size: int, mtime: float
    ) -> str | None:
        """Return the cached ``"duration:fingerprint"`` string or None.

        v0.8 audio near-duplicate detection.  Same eps-tolerant mtime match
        as :meth:`get_cached_hash` — SQLite REAL round-trip can lose the
        last bit of a float, so an exact match check would silently drop
        cache hits.  A miss returns None; :func:`compare.audio.compute_audio_fingerprint`
        recomputes and (when a store is passed) re-populates the row.
        """
        cur = self._conn.execute(
            "SELECT fingerprint FROM audio_fingerprint_cache "
            "WHERE path = ? AND size = ? AND ABS(mtime - ?) <= ?",
            (str(path), size, mtime, _MTIME_EPS),
        )
        row = cur.fetchone()
        if row is None:
            return None
        fp = row["fingerprint"]
        return fp if isinstance(fp, str) else None

    def put_audio_fingerprint(
        self, path: Path, size: int, mtime: float, fingerprint: str
    ) -> None:
        """Persist a Chromaprint fingerprint for a local audio path.  Idempotent."""
        self._conn.execute(
            "INSERT OR REPLACE INTO audio_fingerprint_cache "
            "(path, size, mtime, fingerprint, computed_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(path), size, mtime, fingerprint, time.time()),
        )
        self._conn.commit()

    def purge_stale_audio_fingerprints(self, max_age_days: float = 90.0) -> int:
        """Drop audio_fingerprint_cache rows older than ``max_age_days``.

        Mirror of :meth:`purge_stale_phashes` — cheap disk hygiene at scan
        start so the cache DB never grows without bound.  Rows for still-
        live audio files are re-populated on the next scan.
        """
        cutoff = time.time() - (max_age_days * 86400.0)
        cur = self._conn.execute(
            "DELETE FROM audio_fingerprint_cache WHERE computed_ts < ?",
            (cutoff,),
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def get_cached_video_signature(
        self, path: Path, size: int, mtime: float
    ) -> str | None:
        """Return the cached ``"duration:phash1,phash2,..."`` signature or None.

        v0.8 video near-duplicate detection.  Same eps-tolerant mtime match
        as :meth:`get_cached_hash`.
        """
        cur = self._conn.execute(
            "SELECT signature FROM video_signature_cache "
            "WHERE path = ? AND size = ? AND ABS(mtime - ?) <= ?",
            (str(path), size, mtime, _MTIME_EPS),
        )
        row = cur.fetchone()
        if row is None:
            return None
        sig = row["signature"]
        return sig if isinstance(sig, str) else None

    def put_video_signature(
        self, path: Path, size: int, mtime: float, signature: str
    ) -> None:
        """Persist a video signature (duration + keyframe pHashes) for a local path."""
        self._conn.execute(
            "INSERT OR REPLACE INTO video_signature_cache "
            "(path, size, mtime, signature, computed_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(path), size, mtime, signature, time.time()),
        )
        self._conn.commit()

    def purge_stale_video_signatures(self, max_age_days: float = 90.0) -> int:
        """Drop video_signature_cache rows older than ``max_age_days``."""
        cutoff = time.time() - (max_age_days * 86400.0)
        cur = self._conn.execute(
            "DELETE FROM video_signature_cache WHERE computed_ts < ?",
            (cutoff,),
        )
        self._conn.commit()
        return int(cur.rowcount or 0)


def _signal_set_to_json(signal_set: Any) -> dict[str, Any]:
    """Coerce a SignalSet dataclass to a JSON-safe dict; frozensets become sorted lists."""
    out: dict[str, Any] = {}
    for k, v in signal_set.__dict__.items():
        if isinstance(v, frozenset):
            out[k] = sorted(v)
        elif isinstance(v, tuple):
            out[k] = list(v)
        else:
            out[k] = v
    return out


def _signal_set_from_json(data: dict[str, Any]) -> dict[str, Any]:
    """Restore frozenset/tuple typing from a stored JSON blob."""
    out = dict(data)
    if "fname_keywords" in out and out["fname_keywords"] is not None:
        out["fname_keywords"] = frozenset(out["fname_keywords"])
    if "pdf_keyword_hits" in out and out["pdf_keyword_hits"] is not None:
        out["pdf_keyword_hits"] = frozenset(out["pdf_keyword_hits"])
    if "path_tokens" in out and out["path_tokens"] is not None:
        out["path_tokens"] = tuple(out["path_tokens"])
    return out
