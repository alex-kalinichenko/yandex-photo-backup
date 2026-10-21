"""Print photo pending/verified counts by year."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

DB = Path(os.environ.get('YANDEX_PHOTO_ROOT', Path.home() / 'YandexPhoto')) / (
    'yandex_photo_backup.sqlite'
)


def main() -> None:
    if not DB.exists():
        print(f'DB not found: {DB}', file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        """
        SELECT year,
               SUM(CASE WHEN verify_status = 'verified' THEN 1 ELSE 0 END) AS v,
               SUM(
                 CASE
                   WHEN download_status IN ('pending', 'failed') THEN 1
                   ELSE 0
                 END
               ) AS p,
               COUNT(*) AS t
        FROM objects
        WHERE COALESCE(media_type, '') NOT LIKE 'video/%'
        GROUP BY year
        ORDER BY
          CASE WHEN year IS NULL THEN 2 WHEN year >= 2026 THEN 1 ELSE 0 END,
          CASE WHEN year >= 2026 THEN year END ASC,
          CASE WHEN year < 2026 THEN year END DESC
        """
    )
    print('year\tverified\tpending\ttotal')
    for year, v, p, t in rows:
        print(f'{year}\t{v}\t{p}\t{t}')
    conn.close()


if __name__ == '__main__':
    main()
