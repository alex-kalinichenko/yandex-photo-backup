"""Tests for adaptive download pacing."""

from yandex_photo_backup.adaptive import AdaptiveController


def test_starts_conservative() -> None:
    ctl = AdaptiveController(start_workers=2, max_workers=6)
    assert ctl.workers == 2
    assert ctl.mode == 'probe'


def test_scales_up_on_high_success() -> None:
    ctl = AdaptiveController(start_workers=2, max_workers=6, window=40)
    for _ in range(20):
        ctl.record(True, bytes_written=1000, duration_s=0.5)
    assert ctl.workers >= 3
    assert ctl.mode in {'scale_up', 'steady'}
    assert ctl.cluster_delay() <= 0.4


def test_degrades_on_failures() -> None:
    ctl = AdaptiveController(start_workers=4, max_workers=6, window=40)
    for _ in range(20):
        ctl.record(False)
    assert ctl.workers == 1
    assert ctl.mode == 'degraded'
    assert ctl.prefer_small_first()
    assert ctl.cluster_delay() < 2.0
    stall = ctl.curl_stall()
    assert stall.speed_time_s <= 30


def test_rate_limit_forces_min_workers() -> None:
    ctl = AdaptiveController(start_workers=5, max_workers=6)
    ctl.record(False, rate_limited=True)
    assert ctl.workers == 1
    assert ctl.mode == 'rate_limit'


def test_recover_fast_after_good_streak() -> None:
    ctl = AdaptiveController(start_workers=1, max_workers=6, window=40)
    for _ in range(20):
        ctl.record(False)
    assert ctl.mode == 'degraded'
    for _ in range(8):
        ctl.record(True, bytes_written=1000, duration_s=0.5)
    assert ctl.workers >= 2
    assert ctl.mode == 'scale_up'


def test_abandon_dead_cluster() -> None:
    ctl = AdaptiveController(start_workers=1, max_workers=6, window=40)
    for _ in range(12):
        ctl.record(False)
    assert ctl.should_abandon_cluster(cluster_ok=0, cluster_fail=3)
    assert not ctl.should_abandon_cluster(cluster_ok=1, cluster_fail=10)


def test_cdn_timeout_shorter_when_degraded() -> None:
    ctl = AdaptiveController(start_workers=1, max_workers=6, window=40)
    for _ in range(12):
        ctl.record(False)
    degraded_timeout = ctl.cdn_read_timeout_s(5_000_000)
    for _ in range(20):
        ctl.record(True, bytes_written=1000, duration_s=0.5)
    healthy_timeout = ctl.cdn_read_timeout_s(5_000_000)
    assert degraded_timeout < healthy_timeout
    assert degraded_timeout <= 180
