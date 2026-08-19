import datetime as dt
import unittest

import pandas as pd

from ipo_returns.ipo38 import IpoRow
from ipo_returns.pipeline import (build_stock_rows, collect,
                                  is_bullish_listing_day,
                                  pct_change_from_prev_close)


def make_ohlcv(dates, rows):
    return pd.DataFrame(
        rows, columns=["시가", "고가", "저가", "종가", "거래량"],
        index=pd.to_datetime(dates),
    )


BULL = make_ohlcv(
    ["20240321", "20240322", "20240325", "20240326", "20240327", "20240328"],
    [[15000, 20000, 14000, 18000, 100],
     [18500, 19000, 17100, 17820, 90],     # 전일 종가 18000 대비 종가 -1%
     [17820, 18000, 17000, 17300, 80],
     [17300, 17500, 16800, 17000, 70],
     [17000, 17200, 16500, 16600, 60],
     [16600, 16800, 16000, 16100, 50]],
)

BEAR = make_ohlcv(
    ["20240402", "20240403"],
    [[20000, 21000, 11000, 12000, 100],
     [12000, 12500, 11500, 11800, 90]],
)

INDEX = make_ohlcv(
    ["20240320", "20240321", "20240322", "20240325", "20240326", "20240327"],
    [[2000, 2010, 1990, 2000, 0],
     [2010, 2030, 2000, 2020, 0],          # 전일 종가 2000 -> 종가 +1%
     [2020, 2025, 1990, 1990, 0],
     [1990, 2000, 1970, 1980, 0],
     [1980, 1990, 1960, 1970, 0],
     [1970, 1980, 1950, 1960, 0]],
)


class PctChangeTest(unittest.TestCase):
    def test_pct_change_from_prev_close(self):
        pct = pct_change_from_prev_close(INDEX)
        row = pct.loc[pd.Timestamp("20240321")]
        self.assertAlmostEqual(row["종가"], 1.0)
        self.assertAlmostEqual(row["시가"], 0.5)
        self.assertTrue(pd.isna(pct.iloc[0]["종가"]))  # 첫 행은 기준가 없음

    def test_empty_frame(self):
        self.assertTrue(pct_change_from_prev_close(pd.DataFrame()).empty)


class StockRowTest(unittest.TestCase):
    def setUp(self):
        self.ipo = IpoRow("가나테크", 10000, dt.date(2024, 3, 21))
        self.index_pct = {"KOSPI": pct_change_from_prev_close(INDEX),
                          "KOSDAQ": pct_change_from_prev_close(INDEX)}

    def test_listing_day_is_relative_to_offer_price(self):
        rows = build_stock_rows(self.ipo, "123456", "KOSDAQ", BULL, self.index_pct)
        self.assertEqual(len(rows), 5)                 # D+0 ~ D+4
        d0 = rows[0]
        self.assertEqual(d0["구분"], "D+0")
        self.assertAlmostEqual(d0["시가등락률"], 50.0)   # 15000 / 10000
        self.assertAlmostEqual(d0["고가등락률"], 100.0)
        self.assertAlmostEqual(d0["저가등락률"], 40.0)
        self.assertAlmostEqual(d0["종가등락률"], 80.0)

    def test_next_days_are_relative_to_prev_close(self):
        rows = build_stock_rows(self.ipo, "123456", "KOSDAQ", BULL, self.index_pct)
        d1 = rows[1]
        self.assertEqual(d1["구분"], "D+1")
        self.assertAlmostEqual(d1["종가등락률"], -1.0)   # 17820 / 18000
        self.assertAlmostEqual(d1["시가등락률"], 2.7777777, places=5)

    def test_index_columns_attached_by_date(self):
        rows = build_stock_rows(self.ipo, "123456", "KOSDAQ", BULL, self.index_pct)
        self.assertAlmostEqual(rows[0]["KOSPI_종가"], 1.0)
        self.assertAlmostEqual(rows[0]["KOSDAQ_시가"], 0.5)
        self.assertAlmostEqual(rows[4]["KOSPI_종가"], -0.5076142, places=5)
        # 지수 데이터가 없는 날짜(20240328)는 None
        rows6 = build_stock_rows(self.ipo, "123456", "KOSDAQ", BULL,
                                 self.index_pct, days=6)
        self.assertIsNone(rows6[5]["KOSPI_종가"])

    def test_bullish_detection(self):
        self.assertTrue(is_bullish_listing_day(BULL, dt.date(2024, 3, 21)))
        self.assertFalse(is_bullish_listing_day(BEAR, dt.date(2024, 4, 2)))
        self.assertIsNone(is_bullish_listing_day(BULL, dt.date(2025, 1, 1)))


class CollectTest(unittest.TestCase):
    def test_only_bullish_listings_are_kept(self):
        ipos = [IpoRow("가나테크", 10000, dt.date(2024, 3, 21)),
                IpoRow("다라바이오", 10000, dt.date(2024, 4, 2)),
                IpoRow("이름없음", 5000, dt.date(2024, 4, 10))]
        frames = {"123456": BULL, "222222": BEAR}
        tickers = {"가나테크": ("123456", "KOSDAQ"), "다라바이오": ("222222", "KOSPI")}
        index_pct = {"KOSPI": pct_change_from_prev_close(INDEX),
                     "KOSDAQ": pct_change_from_prev_close(INDEX)}

        main_df, bearish_df, skipped = collect(
            ipos,
            resolve_ticker=lambda name, date: tickers.get(name),
            fetch_ohlcv=lambda t, s, e: frames[t],
            index_pct=index_pct,
            verbose=False,
        )
        self.assertEqual(sorted(main_df["종목명"].unique()), ["가나테크"])
        self.assertEqual(len(main_df), 5)
        self.assertEqual(list(bearish_df["종목명"]), ["다라바이오"])
        self.assertAlmostEqual(bearish_df.iloc[0]["종가등락률"], 20.0)
        self.assertEqual([s.name for s in skipped], ["이름없음"])
        self.assertIn("티커", skipped[0].reason)


if __name__ == "__main__":
    unittest.main()
