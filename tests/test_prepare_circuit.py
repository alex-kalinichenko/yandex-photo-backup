"""Prepare circuit must wait instead of instantly failing videos."""

from __future__ import annotations

import time
from pathlib import Path

import yandex_photo_backup.downloader as dl


def test_prepare_waits_for_circuit() -> None:
    dl._PREPARE_BLOCKED_UNTIL = time.time() + 0.35
    calls: list[float] = []

    class _Client:
        def prepare_download_url(self, _paths: list[str]) -> str:
            calls.append(time.time())
            assert time.time() >= dl._PREPARE_BLOCKED_UNTIL - 0.05
            raise RuntimeError('stop-after-wait')

    try:
        dl._download_via_prepare(
            _Client(),  # type: ignore[arg-type]
            '/disk/Photo/a.mp4',
            Path('x.part'),
            Path('x.zip.part'),
            Path('x.zip'),
            expected_size=1000,
        )
    except RuntimeError as exc:
        assert 'stop-after-wait' in str(exc)
    else:
        raise AssertionError('expected RuntimeError from stub client')
    assert calls, 'prepare should run after waiting out the circuit'
