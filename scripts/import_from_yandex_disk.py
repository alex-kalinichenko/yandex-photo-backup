"""Import already-synced Yandex Disk files into the backup tree.

Many pending photos/videos live under the desktop client's sync folder
(e.g. C:\\Users\\...\\Yandex.Disk\\...) and do not need CDN/prepare download.
This copies matching files into YANDEX_PHOTO_ROOT/unlimited and marks them verified.

Hydrating Files On-Demand placeholders writes bytes onto the sync volume
(usually C:). After a verified copy onto the backup volume, sources are
dehydrated (online-only) so C: space is reclaimed.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yandex_photo_backup.cloud_local import (  # noqa: E402
    DEFAULT_FREE_RESERVE_BYTES,
    dehydrate_cloud_file,
    ensure_free_space,
    free_bytes,
    is_cloud_placeholder,
    same_volume,
)
from yandex_photo_backup.config import Config  # noqa: E402
from yandex_photo_backup.db import ManifestDB  # noqa: E402
from yandex_photo_backup.downloader import local_path_for  # noqa: E402
from yandex_photo_backup.verify import md5_file, verify_local_file  # noqa: E402

logger = logging.getLogger("import_from_yandex_disk")


def _disk_local_path(yandex_disk_root: Path, remote_path: str | None) -> Path | None:
    if not remote_path:
        return None
    p = remote_path.replace("\\", "/")
    if not p.startswith("/disk/"):
        return None
    rel = p[len("/disk/") :]
    return yandex_disk_root / Path(rel)


def _maybe_dehydrate(src: Path, dest: Path, *, enabled: bool) -> bool:
    """Dehydrate sync-folder source after a verified backup copy on another volume."""
    if not enabled:
        return False
    if same_volume(src, dest):
        return False
    if is_cloud_placeholder(src):
        return False
    ok = dehydrate_cloud_file(src)
    if ok:
        logger.info("dehydrated source %s (freed local blocks on sync volume)", src)
    return ok


def _reclaim_verified(
    db: ManifestDB,
    yd: Path,
    files_dir: Path,
    *,
    media_filter: str,
    dry_run: bool,
    limit: int | None,
) -> int:
    """Dehydrate Yandex.Disk sources that already have a verified backup elsewhere."""
    rows = list(
        db.conn.execute(
            f"""
            SELECT remote_id, name, remote_path, size, md5, media_type, year, month,
                   local_path
            FROM objects
            WHERE verify_status = 'verified'
              AND remote_path LIKE '/disk/%'
              {media_filter}
            ORDER BY size DESC
            """
        )
    )
    if limit is not None:
        rows = rows[:limit]

    freed = 0
    freed_bytes = 0
    logger.info(
        "Reclaim pass: scanning %s verified /disk/ objects (free=%.1fGB)",
        len(rows),
        free_bytes(yd) / (1024**3),
    )
    for row in rows:
        src = _disk_local_path(yd, row["remote_path"])
        if src is None or not src.is_file():
            continue
        if is_cloud_placeholder(src):
            continue
        dest = (
            Path(row["local_path"])
            if row["local_path"]
            else local_path_for(row, files_dir)
        )
        if not dest.is_file():
            continue
        if same_volume(src, dest):
            continue
        ok, _reason, _ = verify_local_file(
            dest, expected_size=row["size"], expected_md5=row["md5"]
        )
        if not ok:
            continue
        size = int(row["size"] or 0)
        if dry_run:
            logger.info("DRY-RUN dehydrate %s (%s bytes)", src, size)
            freed += 1
            freed_bytes += size
            continue
        if _maybe_dehydrate(src, dest, enabled=True):
            freed += 1
            freed_bytes += size
            if freed % 50 == 0:
                logger.info(
                    "reclaim dehydrated=%s (%.1f GB) free=%.1fGB",
                    freed,
                    freed_bytes / 1e9,
                    free_bytes(yd) / (1024**3),
                )
    logger.info(
        "Reclaim done dehydrated=%s (%.2f GB) free=%.1fGB",
        freed,
        freed_bytes / 1e9,
        free_bytes(yd) / (1024**3),
    )
    return freed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--yandex-disk",
        type=Path,
        default=Path.home() / "Yandex.Disk",
        help="Desktop client sync root (default: ~/Yandex.Disk)",
    )
    ap.add_argument("--root", type=Path, default=None, help="Backup root override")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--media",
        choices=["all", "photos", "videos"],
        default="all",
        help="Filter by media type (videos hydrate reliably via Disk client)",
    )
    ap.add_argument(
        "--keep-local",
        action="store_true",
        help="Do not dehydrate Yandex.Disk sources after a verified copy",
    )
    ap.add_argument(
        "--reclaim-verified",
        action="store_true",
        help="Also dehydrate sources for objects already verified on the backup volume",
    )
    ap.add_argument(
        "--reclaim-only",
        action="store_true",
        help="Skip import; only dehydrate sources of already-verified /disk/ objects",
    )
    ap.add_argument(
        "--min-free-gb",
        type=float,
        default=DEFAULT_FREE_RESERVE_BYTES / (1024**3),
        help="Abort a hydrate when sync volume free space would fall below this (default: 5)",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    yd = args.yandex_disk.expanduser()
    if not yd.is_dir():
        logger.error("Yandex Disk root not found: %s", yd)
        return 1

    cfg = Config.from_env(args.root)
    cfg.ensure_dirs()
    db = ManifestDB(cfg.db_path)

    media_filter = ""
    if args.media in {"photo", "photos"}:
        media_filter = " AND COALESCE(media_type, '') NOT LIKE 'video/%'"
    elif args.media in {"video", "videos"}:
        media_filter = " AND COALESCE(media_type, '') LIKE 'video/%'"

    dehydrate_after = not args.keep_local and not args.dry_run
    try:
        if args.reclaim_only or args.reclaim_verified:
            _reclaim_verified(
                db,
                yd,
                cfg.files_dir,
                media_filter=media_filter,
                dry_run=args.dry_run,
                limit=args.limit if args.reclaim_only else None,
            )
            if args.reclaim_only:
                return 0

        rows = list(
            db.conn.execute(
                f"""
                SELECT remote_id, name, remote_path, size, md5, media_type, year, month,
                       download_status
                FROM objects
                WHERE download_status IN ('pending', 'failed', 'failed_permanent')
                {media_filter}
                ORDER BY size ASC
                """
            )
        )
        if args.limit:
            rows = rows[: args.limit]

        imported = 0
        imported_bytes = 0
        missing = 0
        size_mismatch = 0
        errors = 0
        dehydrated = 0
        skipped_low_space = 0
        reserve_bytes = int(max(0.0, args.min_free_gb) * (1024**3))

        logger.info(
            "Scanning %s pending/failed objects against %s (dehydrate=%s min_free=%.1fGB)",
            len(rows),
            yd,
            dehydrate_after,
            reserve_bytes / (1024**3),
        )
        logger.info("sync volume free=%.1f GB", free_bytes(yd) / (1024**3))

        for row in rows:
            src = _disk_local_path(yd, row["remote_path"])
            if src is None or not src.is_file():
                missing += 1
                continue
            try:
                src_size = src.stat().st_size
            except OSError as exc:
                logger.warning("stat failed %s: %s", src, exc)
                errors += 1
                continue
            expected = int(row["size"] or 0)
            if expected > 0 and src_size != expected:
                size_mismatch += 1
                continue

            dest = local_path_for(row, cfg.files_dir)
            if args.dry_run:
                logger.info("DRY-RUN %s <- %s (%s bytes)", dest, src, src_size)
                imported += 1
                imported_bytes += src_size
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                ok, reason, _ = verify_local_file(
                    dest, expected_size=row["size"], expected_md5=row["md5"]
                )
                if ok:
                    local_md5 = md5_file(dest)
                    db.mark_verified(row["remote_id"], str(dest), local_md5=local_md5)
                    imported += 1
                    imported_bytes += src_size
                    if _maybe_dehydrate(src, dest, enabled=dehydrate_after):
                        dehydrated += 1
                    continue
                dest.unlink(missing_ok=True)

            # Hydrate only when the backup copy is missing/invalid.
            try:
                ensure_free_space(src, src_size, reserve_bytes=reserve_bytes)
            except OSError as exc:
                skipped_low_space += 1
                logger.error("%s — pausing further hydrates", exc)
                break

            tmp = dest.with_suffix(dest.suffix + ".importing")
            try:
                # Prefer raw read/write: shutil.copy2 hits WinError 389 on
                # Yandex Disk "Files On-Demand" placeholders; open()+read hydrates.
                with open(src, "rb") as inf, open(tmp, "wb") as out:
                    shutil.copyfileobj(inf, out, length=1024 * 1024)
                ok, reason, actual_md5 = verify_local_file(
                    tmp, expected_size=row["size"], expected_md5=row["md5"]
                )
                if not ok:
                    tmp.unlink(missing_ok=True)
                    logger.warning(
                        "verify failed after copy %s: %s", row["name"], reason
                    )
                    errors += 1
                    continue
                tmp.replace(dest)
                db.mark_verified(
                    row["remote_id"],
                    str(dest),
                    local_md5=actual_md5,
                )
                imported += 1
                imported_bytes += src_size
                if _maybe_dehydrate(src, dest, enabled=dehydrate_after):
                    dehydrated += 1
                if imported % 50 == 0:
                    logger.info(
                        "progress imported=%s (%.1f GB) dehydrated=%s free_C=%.1fGB",
                        imported,
                        imported_bytes / 1e9,
                        dehydrated,
                        free_bytes(yd) / (1024**3),
                    )
            except OSError as exc:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                logger.warning("copy failed %s: %s", src, exc)
                errors += 1
            # Brief pause so Yandex Disk client can settle dehydrate/hydrate work.
            if dehydrated and dehydrated % 20 == 0:
                time.sleep(0.2)
    finally:
        db.close()

    logger.info(
        "Done imported=%s (%.2f GB) dehydrated=%s missing=%s size_mismatch=%s "
        "errors=%s skipped_low_space=%s dry_run=%s free_sync=%.1fGB",
        imported,
        imported_bytes / 1e9,
        dehydrated,
        missing,
        size_mismatch,
        errors,
        skipped_low_space,
        args.dry_run,
        free_bytes(yd) / (1024**3),
    )
    return 0 if errors == 0 and skipped_low_space == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
