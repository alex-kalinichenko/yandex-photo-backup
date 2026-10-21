"""SQLite manifest for remote objects and verification state."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    remote_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    remote_path TEXT,
    cluster_id TEXT,
    media_type TEXT,
    size INTEGER,
    md5 TEXT,
    sha256 TEXT,
    original_url TEXT,
    taken_at TEXT,
    year INTEGER,
    month INTEGER,
    local_path TEXT,
    download_status TEXT NOT NULL DEFAULT 'pending',
    verify_status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at REAL,
    verified_at REAL,
    updated_at REAL NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_objects_download ON objects(download_status, attempts);
CREATE INDEX IF NOT EXISTS idx_objects_verify ON objects(verify_status);
CREATE INDEX IF NOT EXISTS idx_objects_cluster ON objects(cluster_id);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id TEXT PRIMARY KEY,
    item_count INTEGER,
    fetched INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    kind TEXT NOT NULL,
    stats_json TEXT
);
"""


class ManifestDB:
    """Append/update backup manifest. Never deletes local files on remote disappear."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Shared across download worker threads; callers must serialize via a lock.
        self.conn = sqlite3.connect(self.path, timeout=60, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')
        self.conn.execute('PRAGMA busy_timeout=60000')
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute('PRAGMA table_info(objects)')}
        if 'original_url' not in cols:
            self.conn.execute('ALTER TABLE objects ADD COLUMN original_url TEXT')

    def close(self) -> None:
        self.conn.close()

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            'INSERT INTO meta(key, value) VALUES(?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return None if row is None else str(row['value'])

    def upsert_cluster(self, cluster_id: str, item_count: int | None) -> None:
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO clusters(cluster_id, item_count, fetched, updated_at)
            VALUES(?, ?, 0, ?)
            ON CONFLICT(cluster_id) DO UPDATE SET
                item_count=COALESCE(excluded.item_count, clusters.item_count),
                updated_at=excluded.updated_at
            """,
            (cluster_id, item_count, now),
        )

    def mark_cluster_fetched(self, cluster_id: str) -> None:
        self.conn.execute(
            'UPDATE clusters SET fetched=1, updated_at=? WHERE cluster_id=?',
            (time.time(), cluster_id),
        )

    def unfetched_clusters(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                'SELECT cluster_id, item_count FROM clusters WHERE fetched=0 ORDER BY cluster_id'
            )
        )

    def reset_cluster_fetched(self) -> None:
        self.conn.execute('UPDATE clusters SET fetched=0')
        self.conn.commit()

    def upsert_object(self, obj: dict[str, Any]) -> None:
        now = time.time()
        remote_id = obj['remote_id']
        existing = self.conn.execute(
            'SELECT download_status, verify_status, local_path, md5, size FROM objects WHERE remote_id=?',
            (remote_id,),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO objects(
                    remote_id, name, remote_path, cluster_id, media_type, size, md5, sha256,
                    original_url, taken_at, year, month, local_path, download_status, verify_status,
                    error, attempts, updated_at, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending', 'pending',
                          NULL, 0, ?, ?, ?)
                """,
                (
                    remote_id,
                    obj['name'],
                    obj.get('remote_path'),
                    obj.get('cluster_id'),
                    obj.get('media_type'),
                    obj.get('size'),
                    obj.get('md5'),
                    obj.get('sha256'),
                    obj.get('original_url'),
                    obj.get('taken_at'),
                    obj.get('year'),
                    obj.get('month'),
                    now,
                    now,
                    now,
                ),
            )
            return

        # Keep verified/downloaded rows; update metadata; re-queue if size/md5 changed.
        size = obj.get('size')
        md5 = obj.get('md5')
        changed = False
        if size is not None and existing['size'] is not None and int(size) != int(existing['size']):
            changed = True
        if md5 and existing['md5'] and str(md5).lower() != str(existing['md5']).lower():
            changed = True

        download_status = existing['download_status']
        verify_status = existing['verify_status']
        if changed and download_status in {'downloaded', 'verified'}:
            download_status = 'pending'
            verify_status = 'pending'

        self.conn.execute(
            """
            UPDATE objects SET
                name=?,
                remote_path=COALESCE(?, remote_path),
                cluster_id=COALESCE(?, cluster_id),
                media_type=COALESCE(?, media_type),
                size=COALESCE(?, size),
                md5=COALESCE(?, md5),
                sha256=COALESCE(?, sha256),
                original_url=COALESCE(?, original_url),
                taken_at=COALESCE(?, taken_at),
                year=COALESCE(?, year),
                month=COALESCE(?, month),
                download_status=?,
                verify_status=?,
                updated_at=?,
                last_seen_at=?
            WHERE remote_id=?
            """,
            (
                obj['name'],
                obj.get('remote_path'),
                obj.get('cluster_id'),
                obj.get('media_type'),
                obj.get('size'),
                obj.get('md5'),
                obj.get('sha256'),
                obj.get('original_url'),
                obj.get('taken_at'),
                obj.get('year'),
                obj.get('month'),
                download_status,
                verify_status,
                now,
                now,
                remote_id,
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    def pending_downloads(
        self,
        limit: int | None = None,
        *,
        with_md5_only: bool = False,
        media: str = 'all',
    ) -> list[sqlite3.Row]:
        # Photos before videos; years 2025→older, then 2026 last (same for videos).
        where = "download_status IN ('pending', 'failed')"
        if with_md5_only:
            where += " AND md5 IS NOT NULL AND md5 != ''"
        media_norm = (media or 'all').lower()
        if media_norm in {'photo', 'photos', 'image', 'images'}:
            where += " AND COALESCE(media_type, '') NOT LIKE 'video/%'"
        elif media_norm in {'video', 'videos'}:
            where += " AND COALESCE(media_type, '') LIKE 'video/%'"
        elif media_norm not in {'all', '*'}:
            raise ValueError(f'Unsupported media filter: {media}')
        sql = (
            f'SELECT * FROM objects WHERE {where} '
            'ORDER BY attempts ASC, '
            "CASE WHEN COALESCE(media_type, '') LIKE 'video/%' THEN 1 ELSE 0 END, "
            # 2025, 2024, ... first; current year 2026 last; unknown year after that.
            'CASE '
            'WHEN year IS NULL THEN 2 '
            'WHEN year >= 2026 THEN 1 '
            'ELSE 0 END, '
            'CASE WHEN year >= 2026 THEN year END ASC, '
            'CASE WHEN year < 2026 THEN year END DESC, '
            "CASE WHEN md5 IS NOT NULL AND md5 != '' THEN 0 ELSE 1 END, "
            "CASE WHEN remote_path LIKE '/photounlim/%' THEN 0 ELSE 1 END, "
            'cluster_id ASC, size ASC'
        )
        if limit is not None:
            sql += f' LIMIT {int(limit)}'
        return list(self.conn.execute(sql))

    def mark_attempt(self, remote_id: str) -> None:
        self.conn.execute(
            'UPDATE objects SET attempts=attempts+1, last_attempt_at=?, updated_at=? WHERE remote_id=?',
            (time.time(), time.time(), remote_id),
        )
        self.conn.commit()

    def mark_verified(
        self,
        remote_id: str,
        local_path: str,
        *,
        local_md5: str | None = None,
    ) -> None:
        now = time.time()
        self.conn.execute(
            """
            UPDATE objects SET
                local_path=?,
                download_status='verified',
                verify_status='verified',
                error=NULL,
                verified_at=?,
                updated_at=?,
                md5=COALESCE(?, md5)
            WHERE remote_id=?
            """,
            (local_path, now, now, local_md5, remote_id),
        )
        self.conn.commit()

    def mark_failed(self, remote_id: str, error: str, *, permanent: bool = False) -> None:
        status = 'failed_permanent' if permanent else 'failed'
        self.conn.execute(
            """
            UPDATE objects SET
                download_status=?,
                verify_status='failed',
                error=?,
                updated_at=?
            WHERE remote_id=?
            """,
            (status, error[:1000], time.time(), remote_id),
        )
        self.conn.commit()

    def iter_all(self) -> Iterable[sqlite3.Row]:
        yield from self.conn.execute('SELECT * FROM objects ORDER BY year, month, name')

    def stats(self) -> dict[str, Any]:
        rows = list(
            self.conn.execute(
                'SELECT download_status, verify_status, COUNT(*) AS n, '
                'COALESCE(SUM(size), 0) AS bytes FROM objects GROUP BY download_status, verify_status'
            )
        )
        total = self.conn.execute('SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects').fetchone()
        verified = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects WHERE verify_status='verified'"
        ).fetchone()
        missing_local = self.conn.execute(
            "SELECT COUNT(*) FROM objects WHERE verify_status!='verified'"
        ).fetchone()[0]
        failed_perm = self.conn.execute(
            "SELECT COUNT(*) FROM objects WHERE download_status='failed_permanent'"
        ).fetchone()[0]
        zero = self.conn.execute(
            "SELECT COUNT(*) FROM objects WHERE size IS NOT NULL AND size <= 0"
        ).fetchone()[0]
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM objects WHERE download_status IN ('pending','failed')"
        ).fetchone()[0]
        return {
            'remote_objects': int(total[0]),
            'remote_total_bytes': int(total[1]),
            'verified': int(verified[0]),
            'verified_bytes': int(verified[1]),
            'not_verified': int(missing_local),
            'failed_permanent': int(failed_perm),
            'zero_size_remote': int(zero),
            'pending': int(pending),
            'by_status': [
                {
                    'download_status': r['download_status'],
                    'verify_status': r['verify_status'],
                    'count': r['n'],
                    'bytes': r['bytes'],
                }
                for r in rows
            ],
        }

    def failed_rows(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT remote_id, name, taken_at, size, error, download_status "
                "FROM objects WHERE download_status IN ('failed', 'failed_permanent') "
                'OR verify_status = \'failed\' ORDER BY name'
            )
        )
