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

        result = collect(
            ipos,
            resolve_ticker=lambda name, date: tickers.get(name),
            fetch_ohlcv=lambda t, s, e: frames[t],
            index_pct=index_pct,
            verbose=False,
        )
        main_df, bearish_df, skipped = result.main, result.bearish, result.skipped
        self.assertEqual(sorted(main_df["종목명"].unique()), ["가나테크"])
        self.assertEqual(len(main_df), 5)
        self.assertEqual(list(bearish_df["종목명"]), ["다라바이오"])
        self.assertAlmostEqual(bearish_df.iloc[0]["종가등락률"], 20.0)
        self.assertEqual([s.name for s in skipped], ["이름없음"])
        self.assertIn("티커", skipped[0].reason)


if __name__ == "__main__":
    unittest.main()


# 인스웨이브시스템즈 형태: 상장 후 무상증자로 네이버 수정주가가 1/4 로 축소된 경우
ADJUSTED = make_ohlcv(
    ["20230925", "20230926", "20230927"],
    [[12_050, 13_000, 11_000, 12_500, 100],
     [12_500, 12_800, 12_000, 12_750, 90],
     [12_750, 13_500, 12_600, 13_000, 80]],
)


class AdjustedPriceTest(unittest.TestCase):
    def setUp(self):
        from ipo_returns.pipeline import price_adjustment_factor

        self.factor_fn = price_adjustment_factor
        self.ipo = IpoRow("인스웨이브시스템즈", 24_000, dt.date(2023, 9, 25),
                          first_price=48_200)
        self.index_pct = {"KOSPI": pd.DataFrame(), "KOSDAQ": pd.DataFrame()}

    def test_factor_from_38_first_price(self):
        self.assertAlmostEqual(self.factor_fn(self.ipo, ADJUSTED), 4.0, places=2)

    def test_no_factor_when_prices_already_match(self):
        ipo = IpoRow("보통회사", 10_000, dt.date(2023, 9, 25), first_price=12_050)
        self.assertEqual(self.factor_fn(ipo, ADJUSTED), 1.0)

    def test_listing_day_return_uses_real_price(self):
        factor = self.factor_fn(self.ipo, ADJUSTED)
        rows = build_stock_rows(self.ipo, "450520", "KOSDAQ", ADJUSTED,
                                self.index_pct, days=3, factor=factor)
        # 보정 전이면 (12050/24000-1)= -49.8%, 보정 후에는 공모가의 약 2 배
        self.assertAlmostEqual(rows[0]["시가등락률"], 100.83, places=1)
        self.assertAlmostEqual(rows[0]["종가등락률"],
                               (12_500 * factor / 24_000 - 1) * 100, places=6)

    def test_next_day_returns_are_unaffected_by_factor(self):
        plain = build_stock_rows(self.ipo, "450520", "KOSDAQ", ADJUSTED,
                                 self.index_pct, days=3)
        fixed = build_stock_rows(self.ipo, "450520", "KOSDAQ", ADJUSTED,
                                 self.index_pct, days=3, factor=4.0)
        self.assertAlmostEqual(plain[1]["종가등락률"], fixed[1]["종가등락률"])


class TransferListingTest(unittest.TestCase):
    """코넥스 -> 코스닥 이전상장처럼 상장일 이전 시세가 있는 종목."""

    FRAME = make_ohlcv(
        ["20230620", "20230621", "20230629", "20230630", "20230703"],
        [[5_000, 5_100, 4_900, 5_000, 10],      # 이전 시장에서의 거래
         [5_000, 5_050, 4_950, 5_000, 10],
         [5_800, 7_000, 5_700, 6_500, 100],     # 상장일
         [6_500, 6_800, 6_200, 6_300, 90],
         [6_300, 6_400, 6_000, 6_100, 80]],
    )

    def _collect(self, ipo, **kwargs):
        return collect([ipo],
                       resolve_ticker=lambda n, d: ("232830", "KOSDAQ"),
                       fetch_ohlcv=lambda t, s, e: self.FRAME,
                       index_pct={"KOSPI": pd.DataFrame(), "KOSDAQ": pd.DataFrame()},
                       verbose=False, **kwargs)

    def test_included_when_first_price_confirms_ticker(self):
        ipo = IpoRow("시큐센", 3_000, dt.date(2023, 6, 29), first_price=5_800)
        result = self._collect(ipo)
        self.assertEqual(len(result.main), 3)
        self.assertEqual(list(result.transfers["종목명"]), ["시큐센"])
        self.assertEqual(result.skipped, [])

    def test_excluded_when_first_price_disagrees(self):
        # 38 시초가와 조회된 시가가 전혀 다르면 티커 오매칭으로 본다
        ipo = IpoRow("엉뚱한종목", 3_000, dt.date(2023, 6, 29), first_price=30_000)
        result = self._collect(ipo)
        self.assertTrue(result.main.empty)
        self.assertIn("오매칭", result.skipped[0].reason)

    def test_opt_out(self):
        ipo = IpoRow("시큐센", 3_000, dt.date(2023, 6, 29), first_price=5_800)
        result = self._collect(ipo, include_transfer_listing=False)
        self.assertTrue(result.main.empty)
        self.assertIn("이전상장", result.skipped[0].reason)
