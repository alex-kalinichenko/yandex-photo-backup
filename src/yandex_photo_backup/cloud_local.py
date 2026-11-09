"""Helpers for Windows Files On-Demand (Yandex Disk) local copies.

Hydrating a cloud placeholder writes bytes onto the sync drive (often C:).
After a verified copy into the backup root on another volume, dehydrate the
source so C: space is reclaimed while the cloud placeholder remains.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Keep this much free on the hydrate volume before opening a new file.
DEFAULT_FREE_RESERVE_BYTES = 5 * 1024**3

# Win32 cloud-file attribute bits (CfAPI / Files On-Demand).
_FILE_ATTRIBUTE_PINNED = 0x00080000
_FILE_ATTRIBUTE_UNPINNED = 0x00100000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def free_bytes(path: Path) -> int:
    """Return free bytes on the volume that contains ``path``."""
    return int(shutil.disk_usage(path).free)


def ensure_free_space(
    path: Path,
    needed_bytes: int,
    *,
    reserve_bytes: int = DEFAULT_FREE_RESERVE_BYTES,
) -> None:
    """Raise ``OSError`` if the volume cannot fit ``needed_bytes`` plus reserve."""
    free = free_bytes(path)
    required = max(0, int(needed_bytes)) + max(0, int(reserve_bytes))
    if free < required:
        raise OSError(
            f"insufficient free space on {path.anchor or path}: "
            f"free={free} required={required} (need={needed_bytes} + reserve={reserve_bytes})"
        )


def _win_file_attributes(path: Path) -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes

    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    if attrs == _INVALID_FILE_ATTRIBUTES:
        return None
    return int(attrs)


def is_cloud_placeholder(path: Path) -> bool:
    """Return True when the path looks like an online-only cloud placeholder."""
    attrs = _win_file_attributes(path)
    if attrs is None:
        return False
    return bool(attrs & _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS) or bool(
        attrs & _FILE_ATTRIBUTE_UNPINNED
    )


def is_locally_hydrated(path: Path) -> bool:
    """Return True when a cloud file appears fully present on local disk."""
    if not path.is_file():
        return False
    attrs = _win_file_attributes(path)
    if attrs is None:
        # Non-Windows / unknown: treat existing regular files as hydrated.
        return True
    if attrs & _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
        return False
    if attrs & _FILE_ATTRIBUTE_UNPINNED and not (attrs & _FILE_ATTRIBUTE_PINNED):
        # Unpinned without recall bit can still be partially local; prefer size>0
        # and absence of recall as "present enough" — dehydrate is still safe.
        return True
    return True


def dehydrate_cloud_file(path: Path) -> bool:
    """Mark a Windows cloud file online-only, freeing local disk blocks.

    Uses ``attrib +U -P`` (unpin). Returns True when the command succeeds.
    No-op (returns False) on non-Windows or missing files.
    """
    if sys.platform != "win32":
        return False
    if not path.exists():
        return False
    try:
        # +U = unpinned (online-only), -P = clear pinned.
        completed = subprocess.run(
            ["attrib", "+U", "-P", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("dehydrate failed %s: %s", path, exc)
        return False
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        logger.warning(
            "dehydrate attrib exit %s for %s: %s",
            completed.returncode,
            path,
            err or "no output",
        )
        return False
    logger.debug("dehydrated %s", path)
    return True


def same_volume(a: Path, b: Path) -> bool:
    """Return True when both paths resolve onto the same drive/volume."""
    try:
        a_dev = os.stat(a).st_dev
        b_dev = os.stat(b if b.exists() else b.parent).st_dev
        return a_dev == b_dev
    except OSError:
        return a.resolve().anchor.lower() == b.resolve().anchor.lower()
