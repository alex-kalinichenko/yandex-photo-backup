"""Tests for resumable curl download command building."""

from pathlib import Path

from yandex_photo_backup.client import build_curl_download_cmd


def test_curl_cmd_without_resume() -> None:
    cmd = build_curl_download_cmd(
        'curl.exe',
        'https://example/file.bin',
        Path('out.bin'),
        connect_timeout=10,
        max_time=100,
        speed_limit_bps=256,
        speed_time_s=60,
        resume=False,
    )
    assert '-C' not in cmd
    assert '-o' in cmd
    assert str(Path('out.bin')) in cmd
    assert 'https://example/file.bin' in cmd


def test_curl_cmd_with_resume() -> None:
    cmd = build_curl_download_cmd(
        'curl.exe',
        'https://example/file.bin',
        Path('out.bin'),
        connect_timeout=10,
        max_time=100,
        speed_limit_bps=256,
        speed_time_s=60,
        resume=True,
    )
    assert cmd[cmd.index('-C') + 1] == '-'
    assert '-o' in cmd
    assert 'https://example/file.bin' in cmd
