"""CLI entry: python -m yandex_photo_backup <command>."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Allow `python -m yandex_photo_backup` from repo without install.
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from yandex_photo_backup.auth import (  # noqa: E402
    cookies_look_logged_in,
    load_cookies,
    save_cookies_json,
    try_load_firefox_cookies,
)
from yandex_photo_backup.client import (  # noqa: E402
    AuthExpiredError,
    CaptchaRequiredError,
    YandexPhotoClient,
)
from yandex_photo_backup.collect import collect_manifest  # noqa: E402
from yandex_photo_backup.config import Config  # noqa: E402
from yandex_photo_backup.db import ManifestDB  # noqa: E402
from yandex_photo_backup.downloader import download_pending  # noqa: E402
from yandex_photo_backup.report import (  # noqa: E402
    format_stats,
    human_bytes,
    reconcile_local,
    write_failed_report,
)


def setup_logging(logs_dir: Path) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f'backup-{datetime.now().strftime("%Y-%m-%d")}.log'
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fh = logging.FileHandler(log_path, encoding='utf-8')
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    return log_path


def resolve_cookies(cfg: Config) -> dict[str, str]:
    if cfg.cookies_path.exists():
        cookies = load_cookies(cfg.cookies_path)
        if cookies_look_logged_in(cookies):
            return cookies
        raise SystemExit(
            f'{cfg.cookies_path} exists but has no Session_id/sessionid2. '
            'Re-export cookies while logged into disk.yandex.ru'
        )
    ff = try_load_firefox_cookies()
    if ff:
        save_cookies_json(ff, cfg.cookies_path)
        return ff
    raise SystemExit(
        'No cookies.json found.\n'
        '1) Open https://disk.yandex.ru/client/photo in Chrome and log in.\n'
        '2) Export cookies with Cookie-Editor as JSON into:\n'
        f'   {cfg.cookies_path}\n'
        'Or place Firefox profile cookies (auto-detected if available).'
    )


def make_client(cfg: Config) -> YandexPhotoClient:
    cookies = resolve_cookies(cfg)
    # Conservative pacing: cluster refresh per file + CDN download.
    return YandexPhotoClient(cookies, min_delay=1.0, max_delay=2.5)


def cmd_collect(cfg: Config, args: argparse.Namespace) -> int:
    client = make_client(cfg)
    db = ManifestDB(cfg.db_path)
    try:
        result = collect_manifest(
            client,
            db,
            cluster_batch=cfg.cluster_batch,
            hide_screenshots=False,
            refresh_clusters=not args.resume_clusters,
        )
        print(f"Clusters: {result['clusters']}")
        print(f"Objects in manifest: {result['objects']}")
        print(format_stats(db.stats()))
        return 0
    finally:
        db.close()
        client.close()


def cmd_download(cfg: Config, args: argparse.Namespace) -> int:
    client = make_client(cfg)
    db = ManifestDB(cfg.db_path)
    try:
        if db.stats()['remote_objects'] == 0:
            print('Manifest empty — running collect first...')
            collect_manifest(client, db, cluster_batch=cfg.cluster_batch)
        counters = download_pending(
            client,
            db,
            files_root=cfg.files_dir,
            tmp_root=cfg.tmp_dir,
            limit=args.limit,
            max_attempts=cfg.max_attempts,
            with_md5_only=bool(getattr(args, 'with_md5_only', False)),
            workers=int(getattr(args, 'workers', 6) or 6),
            adaptive=not bool(getattr(args, 'no_adaptive', False)),
            media=str(getattr(args, 'media', 'all') or 'all'),
        )
        print(f'Download counters: {counters}')
        print(format_stats(db.stats()))
        failed_path = cfg.root / 'failed_objects.tsv'
        n = write_failed_report(db, failed_path)
        if n:
            print(f'Failed list: {failed_path} ({n} rows)')
        return 0 if counters['failed'] == 0 else 2
    except (AuthExpiredError, CaptchaRequiredError) as exc:
        print(f'AUTH/CAPTCHA: {exc}', file=sys.stderr)
        return 3
    finally:
        db.close()
        client.close()


def cmd_sample(cfg: Config, args: argparse.Namespace) -> int:
    args.limit = args.limit or 100
    return cmd_download(cfg, args)


def cmd_status(cfg: Config, _args: argparse.Namespace) -> int:
    if not cfg.db_path.exists():
        print('No database yet. Run: backup.py collect')
        return 1
    db = ManifestDB(cfg.db_path)
    try:
        print(format_stats(db.stats()))
        print(f'\nDB:   {cfg.db_path}')
        print(f'Files:{cfg.files_dir}')
        print(f'Logs: {cfg.logs_dir}')
        return 0
    finally:
        db.close()


def cmd_reconcile(cfg: Config, _args: argparse.Namespace) -> int:
    db = ManifestDB(cfg.db_path)
    try:
        result = reconcile_local(db)
        print(f'Reconcile: {result}')
        print(format_stats(db.stats()))
        failed_path = cfg.root / 'failed_objects.tsv'
        n = write_failed_report(db, failed_path)
        stats = db.stats()
        print('\n=== FINAL REPORT ===')
        print(format_stats(stats))
        print(f'Missing (no verified local): {result["missing"]}')
        print(f'Corrupt:                     {result["corrupt"]}')
        if n:
            print(f'Exceptions list: {failed_path}')
        missing = stats['pending'] + stats['failed_permanent'] + result['missing'] + result['corrupt']
        return 0 if missing == 0 else 2
    finally:
        db.close()


def cmd_backup(cfg: Config, args: argparse.Namespace) -> int:
    """Incremental full pipeline: collect → download → reconcile."""
    client = make_client(cfg)
    db = ManifestDB(cfg.db_path)
    try:
        print('=== COLLECT ===')
        collect_manifest(client, db, cluster_batch=cfg.cluster_batch, refresh_clusters=True)
        print(format_stats(db.stats()))
        print('\n=== DOWNLOAD ===')
        counters = download_pending(
            client,
            db,
            files_root=cfg.files_dir,
            tmp_root=cfg.tmp_dir,
            limit=args.limit,
            max_attempts=cfg.max_attempts,
        )
        print(f'Download counters: {counters}')
        print('\n=== RECONCILE ===')
        result = reconcile_local(db)
        stats = db.stats()
        print(f'Reconcile: {result}')
        print('\n=== FINAL REPORT ===')
        print(format_stats(stats))
        print(f'Downloaded/verified this concept size: {human_bytes(stats["verified_bytes"])}')
        failed_path = cfg.root / 'failed_objects.tsv'
        n = write_failed_report(db, failed_path)
        if n:
            print(f'Exceptions ({n}): {failed_path}')
        ok = (
            stats['pending'] == 0
            and stats['failed_permanent'] == 0
            and result['missing'] == 0
            and result['corrupt'] == 0
        )
        return 0 if ok else 2
    except (AuthExpiredError, CaptchaRequiredError) as exc:
        print(f'AUTH/CAPTCHA: {exc}', file=sys.stderr)
        return 3
    finally:
        db.close()
        client.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='yandex_photo_backup',
        description='Read-only verified backup of Yandex Unlimited Photos / Photoslice',
    )
    ap.add_argument(
        '--root',
        type=Path,
        default=None,
        help='Backup root (default: YANDEX_PHOTO_ROOT env or ~/YandexPhoto)',
    )
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_collect = sub.add_parser('collect', help='Build/refresh remote manifest')
    p_collect.add_argument(
        '--resume-clusters',
        action='store_true',
        help='Do not reset cluster fetched flags',
    )
    p_collect.set_defaults(func=cmd_collect)

    p_dl = sub.add_parser('download', help='Download pending objects with verification')
    p_dl.add_argument('--limit', type=int, default=None)
    p_dl.add_argument(
        '--with-md5-only',
        action='store_true',
        help='Only download objects that have remote MD5 (usually images with ORIGINAL CDN)',
    )
    p_dl.add_argument(
        '--workers',
        type=int,
        default=6,
        help='Max parallel CDN workers; adaptive starts lower (default: 6)',
    )
    p_dl.add_argument(
        '--no-adaptive',
        action='store_true',
        help='Disable adaptive worker/delay tuning (use fixed --workers)',
    )
    p_dl.add_argument(
        '--media',
        choices=['all', 'photos', 'videos'],
        default='all',
        help='Filter queue: photos first/only, videos only, or all (default: all)',
    )
    p_dl.set_defaults(func=cmd_download)

    p_sample = sub.add_parser('sample', help='Download a small verified sample')
    p_sample.add_argument('--limit', type=int, default=100)
    p_sample.set_defaults(func=cmd_sample)

    p_status = sub.add_parser('status', help='Show manifest statistics')
    p_status.set_defaults(func=cmd_status)

    p_rec = sub.add_parser('reconcile', help='Re-verify local files vs manifest')
    p_rec.set_defaults(func=cmd_reconcile)

    p_backup = sub.add_parser('backup', help='Incremental collect+download+reconcile')
    p_backup.add_argument('--limit', type=int, default=None)
    p_backup.set_defaults(func=cmd_backup)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    cfg = Config.from_env(args.root)
    cfg.ensure_dirs()
    log_path = setup_logging(cfg.logs_dir)
    logging.getLogger(__name__).info('Log file: %s', log_path)
    return int(args.func(cfg, args))


if __name__ == '__main__':
    raise SystemExit(main())
