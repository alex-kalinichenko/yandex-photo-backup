"""download_one keeps partial files and finalizes a complete .part."""

from __future__ import annotations

from pathlib import Path
from yandex_photo_backup.downloader import download_one


def test_download_one_finalizes_complete_part(tmp_path: Path) -> None:
    files = tmp_path / 'files'
    tmp = tmp_path / 'tmp'
    files.mkdir()
    tmp.mkdir()
    payload = b'hello-video-bytes-12345'
    row = {
        'remote_id': 'uid:video1',
        'remote_path': '/disk/Photo/clip.mp4',
        'name': 'clip.mp4',
        'size': len(payload),
        'md5': None,
        'media_type': 'video/mp4',
        'year': 2019,
        'month': 1,
    }
    digest = abs(hash(row['remote_id'])) % 10_000_000
    part = tmp / f'{digest}.part'
    part.write_bytes(payload)

    class _Client:
        def get_file_url(self, _path: str) -> None:
            return None

    ok, message, final = download_one(
        _Client(),  # type: ignore[arg-type]
        row,
        files_root=files,
        tmp_root=tmp,
        url=None,
    )
    assert ok
    assert final is not None
    assert final.exists()
    assert final.read_bytes() == payload
    assert 'resumed complete part' in message
    assert not part.exists()
