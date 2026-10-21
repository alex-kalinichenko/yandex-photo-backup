"""Human-readable reconciliation reports."""

from __future__ import annotations

from pathlib import Path

from .db import ManifestDB
from .verify import verify_local_file


def human_bytes(n: int | float) -> str:
    value = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
        if abs(value) < 1024.0 or unit == 'PB':
            return f'{value:.2f} {unit}'
        value /= 1024.0
    return f'{n} B'


def format_stats(stats: dict) -> str:
    lines = [
        f"Remote objects:        {stats['remote_objects']}",
        f"Remote total size:     {human_bytes(stats['remote_total_bytes'])}",
        '',
        f"Verified:              {stats['verified']}",
        f"Verified size:         {human_bytes(stats['verified_bytes'])}",
        f"Pending:               {stats['pending']}",
        f"Not verified:          {stats['not_verified']}",
        f"Failed permanently:    {stats['failed_permanent']}",
        f"Zero size (remote):    {stats['zero_size_remote']}",
    ]
    return '\n'.join(lines)


def reconcile_local(db: ManifestDB) -> dict[str, int]:
    """Re-check every verified/downloaded local file against manifest."""
    corrupt = 0
    missing = 0
    verified = 0
    for row in db.iter_all():
        local = row['local_path']
        if not local:
            if row['verify_status'] != 'verified':
                missing += 1
            continue
        path = Path(local)
        ok, reason, _ = verify_local_file(
            path,
            expected_size=row['size'],
            expected_md5=row['md5'],
        )
        if ok:
            verified += 1
            if row['verify_status'] != 'verified':
                db.mark_verified(row['remote_id'], str(path))
        else:
            if not path.exists():
                missing += 1
            else:
                corrupt += 1
            db.mark_failed(row['remote_id'], f'reconcile: {reason}', permanent=False)
    return {'verified': verified, 'missing': missing, 'corrupt': corrupt}


def write_failed_report(db: ManifestDB, path: Path) -> int:
    rows = db.failed_rows()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as fh:
        fh.write('remote_id\tname\ttaken_at\tsize\tstatus\terror\n')
        for row in rows:
            fh.write(
                f"{row['remote_id']}\t{row['name']}\t{row['taken_at']}\t"
                f"{row['size']}\t{row['download_status']}\t{row['error']}\n"
            )
    return len(rows)
