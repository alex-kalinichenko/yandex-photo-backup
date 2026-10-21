#!/usr/bin/env python3
"""Export Yandex cookies via Chrome DevTools Protocol (local only).

Usage:
  1. Start Chrome:

     & \"$env:ProgramFiles\\Google\\Chrome\\Application\\chrome.exe\" `
       --remote-debugging-port=9222 `
       --remote-allow-origins=* `
       --user-data-dir=\"$env:TEMP\\chrome-yadisk-debug\" `
       https://disk.yandex.ru/client/photo

  2. Log into Yandex in that window.
  3. Run:  python export_cookies_cdp.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

try:
    import websocket  # type: ignore
except ImportError:
    import subprocess

    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'websocket-client', '-q'])
    import websocket  # type: ignore

PORT = 9222
OUT = Path(__file__).resolve().parent / 'cookies.json'


def _recv_id(ws, expect_id: int) -> dict:
    while True:
        data = json.loads(ws.recv())
        if data.get('id') == expect_id:
            return data


def main() -> int:
    try:
        tabs = json.loads(
            urllib.request.urlopen(f'http://127.0.0.1:{PORT}/json', timeout=5).read().decode()
        )
    except Exception as exc:
        print(f'Cannot connect to Chrome CDP on port {PORT}: {exc}')
        print('Start Chrome with --remote-debugging-port=9222 --remote-allow-origins=* first.')
        return 1

    page = next(
        (
            t
            for t in tabs
            if t.get('type') == 'page' and 'yandex' in (t.get('url') or '')
        ),
        None,
    )
    if page is None:
        page = next((t for t in tabs if t.get('type') == 'page'), None)
    if page is None or not page.get('webSocketDebuggerUrl'):
        print('No page target found in Chrome CDP.')
        return 1

    ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=10)
    ws.send(json.dumps({'id': 1, 'method': 'Network.enable'}))
    _recv_id(ws, 1)
    ws.send(json.dumps({'id': 2, 'method': 'Network.getAllCookies'}))
    data = _recv_id(ws, 2)
    ws.close()

    cookies = data.get('result', {}).get('cookies', [])
    yandex = [
        {
            'name': c['name'],
            'value': c['value'],
            'domain': c.get('domain', ''),
            'path': c.get('path', '/'),
            'httpOnly': c.get('httpOnly', False),
            'secure': c.get('secure', False),
        }
        for c in cookies
        if 'yandex' in (c.get('domain') or '')
    ]
    if not any(c['name'] in {'Session_id', 'sessionid2'} for c in yandex):
        print('No Yandex session cookie found. Log into disk.yandex.ru first.')
        return 2

    OUT.write_text(json.dumps(yandex, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Wrote {len(yandex)} Yandex cookies to {OUT}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
