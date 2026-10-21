# Yandex Unlimited Photos Backup

Local, resumable backup of **Yandex Disk Unlimited Photos** (Photoslice / «Фото»).
Downloads originals to your disk with SQLite state, size verification, and adaptive
parallelism when Yandex CDN clusters are slow or flaky.

> **Not** a full Yandex Disk file-tree client. It targets the photo timeline API
> (photos and videos in Unlimited Photos), not arbitrary Disk folders.

[Русская версия](README.ru.md)

## Features

- Cookie-based auth (no OAuth app registration)
- Manifest collect → download → verify pipeline
- Resume-safe SQLite WAL database
- Adaptive worker pool (stall detection, abandon dead CDN hosts)
- Filter by media type (`photos` / `videos` / `all`)
- Year-aware download order (newer years first, current year last)
- Optional MD5-only queue (`--with-md5-only`) for items that already have checksums

## Requirements

- Python 3.11+
- Windows, macOS, or Linux
- `curl` on `PATH` (Windows 10+ includes `curl.exe`)
- A logged-in Yandex session exported as cookies

```bash
pip install -r requirements.txt
```

## Authentication

**Never commit `cookies.json`.** It contains your session and is equivalent to a password.

### Option A — Cookie-Editor (recommended)

1. Open [disk.yandex.ru/client/photo](https://disk.yandex.ru/client/photo) and log in.
2. Export cookies as JSON with [Cookie-Editor](https://cookie-editor.com/).
3. Save as `cookies.json` in the repo root (see `cookies.json.example`).

You need at least `Session_id` (and usually `sessionid2`).

### Option B — Chrome DevTools (local)

```bash
python export_cookies_cdp.py
```

See comments at the top of `export_cookies_cdp.py` for how to start Chrome with remote debugging.

### Option C — Firefox profile

If Firefox is installed and logged into Yandex, the tool may auto-detect cookies once.

## Quick start

```bash
# Optional: where files and the SQLite DB live (default: ~/YandexPhoto)
# Windows PowerShell:
$env:YANDEX_PHOTO_ROOT = 'D:\YandexPhoto'

# 1) Build / refresh the remote manifest
python backup.py collect

# 2) Download photos first (adaptive workers)
python backup.py download --media photos --with-md5-only --workers 6

# 3) Then videos
python backup.py download --media videos --workers 4

# 4) Re-verify local files against the DB
python backup.py reconcile

# 5) Progress summary
python backup.py status
```

Windows shortcut:

```bat
yandex-photo-backup.bat download --media photos --workers 6
```

## Useful options

| Option | Meaning |
|--------|---------|
| `--root PATH` | Backup root (files + DB + logs) |
| `--media photos\|videos\|all` | What to download |
| `--workers N` | Max parallel downloads (adaptive may use fewer) |
| `--with-md5-only` | Only items that already have MD5 in the manifest |
| `--limit N` | Stop after N successful downloads |
| `--no-adaptive` | Fixed worker count, no stall/abandon logic |
| `--resume-clusters` | On collect, continue without full cluster refresh |

## Helper scripts

Set `YANDEX_PHOTO_ROOT` to the same path you use for backup.

```bash
python scripts/photo_progress.py   # photos vs videos progress
python scripts/year_stats.py       # per-year done / pending
python scripts/watchdog_download.py  # restart download if the process dies
```

## Layout under the backup root

```
YandexPhoto/
  yandex_photo_backup.sqlite   # manifest + download state
  files/                       # downloaded originals
  tmp/                         # in-progress .part files
  logs/                        # daily logs
```

## Security notes

- Keep `cookies.json` private; rotate the Yandex session if it was ever shared.
- This tool is **read-only** toward your cloud account (list + download).
- Do not publish sample API responses — they may contain your `uid` and signed URLs.

## Disclaimer

Unofficial tool. Yandex APIs and CDN behavior can change. Use at your own risk
and respect Yandex Terms of Service. Prefer moderate `--workers` values.

## License

MIT — see [LICENSE](LICENSE).
