"""
Unit-тесты сетевого клиента, поиска курсов и кэширования (tests/test_rates.py).

Покрывает задачу T008 [US2]:
- Пустой и повреждённый XML (FR-04, FR-05, SC-03).
- Ошибки сети, HTTP и тайм-аута (FR-05, SC-03).
- Поиск курса за предыдущие даты до 14 дней (FR-04).
- Локальный JSON-кэш: сохранение, повреждение, подбор подходящего и применение (FR-05, FR-06).
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
import unittest

from app.core import RateSnapshot, format_date
from app.rates import (
    BNMClient,
    HttpError,
    NetworkError,
    RateResult,
    RatesCache,
    RatesFormatError,
    RatesNotFoundError,
    RatesService,
)


class TestRatesServiceAndCache(unittest.TestCase):
    """Тесты сетевой логики, поиска и кэша без реальных внешних обращений."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="rates_test_"))
        self.cache_file = self.temp_dir / "cache" / "rates.json"
        self.cache = RatesCache(cache_path=self.cache_file)

        self.valid_xml_template = """<?xml version="1.0" encoding="utf-8"?>
<ValCurs Date="{date}" name="Official exchange rates">
    <Valute ID="47">
        <NumCode>978</NumCode>
        <CharCode>EUR</CharCode>
        <Nominal>1</Nominal>
        <Name>Euro</Name>
        <Value>20.0000</Value>
    </Valute>
    <Valute ID="44">
        <NumCode>840</NumCode>
        <CharCode>USD</CharCode>
        <Nominal>1</Nominal>
        <Name>Dolar</Name>
        <Value>18.0000</Value>
    </Valute>
</ValCurs>"""

        self.empty_xml_template = """<?xml version="1.0" encoding="utf-8"?>
<ValCurs Date="{date}" name="Official exchange rates">
</ValCurs>"""

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_empty_xml_triggers_previous_day_search(self) -> None:
        """Пустой ответ за запрошенную дату запускает поиск до 14 дней (FR-04)."""
        requested = date(2026, 11, 10)
        requested_log: list[date] = []

        def mock_fetch(target_date: date) -> str:
            requested_log.append(target_date)
            # Курсы есть только за 08.11.2026 (например, после выходных)
            if target_date == date(2026, 11, 8):
                return self.valid_xml_template.format(date=format_date(target_date))
            return self.empty_xml_template.format(date=format_date(target_date))

        service = RatesService(cache=self.cache, fetch_fn=mock_fetch)
        result = service.fetch_rates(requested, ["EUR", "USD"])

        # Выполнено 3 запроса: 10.11, 09.11, 08.11
        self.assertEqual(len(requested_log), 3)
        self.assertEqual(result.requested_date, "10.11.2026")
        self.assertEqual(result.actual_date, "08.11.2026")
        self.assertTrue(result.is_fallback_date)
        self.assertIsNotNone(result.substitution_reason)
        self.assertIn("08.11.2026", result.substitution_reason)
        self.assertEqual(result.snapshot.rates["EUR"], Decimal("20.0000"))

    def test_missing_currency_triggers_previous_day_search(self) -> None:
        """Отсутствие одной из запрошенных валют в ответе также запускает поиск назад (FR-04)."""
        requested = date(2026, 11, 10)

        # XML только с USD на 10.11
        usd_only_xml = """<ValCurs Date="10.11.2026">
            <Valute><CharCode>USD</CharCode><Nominal>1</Nominal><Value>18.00</Value></Valute>
        </ValCurs>"""

        def mock_fetch(target_date: date) -> str:
            if target_date == requested:
                return usd_only_xml
            return self.valid_xml_template.format(date=format_date(target_date))

        service = RatesService(cache=self.cache, fetch_fn=mock_fetch)
        # Запрашиваем EUR, которого нет в usd_only_xml
        result = service.fetch_rates(requested, ["EUR"])

        self.assertEqual(result.actual_date, "09.11.2026")
        self.assertTrue(result.is_fallback_date)

    def test_exhausted_14_days_raises_rates_not_found(self) -> None:
        """Если за 14 дней курсы не найдены, выбрасывается RatesNotFoundError (FR-04)."""
        calls: list[date] = []

        def mock_all_empty(target_date: date) -> str:
            calls.append(target_date)
            return self.empty_xml_template.format(date=format_date(target_date))

        service = RatesService(cache=self.cache, fetch_fn=mock_all_empty)
        with self.assertRaises(RatesNotFoundError):
            service.fetch_rates(date(2026, 11, 10), ["EUR"])

        # 1 запрос на дату + 14 предыдущих = 15 запросов
        self.assertEqual(len(calls), 15)

    def test_corrupted_xml_stops_search_immediately(self) -> None:
        """Повреждённый XML прекращает перебор немедленно на первом запросе (FR-05, analysis.md)."""
        calls: list[date] = []

        def mock_corrupt(target_date: date) -> str:
            calls.append(target_date)
            return "<ValCurs Date='10.11.2026'><Invalid <Tag>"

        service = RatesService(cache=self.cache, fetch_fn=mock_corrupt)
        with self.assertRaises(RatesFormatError):
            service.fetch_rates(date(2026, 11, 10), ["EUR"])

        self.assertEqual(len(calls), 1, "Перебор должен остановиться на первом ошибочном запросе")

    def test_network_error_stops_search_immediately(self) -> None:
        """Ошибка сети прекращает перебор немедленно (FR-05)."""
        calls: list[date] = []

        def mock_network_err(target_date: date) -> str:
            calls.append(target_date)
            raise NetworkError("Сетевой тайм-аут")

        service = RatesService(cache=self.cache, fetch_fn=mock_network_err)
        with self.assertRaises(NetworkError):
            service.fetch_rates(date(2026, 11, 10), ["EUR"])

        self.assertEqual(len(calls), 1)

    def test_http_error_stops_search_immediately(self) -> None:
        """HTTP-ошибка 500 прекращает перебор немедленно (FR-05)."""
        calls: list[date] = []

        def mock_http_err(target_date: date) -> str:
            calls.append(target_date)
            raise HttpError("HTTP 500", status_code=500)

        service = RatesService(cache=self.cache, fetch_fn=mock_http_err)
        with self.assertRaises(HttpError):
            service.fetch_rates(date(2026, 11, 10), ["EUR"])

        self.assertEqual(len(calls), 1)

    def test_cache_save_and_load(self) -> None:
        """Проверяет сохранение снимка в JSON и корректную загрузку (FR-06)."""
        snap = RateSnapshot(
            date="10.11.2026",
            rates={"MDL": Decimal("1"), "EUR": Decimal("20.1234"), "USD": Decimal("18.5678")},
        )
        saved, err = self.cache.save(snap)
        self.assertTrue(saved)
        self.assertIsNone(err)

        loaded = self.cache.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.date, "10.11.2026")
        self.assertEqual(loaded.rates["EUR"], Decimal("20.1234"))
        self.assertEqual(loaded.rates["USD"], Decimal("18.5678"))

    def test_corrupted_cache_returns_none(self) -> None:
        """Повреждённый кэш считается недоступным и возвращает None (FR-06)."""
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_file.open("w", encoding="utf-8") as f:
            f.write("Not valid JSON content!!!")

        self.assertIsNone(self.cache.load())

    def test_network_failure_offers_suitable_cache(self) -> None:
        """При сбое сети сервис прикрепляет подходящий кэш к ошибке (FR-05, US2)."""
        # Сохраняем в кэш снимок за 08.11.2026
        self.cache.save(RateSnapshot(
            date="08.11.2026",
            rates={"MDL": Decimal("1"), "EUR": Decimal("19.8000"), "USD": Decimal("18.0000")},
        ))

        def mock_fail(target_date: date) -> str:
            raise NetworkError("Нет подключения к сети")

        service = RatesService(cache=self.cache, fetch_fn=mock_fail)
        try:
            service.fetch_rates(date(2026, 11, 10), ["EUR", "USD"])
            self.fail("Должно быть выброшено NetworkError")
        except NetworkError as e:
            self.assertIsNotNone(e.suitable_cache)
            self.assertEqual(e.suitable_cache.date, "08.11.2026")

    def test_future_cache_not_offered(self) -> None:
        """Кэш с датой позже запрошенной не считается подходящим (FR-05)."""
        # Кэш за 12.11.2026
        self.cache.save(RateSnapshot(
            date="12.11.2026",
            rates={"MDL": Decimal("1"), "EUR": Decimal("19.8000")},
        ))

        def mock_fail(target_date: date) -> str:
            raise NetworkError("Сбой сети")

        service = RatesService(cache=self.cache, fetch_fn=mock_fail)
        try:
            # Запрашиваем 10.11.2026 (раньше, чем 12.11.2026)
            service.fetch_rates(date(2026, 11, 10), ["EUR"])
            self.fail("Должно быть выброшено NetworkError")
        except NetworkError as e:
            self.assertIsNone(e.suitable_cache, "Кэш позже запрошенной даты не должен предлагаться")

    def test_cache_missing_currency_not_offered(self) -> None:
        """Кэш без одной из требуемых валют не считается подходящим (FR-05)."""
        # В кэше только USD, а запрашивается EUR
        self.cache.save(RateSnapshot(
            date="08.11.2026",
            rates={"MDL": Decimal("1"), "USD": Decimal("18.0000")},
        ))

        def mock_fail(target_date: date) -> str:
            raise NetworkError("Сбой сети")

        service = RatesService(cache=self.cache, fetch_fn=mock_fail)
        try:
            service.fetch_rates(date(2026, 11, 10), ["EUR", "USD"])
            self.fail("Должно быть выброшено NetworkError")
        except NetworkError as e:
            self.assertIsNone(e.suitable_cache, "Кэш без запрошенной валюты EUR не должен предлагаться")

    def test_apply_cache_details(self) -> None:
        """Применение кэша формирует RateResult с явным источником 'Локальный кэш' (FR-05, US2)."""
        cached_snap = RateSnapshot(
            date="05.11.2026",
            rates={"MDL": Decimal("1"), "EUR": Decimal("19.7000")},
        )
        service = RatesService(cache=self.cache)
        res = service.apply_cache(cached_snap, date(2026, 11, 10))

        self.assertEqual(res.source, "Локальный кэш")
        self.assertEqual(res.actual_date, "05.11.2026")
        self.assertEqual(res.requested_date, "10.11.2026")
        self.assertTrue(res.is_fallback_date)
        self.assertIn("05.11.2026", res.substitution_reason)

    def test_network_failure_does_not_corrupt_existing_cache(self) -> None:
        """Невалидный ответ или ошибка сети не портит сохранённый кэш (FR-06)."""
        original_snap = RateSnapshot(
            date="07.11.2026",
            rates={"MDL": Decimal("1"), "EUR": Decimal("20.0000")},
        )
        self.cache.save(original_snap)

        def mock_corrupt_xml(target_date: date) -> str:
            return "broken xml"

        service = RatesService(cache=self.cache, fetch_fn=mock_corrupt_xml)
        with self.assertRaises(RatesFormatError):
            service.fetch_rates(date(2026, 11, 10), ["EUR"])

        # Проверяем, что исходный кэш остался целым
        loaded = self.cache.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.date, "07.11.2026")


if __name__ == "__main__":
    unittest.main()
