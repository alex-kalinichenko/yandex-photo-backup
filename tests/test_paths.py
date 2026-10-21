from pathlib import Path

import pytest

from yandex_photo_backup.paths import ensure_under, sanitize_filename, year_month_subdir


def test_sanitize_filename_strips_path_and_invalid_chars() -> None:
    assert sanitize_filename(r'..\..\evil:name?.jpg') == 'evil_name_.jpg'
    assert sanitize_filename('') == 'unnamed'


def test_year_month_subdir() -> None:
    assert year_month_subdir(2020, 7) == Path('2020') / '07'
    assert year_month_subdir(None, None) == Path('unknown_date')


def test_ensure_under_blocks_traversal(tmp_path: Path) -> None:
    base = tmp_path / 'root'
    base.mkdir()
    with pytest.raises(ValueError):
        ensure_under(base, base / '..' / 'outside.txt')
