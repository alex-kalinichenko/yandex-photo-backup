import zipfile
from pathlib import Path

from yandex_photo_backup.downloader import _extract_single_from_zip


def test_extract_single_from_zip_picks_largest(tmp_path: Path) -> None:
    zpath = tmp_path / 't.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        zf.writestr('small.txt', b'abc')
        zf.writestr('folder/big.jpg', b'x' * 1000)
    dest = tmp_path / 'out.part'
    _extract_single_from_zip(zpath, dest)
    assert dest.read_bytes() == b'x' * 1000
