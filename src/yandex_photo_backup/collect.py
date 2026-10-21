"""Enumerate Unlimited Photos / Photoslice into the local manifest."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from .client import YandexPhotoClient
from .db import ManifestDB
from .paths import sanitize_filename

logger = logging.getLogger(__name__)

CLUSTER_ID_DATE = re.compile(r'(?P<y>\d{4})[-_/ ]?(?P<m>\d{2})[-_/ ]?(?P<d>\d{2})')


def _normalize_path(path: str | None, name: str | None) -> str | None:
    if not path and not name:
        return None
    if path:
        p = str(path).strip()
        if p.startswith('photounlim:/'):
            return '/photounlim/' + p[len('photounlim:/') :]
        if p.startswith('photounlim/'):
            return '/' + p
        if p.startswith('/'):
            return p
        if ':/' in p:
            schema, rest = p.split(':/', 1)
            return f'/{schema}/{rest}'
        return p
    return f'/photounlim/{name}'


def _abs_url(url: str | None) -> str | None:
    if not url:
        return None
    url = str(url)
    if url.startswith('//'):
        return 'https:' + url
    return url


def _original_from_meta(meta: dict[str, Any]) -> tuple[str | None, str | None, int | None]:
    """Return (original_url, etag/md5, fsize) from meta.sizes ORIGINAL entry."""
    sizes = meta.get('sizes') or []
    for entry in sizes:
        if not isinstance(entry, dict):
            continue
        if str(entry.get('name', '')).upper() != 'ORIGINAL':
            continue
        url = _abs_url(entry.get('url'))
        md5 = None
        fsize = None
        if url:
            qs = parse_qs(urlparse(url).query)
            if qs.get('etag'):
                md5 = qs['etag'][0].lower()
            if qs.get('fsize'):
                try:
                    fsize = int(qs['fsize'][0])
                except ValueError:
                    fsize = None
        return url, md5, fsize
    return None, None, None


def _parse_year_month(
    etime: Any,
    name: str | None,
    cluster_id: str | None,
) -> tuple[int | None, int | None, str | None]:
    year = month = None
    taken_at = None
    if isinstance(etime, (int, float)):
        ts = float(etime)
        if ts > 1e12:
            ts /= 1000.0
        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            year, month = dt.year, dt.month
            taken_at = dt.isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    if (year is None or month is None) and name:
        m = CLUSTER_ID_DATE.search(name)
        if m:
            year = year or int(m.group('y'))
            month = month or int(m.group('m'))
    if (year is None or month is None) and cluster_id:
        # cluster ids look like 0000001791397488000_... — not calendar dates
        m = CLUSTER_ID_DATE.search(cluster_id)
        if m:
            year = year or int(m.group('y'))
            month = month or int(m.group('m'))
    return year, month, taken_at


def parse_resource_item(item: dict[str, Any], cluster_id: str | None = None) -> dict[str, Any] | None:
    """Normalize one MPFS resource (resources.fetched) into a manifest row."""
    meta = item.get('meta') if isinstance(item.get('meta'), dict) else {}
    # Skip sparse photoslice index items that lack MPFS meta.
    if not meta and not item.get('path') and not item.get('name'):
        return None
    if item.get('width') is not None and not meta and 'beauty' in item:
        # cluster.items stub — not enough for verified backup
        return None

    remote_id = (
        meta.get('resource_id')
        or meta.get('file_id')
        or item.get('id')
        or item.get('path')
        or item.get('resource_id')
    )
    if not remote_id:
        return None
    remote_id = str(remote_id)

    name = item.get('name') or str(item.get('path') or remote_id).rsplit('/', 1)[-1]
    name = sanitize_filename(str(name))
    remote_path = _normalize_path(item.get('path') or item.get('id'), name)

    original_url, etag, fsize = _original_from_meta(meta)
    size = meta.get('size') if meta.get('size') is not None else item.get('size')
    if size is None:
        size = fsize
    try:
        size_i = int(size) if size is not None else None
    except (TypeError, ValueError):
        size_i = None

    md5 = (
        meta.get('md5')
        or meta.get('etag')
        or item.get('md5')
        or etag
    )
    if md5 is not None:
        md5 = str(md5).strip().lower() or None

    media_type = (
        meta.get('mimetype')
        or meta.get('mediatype')
        or item.get('mimetype')
        or item.get('type')
    )

    etime = (
        meta.get('etime')
        or meta.get('photoslice_time')
        or item.get('etime')
        or item.get('mtime')
        or item.get('ctime')
    )
    cid = cluster_id or item.get('clusterId') or item.get('cluster_id')
    year, month, taken_at = _parse_year_month(etime, name, str(cid) if cid else None)

    return {
        'remote_id': remote_id,
        'name': name,
        'remote_path': remote_path,
        'cluster_id': str(cid) if cid else None,
        'media_type': str(media_type) if media_type else None,
        'size': size_i,
        'md5': md5,
        'sha256': meta.get('sha256'),
        'taken_at': taken_at,
        'year': year,
        'month': month,
        'original_url': original_url,
    }


def extract_resources_from_response(data: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    """Prefer resources.fetched (full MPFS meta). Fall back to cluster items only if needed."""
    out: list[tuple[str | None, dict[str, Any]]] = []

    resources = data.get('resources') or {}
    fetched_res = resources.get('fetched') if isinstance(resources, dict) else None
    if isinstance(fetched_res, list):
        for item in fetched_res:
            if isinstance(item, dict):
                cid = item.get('clusterId') or item.get('cluster_id')
                out.append((str(cid) if cid else None, item))
        if out:
            return out

    clusters = data.get('clusters') or {}
    fetched_clusters = clusters.get('fetched') if isinstance(clusters, dict) else None
    if isinstance(fetched_clusters, list):
        for cluster in fetched_clusters:
            if not isinstance(cluster, dict):
                continue
            cid = cluster.get('id') or cluster.get('cluster_id')
            for item in cluster.get('items') or []:
                if isinstance(item, dict):
                    out.append((str(cid) if cid else None, item))
    return out


def collect_manifest(
    client: YandexPhotoClient,
    db: ManifestDB,
    *,
    cluster_batch: int = 8,
    hide_screenshots: bool = False,
    refresh_clusters: bool = True,
) -> dict[str, int]:
    """Build / refresh remote object list. Read-only against Yandex."""
    psid = client.init_photoslice()
    db.set_meta('photoslice_id', psid)
    logger.info('photoslice_id=%s', psid)

    clusters = client.get_clusters(psid)
    logger.info('Remote clusters: %s', len(clusters))
    for cluster in clusters:
        cid = str(cluster.get('id') or cluster.get('cluster_id') or '')
        if not cid:
            continue
        size = cluster.get('size') or cluster.get('count') or cluster.get('items_count')
        try:
            size_i = int(size) if size is not None else None
        except (TypeError, ValueError):
            size_i = None
        db.upsert_cluster(cid, size_i)
    db.commit()

    if refresh_clusters:
        db.reset_cluster_fetched()

    added = 0
    for round_idx in range(1, 4):
        todo = db.unfetched_clusters()
        if not todo:
            break
        logger.info('Fetch round %s: clusters remaining=%s', round_idx, len(todo))
        for i in range(0, len(todo), cluster_batch):
            batch = todo[i : i + cluster_batch]
            clusters_map: dict[str, dict[str, Any]] = {}
            for row in batch:
                count = max(int(row['item_count'] or 0), 1)
                clusters_map[row['cluster_id']] = {'range': [0, max(count - 1, 0)]}
            try:
                data = client.get_cluster_resources(
                    psid,
                    clusters_map,
                    hide_screenshots=hide_screenshots,
                )
            except Exception as exc:
                logger.error('Cluster batch %s failed: %s', i, exc)
                continue

            resources = extract_resources_from_response(data if isinstance(data, dict) else {})
            for cid, item in resources:
                parsed = parse_resource_item(item, cid)
                if not parsed:
                    continue
                db.upsert_object(parsed)
                added += 1
            for row in batch:
                db.mark_cluster_fetched(row['cluster_id'])
            db.commit()
            logger.info(
                'Round %s clusters %s/%s — upserted=%s (batch=%s)',
                round_idx,
                min(i + cluster_batch, len(todo)),
                len(todo),
                added,
                len(resources),
            )

    leftover = len(db.unfetched_clusters())
    if leftover:
        logger.warning('Unfetched clusters remaining after retries: %s', leftover)

    stats = db.stats()
    logger.info(
        'Manifest ready: remote_objects=%s remote_bytes=%s',
        stats['remote_objects'],
        stats['remote_total_bytes'],
    )
    return {
        'clusters': len(clusters),
        'objects': stats['remote_objects'],
        'upserted_events': added,
    }
