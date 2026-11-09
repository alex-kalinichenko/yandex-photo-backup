"""Prepare circuit must fail fast (not sleep) so CDN workers stay free."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import yandex_photo_backup.downloader as dl
from yandex_photo_backup.client import RateLimitError


def test_prepare_fails_fast_when_circuit_open() -> None:
    dl._PREPARE_BLOCKED_UNTIL = time.time() + 30

    class _Client:
        def prepare_download_url(self, _paths: list[str]) -> str:
            raise AssertionError('prepare must not be called while circuit is open')

    with pytest.raises(RateLimitError, match='circuit open'):
        dl._download_via_prepare(
            _Client(),  # type: ignore[arg-type]
            '/disk/Photo/a.mp4',
            Path('x.part'),
            Path('x.zip.part'),
            Path('x.zip'),
            expected_size=1000,
        )
