"""Adaptive download pacing based on recent success / throughput."""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdaptiveState:
    """Snapshot of current pacing decisions."""

    workers: int
    cluster_delay_s: float
    success_rate: float
    samples: int
    mode: str


@dataclass(frozen=True)
class CurlStall:
    """curl --speed-limit / --speed-time knobs."""

    speed_limit_bps: int
    speed_time_s: int


class AdaptiveController:
    """Scale workers and delays from a rolling window of download outcomes.

    Conservative by default (Yandex DCs may be degraded). Scale up only when
    recent success stays high; back off hard on sustained failure / 429.
    When degraded, fail fast (short curl stall) and abandon dead clusters.
    """

    def __init__(
        self,
        *,
        min_workers: int = 1,
        max_workers: int = 6,
        start_workers: int = 2,
        window: int = 40,
    ) -> None:
        self.min_workers = max(1, min_workers)
        self.max_workers = max(self.min_workers, max_workers)
        self.workers = max(self.min_workers, min(start_workers, self.max_workers))
        self._outcomes: deque[bool] = deque(maxlen=max(10, window))
        self._bytes: deque[int] = deque(maxlen=max(10, window))
        self._durations: deque[float] = deque(maxlen=max(10, window))
        self._cooldown_until = 0.0
        self.mode = 'probe'

    def record(
        self,
        ok: bool,
        *,
        bytes_written: int = 0,
        duration_s: float = 0.0,
        rate_limited: bool = False,
    ) -> None:
        """Record one download attempt outcome and maybe retune."""
        self._outcomes.append(bool(ok))
        self._bytes.append(max(0, int(bytes_written)))
        self._durations.append(max(0.0, float(duration_s)))
        if rate_limited:
            self._cooldown_until = max(self._cooldown_until, time.time() + 120.0)
            self.workers = self.min_workers
            self.mode = 'rate_limit'
            logger.warning('Adaptive: rate limit — workers=%s cooldown=120s', self.workers)
            return
        self._retune()

    def _success_rate(self) -> float:
        if not self._outcomes:
            return 0.0
        return sum(1 for x in self._outcomes if x) / len(self._outcomes)

    def _recent_success_rate(self, n: int = 8) -> float:
        if not self._outcomes:
            return 0.0
        recent = list(self._outcomes)[-n:]
        return sum(1 for x in recent if x) / len(recent)

    def _retune(self) -> None:
        n = len(self._outcomes)
        if n < 8:
            self.mode = 'probe'
            return
        rate = self._success_rate()
        recent = self._recent_success_rate(8)
        # Recover quickly after a good streak even if the long window is still low.
        if recent >= 0.75 and self.workers < self.max_workers:
            bump = 2 if recent >= 0.85 else 1
            self.workers = min(self.max_workers, self.workers + bump)
            self.mode = 'scale_up'
            logger.info(
                'Adaptive: scale_up workers=%s success=%.0f%% recent=%.0f%%',
                self.workers,
                rate * 100,
                recent * 100,
            )
            return
        if rate >= 0.85 and self.workers < self.max_workers:
            self.workers = min(self.max_workers, self.workers + 2)
            self.mode = 'scale_up'
            logger.info('Adaptive: scale_up workers=%s success=%.0f%%', self.workers, rate * 100)
        elif rate >= 0.65:
            self.mode = 'steady'
        elif rate >= 0.40:
            self.workers = max(self.min_workers, self.workers - 1)
            self.mode = 'scale_down'
            logger.info('Adaptive: scale_down workers=%s success=%.0f%%', self.workers, rate * 100)
        else:
            self.workers = self.min_workers
            self.mode = 'degraded'
            logger.warning('Adaptive: degraded workers=1 success=%.0f%%', rate * 100)

    def cluster_delay(self) -> float:
        """Seconds to sleep after a cluster batch."""
        if time.time() < self._cooldown_until:
            return 15.0
        rate = self._success_rate()
        if len(self._outcomes) < 8:
            return 0.4
        if rate >= 0.85:
            return 0.1
        if rate >= 0.65:
            return 0.25
        if rate >= 0.40:
            return 0.6
        # Degraded: move to the next cluster quickly instead of idling.
        return 0.8

    def prefer_small_first(self) -> bool:
        """When degraded, try smaller files first to keep progress moving."""
        return self.mode in {'degraded', 'scale_down', 'rate_limit'} or self._success_rate() < 0.5

    def curl_stall(self) -> CurlStall:
        """Fail-fast curl stall when CDN is bad; more patient when healthy."""
        rate = self._success_rate()
        if self.mode == 'rate_limit' or (len(self._outcomes) >= 8 and rate < 0.20):
            return CurlStall(speed_limit_bps=512, speed_time_s=18)
        if self.mode == 'degraded' or (len(self._outcomes) >= 8 and rate < 0.40):
            return CurlStall(speed_limit_bps=512, speed_time_s=28)
        if len(self._outcomes) >= 8 and rate < 0.65:
            return CurlStall(speed_limit_bps=256, speed_time_s=40)
        return CurlStall(speed_limit_bps=256, speed_time_s=60)

    def cdn_read_timeout_s(self, size: int) -> int:
        """Wall-clock CDN timeout; keep short while degraded."""
        size = max(0, int(size or 0))
        rate = self._success_rate()
        if self.mode in {'degraded', 'rate_limit'} or (len(self._outcomes) >= 8 and rate < 0.35):
            # Assume ~8 KB/s or give up within ~3 minutes.
            return min(180, max(40, size // (8 * 1024) + 40))
        min_bps = 512
        return min(3600, max(120, size // min_bps + 90))

    def should_abandon_cluster(self, *, cluster_ok: int, cluster_fail: int) -> bool:
        """Skip remaining files in a cluster that looks globally dead right now."""
        if cluster_ok > 0:
            return False
        if self.mode in {'degraded', 'rate_limit'} and cluster_fail >= 3:
            return True
        if cluster_fail >= 5:
            return True
        return False

    def wait_if_cooling(self) -> None:
        """Block until rate-limit cooldown ends."""
        remaining = self._cooldown_until - time.time()
        if remaining > 0:
            logger.warning('Adaptive: cooling down %.1fs', remaining)
            time.sleep(remaining)

    def state(self) -> AdaptiveState:
        return AdaptiveState(
            workers=self.workers,
            cluster_delay_s=self.cluster_delay(),
            success_rate=self._success_rate(),
            samples=len(self._outcomes),
            mode=self.mode,
        )
