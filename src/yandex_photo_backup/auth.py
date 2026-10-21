"""Local-only authentication via browser cookies."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

IMPORTANT_COOKIE_NAMES = {
    'Session_id',
    'sessionid2',
    'yandexuid',
    'L',
    'yp',
    'ys',
    'i',
    'yashr',
    'uniqueuid',
}


def load_cookies(cookies_path: Path) -> dict[str, str]:
    """Load cookies from Cookie-Editor JSON export or name/value map."""
    if not cookies_path.exists():
        raise FileNotFoundError(
            f'Cookies file not found: {cookies_path}. '
            'Export cookies from disk.yandex.ru (Cookie-Editor JSON) '
            'or run: python -m yandex_photo_backup.auth_helper'
        )
    raw = json.loads(cookies_path.read_text(encoding='utf-8'))
    if isinstance(raw, dict):
        if 'cookies' in raw and isinstance(raw['cookies'], list):
            raw = raw['cookies']
        else:
            return {str(k): str(v) for k, v in raw.items() if v is not None}
    if not isinstance(raw, list):
        raise ValueError('cookies.json must be a list or name->value object')
    cookies: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        value = item.get('value')
        if name and value is not None:
            cookies[str(name)] = str(value)
    if not cookies:
        raise ValueError('No cookies found in cookies.json')
    return cookies


def cookies_look_logged_in(cookies: dict[str, str]) -> bool:
    """Heuristic check that session cookies are present."""
    return bool(cookies.get('Session_id') or cookies.get('sessionid2'))


def save_cookies_json(cookies: list[dict] | dict[str, str], path: Path) -> None:
    """Persist cookies locally (never transmit)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(cookies, dict):
        payload = [{'name': k, 'value': v, 'domain': '.yandex.ru'} for k, v in cookies.items()]
    else:
        payload = cookies
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    logger.info('Saved %s cookies to %s', len(payload), path)


def try_load_firefox_cookies(profile_dir: Path | None = None) -> dict[str, str] | None:
    """Best-effort read of Yandex cookies from a Firefox profile (read-only copy)."""
    profiles_root = Path(os.environ.get('APPDATA', '')) / 'Mozilla' / 'Firefox' / 'Profiles'
    candidates: list[Path] = []
    if profile_dir:
        candidates.append(profile_dir)
    elif profiles_root.exists():
        candidates.extend(sorted(profiles_root.glob('*')))
    for profile in candidates:
        db = profile / 'cookies.sqlite'
        if not db.exists():
            continue
        tmp = Path(tempfile.gettempdir()) / f'ff_cookies_{os.getpid()}.sqlite'
        try:
            # Firefox often allows copy even while running.
            import shutil

            shutil.copy2(db, tmp)
            con = sqlite3.connect(tmp)
            rows = con.execute(
                "SELECT name, value, host FROM moz_cookies "
                "WHERE host LIKE '%yandex%' ORDER BY lastAccessed DESC"
            ).fetchall()
            con.close()
        except OSError as exc:
            logger.debug('Firefox cookie read failed for %s: %s', profile, exc)
            continue
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        by_name: dict[str, tuple[str, int]] = {}
        priority = {
            'disk.yandex.ru': 0,
            '.disk.yandex.ru': 0,
            '.yandex.ru': 1,
            'yandex.ru': 1,
        }
        for name, value, host in rows:
            prio = priority.get(host, 2)
            if name not in by_name or prio < by_name[name][1]:
                by_name[name] = (value, prio)
        cookies = {k: v[0] for k, v in by_name.items()}
        if cookies_look_logged_in(cookies):
            logger.info('Loaded Firefox cookies from %s (%s cookies)', profile, len(cookies))
            return cookies
    return None
