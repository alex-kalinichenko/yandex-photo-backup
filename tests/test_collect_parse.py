from yandex_photo_backup.collect import extract_resources_from_response, parse_resource_item


def test_parse_resource_from_mpfs_meta() -> None:
    item = {
        'id': '/photounlim/IMG_001.JPG',
        'path': '/photounlim/IMG_001.JPG',
        'name': 'IMG_001.JPG',
        'clusterId': 'c1',
        'etime': 1609459200,
        'meta': {
            'size': 1234,
            'mimetype': 'image/jpeg',
            'mediatype': 'image',
            'resource_id': 'uid:filehash',
            'sizes': [
                {
                    'name': 'ORIGINAL',
                    'url': (
                        '//downloader.disk.yandex.ru/disk/x?'
                        'fsize=1234&etag=AaBbCc&media_type=image'
                    ),
                }
            ],
        },
    }
    parsed = parse_resource_item(item)
    assert parsed is not None
    assert parsed['remote_id'] == 'uid:filehash'
    assert parsed['remote_path'] == '/photounlim/IMG_001.JPG'
    assert parsed['md5'] == 'aabbcc'
    assert parsed['size'] == 1234
    assert parsed['year'] == 2021
    assert parsed['original_url'].startswith('https://')


def test_parse_skips_cluster_stub_items() -> None:
    stub = {
        'id': '/photounlim/a.jpg',
        'itemId': 'x',
        'width': 100,
        'height': 100,
        'beauty': 1.0,
        'albums': ['camera'],
    }
    assert parse_resource_item(stub) is None


def test_extract_prefers_resources_fetched() -> None:
    data = {
        'clusters': {
            'fetched': [
                {
                    'id': 'c1',
                    'items': [{'id': '/photounlim/a.jpg', 'width': 1, 'height': 1, 'beauty': 1}],
                }
            ]
        },
        'resources': {
            'fetched': [
                {
                    'clusterId': 'c1',
                    'id': '/photounlim/a.jpg',
                    'path': '/photounlim/a.jpg',
                    'name': 'a.jpg',
                    'meta': {'size': 10, 'resource_id': 'r1', 'sizes': []},
                }
            ]
        },
    }
    out = extract_resources_from_response(data)
    assert len(out) == 1
    assert out[0][1]['name'] == 'a.jpg'
