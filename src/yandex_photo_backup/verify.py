"""Per-file integrity checks."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

HTML_SNIFF = (b'<!DOCTYPE', b'<html', b'<HTML', b'{"error"', b'captcha')


def md5_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute MD5 hex digest of a local file."""
    digest = hashlib.md5()
    with path.open('rb') as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sniff_is_error_page(path: Path) -> bool:
    """Detect HTML/JSON error pages saved instead of media."""
    try:
        with path.open('rb') as fh:
            head = fh.read(256)
    except OSError:
        return False
    stripped = head.lstrip()
    return any(stripped.startswith(sig) for sig in HTML_SNIFF)


def verify_local_file(
    path: Path,
    *,
    expected_size: int | None,
    expected_md5: str | None,
) -> tuple[bool, str, str | None]:
    """Return (ok, reason, actual_md5)."""
    if not path.exists():
        return False, 'missing file', None
    actual_size = path.stat().st_size
    if actual_size <= 0:
        return False, 'zero-byte file', None
    if expected_size is not None and int(expected_size) > 0 and actual_size != int(expected_size):
        return False, f'size mismatch local={actual_size} remote={expected_size}', None
    if sniff_is_error_page(path):
        return False, 'content looks like error/HTML page', None
    actual_md5 = md5_file(path)
    if expected_md5:
        if actual_md5.lower() != expected_md5.lower():
            return False, f'md5 mismatch local={actual_md5} remote={expected_md5}', actual_md5
    return True, 'ok', actual_md5
