"""
Модуль ядра приложения (app/core.py).

Отвечает за:
- Валидацию пользовательского ввода: сумма (FR-02), дата (FR-02), валюта (FR-01).
- Разбор официального XML Национального Банка Молдовы (BNM) с учетом номинала (FR-03).
- Точный расчет конвертации валют в Decimal с округлением до 2 знаков HALF_UP (FR-03, SC-01).
- Обработку сценария одинаковых валют без обращения к курсам (US3, FR-03).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple
import xml.etree.ElementTree as ET


# Поддерживаемые валюты приложения (FR-01)
SUPPORTED_CURRENCIES: Tuple[str, ...] = ("MDL", "EUR", "USD", "RON", "UAH", "GBP")

# Ограничения на ввод суммы (FR-02): до 12 цифр в целой и до 6 в дробной части
MAX_INTEGER_DIGITS = 12
MAX_FRACTIONAL_DIGITS = 6

# Регулярные выражения для валидации
AMOUNT_PATTERN = re.compile(r"^(\d+)(?:[.,](\d+))?$")
DATE_PATTERN = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


class CoreError(Exception):
    """Базовое исключение для ошибок ядра."""
    pass


class ValidationError(CoreError):
    """Ошибка валидации пользовательского ввода (сумма, дата, валюта)."""
    pass


class RatesParseError(CoreError):
    """Ошибка синтаксиса или структуры XML курсов валют."""
    pass


class EmptyRatesError(RatesParseError):
    """Ответ от BNM пуст или не содержит котировок валют."""
    pass


@dataclass(frozen=True)
class RateSnapshot:
    """
    Снимок курсов валют на определенную дату.
    Курс каждой валюты нормализован к MDL (стоимость 1 единицы валюты в MDL).
    Курс MDL всегда равен 1.
    """
    date: str  # В формате ДД.ММ.ГГГГ
    rates: Dict[str, Decimal]

    @property
    def is_empty(self) -> bool:
        """Возвращает True, если кроме базового MDL котировки отсутствуют."""
        return len([c for c in self.rates if c != "MDL"]) == 0

    def has_currency(self, currency: str) -> bool:
        """Проверяет наличие курса для указанной валюты."""
        return currency.strip().upper() in self.rates

    def get_rate(self, currency: str) -> Decimal:
        """Возвращает нормализованный курс валюты к MDL."""
        code = currency.strip().upper()
        if code not in self.rates:
            raise KeyError(f"Курс для валюты '{code}' отсутствует в данных за {self.date}")
        return self.rates[code]

    def to_dict(self) -> Dict[str, Any]:
        """Сериализует снимок в словарь для JSON-кэша со строковыми Decimal."""
        return {
            "date": self.date,
            "rates": {code: str(rate) for code, rate in self.rates.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RateSnapshot:
        """Восстанавливает снимок из словаря кэша."""
        if not isinstance(data, dict):
            raise RatesParseError("Данные снимка должны быть объектом (словарем)")
        if "date" not in data or "rates" not in data:
            raise RatesParseError("В данных снимка отсутствуют обязательные поля 'date' или 'rates'")
        raw_rates = data.get("rates", {})
        if not isinstance(raw_rates, dict):
            raise RatesParseError("Поле 'rates' должно быть словарем котировок")

        rates = {str(k).upper(): Decimal(str(v)) for k, v in raw_rates.items()}
        if any(not rate.is_finite() or rate <= 0 for rate in rates.values()):
            raise RatesParseError("Кэш содержит недопустимую котировку")
        rates["MDL"] = Decimal("1")
        return cls(date=str(data["date"]).strip(), rates=rates)


@dataclass(frozen=True)
class ConversionResult:
    """Результат конвертации с подробным контекстом (для UI и логики)."""
    original_amount: Decimal
    from_currency: str
    to_currency: str
    result_amount: Decimal
    rate_from: Decimal
    rate_to: Decimal
    is_same_currency: bool
    explanation: str


def parse_amount(raw_amount: str | int | Decimal) -> Decimal:
    """
    Проверяет и преобразует введенную сумму в Decimal согласно FR-02:
    - Допустима положительная десятичная сумма с точкой или запятой и пробелами по краям.
    - До 12 цифр в целой и до 6 в дробной части.
    - Пустые, нулевые, отрицательные, буквенные и специальные значения отклоняются.
    """
    if isinstance(raw_amount, (int, Decimal)):
        raw_str = str(raw_amount)
    elif isinstance(raw_amount, str):
        raw_str = raw_amount
    else:
        raise ValidationError("Сумма должна быть строкой или числовым значением")

    cleaned = raw_str.strip()
    if not cleaned:
        raise ValidationError("Поле суммы не может быть пустым")

    if cleaned.startswith("-"):
        raise ValidationError("Сумма должна быть положительной (отрицательные значения недопустимы)")
    if cleaned.startswith("+"):
        raise ValidationError("Недопустимый знак '+' в сумме")

    if " " in cleaned:
        raise ValidationError("Сумма не должна содержать пробелов внутри числа")

    match = AMOUNT_PATTERN.match(cleaned)
    if not match:
        raise ValidationError(
            f"Некорректный формат суммы: '{cleaned}'. "
            "Используйте положительное число с точкой или запятой (например, 100 или 100.50)"
        )

    int_part, frac_part = match.groups()

    if len(int_part) > MAX_INTEGER_DIGITS:
        raise ValidationError(
            f"Целая часть суммы содержит {len(int_part)} цифр (допустимо не более {MAX_INTEGER_DIGITS})"
        )

    if frac_part is not None and len(frac_part) > MAX_FRACTIONAL_DIGITS:
        raise ValidationError(
            f"Дробная часть суммы содержит {len(frac_part)} знаков (допустимо не более {MAX_FRACTIONAL_DIGITS})"
        )

    normalized_str = f"{int_part}.{frac_part}" if frac_part is not None else int_part

    try:
        val = Decimal(normalized_str)
    except InvalidOperation as e:
        raise ValidationError(f"Не удалось преобразовать значение '{cleaned}' в число") from e

    if val <= Decimal("0"):
        raise ValidationError("Сумма должна быть строго больше нуля")

    return val


def parse_date(raw_date: str, max_date: Optional[date] = None) -> date:
    """
    Проверяет дату в формате ДД.ММ.ГГГГ согласно FR-01, FR-02:
    - Дата должна соответствовать формату ДД.ММ.ГГГГ.
    - Несуществующие даты (например, 31.02.2024) отклоняются.
    - Будущие даты (относительно max_date или локального date.today()) отклоняются.
    """
    if not isinstance(raw_date, str):
        raise ValidationError("Дата должна быть строкой")

    cleaned = raw_date.strip()
    if not cleaned:
        raise ValidationError("Поле даты не может быть пустым")

    if not DATE_PATTERN.match(cleaned):
        raise ValidationError(
            f"Неверный формат даты: '{cleaned}'. "
            "Используйте формат ДД.ММ.ГГГГ (например, 10.11.2026)"
        )

    try:
        parsed = datetime.strptime(cleaned, "%d.%m.%Y").date()
    except ValueError:
        raise ValidationError(f"Указана несуществующая календарная дата: '{cleaned}'")

    current_date = max_date if max_date is not None else date.today()
    if parsed > current_date:
        today_str = format_date(current_date)
        raise ValidationError(
            f"Дата '{cleaned}' не может быть в будущем (текущая дата: {today_str})"
        )

    return parsed


def format_date(d: date) -> str:
    """Форматирует объект date в строку ДД.ММ.ГГГГ."""
    return d.strftime("%d.%m.%Y")


def validate_currency(currency: str) -> str:
    """
    Проверяет и нормализует код валюты согласно FR-01.
    """
    if not isinstance(currency, str):
        raise ValidationError("Код валюты должен быть строкой")

    code = currency.strip().upper()
    if code not in SUPPORTED_CURRENCIES:
        allowed = ", ".join(SUPPORTED_CURRENCIES)
        raise ValidationError(f"Неподдерживаемая валюта: '{currency}'. Допустимы: {allowed}")
    return code


def parse_rates_xml(xml_content: str | bytes, allow_empty: bool = True) -> RateSnapshot:
    """
    Разбирает официальный XML BNM курсов валют согласно FR-03:
    - Извлекает дату из атрибута Date корневого тега ValCurs.
    - Извлекает CharCode, Nominal, Value из каждого тега Valute.
    - Нормализует курс как Value / Nominal (стоимость 1 единицы в MDL).
    - Базовая валюта MDL автоматически добавляется с курсом 1.
    - Если ответ пуст или поврежден, возбуждает соответствующее исключение.
    """
    if isinstance(xml_content, str):
        if not xml_content.strip():
            raise EmptyRatesError("Получен пустой ответ от BNM")
        data = xml_content.encode("utf-8")
    elif isinstance(xml_content, bytes):
        if not xml_content.strip():
            raise EmptyRatesError("Получен пустой ответ от BNM")
        data = xml_content
    else:
        raise RatesParseError("Содержимое XML должно быть строкой или байтами")

    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise RatesParseError(f"Ошибка синтаксического анализа XML: {e}") from e

    # Игнорируем XML-пространства имен при проверке корневого тега
    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if tag != "ValCurs":
        raise RatesParseError(f"Неверный корневой тег XML: '{root.tag}', ожидался 'ValCurs'")

    date_str = root.attrib.get("Date") or root.attrib.get("date")
    if not date_str:
        raise RatesParseError("В элементе ValCurs отсутствует обязательный атрибут Date")

    date_str = date_str.strip()

    rates: Dict[str, Decimal] = {"MDL": Decimal("1")}

    valute_nodes = root.findall(".//Valute") if "}" in root.tag else root.findall("Valute")

    if not valute_nodes and not allow_empty:
        raise EmptyRatesError(f"В ответе за {date_str} отсутствуют котировки валют")

    for node in valute_nodes:
        char_code_node = node.find("CharCode")
        nominal_node = node.find("Nominal")
        value_node = node.find("Value")

        if char_code_node is None or nominal_node is None or value_node is None:
            raise RatesParseError("В элементе Valute отсутствуют обязательные теги CharCode, Nominal или Value")

        char_code = (char_code_node.text or "").strip().upper()
        if not char_code:
            raise RatesParseError("Пустой тег CharCode в элементе Valute")

        nominal_str = (nominal_node.text or "").strip()
        try:
            nominal = Decimal(nominal_str)
            if not nominal.is_finite() or nominal <= 0:
                raise ValueError("Номинал должен быть положительным числом")
        except Exception as e:
            raise RatesParseError(f"Некорректный Nominal '{nominal_str}' для валюты {char_code}: {e}") from e

        value_str = (value_node.text or "").strip().replace(",", ".")
        try:
            value = Decimal(value_str)
            if not value.is_finite() or value <= 0:
                raise ValueError("Курс должен быть положительным числом")
        except Exception as e:
            raise RatesParseError(f"Некорректный Value '{value_str}' для валюты {char_code}: {e}") from e

        # Нормализованная стоимость 1 единицы валюты в MDL
        rates[char_code] = value / nominal

    return RateSnapshot(date=date_str, rates=rates)


def round_amount(amount: Decimal) -> Decimal:
    """
    Округляет Decimal до 2 знаков после запятой по правилу половины вверх (ROUND_HALF_UP, FR-03).
    """
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def convert(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    rates: Dict[str, Decimal],
    round_result: bool = True,
) -> Decimal:
    """
    Выполняет конвертацию суммы из from_currency в to_currency (FR-03).
    Формула: сумма * курс_исходной / курс_целевой (курс нормализован к MDL).
    При совпадении валют внешний курс не применяется (FR-03, US3).
    """
    from_curr = from_currency.strip().upper()
    to_curr = to_currency.strip().upper()

    if from_curr == to_curr:
        return round_amount(amount) if round_result else amount

    if from_curr not in rates:
        raise KeyError(f"Отсутствует курс для исходной валюты: {from_curr}")
    if to_curr not in rates:
        raise KeyError(f"Отсутствует курс для целевой валюты: {to_curr}")

    rate_from = rates[from_curr]
    rate_to = rates[to_curr]

    if rate_to <= Decimal("0"):
        raise ValueError(f"Курс целевой валюты {to_curr} должен быть больше нуля")

    converted = (amount * rate_from) / rate_to

    return round_amount(converted) if round_result else converted


def convert_with_details(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    rates: Optional[Dict[str, Decimal]] = None,
    round_result: bool = True,
) -> ConversionResult:
    """
    Выполняет конвертацию и возвращает расширенный объект ConversionResult (US1, US3, FR-03).
    Для одинаковых валют передача таблицы rates не требуется.
    """
    from_curr = from_currency.strip().upper()
    to_curr = to_currency.strip().upper()

    if from_curr == to_curr:
        result = round_amount(amount) if round_result else amount
        return ConversionResult(
            original_amount=amount,
            from_currency=from_curr,
            to_currency=to_curr,
            result_amount=result,
            rate_from=Decimal("1"),
            rate_to=Decimal("1"),
            is_same_currency=True,
            explanation="Исходная и целевая валюты совпадают: внешний курс не применялся, возвращена исходная сумма.",
        )

    if rates is None:
        raise ValueError("Таблица курсов (rates) обязательна при конвертации разных валют")

    result = convert(amount, from_curr, to_curr, rates, round_result=round_result)
    rate_from = rates[from_curr]
    rate_to = rates[to_curr]

    return ConversionResult(
        original_amount=amount,
        from_currency=from_curr,
        to_currency=to_curr,
        result_amount=result,
        rate_from=rate_from,
        rate_to=rate_to,
        is_same_currency=False,
        explanation=f"Конвертация по курсу {from_curr} ({rate_from}) к {to_curr} ({rate_to})",
    )
