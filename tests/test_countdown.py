"""Deadline precision and timezone checks; no live clock or network dependency."""

import sys
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grantfinder.countdown import countdown_html


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.attributes = []
        self.visible = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.attributes.append(dict(attrs))

    def handle_data(self, text):
        self.visible.append(text)

    def value(self, attribute):
        return next((attrs[attribute] for attrs in self.attributes if attribute in attrs), None)


class CountdownTests(unittest.TestCase):
    def test_offset_deadline_is_unambiguous_and_preserves_the_instant(self):
        markup = Markup(countdown_html("2026-11-11T23:59:00-08:00"))
        value = markup.value("data-countdown-at")
        self.assertIsNotNone(value)
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
        self.assertIsNotNone(point.tzinfo)
        self.assertEqual(point.astimezone(timezone.utc),
                         datetime(2026, 11, 12, 7, 59, tzinfo=timezone.utc))
        self.assertIsNone(markup.value("data-countdown-date"))

    def test_named_zone_applies_daylight_saving_and_standard_time(self):
        cases = (("2026-07-01T12:00:00", 19), ("2026-12-01T12:00:00", 20))
        for value, hour in cases:
            with self.subTest(value=value):
                markup = Markup(countdown_html(value, timezone="America/Los_Angeles"))
                point = datetime.fromisoformat(markup.value("data-countdown-at").replace("Z", "+00:00"))
                self.assertEqual(point.astimezone(timezone.utc).hour, hour)

    def test_date_only_keeps_original_local_date_without_inventing_a_time(self):
        markup = Markup(countdown_html("2026-11-19", precision="date", timezone="Europe/Paris"))
        self.assertEqual(markup.value("data-countdown-date"), "2026-11-19")
        self.assertEqual(markup.value("data-countdown-zone"), "Europe/Paris")
        self.assertIsNone(markup.value("data-countdown-at"))
        default_zone = Markup(countdown_html("2026-11-19", precision="date"))
        self.assertEqual(default_zone.value("data-countdown-zone"), "Asia/Seoul")

    def test_unspecified_timezone_and_date_mislabelled_as_datetime_do_not_count_down(self):
        for value in ("2026-11-19T13:00:00", "2026-11-19"):
            with self.subTest(value=value):
                markup = Markup(countdown_html(value, fallback="시각 확인 필요"))
                self.assertIsNone(markup.value("data-countdown-at"))
                self.assertIsNone(markup.value("data-countdown-date"))
                self.assertIn("시각 확인 필요", "".join(markup.visible))

    def test_unknown_month_and_relative_schedules_never_become_exact_deadlines(self):
        for value, precision in ((None, "unknown"), ("2027-03", "month"),
                                 ("약 3개월 후", "relative"), ("2027년 봄", "unknown")):
            with self.subTest(value=value, precision=precision):
                markup = Markup(countdown_html(value, precision=precision))
                self.assertIsNone(markup.value("data-countdown-at"))
                self.assertIsNone(markup.value("data-countdown-date"))

    def test_invalid_dates_and_zones_use_the_fallback(self):
        cases = (("2026-02-30", "date", "Asia/Seoul"),
                 ("2026-11-19T25:00:00+09:00", "datetime", None),
                 ("2026-11-19T13:00:00", "datetime", "Not/AZone"),
                 ("2026-11-19", "date", "Not/AZone"))
        for value, precision, zone in cases:
            with self.subTest(value=value, precision=precision, zone=zone):
                markup = Markup(countdown_html(value, precision=precision, timezone=zone, fallback="확인 필요"))
                self.assertIsNone(markup.value("data-countdown-at"))
                self.assertIsNone(markup.value("data-countdown-date"))
                self.assertIn("확인 필요", "".join(markup.visible))

    def test_fallback_is_text_and_cannot_inject_markup(self):
        payload = '<script>alert("x")</script><img src=x onerror=alert(1)>'
        text = countdown_html(None, fallback=payload)
        self.assertNotIn("<script>", text)
        self.assertNotIn("<img ", text)
        self.assertEqual("".join(Markup(text).visible), payload)


if __name__ == "__main__":
    unittest.main()
