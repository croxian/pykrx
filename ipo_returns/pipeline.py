"""등락률 산출 및 양봉 필터.

정의
----
* D+0 (상장일) 등락률 = (시/고/저/종가 ÷ **공모가** - 1) × 100
* D+1 ~ D+4 등락률   = (시/고/저/종가 ÷ **전 거래일 종가** - 1) × 100
* 지수(KOSPI/KOSDAQ) 등락률 = (지수 시/고/저/종 ÷ 전 거래일 지수 종가 - 1) × 100
* 필터: 상장일이 양봉(시가 < 종가)인 종목만 남기고, 음봉/보합은 폐기
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Callable, Sequence

import pandas as pd

from .ipo38 import IpoRow

OHLC = ("시가", "고가", "저가", "종가")
INDEX_COLUMNS = [f"{idx}_{col}" for idx in ("KOSPI", "KOSDAQ") for col in OHLC]

COLUMNS = (
    ["종목명", "종목코드", "시장", "공모가", "구분", "날짜"]
    + [f"{c}등락률" for c in OHLC]
    + INDEX_COLUMNS
)


@dataclasses.dataclass
class Skipped:
    name: str
    listing_date: dt.date
    reason: str


def pct_change_from_prev_close(df: pd.DataFrame) -> pd.DataFrame:
    """일봉 OHLC 를 '전 거래일 종가 대비 등락률(%)' 로 변환."""
    if df.empty:
        return pd.DataFrame(columns=list(OHLC))
    prev_close = df["종가"].shift(1)
    out = pd.DataFrame(index=df.index)
    for col in OHLC:
        out[col] = (df[col] / prev_close - 1.0) * 100.0
    return out


def _index_row(index_pct: dict[str, pd.DataFrame], date: pd.Timestamp) -> dict:
    values: dict[str, float | None] = {}
    for name, frame in index_pct.items():
        for col in OHLC:
            value = None
            if frame is not None and not frame.empty and date in frame.index:
                raw = frame.at[date, col]
                value = None if pd.isna(raw) else float(raw)
            values[f"{name}_{col}"] = value
    return values


def build_stock_rows(ipo: IpoRow, ticker: str, market: str, ohlcv: pd.DataFrame,
                     index_pct: dict[str, pd.DataFrame], days: int = 5) -> list[dict]:
    """한 종목의 D+0 ~ D+(days-1) 등락률 행을 만든다."""
    listing_ts = pd.Timestamp(ipo.listing_date)
    frame = ohlcv[ohlcv.index >= listing_ts].head(days)
    if frame.empty:
        return []

    rows: list[dict] = []
    base = float(ipo.offer_price)  # D+0 기준가 = 공모가
    for offset, (date, bar) in enumerate(frame.iterrows()):
        row = {
            "종목명": ipo.name,
            "종목코드": ticker,
            "시장": market,
            "공모가": ipo.offer_price,
            "구분": f"D+{offset}",
            "날짜": date.date(),
        }
        for col in OHLC:
            price = float(bar[col])
            row[f"{col}등락률"] = (price / base - 1.0) * 100.0 if base else None
        row.update(_index_row(index_pct, date))
        rows.append(row)
        base = float(bar["종가"])  # 다음 날의 기준가 = 당일 종가
    return rows


def is_bullish_listing_day(ohlcv: pd.DataFrame, listing_date: dt.date) -> bool | None:
    """상장일 양봉(시가 < 종가) 여부. 데이터가 없으면 None."""
    frame = ohlcv[ohlcv.index >= pd.Timestamp(listing_date)].head(1)
    if frame.empty:
        return None
    bar = frame.iloc[0]
    return float(bar["시가"]) < float(bar["종가"])


def collect(
    ipos: Sequence[IpoRow],
    resolve_ticker: Callable[[str, dt.date], tuple[str, str] | None],
    fetch_ohlcv: Callable[[str, dt.date, dt.date], pd.DataFrame],
    index_pct: dict[str, pd.DataFrame],
    days: int = 5,
    lookahead_days: int = 21,
    prior_days: int = 40,
    verify_listing: bool = True,
    verbose: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, list[Skipped]]:
    """신규상장 목록 -> (양봉 종목 등락률, 음봉 제외 목록, 수집 실패 목록).

    verify_listing=True 면 상장일 이전에 시세가 있는 종목(티커 오매칭 또는
    이전상장·재상장)을 데이터에서 제외하고 실패 목록에 남긴다.
    """
    kept: list[dict] = []
    bearish: list[dict] = []
    skipped: list[Skipped] = []

    for i, ipo in enumerate(ipos, start=1):
        if verbose:
            print(f"  [{i}/{len(ipos)}] {ipo.name} ({ipo.listing_date})")
        resolved = resolve_ticker(ipo.name, ipo.listing_date)
        if resolved is None:
            skipped.append(Skipped(ipo.name, ipo.listing_date, "티커 매칭 실패"))
            continue
        ticker, market = resolved
        try:
            ohlcv = fetch_ohlcv(
                ticker, ipo.listing_date - dt.timedelta(days=prior_days),
                ipo.listing_date + dt.timedelta(days=lookahead_days),
            )
        except Exception as exc:
            skipped.append(Skipped(ipo.name, ipo.listing_date, f"시세 조회 실패: {exc}"))
            continue
        if ohlcv is None or ohlcv.empty:
            skipped.append(Skipped(ipo.name, ipo.listing_date, "시세 데이터 없음"))
            continue

        if verify_listing:
            prior = ohlcv[ohlcv.index < pd.Timestamp(ipo.listing_date)]
            if not prior.empty:
                skipped.append(Skipped(
                    ipo.name, ipo.listing_date,
                    f"상장일 이전 시세 존재({ticker}, {len(prior)}일) "
                    "- 티커 오매칭 또는 이전상장/재상장 의심"))
                continue

        bullish = is_bullish_listing_day(ohlcv, ipo.listing_date)
        if bullish is None:
            skipped.append(Skipped(ipo.name, ipo.listing_date, "상장일 시세 없음"))
            continue

        rows = build_stock_rows(ipo, ticker, market, ohlcv, index_pct, days=days)
        if not rows:
            skipped.append(Skipped(ipo.name, ipo.listing_date, "상장일 이후 시세 없음"))
            continue

        if bullish:
            kept.extend(rows)
        else:
            first = rows[0]
            bearish.append({
                "종목명": ipo.name,
                "종목코드": ticker,
                "시장": market,
                "공모가": ipo.offer_price,
                "상장일": ipo.listing_date,
                "시가등락률": first["시가등락률"],
                "종가등락률": first["종가등락률"],
                "제외사유": "상장일 음봉(시가 >= 종가)",
            })

    kept_df = pd.DataFrame(kept, columns=COLUMNS)
    bearish_df = pd.DataFrame(bearish)
    return kept_df, bearish_df, skipped
