"""Path helpers and safe filename sanitization."""

from __future__ import annotations

import os
import re
from pathlib import Path

WINDOWS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, fallback: str = 'unnamed') -> str:
    """Return a Windows-safe filename without path components."""
    base = Path(str(name).replace('\\', '/')).name.strip()
    base = WINDOWS_INVALID.sub('_', base).rstrip(' .')
    if not base or base in {'.', '..'}:
        return fallback
    if len(base) > 180:
        stem, suffix = os.path.splitext(base)
        base = f'{stem[:160]}{suffix[:20]}'
    return base


def ensure_under(base_dir: Path, candidate: Path) -> Path:
    """Ensure candidate resolves under base_dir; raise on traversal."""
    base = base_dir.resolve()
    target = candidate.resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f'path escapes base dir: {candidate}') from exc
    return target


def year_month_subdir(year: int | None, month: int | None) -> Path:
    """Build relative year/month directory path."""
    if year and 1970 <= year <= 2100:
        if month and 1 <= month <= 12:
            return Path(f'{year:04d}') / f'{month:02d}'
        return Path(f'{year:04d}')
    return Path('unknown_date')
