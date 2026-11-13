"""
Модуль сетевого взаимодействия и кэширования курсов (app/rates.py).

Отвечает за:
- Загрузку официального XML курсов валют BNM через HTTP GET (FR-03, plan.md).
- Ограничение тайм-аута (3 с) и размера ответа (512 КиБ) (plan.md).
- Поиск курса за предыдущие дни (до 14 календарных дней) при пустом ответе или отсутствии валюты (FR-04).
- Немедленное прекращение поиска при ошибках сети, HTTP или формата данных (FR-05, analysis.md).
- Атомарное сохранение валидных данных в локальный JSON-кэш (FR-06, plan.md).
- Подбор и предложение подходящего кэша при сбоях источника (FR-05, FR-06).
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Set, Tuple

from app.core import (
    CoreError,
    EmptyRatesError,
    RateSnapshot,
    RatesParseError,
    ValidationError,
    format_date,
    parse_date,
    parse_rates_xml,
    validate_currency,
)


DEFAULT_BNM_URL = "https://www.bnm.md/ro/official_exchange_rates"
DEFAULT_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 512 * 1024  # 512 КиБ
MAX_FALLBACK_DAYS = 14            # До 14 предыдущих календарных дней
MAX_TOTAL_REQUESTS = 15          # 1 запрошенная дата + до 14 предыдущих
DEFAULT_CACHE_PATH = Path("data/cache/rates.json")


def write_json_atomically(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class RatesServiceError(CoreError):
    """Базовое исключение сервиса курсов валют."""
    def __init__(self, message: str, suitable_cache: Optional[RateSnapshot] = None):
        super().__init__(message)
        self.suitable_cache = suitable_cache


class NetworkError(RatesServiceError):
    """Ошибка сети, DNS или тайм-аута соединения при обращении к BNM."""
    pass


class HttpError(RatesServiceError):
    """Ошибка протокола HTTP (статусы 4xx, 5xx) от сервера BNM."""
    def __init__(self, message: str, status_code: int = 0, suitable_cache: Optional[RateSnapshot] = None):
        super().__init__(message, suitable_cache=suitable_cache)
        self.status_code = status_code


class RatesFormatError(RatesServiceError):
    """Ошибка формата данных (поврежденный XML, превышение размера 512 КиБ)."""
    pass


class RatesNotFoundError(RatesServiceError):
    """Курсы не найдены за запрошенную дату и все 14 предыдущих календарных дней."""
    pass


class CacheError(RatesServiceError):
    """Ошибка при работе с локальным кэшем."""
    pass


@dataclass(frozen=True)
class RateResult:
    """
    Результат получения набора курсов валют с указанием источника и контекста.
    """
    snapshot: RateSnapshot
    source: str                     # Например, "BNM (официальный)" или "Локальный кэш"
    requested_date: str             # ДД.ММ.ГГГГ
    actual_date: str                # ДД.ММ.ГГГГ (дата из ответа или кэша)
    is_fallback_date: bool          # True, если использована дата ранее запрошенной
    substitution_reason: Optional[str] = None  # Пояснение выбора даты/кэша (FR-04, US2)
    warning: Optional[str] = None   # Предупреждение (например, при сбое записи кэша)


class BNMClient:
    """
    HTTP-клиент для загрузки официального XML курсов валют Национального Банка Молдовы.
    """
    def __init__(
        self,
        base_url: str = DEFAULT_BNM_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ):
        self.base_url = base_url
        self.timeout = timeout
        self.max_bytes = max_bytes

    def fetch_xml(self, target_date: date) -> str:
        """
        Выполняет HTTPS GET запрос к BNM за указанную дату с ограничением размера и времени.
        """
        date_str = format_date(target_date)
        url = f"{self.base_url}?get_xml=1&date={date_str}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "CurrencyConverterApp/1.0 (+https://github.com/)",
                "Accept": "application/xml, text/xml, */*",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                content = response.read(self.max_bytes + 1)
                if len(content) > self.max_bytes:
                    raise RatesFormatError(
                        f"Размер ответа BNM превысил допустимый лимит в {self.max_bytes // 1024} КиБ"
                    )
                charset = response.headers.get_content_charset() or "utf-8"
                return content.decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            raise HttpError(
                f"Сервер BNM вернул HTTP-ошибку {e.code}: {e.reason}",
                status_code=e.code,
            ) from e
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            raise NetworkError(f"Ошибка сетевого соединения с BNM: {e}") from e
        except OSError as e:
            raise NetworkError(f"Системная сетевая ошибка: {e}") from e


class RatesCache:
    """
    Управляет локальным JSON-кэшем курсов валют (FR-06, plan.md).
    - Сохранение выполняется атомарно через временный файл.
    - Ошибки записи не блокируют вычисления.
    - Поврежденный или невалидный кэш считается недоступным.
    """
    def __init__(self, cache_path: Path | str = DEFAULT_CACHE_PATH):
        self.path = Path(cache_path)

    def load(self) -> Optional[RateSnapshot]:
        """
        Загружает последний сохраненный снимок курсов из JSON-файла.
        При отсутствии или повреждении файла возвращает None.
        """
        if not self.path.exists() or not self.path.is_file():
            return None

        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return RateSnapshot.from_dict(data)
        except Exception:
            # Любая ошибка десериализации/чтения означает повреждение кэша
            return None

    def save(self, snapshot: RateSnapshot) -> Tuple[bool, Optional[str]]:
        """
        Атомарно сохраняет снимок курсов в JSON-файл (FR-06).
        Возвращает кортеж (успех, сообщение_об_ошибке).
        """
        try:
            write_json_atomically(self.path, snapshot.to_dict())
            return True, None
        except Exception as e:
            return False, str(e)

    def is_suitable(
        self,
        snapshot: RateSnapshot,
        requested_date: date,
        required_currencies: Iterable[str],
    ) -> bool:
        """
        Проверяет пригодность снимка для ответа на запрос (FR-05):
        - Наличие всех запрошенных валют.
        - Дата снимка не позже запрошенной даты.
        """
        try:
            snap_date = parse_date(snapshot.date, max_date=None)
        except Exception:
            return False

        if snap_date > requested_date:
            return False

        for currency in required_currencies:
            code = currency.strip().upper()
            if code != "MDL" and not snapshot.has_currency(code):
                return False

        return True

    def get_suitable(
        self,
        requested_date: date | str,
        required_currencies: Iterable[str],
    ) -> Optional[RateSnapshot]:
        """
        Возвращает сохраненный снимок, если он удовлетворяет критериям (FR-05).
        """
        req_d = parse_date(requested_date) if isinstance(requested_date, str) else requested_date
        snapshot = self.load()
        if snapshot is None:
            return None

        if self.is_suitable(snapshot, req_d, required_currencies):
            return snapshot
        return None


class RatesService:
    """
    Сервис координации загрузки курсов, поиска за предыдущие дни и кэширования.
    """
    def __init__(
        self,
        client: Optional[BNMClient] = None,
        cache: Optional[RatesCache] = None,
        fetch_fn: Optional[Callable[[date], str]] = None,
    ):
        self.client = client or BNMClient()
        self.cache = cache or RatesCache()
        self._fetch_fn = fetch_fn

    def _get_xml_for_date(self, target_date: date) -> str:
        """Получает XML от клиента или через переопределенную функцию загрузки."""
        if self._fetch_fn is not None:
            return self._fetch_fn(target_date)
        return self.client.fetch_xml(target_date)

    def read_snapshot(self, target_date: date) -> RateSnapshot:
        """Читает один датированный снимок BNM для конвертации или графика."""
        try:
            xml_data = self._get_xml_for_date(target_date)
        except RatesServiceError:
            raise
        except Exception as error:
            raise NetworkError(f"Сбой загрузки: {error}") from error

        try:
            snapshot = parse_rates_xml(xml_data, allow_empty=True)
            parse_date(snapshot.date, max_date=date.max)
            return snapshot
        except EmptyRatesError:
            return RateSnapshot(format_date(target_date), {"MDL": Decimal("1")})
        except (RatesParseError, ValidationError) as error:
            raise RatesFormatError(
                f"Ошибка формата ответа BNM за {format_date(target_date)}: {error}"
            ) from error

    def fetch_rates(
        self,
        requested_date: date | str,
        required_currencies: Iterable[str],
    ) -> RateResult:
        """
        Загружает курсы валют для запрошенной даты с поиском до 14 предыдущих дней (FR-04).

        Поведение:
        - Пустой ответ или отсутствие нужной валюты запускает перебор до 14 дней назад.
        - Ошибка сети, HTTP или формата прекращает перебор немедленно (FR-05).
        - При исключениях прикрепляет подходящий кэш (если доступен) к объекту ошибки.
        - Успешно полученный из сети набор сохраняется в кэш (FR-06).
        """
        req_date = parse_date(requested_date) if isinstance(requested_date, str) else requested_date
        req_currencies: Set[str] = {
            validate_currency(c) for c in required_currencies if c.strip().upper() != "MDL"
        }

        # Если запрошен только MDL (например, MDL -> MDL), возвращаем базовый снимок
        if not req_currencies:
            today_str = format_date(req_date)
            return RateResult(
                snapshot=RateSnapshot(date=today_str, rates={"MDL": Decimal("1")}),
                source="Базовая валюта",
                requested_date=today_str,
                actual_date=today_str,
                is_fallback_date=False,
            )

        suitable_cache = self.cache.get_suitable(req_date, req_currencies)

        for day_offset in range(min(MAX_TOTAL_REQUESTS, req_date.toordinal())):
            current_date = req_date - timedelta(days=day_offset)

            try:
                snapshot = self.read_snapshot(current_date)
            except RatesServiceError as error:
                error.suitable_cache = suitable_cache
                raise

            # Проверяем, содержит ли снимок котировки и все требуемые валюты
            has_all_currencies = not snapshot.is_empty and all(
                snapshot.has_currency(c) for c in req_currencies
            )

            if not has_all_currencies:
                # Пустой ответ или неполный набор валют -> продолжаем поиск вглубь истории
                continue

            # Набор найден! Проверяем фактическую дату курсов
            actual_d = parse_date(snapshot.date, max_date=None)
            if actual_d > current_date:
                continue
            is_fallback = (actual_d < req_date) or (day_offset > 0)
            reason = None
            if is_fallback:
                reason = (
                    f"Курс на {format_date(req_date)} отсутствует в источнике BNM. "
                    f"Применён ближайший доступный курс за {format_date(actual_d)}."
                )

            # Сохраняем успешно полученный набор в кэш (FR-06)
            saved, save_err = self.cache.save(snapshot)
            warning = f"Предупреждение кэша: не удалось сохранить файл ({save_err})" if not saved else None

            return RateResult(
                snapshot=snapshot,
                source="BNM (официальный)",
                requested_date=format_date(req_date),
                actual_date=format_date(actual_d),
                is_fallback_date=is_fallback,
                substitution_reason=reason,
                warning=warning,
            )

        # 14 дней исчерпаны без нахождения котировок
        raise RatesNotFoundError(
            f"Курсы для валют {', '.join(sorted(req_currencies))} не найдены "
            f"за дату {format_date(req_date)} и предшествующие {MAX_FALLBACK_DAYS} календарных дней.",
            suitable_cache=suitable_cache,
        )

    def apply_cache(
        self,
        cached_snapshot: RateSnapshot,
        requested_date: date | str,
    ) -> RateResult:
        """
        Формирует RateResult на основе согласованного пользователем кэша (US2, FR-05).
        Кэш явно помечается как локальный и не выдается за свежий курс.
        """
        req_d = parse_date(requested_date) if isinstance(requested_date, str) else requested_date
        actual_d = parse_date(cached_snapshot.date, max_date=None)
        is_fallback = actual_d < req_d

        reason = (
            f"Применён сохранённый локальный кэш за {format_date(actual_d)} "
            f"(запрошенная дата: {format_date(req_d)})."
        )

        return RateResult(
            snapshot=cached_snapshot,
            source="Локальный кэш",
            requested_date=format_date(req_d),
            actual_date=format_date(actual_d),
            is_fallback_date=is_fallback,
            substitution_reason=reason,
        )
