"""ORIGINAL CDN hard failures fall back to Disk; rate limits do not."""

from __future__ import annotations

from pathlib import Path

import pytest

from yandex_photo_backup.downloader import (
    _original_cdn_needs_disk_fallback,
    download_one,
)


def _row() -> dict:
    return {
        'remote_id': 'uid:photo-500',
        'remote_path': '/disk/Photo/2016/trip/DSC_1169.jpg',
        'name': 'DSC_1169.jpg',
        'size': 4,
        'md5': None,
        'media_type': 'image/jpeg',
        'year': 2016,
        'month': 10,
    }


class _Client:
    def __init__(
        self, *, fail_with: str, direct: str | None = 'https://cdn/direct'
    ) -> None:
        self.fail_with = fail_with
        self.direct = direct
        self.file_url_calls = 0
        self.seen: list[str] = []

    def download_bytes_stream(
        self, url: str, dest: Path, **_kwargs: object
    ) -> tuple[int, str]:
        self.seen.append(url)
        if url == 'https://cdn/original':
            raise RuntimeError(self.fail_with)
        payload = b'jpeg'
        dest.write_bytes(payload)
        return len(payload), 'image/jpeg'

    def get_file_url(self, _path: str) -> str | None:
        self.file_url_calls += 1
        return self.direct


def test_http_500_uses_disk_url(tmp_path: Path) -> None:
    client = _Client(
        fail_with='curl failed (22): The requested URL returned error: 500'
    )
    ok, _message, final = download_one(
        client,  # type: ignore[arg-type]
        _row(),
        files_root=tmp_path / 'files',
        tmp_root=tmp_path / 'tmp',
        url='https://cdn/original',
    )
    assert ok
    assert final is not None
    assert final.read_bytes() == b'jpeg'
    assert client.file_url_calls == 1
    assert client.seen == ['https://cdn/original', 'https://cdn/direct']


def test_http_429_does_not_call_disk(tmp_path: Path) -> None:
    client = _Client(
        fail_with='curl failed (22): The requested URL returned error: 429'
    )
    ok, message, final = download_one(
        client,  # type: ignore[arg-type]
        _row(),
        files_root=tmp_path / 'files',
        tmp_root=tmp_path / 'tmp',
        url='https://cdn/original',
    )
    assert not ok
    assert final is None
    assert '429' in message
    assert client.file_url_calls == 0


def test_http_500_falls_through_to_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client(
        fail_with='curl failed (22): The requested URL returned error: 500',
        direct=None,
    )

    def _prepare(_client, _remote, part_path: Path, *_args, **_kwargs) -> None:
        part_path.write_bytes(b'jpeg')

    monkeypatch.setattr(
        'yandex_photo_backup.downloader._download_via_prepare', _prepare
    )
    ok, _message, final = download_one(
        client,  # type: ignore[arg-type]
        _row(),
        files_root=tmp_path / 'files',
        tmp_root=tmp_path / 'tmp',
        url='https://cdn/original',
    )
    assert ok
    assert final is not None
    assert final.read_bytes() == b'jpeg'


class _ClientEmptyOriginal(_Client):
    def download_bytes_stream(
        self, url: str, dest: Path, **_kwargs: object
    ) -> tuple[int, str]:
        self.seen.append(url)
        if url == 'https://cdn/original':
            dest.write_bytes(b'')
            return 1, 'image/jpeg'
        payload = b'jpeg'
        dest.write_bytes(payload)
        return len(payload), 'image/jpeg'


def test_empty_original_body_uses_disk_url(tmp_path: Path) -> None:
    client = _ClientEmptyOriginal(
        fail_with='unused',
        direct='https://cdn/direct',
    )
    ok, _message, final = download_one(
        client,  # type: ignore[arg-type]
        _row(),
        files_root=tmp_path / 'files',
        tmp_root=tmp_path / 'tmp',
        url='https://cdn/original',
    )
    assert ok
    assert final is not None
    assert client.file_url_calls == 1


@pytest.mark.parametrize(
    ('errors', 'expected'),
    [
        (['ORIGINAL CDN: error: 404'], True),
        (['ORIGINAL wrote 0 bytes'], True),
        (['ORIGINAL verify: zero-byte file'], True),
        (['ORIGINAL CDN: curl failed (28): timeout'], False),
        (['prepare HTTP 429'], False),
    ],
)
def test_fallback_predicate(errors: list[str], expected: bool) -> None:
    assert _original_cdn_needs_disk_fallback(errors) is expected
