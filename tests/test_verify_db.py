from pathlib import Path

from yandex_photo_backup.db import ManifestDB
from yandex_photo_backup.verify import sniff_is_error_page, verify_local_file


def test_verify_rejects_zero_and_html(tmp_path: Path) -> None:
    zero = tmp_path / 'z.jpg'
    zero.write_bytes(b'')
    ok, reason, _ = verify_local_file(zero, expected_size=10, expected_md5=None)
    assert not ok
    assert 'zero' in reason

    html = tmp_path / 'h.jpg'
    html.write_bytes(b'<!DOCTYPE html><html>error</html>')
    assert sniff_is_error_page(html)
    ok, reason, _ = verify_local_file(html, expected_size=None, expected_md5=None)
    assert not ok


def test_verify_md5_and_size(tmp_path: Path) -> None:
    p = tmp_path / 'a.bin'
    data = b'hello-photo'
    p.write_bytes(data)
    import hashlib

    digest = hashlib.md5(data).hexdigest()
    ok, reason, actual = verify_local_file(p, expected_size=len(data), expected_md5=digest)
    assert ok
    assert actual == digest

    ok, reason, _ = verify_local_file(p, expected_size=1, expected_md5=digest)
    assert not ok
    assert 'size' in reason


def test_manifest_upsert_does_not_delete_local_semantics(tmp_path: Path) -> None:
    db = ManifestDB(tmp_path / 't.sqlite')
    db.upsert_object(
        {
            'remote_id': 'id1',
            'name': 'a.jpg',
            'remote_path': '/photounlim/a.jpg',
            'cluster_id': 'c',
            'media_type': 'image/jpeg',
            'size': 10,
            'md5': 'abc',
            'sha256': None,
            'taken_at': None,
            'year': 2020,
            'month': 1,
        }
    )
    db.mark_verified('id1', str(tmp_path / 'a.jpg'), local_md5='abc')
    db.upsert_object(
        {
            'remote_id': 'id1',
            'name': 'a.jpg',
            'remote_path': '/photounlim/a.jpg',
            'cluster_id': 'c',
            'media_type': 'image/jpeg',
            'size': 10,
            'md5': 'abc',
            'sha256': None,
            'taken_at': None,
            'year': 2020,
            'month': 1,
        }
    )
    row = db.conn.execute('SELECT download_status, verify_status FROM objects WHERE remote_id=?', ('id1',)).fetchone()
    assert row['verify_status'] == 'verified'
    # Simulate remote disappearance: we simply stop seeing it; row remains.
    stats = db.stats()
    assert stats['verified'] == 1
    db.close()


def test_size_change_requeues(tmp_path: Path) -> None:
    db = ManifestDB(tmp_path / 't2.sqlite')
    db.upsert_object(
        {
            'remote_id': 'id2',
            'name': 'b.jpg',
            'remote_path': '/photounlim/b.jpg',
            'cluster_id': 'c',
            'media_type': 'image/jpeg',
            'size': 10,
            'md5': 'aaa',
            'sha256': None,
            'taken_at': None,
            'year': 2020,
            'month': 2,
        }
    )
    db.mark_verified('id2', str(tmp_path / 'b.jpg'), local_md5='aaa')
    db.upsert_object(
        {
            'remote_id': 'id2',
            'name': 'b.jpg',
            'remote_path': '/photounlim/b.jpg',
            'cluster_id': 'c',
            'media_type': 'image/jpeg',
            'size': 11,
            'md5': 'bbb',
            'sha256': None,
            'taken_at': None,
            'year': 2020,
            'month': 2,
        }
    )
    row = db.conn.execute('SELECT download_status FROM objects WHERE remote_id=?', ('id2',)).fetchone()
    assert row['download_status'] == 'pending'
    db.close()


def test_pending_downloads_photos_before_videos(tmp_path: Path) -> None:
    db = ManifestDB(tmp_path / 'order.sqlite')
    for remote_id, name, media_type in (
        ('v1', 'clip.mp4', 'video/mp4'),
        ('p1', 'shot.jpg', 'image/jpeg'),
        ('v2', 'movie.mov', 'video/quicktime'),
        ('p2', 'pic.png', 'image/png'),
    ):
        db.upsert_object(
            {
                'remote_id': remote_id,
                'name': name,
                'remote_path': f'/photounlim/{name}',
                'cluster_id': 'c',
                'media_type': media_type,
                'size': 100,
                'md5': 'deadbeef',
                'sha256': None,
                'taken_at': None,
                'year': 2024,
                'month': 1,
            }
        )
    db.commit()
    pending = db.pending_downloads()
    assert [r['remote_id'] for r in pending] == ['p1', 'p2', 'v1', 'v2']
    assert [r['remote_id'] for r in db.pending_downloads(media='photos')] == ['p1', 'p2']
    assert [r['remote_id'] for r in db.pending_downloads(media='videos')] == ['v1', 'v2']
    db.close()


def test_pending_downloads_year_order_2025_then_older_then_2026(tmp_path: Path) -> None:
    db = ManifestDB(tmp_path / 'years.sqlite')
    specs = (
        ('p26', 'a.jpg', 'image/jpeg', 2026),
        ('p23', 'b.jpg', 'image/jpeg', 2023),
        ('p25', 'c.jpg', 'image/jpeg', 2025),
        ('p24', 'd.jpg', 'image/jpeg', 2024),
        ('v26', 'e.mp4', 'video/mp4', 2026),
        ('v25', 'f.mp4', 'video/mp4', 2025),
        ('v24', 'g.mp4', 'video/mp4', 2024),
    )
    for remote_id, name, media_type, year in specs:
        db.upsert_object(
            {
                'remote_id': remote_id,
                'name': name,
                'remote_path': f'/photounlim/{name}',
                'cluster_id': 'c',
                'media_type': media_type,
                'size': 100,
                'md5': 'deadbeef',
                'sha256': None,
                'taken_at': None,
                'year': year,
                'month': 1,
            }
        )
    db.commit()
    assert [r['remote_id'] for r in db.pending_downloads()] == [
        'p25',
        'p24',
        'p23',
        'p26',
        'v25',
        'v24',
        'v26',
    ]
    db.close()
