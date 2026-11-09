"""Tests for Files On-Demand free-space and dehydrate helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from yandex_photo_backup.cloud_local import (
    dehydrate_cloud_file,
    ensure_free_space,
    free_bytes,
    is_cloud_placeholder,
    same_volume,
)


def test_free_bytes_positive(tmp_path: Path) -> None:
    assert free_bytes(tmp_path) > 0


def test_ensure_free_space_ok(tmp_path: Path) -> None:
    ensure_free_space(tmp_path, needed_bytes=1, reserve_bytes=1)


def test_ensure_free_space_raises_when_insufficient(tmp_path: Path) -> None:
    huge = 10**18
    with pytest.raises(OSError, match="insufficient free space"):
        ensure_free_space(tmp_path, needed_bytes=huge, reserve_bytes=huge)


def test_same_volume_for_nested_paths(tmp_path: Path) -> None:
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    assert same_volume(tmp_path, child)


def test_dehydrate_noop_on_non_windows(tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc")
    with patch("yandex_photo_backup.cloud_local.sys.platform", "linux"):
        assert dehydrate_cloud_file(f) is False


def test_dehydrate_calls_attrib_on_windows(tmp_path: Path) -> None:
    f = tmp_path / "vid.mp4"
    f.write_bytes(b"abc")
    completed = MagicMock(returncode=0, stderr="", stdout="")
    with (
        patch("yandex_photo_backup.cloud_local.sys.platform", "win32"),
        patch(
            "yandex_photo_backup.cloud_local.subprocess.run",
            return_value=completed,
        ) as run,
    ):
        assert dehydrate_cloud_file(f) is True
    run.assert_called_once()
    args = run.call_args.args[0]
    assert args[:3] == ["attrib", "+U", "-P"]
    assert args[3] == str(f)


def test_dehydrate_false_on_attrib_failure(tmp_path: Path) -> None:
    f = tmp_path / "vid.mp4"
    f.write_bytes(b"abc")
    completed = MagicMock(returncode=1, stderr="access denied", stdout="")
    with (
        patch("yandex_photo_backup.cloud_local.sys.platform", "win32"),
        patch(
            "yandex_photo_backup.cloud_local.subprocess.run",
            return_value=completed,
        ),
    ):
        assert dehydrate_cloud_file(f) is False


def test_is_cloud_placeholder_false_without_win_attrs(tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"x")
    with patch(
        "yandex_photo_backup.cloud_local._win_file_attributes", return_value=None
    ):
        assert is_cloud_placeholder(f) is False
