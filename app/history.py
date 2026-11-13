"""Датированные наблюдения BNM для графика валютных пар."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Callable

from app.core import SUPPORTED_CURRENCIES, CoreError, RateSnapshot, parse_date
from app.rates import DEFAULT_CACHE_PATH, RatesServiceError, write_json_atomically


PERIOD_STEPS = {7: 1, 30: 2, 90: 5, 365: 15}
MAX_ARCHIVE_DATES = 600
HISTORY_WORKERS = 4


@dataclass(frozen=True)
class RatePoint:
    day: date
    value: Decimal


@dataclass(frozen=True)
class RateSeries:
    points: tuple[RatePoint, ...] = ()

    @property
    def change(self) -> Decimal | None:
        if len(self.points) < 2:
            return None
        return (self.points[-1].value / self.points[0].value - 1) * 100


@dataclass(frozen=True)
class HistoryWindow:
    start: date
    end: date
    snapshots: tuple[tuple[date, RateSnapshot], ...]
    from_archive: bool = False
    source_unavailable: bool = False
    archive_write_failed: bool = False

    def series(self, base: str, target: str) -> RateSeries:
        return RateSeries(tuple(
            RatePoint(day, snapshot.get_rate(base) / snapshot.get_rate(target))
            for day, snapshot in self.snapshots
            if snapshot.has_currency(base) and snapshot.has_currency(target)
        ))


class RateHistory:
    """Загружает историю в одном потоке-координаторе и хранит архив отдельно."""

    def __init__(
        self,
        read_snapshot: Callable[[date], RateSnapshot],
        path: Path = DEFAULT_CACHE_PATH.with_name("history.json"),
    ) -> None:
        self.read_snapshot = read_snapshot
        self.path = path
        self._snapshots: dict[date, RateSnapshot] | None = None
        self._checked_days: set[date] = set()

    def _read_archive(self) -> dict[date, RateSnapshot]:
        snapshots = {}
        try:
            content = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return snapshots
        if not isinstance(content, list):
            return snapshots

        for entry in content[:MAX_ARCHIVE_DATES]:
            try:
                snapshot = RateSnapshot.from_dict(entry)
                day = parse_date(snapshot.date, max_date=date.max)
                if not snapshot.is_empty:
                    snapshots[day] = snapshot
            except (CoreError, TypeError, ValueError, ArithmeticError):
                continue
        return snapshots

    def _request(self, requested: date) -> tuple[date, RateSnapshot] | None:
        snapshot = self.read_snapshot(requested)
        if snapshot.is_empty:
            return None
        actual = parse_date(snapshot.date, max_date=date.max)
        if actual > requested:
            return None
        rates = {code: snapshot.get_rate(code) for code in SUPPORTED_CURRENCIES if snapshot.has_currency(code)}
        return actual, RateSnapshot(snapshot.date, rates)

    def load(
        self, end: date, days: int, cancel: Event, refresh: bool = False
    ) -> HistoryWindow:
        start = date.fromordinal(max(1, end.toordinal() - days + 1))
        step = PERIOD_STEPS[days]
        scheduled = set(range(end.toordinal(), start.toordinal() - 1, -step))
        scheduled.update(range(max(start.toordinal(), end.toordinal() - 3), end.toordinal() + 1))
        scheduled.add(start.toordinal())

        if self._snapshots is None:
            self._snapshots = self._read_archive()
        snapshots = self._snapshots
        pending = []
        today = date.today()
        for ordinal in sorted(scheduled, reverse=True):
            day = date.fromordinal(ordinal)
            known = day in self._checked_days or (day in snapshots and day < today)
            if refresh or not known:
                pending.append(day)

        fetched = False
        source_unavailable = False

        def collect(day: date, observation: tuple[date, RateSnapshot] | None) -> None:
            nonlocal fetched
            self._checked_days.add(day)
            if observation is not None:
                actual, snapshot = observation
                snapshots[actual] = snapshot
                fetched = True

        # Проверяем ближайшую дату отдельно, чтобы при отключённой сети
        # не отправлять сразу несколько запросов.
        if pending and not cancel.is_set():
            first = pending.pop(0)
            try:
                collect(first, self._request(first))
            except RatesServiceError:
                source_unavailable = True

        if pending and not source_unavailable and not cancel.is_set():
            with ThreadPoolExecutor(max_workers=HISTORY_WORKERS) as executor:
                for offset in range(0, len(pending), HISTORY_WORKERS):
                    if cancel.is_set() or source_unavailable:
                        break
                    batch = pending[offset:offset + HISTORY_WORKERS]
                    futures = [(day, executor.submit(self._request, day)) for day in batch]
                    for day, future in futures:
                        try:
                            collect(day, future.result())
                        except RatesServiceError:
                            source_unavailable = True

        observations = tuple((day, snapshots[day]) for day in sorted(snapshots) if start <= day <= end)
        archive_write_failed = False
        if fetched:
            retained = sorted(snapshots, reverse=True)[:MAX_ARCHIVE_DATES]
            self._snapshots = {day: snapshots[day] for day in retained}
            try:
                write_json_atomically(self.path, [snapshots[day].to_dict() for day in retained])
            except OSError:
                archive_write_failed = True

        return HistoryWindow(
            start, end, observations,
            from_archive=bool(observations) and not fetched,
            source_unavailable=source_unavailable,
            archive_write_failed=archive_write_failed,
        )
