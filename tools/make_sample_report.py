"""네트워크 없이 엑셀 양식을 확인하기 위한 샘플 리포트 생성기.

가상의 시세 데이터를 파이프라인에 그대로 흘려보내 실제 산출물과 동일한
형식의 엑셀을 만든다. (수치는 실제 시장 데이터가 아님)

    python tools/make_sample_report.py [출력경로]
"""

from __future__ import annotations

import datetime as dt
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from ipo_returns.ipo38 import IpoRow
from ipo_returns.excel_report import write_report
from ipo_returns.pipeline import collect, pct_change_from_prev_close

random.seed(7)
COLS = ["시가", "고가", "저가", "종가", "거래량"]


def make_frame(dates, base, moves):
    rows = []
    price = base
    for open_mult, high_mult, low_mult, close_mult in moves:
        rows.append([round(price * open_mult), round(price * high_mult),
                     round(price * low_mult), round(price * close_mult),
                     random.randint(100_000, 5_000_000)])
        price = rows[-1][3]
    return pd.DataFrame(rows, columns=COLS, index=pd.to_datetime(dates))


DATES_A = ["20240321", "20240322", "20240325", "20240326", "20240327"]
DATES_B = ["20250109", "20250110", "20250113", "20250114", "20250115"]
DATES_C = ["20250213", "20250214", "20250217", "20250218", "20250219"]
INDEX_DATES = sorted(set(["20240320"] + DATES_A + ["20250108"] + DATES_B
                         + ["20250212"] + DATES_C))

FRAMES = {
    "111111": make_frame(DATES_A, 10_000,
                         [(1.60, 2.10, 1.45, 1.95), (1.02, 1.09, 0.97, 1.04),
                          (1.00, 1.03, 0.93, 0.95), (0.96, 1.01, 0.94, 0.99),
                          (0.99, 1.02, 0.96, 0.97)]),
    "222222": make_frame(DATES_B, 24_000,
                         [(1.10, 1.35, 1.05, 1.28), (0.98, 1.02, 0.92, 0.94),
                          (0.95, 0.99, 0.90, 0.92), (0.93, 1.06, 0.92, 1.05),
                          (1.05, 1.12, 1.02, 1.08)]),
    "333333": make_frame(DATES_C, 15_000,
                         [(2.00, 2.05, 1.05, 1.20), (1.00, 1.04, 0.90, 0.93),
                          (0.94, 0.98, 0.89, 0.90), (0.91, 0.95, 0.88, 0.94),
                          (0.95, 1.01, 0.93, 1.00)]),
}

IPOS = [IpoRow("가나테크", 10_000, dt.date(2024, 3, 21)),
        IpoRow("다라바이오", 24_000, dt.date(2025, 1, 9)),
        IpoRow("마바로보틱스", 15_000, dt.date(2025, 2, 13))]
TICKERS = {"가나테크": ("111111", "KOSDAQ"), "다라바이오": ("222222", "KOSPI"),
           "마바로보틱스": ("333333", "KOSDAQ")}


def main(out: str = "sample_신규상장_등락률.xlsx") -> None:
    index_frames = {}
    for name, base in (("KOSPI", 2_600.0), ("KOSDAQ", 850.0)):
        moves = []
        for _ in INDEX_DATES:
            close = 1 + random.uniform(-0.015, 0.015)
            open_ = 1 + random.uniform(-0.006, 0.006)
            moves.append((open_, max(open_, close) + random.uniform(0.001, 0.006),
                          min(open_, close) - random.uniform(0.001, 0.006), close))
        index_frames[name] = make_frame(INDEX_DATES, base, moves)

    index_pct = {k: pct_change_from_prev_close(v) for k, v in index_frames.items()}

    result = collect(
        IPOS,
        resolve_ticker=lambda name, date: TICKERS.get(name),
        fetch_ohlcv=lambda t, s, e: FRAMES[t],
        index_pct=index_pct,
        verbose=False,
    )
    main_df, bearish_df, skipped = result.main, result.bearish, result.skipped
    failed_df = pd.DataFrame([{"종목명": s.name, "상장일": s.listing_date,
                               "사유": s.reason} for s in skipped])
    write_report(out, main_df, bearish_df, failed_df, {
        "샘플": "※ 실제 시장 데이터가 아닌 형식 확인용 가상 데이터",
        "조회 구간": "2023-01-01 ~ 2026-08-19",
        "양봉 종목 수": main_df["종목코드"].nunique(),
        "음봉 제외 종목 수": len(bearish_df),
    })
    print(f"샘플 저장: {out} (양봉 {main_df['종목코드'].nunique()}종목 / "
          f"음봉 제외 {len(bearish_df)}종목)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample_신규상장_등락률.xlsx")
