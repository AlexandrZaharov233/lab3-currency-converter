"""
Unit-тесты логики ядра приложения (tests/test_core.py).

Покрывает задачи T006 и T007:
- T006 [US1]: Проверка разбора XML BNM, номинала, прямого и обратного направлений расчёта (FR-03, SC-01).
- T007 [US3]: Проверка неверных сумм, дат и сценария одинаковых валют (FR-02, FR-03).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest

from app.core import (
    SUPPORTED_CURRENCIES,
    ConversionResult,
    EmptyRatesError,
    RateSnapshot,
    RatesParseError,
    ValidationError,
    convert,
    convert_with_details,
    format_date,
    parse_amount,
    parse_date,
    parse_rates_xml,
    round_amount,
    validate_currency,
)


class TestCoreXMLAndConversion(unittest.TestCase):
    """Тесты разбора XML, номинала и конвертации (T006, FR-03, SC-01)."""

    def setUp(self) -> None:
        self.sample_xml = """<?xml version="1.0" encoding="utf-8"?>
<ValCurs Date="10.11.2026" name="Official exchange rates">
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
        <Name>Dolar S.U.A.</Name>
        <Value>18,0000</Value>
    </Valute>
    <Valute ID="52">
        <NumCode>348</NumCode>
        <CharCode>HUF</CharCode>
        <Nominal>100</Nominal>
        <Name>Forint</Name>
        <Value>4.8123</Value>
    </Valute>
