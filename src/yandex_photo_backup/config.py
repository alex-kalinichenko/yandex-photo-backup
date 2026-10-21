"""Runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Paths and knobs for the backup run."""

    root: Path
    files_dir: Path
    db_path: Path
    logs_dir: Path
    cookies_path: Path
    tmp_dir: Path
    cluster_batch: int = 8
    download_workers_note: str = 'single-threaded by design'
    max_attempts: int = 8

    @classmethod
    def from_env(cls, root: Path | None = None) -> Config:
        default_root = Path.home() / 'YandexPhoto'
        base = Path(root or os.environ.get('YANDEX_PHOTO_ROOT', str(default_root)))
        project = Path(__file__).resolve().parents[2]
        cookies = Path(os.environ.get('YANDEX_PHOTO_COOKIES', project / 'cookies.json'))
        return cls(
            root=base,
            files_dir=base / 'unlimited',
            db_path=base / 'yandex_photo_backup.sqlite',
            logs_dir=base / 'logs',
            cookies_path=cookies,
            tmp_dir=base / '_tmp',
            cluster_batch=int(os.environ.get('YANDEX_PHOTO_CLUSTER_BATCH', '8')),
            max_attempts=int(os.environ.get('YANDEX_PHOTO_MAX_ATTEMPTS', '8')),
        )

    def ensure_dirs(self) -> None:
        for path in (self.root, self.files_dir, self.logs_dir, self.tmp_dir):
            path.mkdir(parents=True, exist_ok=True)
