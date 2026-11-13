import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event

from app.core import format_date
from app.history import RateHistory
from app.rates import NetworkError, RatesCache, RatesFormatError, RatesService


END = date(2026, 5, 18)


def sample_xml(day, eur="20", usd="18"):
    return f"""
    <ValCurs Date="{format_date(day)}">
      <Valute><CharCode>EUR</CharCode><Nominal>1</Nominal><Value>{eur}</Value></Valute>
      <Valute><CharCode>USD</CharCode><Nominal>1</Nominal><Value>{usd}</Value></Valute>
      <Valute><CharCode>UAH</CharCode><Nominal>10</Nominal><Value>4,20</Value></Valute>
    </ValCurs>
    """


class TestRateHistory(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name)
        self.rates_path = self.path / "rates.json"
        self.history_path = self.path / "history.json"

    def service(self, fetch):
        return RatesService(cache=RatesCache(self.rates_path), fetch_fn=fetch)

    def test_cross_rates_use_the_same_nominal_normalization_as_conversion(self):
        def fetch(day):
            return sample_xml(day, eur="22" if day == END else "20", usd="10")

        history = RateHistory(self.service(fetch).read_snapshot, self.history_path).load(END, 7, Event())
        direct = history.series("EUR", "USD")
        reverse = history.series("USD", "EUR")
        self.assertEqual(len(direct.points), 7)
        self.assertEqual(direct.points[0].value, Decimal("2"))
        self.assertEqual(direct.points[-1].value, Decimal("2.2"))
        self.assertEqual(direct.change, Decimal("10"))
        self.assertEqual(reverse.points[0].value, Decimal("0.5"))
        self.assertEqual(history.series("UAH", "MDL").points[-1].value, Decimal("0.42"))
        self.assertEqual(history.series("RON", "USD").points, ())
        self.assertFalse(self.rates_path.exists())

    def test_repeated_older_publication_creates_one_point_with_the_actual_date(self):
        actual = END - timedelta(days=3)
        service = self.service(lambda day: sample_xml(actual) if day >= actual else " ")
        history = RateHistory(service.read_snapshot, self.history_path).load(END, 7, Event())
        series = history.series("EUR", "MDL")
        self.assertEqual([point.day for point in series.points], [actual])
        self.assertIsNone(series.change)

    def test_saved_observations_survive_restart_and_are_marked_offline(self):
        online = self.service(sample_xml)
        RateHistory(online.read_snapshot, self.history_path).load(END, 7, Event())
        calls = []

        def offline(day):
            calls.append(day)
            raise NetworkError("offline")

        new_end = END + timedelta(days=1)
        resumed = RateHistory(self.service(offline).read_snapshot, self.history_path)
        history = resumed.load(new_end, 7, Event())
        self.assertEqual(calls, [new_end])
        self.assertTrue(history.from_archive)
        self.assertTrue(history.source_unavailable)
        self.assertEqual(len(history.series("EUR", "USD").points), 6)

    def test_invalid_archive_entries_do_not_hide_good_observations(self):
        self.history_path.write_text(json.dumps([
            {"date": format_date(END), "rates": {"EUR": "20"}},
            {"date": "n/a", "rates": {"EUR": "19"}},
            {"date": format_date(END - timedelta(days=1)), "rates": {"EUR": "Infinity"}},
            {"date": "01.01.2000", "rates": []},
        ]), encoding="utf-8")

        def offline(_day):
            raise NetworkError("offline")

        history = RateHistory(self.service(offline).read_snapshot, self.history_path).load(END, 7, Event())
        self.assertEqual([point.day for point in history.series("EUR", "MDL").points], [END])

    def test_year_requests_a_bounded_sample(self):
        calls = []

        def fetch(day):
            calls.append(day)
            return sample_xml(day)

        history = RateHistory(self.service(fetch).read_snapshot, self.history_path).load(END, 365, Event())
        self.assertLessEqual(len(calls), 30)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(min(calls), END - timedelta(days=364))
        self.assertEqual(max(calls), END)
        self.assertEqual(history.start, min(calls))

    def test_cancelled_request_does_not_call_the_source(self):
        cancel = Event()
        cancel.set()

        def unexpected(_day):
            self.fail("History touched the source after cancellation")

        history = RateHistory(self.service(unexpected).read_snapshot, self.history_path).load(END, 30, cancel)
        self.assertEqual(history.snapshots, ())

    def test_history_does_not_replace_the_conversion_cache(self):
        service = self.service(sample_xml)
        service.fetch_rates(END, ("EUR", "USD"))
        RateHistory(service.read_snapshot, self.history_path).load(END - timedelta(days=3), 30, Event())
        self.assertEqual(service.cache.load().date, format_date(END))

    def test_truly_empty_body_falls_back_to_the_previous_day(self):
        earlier = END - timedelta(days=1)
        service = self.service(lambda day: " \n " if day == END else sample_xml(day))
        result = service.fetch_rates(END, ("EUR", "MDL"))
        self.assertTrue(result.is_fallback_date)
        self.assertEqual(result.actual_date, format_date(earlier))

    def test_future_observation_is_skipped_and_bad_date_is_a_format_error(self):
        future = END + timedelta(days=1)
        service = self.service(lambda day: sample_xml(future if day == END else day))
        result = service.fetch_rates(END, ("EUR", "MDL"))
        self.assertEqual(result.actual_date, format_date(END - timedelta(days=1)))
        broken = self.service(lambda day: sample_xml(day).replace(format_date(day), "2026-05-18"))
        with self.assertRaises(RatesFormatError):
            broken.fetch_rates(END, ("EUR", "MDL"))


if __name__ == "__main__":
    unittest.main()
