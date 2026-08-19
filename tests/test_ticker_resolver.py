import datetime as dt
import unittest

from ipo_returns.ipo38 import IpoRow
from ipo_returns.krxdata import Listing, TickerResolver, normalize_name, spac_key


def offline_resolver(listings, **kwargs):
    resolver = TickerResolver(use_kind=False, use_krx_by_date=False,
                              verbose=False, **kwargs)
    resolver._built = True
    for listing in listings:
        resolver.add(listing)
    return resolver


class NormalizeTest(unittest.TestCase):
    def test_parenthetical_notes_are_stripped(self):
        # 38 은 사명 변경·시장 구분을 괄호로 덧붙인다
        self.assertEqual(
            normalize_name("HD현대마린솔루션(구.HD현대글로벌서비스)(유가)"),
            normalize_name("HD현대마린솔루션"))
        self.assertEqual(normalize_name("퓨릿(구.신디프)"), normalize_name("퓨릿"))
        self.assertEqual(normalize_name("디에스단석(구,단석산업)"),
                         normalize_name("디에스단석"))
        self.assertEqual(normalize_name("에이피알(유가)"), "에이피알")

    def test_spac_key_bridges_notation(self):
        self.assertEqual(spac_key("KB스팩28호"), spac_key("케이비제28호기업인수목적"))
        self.assertEqual(spac_key("SK증권스팩12호"),
                         spac_key("에스케이증권제12호기업인수목적"))
        self.assertEqual(spac_key("하나스팩29호"), spac_key("하나금융제29호스팩"))
        self.assertEqual(spac_key("삼성전자"), "")   # 스팩이 아니면 빈 키


class AssignTest(unittest.TestCase):
    def test_same_day_names_are_paired(self):
        listings = [
            Listing("456700", "HD현대마린솔루션", "KOSPI", dt.date(2024, 5, 8)),
            Listing("475580", "코칩", "KOSDAQ", dt.date(2024, 5, 7)),
            Listing("475960", "에스케이증권제12호기업인수목적", "KOSDAQ",
                    dt.date(2024, 5, 7)),
        ]
        rows = [
            IpoRow("HD현대마린솔루션(구.HD현대글로벌서비스)(유가)", 83_400,
                   dt.date(2024, 5, 8)),
            IpoRow("코칩", 12_000, dt.date(2024, 5, 7)),
            IpoRow("SK증권스팩12호", 2_000, dt.date(2024, 5, 7)),
        ]
        resolver = offline_resolver(listings)
        resolver.assign(rows)
        self.assertEqual(resolver.resolve(rows[0].name, rows[0].listing_date),
                         ("456700", "KOSPI"))
        self.assertEqual(resolver.resolve("코칩", dt.date(2024, 5, 7)),
                         ("475580", "KOSDAQ"))
        self.assertEqual(resolver.resolve("SK증권스팩12호", dt.date(2024, 5, 7)),
                         ("475960", "KOSDAQ"))

    def test_renamed_survivor_is_matched_when_one_to_one(self):
        # 스팩이 합병 후 사명을 바꿔도 상장일과 티커는 그대로 남는다
        listings = [Listing("442770", "어떤합병법인", "KOSDAQ", dt.date(2023, 6, 28))]
        rows = [IpoRow("하나스팩29호", 2_000, dt.date(2023, 6, 28))]
        resolver = offline_resolver(listings)
        resolver.assign(rows)
        found = resolver.resolve_detail("하나스팩29호", dt.date(2023, 6, 28))
        self.assertEqual(found.ticker, "442770")
        self.assertEqual(found.method, "date-unique")

    def test_ambiguous_day_does_not_guess(self):
        # 같은 날 이름이 전혀 다른 후보가 둘이면 임의 배정하지 않는다
        listings = [Listing("111111", "가나", "KOSDAQ", dt.date(2024, 1, 10)),
                    Listing("222222", "다라", "KOSDAQ", dt.date(2024, 1, 10))]
        rows = [IpoRow("마바", 1000, dt.date(2024, 1, 10)),
                IpoRow("사아", 1000, dt.date(2024, 1, 10))]
        resolver = offline_resolver(listings)
        resolver.assign(rows)
        self.assertIsNone(resolver.resolve("마바", dt.date(2024, 1, 10)))

    def test_name_match_when_listing_date_differs(self):
        listings = [Listing("333333", "우진엔텍", "KOSDAQ", dt.date(2024, 1, 24))]
        resolver = offline_resolver(listings)
        resolver.assign([IpoRow("우진엔텍", 5_300, dt.date(2024, 1, 25))])
        self.assertEqual(resolver.resolve("우진엔텍", dt.date(2024, 1, 25)),
                         ("333333", "KOSDAQ"))

    def test_manual_map_wins(self):
        import csv
        import tempfile
        import os
        path = os.path.join(tempfile.mkdtemp(), "map.csv")
        with open(path, "w", encoding="utf-8", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(["종목명", "종목코드", "시장"])
            writer.writerow(["없는회사", "999999", "KOSDAQ"])
        resolver = offline_resolver([], ticker_map_csv=path)
        self.assertEqual(resolver.resolve("없는회사", dt.date(2024, 1, 1)),
                         ("999999", "KOSDAQ"))


if __name__ == "__main__":
    unittest.main()


class NaverIndexParseTest(unittest.TestCase):
    SAMPLE = """[['날짜', '시가', '고가', '저가', '종가', '거래량', '외국인소진율'],
["20240102", 2669.81, 2676.19, 2645.61, 2669.81, 471244, 0.00],
["20240103", 2660.51, 2660.51, 2601.99, 2607.31, 522910, 0.00]]"""

    def test_parse(self):
        from ipo_returns.krxdata import parse_naver_index

        df = parse_naver_index(self.SAMPLE)
        self.assertEqual(list(df.columns)[:4], ["시가", "고가", "저가", "종가"])
        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(df.iloc[1]["종가"], 2607.31)
        self.assertEqual(str(df.index[0].date()), "2024-01-02")

    def test_pct_change_matches_index_definition(self):
        from ipo_returns.krxdata import parse_naver_index
        from ipo_returns.pipeline import pct_change_from_prev_close

        pct = pct_change_from_prev_close(parse_naver_index(self.SAMPLE))
        self.assertAlmostEqual(pct.iloc[1]["종가"], (2607.31 / 2669.81 - 1) * 100)