</ValCurs>"""
        self.snapshot = parse_rates_xml(self.sample_xml)

    def test_parse_xml_attributes_and_currencies(self) -> None:
        """Проверяет корректное извлечение даты и котировок из XML."""
        self.assertEqual(self.snapshot.date, "10.11.2026")
        self.assertEqual(self.snapshot.rates["MDL"], Decimal("1"))
        self.assertEqual(self.snapshot.rates["EUR"], Decimal("20.0000"))
        self.assertEqual(self.snapshot.rates["USD"], Decimal("18.0000"))

    def test_nominal_division(self) -> None:
        """Проверяет нормализацию курса с учётом номинала (Nominal = 100)."""
        # 4.8123 / 100 = 0.048123 MDL за 1 HUF
        expected_huf_rate = Decimal("4.8123") / Decimal("100")
        self.assertEqual(self.snapshot.rates["HUF"], expected_huf_rate)

    def test_sc01_fixed_rates_conversion(self) -> None:
        """
        SC-01: На фиксированных курсах 20 MDL/EUR и 18 MDL/USD:
        100 EUR -> USD = 111.11 USD
        100 USD -> EUR = 90.00 EUR
        """
        # 100 EUR -> USD
        res_eur_usd = convert(Decimal("100"), "EUR", "USD", self.snapshot.rates)
        self.assertEqual(res_eur_usd, Decimal("111.11"))

        # 100 USD -> EUR (обратное направление)
        res_usd_eur = convert(Decimal("100"), "USD", "EUR", self.snapshot.rates)
        self.assertEqual(res_usd_eur, Decimal("90.00"))

    def test_independent_direct_and_inverse_calculations(self) -> None:
        """Проверяет независимость прямого и обратного расчёта относительно MDL."""
        # 100 EUR -> MDL: 100 * 20 = 2000.00 MDL
        res_to_mdl = convert(Decimal("100"), "EUR", "MDL", self.snapshot.rates)
        self.assertEqual(res_to_mdl, Decimal("2000.00"))

        # 2000 MDL -> EUR: 2000 / 20 = 100.00 EUR
        res_from_mdl = convert(Decimal("2000"), "MDL", "EUR", self.snapshot.rates)
        self.assertEqual(res_from_mdl, Decimal("100.00"))

    def test_conversion_with_nominal_currency(self) -> None:
        """Проверяет конвертацию валюты с номиналом больше единицы."""
        # 1000 HUF по курсу 0.048123 MDL -> 48.123 MDL, округление HALF_UP = 48.12 MDL
        res = convert(Decimal("1000"), "HUF", "MDL", self.snapshot.rates)
        self.assertEqual(res, Decimal("48.12"))

    def test_round_amount_half_up(self) -> None:
        """Проверяет округление до 2 знаков по правилу половины вверх (ROUND_HALF_UP)."""
        self.assertEqual(round_amount(Decimal("111.115")), Decimal("111.12"))
        self.assertEqual(round_amount(Decimal("111.114")), Decimal("111.11"))
        self.assertEqual(round_amount(Decimal("90.005")), Decimal("90.01"))
        self.assertEqual(round_amount(Decimal("90.004")), Decimal("90.00"))

    def test_missing_currency_raises_key_error(self) -> None:
        """Проверяет вызов KeyError при отсутствии курса валюты."""
        with self.assertRaises(KeyError):
            convert(Decimal("100"), "GBP", "USD", self.snapshot.rates)
        with self.assertRaises(KeyError):
            convert(Decimal("100"), "EUR", "GBP", self.snapshot.rates)

    def test_convert_with_details_structure(self) -> None:
        """Проверяет корректность заполнения объекта ConversionResult."""
        details = convert_with_details(Decimal("100"), "EUR", "USD", self.snapshot.rates)
        self.assertIsInstance(details, ConversionResult)
        self.assertEqual(details.original_amount, Decimal("100"))
        self.assertEqual(details.from_currency, "EUR")
        self.assertEqual(details.to_currency, "USD")
        self.assertEqual(details.result_amount, Decimal("111.11"))
        self.assertEqual(details.rate_from, Decimal("20.0000"))
        self.assertEqual(details.rate_to, Decimal("18.0000"))
        self.assertFalse(details.is_same_currency)


class TestCoreValidationAndSameCurrency(unittest.TestCase):
    """Тесты валидации ввода и сценария одинаковых валют (T007, FR-02, FR-03, US3)."""

    def test_parse_amount_valid_cases(self) -> None:
        """Проверяет валидные форматы сумм (FR-02)."""
        self.assertEqual(parse_amount("100"), Decimal("100"))
        self.assertEqual(parse_amount("100.50"), Decimal("100.50"))
        self.assertEqual(parse_amount("100,50"), Decimal("100.50"))
        self.assertEqual(parse_amount("  100.50  "), Decimal("100.50"))
        self.assertEqual(parse_amount("0.5"), Decimal("0.5"))
        self.assertEqual(parse_amount("0,000001"), Decimal("0.000001"))
        # 12 цифр целой части и 6 дробной
        self.assertEqual(
            parse_amount("123456789012.123456"),
            Decimal("123456789012.123456"),
        )
        # Числовые типы
        self.assertEqual(parse_amount(100), Decimal("100"))
        self.assertEqual(parse_amount(Decimal("50.25")), Decimal("50.25"))

    def test_parse_amount_invalid_cases(self) -> None:
        """Проверяет отклонение некорректных значений сумм (FR-02)."""
        invalid_values = [
            "",               # пустая строка
            "   ",            # только пробелы
            "0",              # ноль
            "0.0",            # ноль с точкой
            "0,000000",       # ноль с запятой
            "-10",            # отрицательное целое
            "-0.01",          # отрицательное дробное
            "+100",           # знак плюс
            "abc",            # буквы
            "100 USD",        # буквы после цифр
            "$100",           # спецсимволы
            "10 000",         # пробел внутри
            "1.2.3",          # несколько точек
            "1,2,3",          # несколько запятых
            ".5",             # отсутствует целая часть
            "5.",             # отсутствует дробная часть
            "1234567890123",  # 13 цифр в целой части (> 12)
            "1.1234567",      # 7 знаков в дробной части (> 6)
        ]
        for val in invalid_values:
            with self.subTest(val=val):
                with self.assertRaises(ValidationError):
                    parse_amount(val)

    def test_parse_date_valid_cases(self) -> None:
        """Проверяет валидные календарные даты в формате ДД.ММ.ГГГГ (FR-01, FR-02)."""
        ref_today = date(2026, 11, 10)
        self.assertEqual(parse_date("10.11.2026", max_date=ref_today), ref_today)
        self.assertEqual(parse_date("01.01.2020", max_date=ref_today), date(2020, 1, 1))
        # Високосный год
        self.assertEqual(parse_date("29.02.2024", max_date=ref_today), date(2024, 2, 29))
        self.assertEqual(format_date(ref_today), "10.11.2026")

    def test_parse_date_invalid_cases(self) -> None:
        """Проверяет отклонение несуществующих и будущих дат (FR-02)."""
        ref_today = date(2026, 11, 10)
        invalid_dates = [
            "",            # пустая
            "11.11.2026",  # будущая (завтра)
            "01.01.2099",  # далёкое будущее
            "31.02.2024",  # несуществующая (31 февраля)
            "29.02.2023",  # невисокосный год
            "32.01.2024",  # день 32
            "15.13.2024",  # месяц 13
            "2026-11-10",  # неверный разделитель
            "10/11/2026",  # слэш
            "1.1.2024",    # без ведущих нулей
        ]
        for d_str in invalid_dates:
            with self.subTest(d_str=d_str):
                with self.assertRaises(ValidationError):
                    parse_date(d_str, max_date=ref_today)

    def test_validate_currency(self) -> None:
        """Проверяет валидацию кодов валют (FR-01)."""
        for code in SUPPORTED_CURRENCIES:
            self.assertEqual(validate_currency(code), code)
        self.assertEqual(validate_currency("  eur  "), "EUR")

        with self.assertRaises(ValidationError):
            validate_currency("BTC")
        with self.assertRaises(ValidationError):
            validate_currency("")

    def test_same_currency_scenario(self) -> None:
        """Проверяет конвертацию одинаковых валют без внешних курсов (US3, FR-03)."""
        # convert возвращает исходную сумму
        res = convert(Decimal("150.75"), "EUR", "EUR", rates={})
        self.assertEqual(res, Decimal("150.75"))

        # convert_with_details не требует передачи rates
        details = convert_with_details(Decimal("150.75"), "EUR", "EUR", rates=None)
        self.assertTrue(details.is_same_currency)
        self.assertEqual(details.result_amount, Decimal("150.75"))
        self.assertIn("совпадают", details.explanation)
        self.assertIn("не применялся", details.explanation)


if __name__ == "__main__":
    unittest.main()
