import datetime as dt
import os
import unittest

from ipo_returns import ipo38

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "38_new_listing_sample.html")


class Parse38Test(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE, encoding="utf-8") as fp:
            self.html = fp.read()
        self.rows = ipo38.parse_listing_table(self.html)

    def test_parses_rows_with_offer_price(self):
        names = [r.name for r in self.rows]
        self.assertEqual(names, ["가나테크", "다라바이오", "마바스팩7호"])

    def test_offer_price_and_dates(self):
        row = self.rows[0]
        self.assertEqual(row.offer_price, 10000)
        self.assertEqual(row.listing_date, dt.date(2024, 3, 21))
        self.assertEqual(row.first_price, 18000)
        self.assertEqual(self.rows[1].offer_price, 12000)          # '12,000 원'
        self.assertEqual(self.rows[1].listing_date, dt.date(2024, 2, 5))
        self.assertEqual(self.rows[2].listing_date, dt.date(2022, 12, 28))

    def test_rows_without_offer_price_are_dropped(self):
        self.assertNotIn("공모미정기업", [r.name for r in self.rows])

    def test_collect_filters_by_listing_date(self):
        rows = ipo38.collect_ipos(dt.date(2023, 1, 1), dt.date(2026, 12, 31),
                                  html_dir=os.path.dirname(FIXTURE), verbose=False)
        self.assertEqual([r.name for r in rows], ["다라바이오", "가나테크"])

    def test_date_and_int_helpers(self):
        self.assertEqual(ipo38._parse_int("1,234,500원"), 1234500)
        self.assertIsNone(ipo38._parse_int("-"))
        self.assertEqual(ipo38._parse_date("20250103"), dt.date(2025, 1, 3))
        self.assertIsNone(ipo38._parse_date("미정"))


if __name__ == "__main__":
    unittest.main()
