"""Read-only Yandex Disk web/models-v2 client for Photoslice / photounlim."""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any
from urllib.parse import urljoin

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

BASE = 'https://disk.yandex.ru'
MODELS = f'{BASE}/models-v2'


class AuthExpiredError(RuntimeError):
    """Session cookies or CSRF token are no longer valid."""


class CaptchaRequiredError(RuntimeError):
    """Yandex asked for captcha; user must pass it in browser."""


class RateLimitError(RuntimeError):
    """HTTP 429 or soft rate limit."""


class YandexPhotoClient:
    """Minimal read-only client around disk.yandex.ru models-v2."""

    def __init__(
        self,
        cookies: dict[str, str],
        *,
        impersonate: str = 'chrome131',
        min_delay: float = 0.6,
        max_delay: float = 1.5,
    ) -> None:
        self.cookies = cookies
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._last_call = 0.0
        self.session = cffi_requests.Session(impersonate=impersonate)
        self.session.headers.update(
            {
                'Accept': '*/*',
                'Accept-Language': 'ru,en;q=0.9',
                'Origin': BASE,
                'Referer': f'{BASE}/client/photo',
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json',
            }
        )
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain='.yandex.ru')
        self.sk, self.uid = self._extract_sk_uid()
        self.connection_id = f'{self.uid}{int(time.time() * 1000)}{random.randint(100, 999)}'
        logger.info('Authenticated uid=%s', self.uid)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self._last_call + random.uniform(self.min_delay, self.max_delay) - now
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _extract_sk_uid(self) -> tuple[str, str]:
        self._throttle()
        resp = self.session.get(f'{BASE}/client/photo', timeout=60)
        text = resp.text or ''
        if 'passport.yandex' in str(resp.url) or 'auth' in str(resp.url):
            raise AuthExpiredError('Not logged in — update cookies.json')
        if 'captcha' in text.lower() and '"sk"' not in text:
            raise CaptchaRequiredError('CAPTCHA on page load — open disk.yandex.ru and pass it')
        sk_m = re.search(r'"sk"\s*:\s*"([^"]+)"', text)
        uid_m = re.search(r'"uid"\s*:\s*"?(\d+)"?', text)
        if not sk_m or not uid_m:
            raise AuthExpiredError('Could not extract sk/uid — cookies may be invalid')
        return sk_m.group(1), uid_m.group(1)

    def refresh_session(self) -> None:
        """Re-read sk/uid from the photo page."""
        self.sk, self.uid = self._extract_sk_uid()
        self.connection_id = f'{self.uid}{int(time.time() * 1000)}{random.randint(100, 999)}'
        logger.info('Session refreshed uid=%s', self.uid)

    def api(self, method: str, params: Any = None, *, retries: int = 6) -> Any:
        """POST models-v2 method with retries / backoff / CSRF rotation."""
        last_err: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            body: dict[str, Any] = {
                'sk': self.sk,
                'connection_id': self.connection_id,
                'apiMethod': method,
            }
            if params is not None:
                body['requestParams'] = params
            try:
                resp = self.session.post(
                    MODELS,
                    params={'m': method},
                    json=body,
                    timeout=180,
                )
            except Exception as exc:
                last_err = exc
                sleep_for = min(120.0, (2**attempt) + random.uniform(0, 1))
                logger.warning('%s transport error: %s; sleep %.1fs', method, exc, sleep_for)
                time.sleep(sleep_for)
                continue

            if resp.status_code == 429:
                sleep_for = min(180.0, 20 * (attempt + 1) + random.uniform(0, 5))
                logger.warning('%s HTTP 429; sleep %.1fs', method, sleep_for)
                time.sleep(sleep_for)
                last_err = RateLimitError(f'{method} HTTP 429')
                continue

            if resp.status_code in {401, 403}:
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                err = (data or {}).get('error') if isinstance(data, dict) else None
                if isinstance(err, dict) and err.get('code') == 'CKEY' and err.get('ckey'):
                    self.sk = str(err['ckey'])
                    logger.info('CSRF rotated for %s', method)
                    continue
                try:
                    self.refresh_session()
                    continue
                except Exception as exc:
                    raise AuthExpiredError(f'Session expired for {method}: {exc}') from exc

            if resp.status_code >= 500:
                sleep_for = min(120.0, (2**attempt) + random.uniform(0, 2))
                logger.warning('%s HTTP %s; sleep %.1fs', method, resp.status_code, sleep_for)
                time.sleep(sleep_for)
                last_err = RuntimeError(f'{method} HTTP {resp.status_code}')
                continue

            try:
                data = resp.json()
            except Exception as exc:
                last_err = RuntimeError(f'{method} non-JSON: {resp.text[:200]}')
                time.sleep(2**attempt)
                continue

            if isinstance(data, dict) and data.get('type') == 'captcha':
                raise CaptchaRequiredError('CAPTCHA required — pass it in browser and retry')

            if resp.status_code != 200:
                last_err = RuntimeError(f'{method} HTTP {resp.status_code}: {data}')
                time.sleep(2**attempt)
                continue

            return data

        raise RuntimeError(f'{method} failed after {retries} retries: {last_err}')

    def init_photoslice(self) -> str:
        data = self.api('intapi/photo-init-snapshot')
        psid = data.get('photoslice_id') or data.get('id')
        if not psid:
            raise RuntimeError(f'No photoslice_id in response: {data!r}')
        return str(psid)

    def get_clusters(self, photoslice_id: str, amount: int = 100000) -> list[dict[str, Any]]:
        data = self.api(
            'intapi/photo-get-snapshot',
            {'id': photoslice_id, 'locale': 'ru', 'amount': amount, 'offset': 0},
        )
        items = data.get('items') or []
        if not items:
            # Fallback older endpoint
            data = self.api(
                'intapi/photo-get-clusters',
                {'id': photoslice_id, 'offset': 0, 'amount': amount},
            )
            items = data.get('items') or []
        return list(items)

    def get_cluster_resources(
        self,
        photoslice_id: str,
        clusters: dict[str, dict[str, Any]],
        *,
        hide_screenshots: bool = False,
    ) -> dict[str, Any]:
        return self.api(
            'intapi/photo-get-clusters-with-resources',
            {
                'photosliceId': photoslice_id,
                'clusters': clusters,
                'hideScreenshots': hide_screenshots,
            },
        )

    def prepare_download_url(self, paths: list[str]) -> str:
        """Ask MPFS for a temporary download URL (ZIP for one or more paths)."""
        # Videos rely on this path. Keep retries modest so a 429 does not park a
        # worker for minutes; the downloader circuit-breaker will back off.
        data = self.api('mpfs/bulk-download-prepare', {'items': paths}, retries=3)
        url = None
        if isinstance(data, dict):
            url = data.get('download_url') or data.get('url')
            if not url and isinstance(data.get('data'), dict):
                url = data['data'].get('download_url') or data['data'].get('url')
        if not url:
            raise RuntimeError(f'No download_url in prepare response: {data!r}')
        url = str(url)
        if url.startswith('//'):
            url = 'https:' + url
        elif url.startswith('/'):
            url = urljoin(BASE, url)
        return url

    def get_file_url(self, path: str) -> str | None:
        """Try mpfs/url for a direct single-file URL. Returns None if unsupported."""
        try:
            data = self.api('mpfs/url', {'path': path}, retries=2)
        except Exception as exc:
            logger.debug('mpfs/url failed for %s: %s', path, exc)
            return None
        if not isinstance(data, dict):
            return None
        url = data.get('url') or data.get('download_url') or data.get('href')
        if isinstance(data.get('data'), dict):
            url = url or data['data'].get('url') or data['data'].get('download_url')
        if not url:
            return None
        url = str(url)
        if url.startswith('//'):
            url = 'https:' + url
        return url

    def download_bytes_stream(
        self,
        url: str,
        dest_path,
        *,
        timeout: tuple[int, int] = (20, 300),
        speed_limit_bps: int = 256,
        speed_time_s: int = 60,
        resume: bool = True,
    ):
        """Download URL to dest_path; return (bytes_written, content_type).

        Prefer Windows curl.exe for CDN downloads: curl_cffi often stalls on
        downloader.disk.yandex.ru even when signed URLs are valid.

        When resume=True and dest_path already has bytes, continue with HTTP Range
        (curl -C -) so large videos can survive stalls / restarts.
        """
        import shutil
        import subprocess
        from pathlib import Path

        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        existing = dest.stat().st_size if dest.exists() else 0
        use_resume = bool(resume and existing > 0)

        curl = shutil.which('curl.exe') or shutil.which('curl')
        if curl:
            max_time = int(timeout[0] + timeout[1])
            connect_timeout = max(3, int(timeout[0]))
            cmd = build_curl_download_cmd(
                curl,
                url,
                dest,
                connect_timeout=connect_timeout,
                max_time=max_time,
                speed_limit_bps=speed_limit_bps,
                speed_time_s=speed_time_s,
                resume=use_resume,
            )
            result = subprocess.run(cmd, capture_output=True, text=True)
            written = dest.stat().st_size if dest.exists() else 0
            if result.returncode != 0:
                err = (result.stderr or result.stdout or '').strip()
                if '429' in err:
                    raise RateLimitError('download HTTP 429')
                # curl 33 = HTTP range not satisfiable — file may already be complete
                if use_resume and written > 0 and result.returncode in {33}:
                    return written, ''
                raise RuntimeError(f'curl failed ({result.returncode}): {err[:300]}')
            if written <= 0:
                raise RuntimeError('curl wrote 0 bytes')
            return written, ''

        # Fallback: curl_cffi (may stall on some CDN hosts).
        headers = {}
        if use_resume:
            headers['Range'] = f'bytes={existing}-'
        written = existing if use_resume else 0
        content_type = ''
        resp = self.session.get(
            url,
            stream=True,
            timeout=timeout,
            allow_redirects=True,
            headers=headers or None,
        )
        try:
            if resp.status_code == 429:
                raise RateLimitError('download HTTP 429')
            if resp.status_code >= 500:
                raise RuntimeError(f'download HTTP {resp.status_code}')
            if use_resume and resp.status_code == 416:
                return written, ''
            if resp.status_code not in {200, 206}:
                body = ''
                try:
                    body = resp.text[:200]
                except Exception:
                    body = ''
                raise RuntimeError(f'download HTTP {resp.status_code}: {body}')
            content_type = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            mode = 'ab' if (use_resume and resp.status_code == 206) else 'wb'
            if mode == 'wb':
                written = 0
            with open(dest, mode) as out:
                for chunk in resp.iter_content(1024 * 1024):
                    if chunk:
                        out.write(chunk)
                        written += len(chunk)
        finally:
            try:
                resp.close()
            except Exception:
                pass
        return written, content_type


def build_curl_download_cmd(
    curl: str,
    url: str,
    dest,
    *,
    connect_timeout: int,
    max_time: int,
    speed_limit_bps: int,
    speed_time_s: int,
    resume: bool,
) -> list[str]:
    """Build curl argv for a CDN download (testable helper)."""
    from pathlib import Path

    dest_s = str(Path(dest))
    cmd = [
        curl,
        '-fsSL',
        '--http1.1',
        '--connect-timeout',
        str(connect_timeout),
        '--max-time',
        str(max_time),
        '--speed-limit',
        str(max(1, int(speed_limit_bps))),
        '--speed-time',
        str(max(1, int(speed_time_s))),
        '-A',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        '-e',
        f'{BASE}/client/photo',
    ]
    if resume:
        cmd.extend(['-C', '-'])
    cmd.extend(['-o', dest_s, url])
    return cmd
