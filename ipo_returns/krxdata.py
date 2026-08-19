"""pykrx 래퍼: 종목 OHLCV, 지수 OHLCV, 종목명 -> 티커 매핑.

pykrx 1.2.8 부터 data.krx.co.kr 조회에 KRX 로그인이 필요하다
(환경변수 ``KRX_ID`` / ``KRX_PW``). 종목 OHLCV 는 네이버 소스로도 받을 수
있어 KRX 로그인이 없으면 자동으로 네이버로 넘어간다. 다만 지수(KOSPI/KOSDAQ)
OHLCV 와 종목명->티커 매핑은 KRX 조회가 필요하다.
"""

from __future__ import annotations

import datetime as dt
import functools
import os
import re
import time

import pandas as pd
from pykrx import stock

KOSPI_INDEX = "1001"
KOSDAQ_INDEX = "2001"
INDEX_TICKERS = {"KOSPI": KOSPI_INDEX, "KOSDAQ": KOSDAQ_INDEX}
MARKETS = ("KOSPI", "KOSDAQ", "KONEX")


def ymd(date: dt.date) -> str:
    return date.strftime("%Y%m%d")


def has_krx_credentials() -> bool:
    return bool(os.getenv("KRX_ID") and os.getenv("KRX_PW"))


def normalize_name(name: str) -> str:
    """종목명 비교용 정규화: 공백/괄호/㈜ 등 제거."""
    text = re.sub(r"\s+", "", name or "")
    text = text.replace("㈜", "").replace("(주)", "")
    text = re.sub(r"[·․.\-_]", "", text)
    return text


@functools.lru_cache(maxsize=512)
def _ticker_name_map(date_str: str, market: str) -> tuple[tuple[str, str], ...]:
    """(티커, 종목명) 튜플 목록. lru_cache 를 위해 해시 가능한 형태로 반환."""
    try:
        from pykrx.website import krx as _krx

        series = _krx.get_market_ticker_and_name(date_str, market)
        time.sleep(0.2)  # KRX 서버 부하 배려 (신규 조회 시에만; 결과는 캐시됨)
        return tuple((str(t), str(n)) for t, n in series.items())
    except Exception:
        pass
    try:  # 대체 경로: 가격변동 조회에 종목명이 함께 온다
        df = stock.get_market_price_change_by_ticker(date_str, date_str, market)
        if not df.empty and "종목명" in df.columns:
            return tuple((str(t), str(n)) for t, n in df["종목명"].items())
    except Exception:
        pass
    return ()


class TickerResolver:
    """38 에서 읽은 종목명을 KRX 티커로 변환한다."""

    def __init__(self, markets: tuple[str, ...] = MARKETS):
        self.markets = markets

    def resolve(self, name: str, on_date: dt.date) -> tuple[str, str] | None:
        """(티커, 시장) 또는 None. 상장일 기준 종목 목록에서 이름으로 찾는다."""
        target = normalize_name(name)
        for market in self.markets:
            pairs = _ticker_name_map(ymd(on_date), market)
            if not pairs:
                continue
            for ticker, krx_name in pairs:
                if normalize_name(krx_name) == target:
                    return ticker, market
        return None


def get_stock_ohlcv(ticker: str, start: dt.date, end: dt.date,
                    prefer: str = "krx") -> pd.DataFrame:
    """종목 일봉. 컬럼: 시가/고가/저가/종가/거래량, 인덱스: 날짜(Timestamp).

    prefer="krx"  : 무수정 주가(KRX). 공모가 대비 계산에 적합. 로그인 필요.
    prefer="naver": 수정 주가(네이버). 로그인 없이 조회 가능.
    """
    order = ["krx", "naver"] if prefer == "krx" else ["naver", "krx"]
    last_err: Exception | None = None
    for source in order:
        try:
            df = stock.get_market_ohlcv_by_date(
                ymd(start), ymd(end), ticker, adjusted=(source == "naver")
            )
            if df is not None and not df.empty:
                return df
        except Exception as exc:
            last_err = exc
    if last_err is not None:
        raise RuntimeError(f"{ticker} OHLCV 조회 실패: {last_err}")
    return pd.DataFrame()


def get_index_ohlcv(index_ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """지수 일봉(KOSPI 1001 / KOSDAQ 2001). 컬럼: 시가/고가/저가/종가 ..."""
    df = stock.get_index_ohlcv_by_date(ymd(start), ymd(end), index_ticker)
    if df is None:
        return pd.DataFrame()
    return df
