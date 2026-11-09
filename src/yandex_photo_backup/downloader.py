"""Resume-safe per-object downloader with mandatory verification."""

from __future__ import annotations

import logging
import threading
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .adaptive import AdaptiveController
from .client import AuthExpiredError, CaptchaRequiredError, RateLimitError, YandexPhotoClient
from .collect import extract_resources_from_response, parse_resource_item
from .db import ManifestDB
from .paths import ensure_under, sanitize_filename, year_month_subdir
from .verify import md5_file, verify_local_file

logger = logging.getLogger(__name__)

_PREPARE_BLOCKED_UNTIL = 0.0
_PREPARE_LOCK = threading.Lock()
_PREPARE_LAST_CALL = 0.0
_PREPARE_MIN_INTERVAL_S = 8.0
DEFAULT_WORKERS = 4


def unique_target(path: Path) -> Path:
    """Avoid clobbering different remote objects that share a filename."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 10_000):
        candidate = path.with_name(f'{stem}__{i}{suffix}')
        if not candidate.exists():
            return candidate
    raise RuntimeError(f'Cannot allocate unique name for {path}')


def local_path_for(row, root: Path) -> Path:
    rel = year_month_subdir(row['year'], row['month']) / sanitize_filename(row['name'])
    return ensure_under(root, root / rel)


def _normalize_remote_path(row) -> str:
    remote_path = row['remote_path'] or row['remote_id']
    if remote_path.startswith('photounlim:/'):
        return '/photounlim/' + remote_path[len('photounlim:/') :]
    if remote_path.startswith('/'):
        return remote_path
    return f'/photounlim/{sanitize_filename(row["name"])}'


def refresh_cluster_urls(
    client: YandexPhotoClient,
    db: ManifestDB | None,
    cluster_id: str,
    *,
    db_lock: threading.Lock | None = None,
) -> dict[str, str]:
    """Refresh ORIGINAL CDN URLs for a whole cluster. Returns lookup keys -> url."""
    psid = getattr(client, 'photoslice_id', None)
    if not psid:
        psid = client.init_photoslice()
        client.photoslice_id = psid
    data = client.get_cluster_resources(
        psid,
        {cluster_id: {'range': [0, 10000]}},
        hide_screenshots=False,
    )
    urls: dict[str, str] = {}
    updates: list[tuple] = []
    for cid, item in extract_resources_from_response(data if isinstance(data, dict) else {}):
        parsed = parse_resource_item(item, cid)
        if not parsed or not parsed.get('original_url'):
            continue
        urls[parsed['remote_id']] = parsed['original_url']
        if parsed.get('remote_path'):
            urls[parsed['remote_path']] = parsed['original_url']
        if parsed.get('name'):
            urls[f'name:{parsed["name"]}'] = parsed['original_url']
        updates.append(
            (
                parsed['original_url'],
                parsed.get('md5'),
                parsed.get('size'),
                time.time(),
                parsed['remote_id'],
            )
        )
    if db is not None and updates:
        def _write() -> None:
            db.conn.executemany(
                'UPDATE objects SET original_url=?, md5=COALESCE(?, md5), size=COALESCE(?, size), '
                'updated_at=? WHERE remote_id=?',
                updates,
            )
            db.conn.commit()

        if db_lock is not None:
            with db_lock:
                _write()
        else:
            _write()
    logger.info('Refreshed %s ORIGINAL URLs for cluster %s', len(urls), cluster_id)
    return urls


def _lookup_url(urls: dict[str, str], row) -> str | None:
    return (
        urls.get(row['remote_id'])
        or urls.get(_normalize_remote_path(row))
        or urls.get(f'name:{row["name"]}')
    )


def _extract_single_from_zip(zip_path: Path, dest_part: Path) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if not members:
            raise RuntimeError('ZIP contains no files')
        members.sort(key=lambda m: m.file_size, reverse=True)
        member = members[0]
        dest_part.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, dest_part.open('wb') as out:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)


def _download_via_prepare(
    client: YandexPhotoClient,
    remote_path: str,
    part_path: Path,
    zip_part: Path,
    zip_path: Path,
    *,
    expected_size: int = 0,
    controller: AdaptiveController | None = None,
) -> None:
    """Download one Disk path via bulk-download-prepare (used for videos).

    Serializes prepare calls and keeps zip .part for resume across stalls.
    """
    global _PREPARE_BLOCKED_UNTIL, _PREPARE_LAST_CALL
    # Do not sleep here: sleeping parks the only adaptive worker and freezes
    # CDN photo progress. Caller soft-fails and retries after the circuit cools.
    if time.time() < _PREPARE_BLOCKED_UNTIL:
        raise RateLimitError('prepare circuit open')
    try:
        with _PREPARE_LOCK:
            if time.time() < _PREPARE_BLOCKED_UNTIL:
                raise RateLimitError('prepare circuit open')
            gap = _PREPARE_MIN_INTERVAL_S - (time.time() - _PREPARE_LAST_CALL)
            if gap > 0:
                time.sleep(gap)
            zip_url = client.prepare_download_url([remote_path])
            _PREPARE_LAST_CALL = time.time()

        if controller is not None:
            stall = controller.curl_stall()
            read_timeout = controller.cdn_read_timeout_s(max(expected_size, 50_000_000))
            speed_limit = stall.speed_limit_bps
            speed_time = stall.speed_time_s
        else:
            # Large videos: allow multi-hour transfers with resume.
            read_timeout = min(6 * 3600, max(300, expected_size // 50_000 + 300))
            speed_limit = 256
            speed_time = 90

        written, content_type = client.download_bytes_stream(
            zip_url,
            zip_part,
            timeout=(20, read_timeout),
            speed_limit_bps=speed_limit,
            speed_time_s=speed_time,
            resume=True,
        )
        if written <= 0:
            raise RuntimeError('ZIP download wrote 0 bytes')
        # Promote .zip.part -> .zip only when complete enough to extract / use.
        zip_part.replace(zip_path)
        if zipfile.is_zipfile(zip_path):
            _extract_single_from_zip(zip_path, part_path)
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            # Some prepare responses are a raw single file, not a ZIP.
            zip_path.replace(part_path)
    except Exception as exc:
        if isinstance(exc, RateLimitError) or '429' in str(exc):
            _PREPARE_BLOCKED_UNTIL = time.time() + 900
            raise RateLimitError('prepare HTTP 429') from exc
        raise


def _safe_replace(src: Path, dst: Path) -> None:
    """Rename with a short retry for Windows file-lock races."""
    last: Exception | None = None
    for _ in range(5):
        try:
            src.replace(dst)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f'rename failed: {last}')


def download_one(
    client: YandexPhotoClient,
    row,
    *,
    files_root: Path,
    tmp_root: Path,
    url: str | None = None,
    controller: AdaptiveController | None = None,
) -> tuple[bool, str, Path | None]:
    """Download and verify one object. Returns (ok, message, final_path).

    Supports resume: partial ``*.part`` / ``*.zip.part`` files are kept and
    continued with HTTP Range (curl -C -). Videos without Photoslice ORIGINAL
    URLs are fetched via ``mpfs/bulk-download-prepare``.
    """
    remote_id = row['remote_id']
    remote_path = _normalize_remote_path(row)
    expected_size = int(row['size'] or 0)

    desired = local_path_for(row, files_root)
    if desired.exists():
        ok, reason, _actual_md5 = verify_local_file(
            desired,
            expected_size=row['size'],
            expected_md5=row['md5'],
        )
        if ok:
            return True, f'already verified: {reason}', desired
        logger.warning('Existing local file failed verification (%s); re-downloading', reason)

    final_path = desired if desired.exists() else unique_target(desired)
    digest = abs(hash(remote_id)) % 10_000_000
    part_path = tmp_root / f'{digest}.part'
    zip_part = tmp_root / f'{digest}.zip.part'
    zip_path = tmp_root / f'{digest}.zip'
    tmp_root.mkdir(parents=True, exist_ok=True)
    final_path.parent.mkdir(parents=True, exist_ok=True)

    # Resume: keep existing .part / .zip.part. Only drop a completed leftover .zip.
    if zip_path.exists() and not zip_part.exists():
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass

    # If a previous run left a complete .part, finalize without re-download.
    if part_path.exists() and expected_size > 0 and part_path.stat().st_size == expected_size:
        ok, reason, actual_md5 = verify_local_file(
            part_path,
            expected_size=row['size'],
            expected_md5=row['md5'],
        )
        if ok:
            _safe_replace(part_path, final_path)
            return True, f'resumed complete part md5={actual_md5}', final_path

    success = False
    try:
        errors: list[str] = []
        downloaded = False

        def _try_cdn(cdn_url: str) -> bool:
            size = expected_size
            if controller is not None:
                stall = controller.curl_stall()
                read_timeout = controller.cdn_read_timeout_s(size)
                speed_limit = stall.speed_limit_bps
                speed_time = stall.speed_time_s
            else:
                read_timeout = min(6 * 3600, max(120, (size // 50_000) + 120 if size else 300))
                speed_limit = 256
                speed_time = 60
            written, content_type = client.download_bytes_stream(
                cdn_url,
                part_path,
                timeout=(15, read_timeout),
                speed_limit_bps=speed_limit,
                speed_time_s=speed_time,
                resume=True,
            )
            if content_type in {'application/zip', 'application/x-zip-compressed'}:
                part_path.replace(zip_path)
                _extract_single_from_zip(zip_path, part_path)
                return True
            return written > 0

        if url:
            try:
                downloaded = _try_cdn(url)
                if not downloaded:
                    errors.append('ORIGINAL wrote 0 bytes')
            except Exception as exc:
                errors.append(f'ORIGINAL CDN: {exc}')
            # Do not fall through to prepare after a failed ORIGINAL attempt:
            # prepare is heavily rate-limited and would block photo workers.

        # Videos / Disk paths usually have no Photoslice ORIGINAL — use prepare.
        if not downloaded and not url:
            direct = None
            try:
                direct = client.get_file_url(remote_path)
            except Exception as exc:
                errors.append(f'mpfs/url: {exc}')
            if direct:
                try:
                    downloaded = _try_cdn(direct)
                    if not downloaded:
                        errors.append('mpfs/url wrote 0 bytes')
                except Exception as exc:
                    errors.append(f'mpfs/url CDN: {exc}')

            if not downloaded:
                try:
                    _download_via_prepare(
                        client,
                        remote_path,
                        part_path,
                        zip_part,
                        zip_path,
                        expected_size=expected_size,
                        controller=controller,
                    )
                    downloaded = True
                except RateLimitError as exc:
                    errors.append(str(exc))
                except Exception as exc:
                    errors.append(f'prepare: {exc}')

        if not downloaded:
            raise RuntimeError('; '.join(errors) or 'no download method succeeded')
        ok, reason, actual_md5 = verify_local_file(
            part_path,
            expected_size=row['size'],
            expected_md5=row['md5'],
        )
        if not ok:
            # Corrupt / HTML error page: drop part so the next attempt starts clean.
            # Size mismatch on a still-growing resume is kept only if below expected.
            keep_partial = (
                expected_size > 0
                and part_path.exists()
                and 0 < part_path.stat().st_size < expected_size
                and 'mismatch' in reason
            )
            if not keep_partial:
                try:
                    part_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return False, reason, None

        _safe_replace(part_path, final_path)
        success = True
        label = f'verified md5={actual_md5}' if row['md5'] else f'verified size={expected_size}'
        return True, label, final_path
    except (AuthExpiredError, CaptchaRequiredError):
        raise
    except Exception as exc:
        return False, f'{type(exc).__name__}: {exc}', None
    finally:
        # On success: remove leftover temps. On failure: keep .part / .zip.part for resume.
        if success:
            for p in (zip_part, zip_path, part_path):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            try:
                if zip_path.exists():
                    zip_path.unlink(missing_ok=True)
            except OSError:
                pass


def download_pending(
    client: YandexPhotoClient,
    db: ManifestDB,
    *,
    files_root: Path,
    tmp_root: Path,
    limit: int | None = None,
    max_attempts: int = 8,
    stop_on_auth_error: bool = True,
    with_md5_only: bool = False,
    workers: int = DEFAULT_WORKERS,
    adaptive: bool = True,
    media: str = 'all',
) -> dict[str, int]:
    """Download pending objects cluster-by-cluster with parallel CDN workers."""
    rows = db.pending_downloads(limit=limit, with_md5_only=with_md5_only, media=media)
    counters = {'ok': 0, 'failed': 0, 'skipped': 0}
    counter_lock = threading.Lock()
    db_lock = threading.Lock()
    done_files = 0
    controller = AdaptiveController(
        min_workers=1,
        max_workers=max(1, min(workers, 8)),
        start_workers=max(1, min(2, workers)),
    )
    if not adaptive:
        controller.workers = max(1, workers)
        controller.max_workers = controller.workers
        controller.min_workers = controller.workers

    by_cluster: dict[str, list] = defaultdict(list)
    no_cluster: list = []
    for row in rows:
        if int(row['attempts'] or 0) >= max_attempts:
            db.mark_failed(row['remote_id'], 'max attempts exceeded', permanent=True)
            counters['failed'] += 1
            continue
        if row['verify_status'] == 'verified' and row['local_path']:
            counters['skipped'] += 1
            continue
        if row['cluster_id']:
            by_cluster[row['cluster_id']].append(row)
        else:
            no_cluster.append(row)

    clusters = list(by_cluster.items())
    total_files = sum(len(v) for v in by_cluster.values()) + len(no_cluster)
    deferred_prepare: list[tuple] = []  # (row, url_or_none)

    def _process(row, url: str | None) -> bool:
        nonlocal done_files
        # Never park CDN workers on prepare rate-limit waits.
        if not url and time.time() < _PREPARE_BLOCKED_UNTIL:
            with counter_lock:
                counters['skipped'] += 1
            logger.info(
                'DEFER %s — prepare circuit open (%.0fs left)',
                row['name'],
                max(0.0, _PREPARE_BLOCKED_UNTIL - time.time()),
            )
            return False
        with counter_lock:
            done_files += 1
            idx = done_files
        with db_lock:
            db.mark_attempt(row['remote_id'])
        logger.info(
            '[%s/%s] downloading %s (%s bytes) url=%s workers=%s mode=%s',
            idx,
            total_files,
            row['name'],
            row['size'],
            'yes' if url else 'no',
            controller.workers,
            controller.mode,
        )
        t0 = time.time()
        ok, message, final_path = download_one(
            client,
            row,
            files_root=files_root,
            tmp_root=tmp_root,
            url=url,
            controller=controller if adaptive else None,
        )
        duration = time.time() - t0
        rate_limited = (not ok) and (
            '429' in message
            or 'rate limit' in message.lower()
            or 'circuit open' in message.lower()
        )
        bytes_written = int(row['size'] or 0) if ok else 0
        if adaptive:
            controller.record(
                ok,
                bytes_written=bytes_written,
                duration_s=duration,
                rate_limited=rate_limited,
            )
        if ok and final_path is not None:
            local_md5 = md5_file(final_path)
            with db_lock:
                db.mark_verified(row['remote_id'], str(final_path), local_md5=local_md5)
            with counter_lock:
                counters['ok'] += 1
            logger.info('OK %s — %s (%.1fs)', row['name'], message, duration)
            return True
        # Circuit / 429: keep as pending without permanent pressure from spam fails.
        soft = 'circuit open' in message.lower() or 'prepare http 429' in message.lower()
        with db_lock:
            if soft:
                # Do not consume attempt budget on API rate limits.
                db.conn.execute(
                    "UPDATE objects SET download_status='pending', "
                    'attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END, '
                    'error=?, updated_at=? WHERE remote_id=?',
                    (message, time.time(), row['remote_id']),
                )
                db.conn.commit()
            else:
                db.mark_failed(row['remote_id'], message, permanent=False)
        with counter_lock:
            counters['failed'] += 1
        logger.warning('FAIL %s — %s (%.1fs)', row['name'], message, duration)
        return False

    def _run_batch(batch_rows: list, urls: dict[str, str]) -> tuple[int, int]:
        """Run one parallel batch; return (ok_delta, fail_delta)."""
        ok_before = counters['ok']
        fail_before = counters['failed']
        worker_n = max(1, min(controller.workers, len(batch_rows)))
        with ThreadPoolExecutor(max_workers=worker_n) as pool:
            futures = [
                pool.submit(_process, row, _lookup_url(urls, row)) for row in batch_rows
            ]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except (AuthExpiredError, CaptchaRequiredError):
                    if stop_on_auth_error:
                        raise
                except Exception as exc:
                    logger.warning('worker error: %s', exc)
                    if adaptive:
                        controller.record(False)
        return counters['ok'] - ok_before, counters['failed'] - fail_before

    for cluster_idx, (cluster_id, cluster_rows) in enumerate(clusters, start=1):
        controller.wait_if_cooling()
        state = controller.state()
        stall = controller.curl_stall()
        logger.info(
            'Cluster %s/%s id=%s files=%s adaptive workers=%s mode=%s '
            'success=%.0f%% stall=%sB/%ss',
            cluster_idx,
            len(clusters),
            cluster_id,
            len(cluster_rows),
            state.workers,
            state.mode,
            state.success_rate * 100,
            stall.speed_limit_bps,
            stall.speed_time_s,
        )
        try:
            urls = refresh_cluster_urls(client, db, cluster_id, db_lock=db_lock)
        except (AuthExpiredError, CaptchaRequiredError):
            raise
        except Exception as exc:
            logger.warning('Cluster refresh failed %s: %s', cluster_id, exc)
            urls = {}
            if adaptive:
                controller.record(False, rate_limited='429' in str(exc))

        # Phase A: ORIGINAL CDN only. Prepare-only (typical videos) deferred so a
        # 429 circuit cannot freeze photo progress when workers=1.
        small_first = adaptive and controller.prefer_small_first()
        cdn_rows = [r for r in cluster_rows if _lookup_url(urls, r)]
        prep_rows = [r for r in cluster_rows if not _lookup_url(urls, r)]
        deferred_prepare.extend((r, None) for r in prep_rows)
        if small_first:
            cdn_rows.sort(key=lambda r: int(r['size'] or 0))

        ok_before = counters['ok']
        cluster_ok = 0
        cluster_fail = 0
        abandoned = False
        cursor = 0
        while cursor < len(cdn_rows):
            worker_n = max(1, min(controller.workers, len(cdn_rows) - cursor))
            batch = cdn_rows[cursor : cursor + worker_n]
            cursor += len(batch)
            batch_ok, batch_fail = _run_batch(batch, urls)
            cluster_ok += batch_ok
            cluster_fail += batch_fail
            if adaptive and controller.should_abandon_cluster(
                cluster_ok=cluster_ok,
                cluster_fail=cluster_fail,
            ):
                left = len(cdn_rows) - cursor
                logger.warning(
                    'Abandoning cluster %s after %s fails / %s ok (%s CDN files deferred)',
                    cluster_id,
                    cluster_fail,
                    cluster_ok,
                    left,
                )
                abandoned = True
                break

        cluster_ok_total = counters['ok'] - ok_before
        if not abandoned and cluster_ok_total > 0 and cdn_rows:
            with db_lock:
                verified_ids = {
                    r['remote_id']
                    for r in db.conn.execute(
                        "SELECT remote_id FROM objects "
                        "WHERE cluster_id=? AND verify_status='verified'",
                        (cluster_id,),
                    )
                }
            failed_ids = [row for row in cdn_rows if row['remote_id'] not in verified_ids]
            if failed_ids:
                time.sleep(0.3)
                try:
                    urls2 = refresh_cluster_urls(client, db, cluster_id, db_lock=db_lock)
                except Exception as exc:
                    logger.warning('Cluster retry refresh failed %s: %s', cluster_id, exc)
                    urls2 = {}
                _run_batch(failed_ids, urls2)

        delay = controller.cluster_delay() if adaptive else 0.2
        time.sleep(delay)

    for row in no_cluster:
        # Unknown cluster: try CDN path only if we somehow get a URL later via prepare.
        deferred_prepare.append((row, None))

    # Phase B: prepare-only items (Disk videos / files without ORIGINAL).
    if deferred_prepare:
        logger.info(
            'CDN pass done; starting prepare pass for %s items (workers=1)',
            len(deferred_prepare),
        )
        if adaptive:
            controller.workers = 1
            controller.max_workers = 1
        for row, _url in deferred_prepare:
            # Skip if already verified during CDN pass.
            with db_lock:
                st = db.conn.execute(
                    'SELECT verify_status FROM objects WHERE remote_id=?',
                    (row['remote_id'],),
                ).fetchone()
            if st and st['verify_status'] == 'verified':
                continue
            # If circuit is open, wait once (outside CDN phase) then continue.
            if time.time() < _PREPARE_BLOCKED_UNTIL:
                wait = _PREPARE_BLOCKED_UNTIL - time.time()
                logger.warning(
                    'prepare circuit open before prepare pass — sleeping %.0fs',
                    wait,
                )
                time.sleep(min(wait + 1.0, 960.0))
            try:
                _process(row, None)
            except (AuthExpiredError, CaptchaRequiredError):
                if stop_on_auth_error:
                    raise

    final = controller.state()
    logger.info(
        'Download pass done ok=%s failed=%s skipped=%s final_workers=%s mode=%s success=%.0f%%',
        counters['ok'],
        counters['failed'],
        counters['skipped'],
        final.workers,
        final.mode,
        final.success_rate * 100,
    )
    return counters
