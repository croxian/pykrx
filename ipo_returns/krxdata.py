"""종목 식별 및 시세 조회.

종목명 -> 티커 매핑
------------------
1순위: **KRX KIND 상장법인목록** (`kind.krx.co.kr`, 로그인 불필요, 시장별 1회 요청)
       회사명·종목코드·시장·상장일이 함께 오므로 **상장일로 후보를 좁힌 뒤**
       이름을 대조한다. 38 과 KRX 의 표기 차이(괄호 주석, 스팩 표기)에 강하다.
2순위: pykrx 전종목시세(상장일 기준). KIND 에 없는 종목(합병·상장폐지된 스팩 등)용.
3순위: 사용자가 준 매핑 CSV (`--ticker-map`).

시세
----
종목 일봉은 KRX(무수정주가) → 실패 시 네이버(수정주가) 순으로 시도하고,
지수(KOSPI 1001 / KOSDAQ 2001) 일봉은 KRX 에서만 받는다. pykrx 1.2.8+ 는
KRX 조회에 로그인(``KRX_ID`` / ``KRX_PW``)이 필요하다.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import difflib
import functools
import io
import os
import re
import time

import pandas as pd
import requests
from pykrx import stock

KOSPI_INDEX = "1001"
KOSDAQ_INDEX = "2001"
INDEX_TICKERS = {"KOSPI": KOSPI_INDEX, "KOSDAQ": KOSDAQ_INDEX}
MARKETS = ("KOSPI", "KOSDAQ", "KONEX")

KIND_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do"
KIND_MARKET_CODES = {"KOSPI": "stockMkt", "KOSDAQ": "kosdaqMkt", "KONEX": "konexMkt"}
KIND_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage",
}

# 스팩 이름 표기 차이 흡수용 (38: 'KB스팩28호' / KIND: '케이비제28호기업인수목적')
_ALIAS = {
    "케이비": "kb", "엔에이치": "nh", "에스케이": "sk", "아이비케이에스": "ibks",
    "아이비케이": "ibk", "케이티비": "ktb", "비엔케이": "bnk", "제이비": "jb",
    "엘비": "lb", "디비": "db", "디에스": "ds", "에스지에이": "sga",
    "에이치엠씨": "hmc", "씨엔": "cn", "아이엠": "im", "에스브이": "sv",
    "케이엘": "kl", "엔에치": "nh", "에이치엔": "hn", "브이아이": "vi",
}
# 스팩 이름에서 양쪽 표기가 들쭉날쭉한 토큰들
# ('하나스팩29호' vs '하나금융제29호스팩' -> 둘 다 '하나29')
_SPAC_TOKENS = ("기업인수목적회사", "기업인수목적", "스팩", "주식회사",
                "자산운용", "투자증권", "증권", "금융", "캐피탈", "제", "호")


def ymd(date: dt.date) -> str:
    return date.strftime("%Y%m%d")


def has_krx_credentials() -> bool:
    return bool(os.getenv("KRX_ID") and os.getenv("KRX_PW"))


def normalize_name(name: str) -> str:
    """비교용 정규화.

    'HD현대마린솔루션(구.HD현대글로벌서비스)(유가)' -> 'hd현대마린솔루션'
    괄호 주석·㈜·공백·구두점을 제거하고 영문은 소문자로 통일한다.
    """
    text = re.sub(r"\([^)]*\)", "", name or "")      # 괄호 주석 제거
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = text.replace("㈜", "").replace("주식회사", "")
    text = re.sub(r"[^0-9A-Za-z가-힣]", "", text)     # 공백/구두점 제거
    return text.lower()


def spac_key(name: str) -> str:
    """스팩 표기 차이를 흡수한 비교키. 'KB스팩28호' == '케이비제28호기업인수목적'."""
    text = normalize_name(name)
    if "스팩" not in text and "기업인수목적" not in text:
        return ""
    for hangul, latin in _ALIAS.items():
        text = text.replace(hangul, latin)
    for token in _SPAC_TOKENS:
        text = text.replace(token, "")
    return text


@dataclasses.dataclass(frozen=True)
class Listing:
    ticker: str
    name: str
    market: str
    listing_date: dt.date | None = None
    source: str = "kind"


def _parse_date(value) -> dt.date | None:
    try:
        parsed = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    return None if pd.isna(parsed) else parsed.date()


def fetch_kind_listings(market: str, timeout: int = 30,
                        retries: int = 3) -> list[Listing]:
    """KIND 상장법인목록(시장별). 회사명/종목코드/상장일을 반환한다."""
    params = {"method": "download", "marketType": KIND_MARKET_CODES[market]}
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(KIND_URL, params=params, headers=KIND_HEADERS,
                                timeout=timeout)
            resp.raise_for_status()
            resp.encoding = "euc-kr"
            tables = pd.read_html(io.StringIO(resp.text), converters={"종목코드": str})
            df = tables[0]
            listings = []
            for _, row in df.iterrows():
                ticker = str(row.get("종목코드", "")).strip().zfill(6)
                name = str(row.get("회사명", "")).strip()
                if not re.fullmatch(r"\d{6}", ticker) or not name:
                    continue
                listings.append(Listing(ticker, name, market,
                                        _parse_date(row.get("상장일")), "kind"))
            return listings
        except Exception as exc:
            last_err = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"KIND {market} 상장법인목록 조회 실패: {last_err}")


@functools.lru_cache(maxsize=1024)
def _krx_listings_on(date_str: str, market: str) -> tuple[Listing, ...]:
    """상장일 기준 KRX 전종목 (티커, 종목명). 실패하면 빈 튜플."""
    for attempt in range(3):
        try:
            from pykrx.website import krx as _krx

            data = _krx.get_market_ticker_and_name(date_str, market)
            # pykrx 는 실패 시 예외 대신 '빈 DataFrame' 을 돌려준다
            if isinstance(data, pd.Series) and not data.empty:
                return tuple(
                    Listing(str(t), str(n), market, _parse_date(date_str), "krx")
                    for t, n in data.items()
                )
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))  # 일시적 차단/세션 만료 대비 백오프
    try:  # 대체 경로: 가격변동 조회에도 종목명이 들어 있다
        df = stock.get_market_price_change_by_ticker(date_str, date_str, market)
        if not df.empty and "종목명" in df.columns:
            return tuple(
                Listing(str(t), str(n), market, _parse_date(date_str), "krx")
                for t, n in df["종목명"].items()
            )
    except Exception:
        pass
    return ()


@dataclasses.dataclass
class Resolution:
    """종목명 -> 티커 매칭 결과와 그 근거."""

    ticker: str
    market: str
    method: str            # date-match / date-unique / name / spac / krx / fuzzy / manual
    matched_name: str
    score: float = 1.0


class TickerResolver:
    """38 종목명 -> (티커, 시장).

    상장일이 같은 종목끼리 먼저 짝을 지어(assign) 매칭한다. 같은 날 상장하는
    종목은 보통 1~3 개뿐이라, 이름 표기가 달라도(사명 변경, 스팩 합병 후 개명)
    정확히 이어붙일 수 있다.
    """

    def __init__(self, markets: tuple[str, ...] = MARKETS, use_kind: bool = True,
                 use_krx_by_date: bool = True, ticker_map_csv: str | None = None,
                 fuzzy_cutoff: float = 0.86, verbose: bool = True):
        self.markets = markets
        self.use_kind = use_kind
        self.use_krx_by_date = use_krx_by_date
        self.fuzzy_cutoff = fuzzy_cutoff
        self.verbose = verbose
        self.manual: dict[str, tuple[str, str]] = {}
        self.by_norm: dict[str, list[Listing]] = {}
        self.by_spac: dict[str, list[Listing]] = {}
        self.by_date: dict[dt.date, list[Listing]] = {}
        self.resolutions: dict[tuple[str, dt.date], Resolution] = {}
        self._built = False
        if ticker_map_csv:
            self._load_manual(ticker_map_csv)

    # ---------------------------------------------------------------- 구축
    def _load_manual(self, path: str) -> None:
        with open(path, encoding="utf-8-sig", newline="") as fp:
            for row in csv.DictReader(fp):
                name = (row.get("종목명") or row.get("name") or "").strip()
                ticker = (row.get("종목코드") or row.get("ticker") or "").strip()
                market = (row.get("시장") or row.get("market") or "").strip() or "미상"
                if name and ticker:
                    self.manual[normalize_name(name)] = (ticker.zfill(6), market)

    def add(self, listing: Listing) -> None:
        self.by_norm.setdefault(normalize_name(listing.name), []).append(listing)
        key = spac_key(listing.name)
        if key:
            self.by_spac.setdefault(key, []).append(listing)
        if listing.listing_date:
            self.by_date.setdefault(listing.listing_date, []).append(listing)

    def build(self) -> None:
        if self._built:
            return
        self._built = True
        if not self.use_kind:
            return
        total = 0
        for market in self.markets:
            try:
                listings = fetch_kind_listings(market)
            except Exception as exc:
                print(f"      [경고] {exc}")
                continue
            for listing in listings:
                self.add(listing)
            total += len(listings)
            if self.verbose:
                print(f"      KIND {market}: {len(listings)} 종목")
        if total == 0:
            print("      [경고] KIND 상장법인목록을 받지 못했습니다. "
                  "KRX 전종목시세(로그인 필요)로만 매칭합니다.")

    # ---------------------------------------------------------------- 점수
    @staticmethod
    def name_score(name_a: str, name_b: str) -> float:
        norm_a, norm_b = normalize_name(name_a), normalize_name(name_b)
        if not norm_a or not norm_b:
            return 0.0
        if norm_a == norm_b:
            return 1.0
        key_a, key_b = spac_key(name_a), spac_key(name_b)
        if key_a and key_a == key_b:
            return 1.0
        score = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
        if norm_a in norm_b or norm_b in norm_a:
            score = max(score, 0.95)
        return score

    # ------------------------------------------------------------- 일괄 매칭
    def assign(self, rows: "list") -> dict[tuple[str, dt.date], Resolution]:
        """(종목명, 상장일) 목록 전체를 한 번에 매칭한다.

        rows 의 각 항목은 ``.name`` 과 ``.listing_date`` 속성을 가져야 한다.
        """
        self.build()
        by_date: dict[dt.date, list] = {}
        for row in rows:
            by_date.setdefault(row.listing_date, []).append(row)

        for date, day_rows in sorted(by_date.items()):
            candidates = list(self.by_date.get(date, []))
            pairs = sorted(
                ((self.name_score(r.name, c.name), i, j)
                 for i, r in enumerate(day_rows) for j, c in enumerate(candidates)),
                reverse=True,
            )
            used_rows: set[int] = set()
            used_cands: set[int] = set()
            for score, i, j in pairs:
                if score < 0.55 or i in used_rows or j in used_cands:
                    continue
                cand = candidates[j]
                self.resolutions[(day_rows[i].name, date)] = Resolution(
                    cand.ticker, cand.market, "date-match", cand.name, score)
                used_rows.add(i)
                used_cands.add(j)

            # 같은 날 남은 것이 1:1 이면 이름이 달라도 짝이 확정된다
            # (스팩 합병 후 사명 변경 등)
            left_rows = [i for i in range(len(day_rows)) if i not in used_rows]
            left_cands = [j for j in range(len(candidates)) if j not in used_cands]
            if len(left_rows) == 1 and len(left_cands) == 1:
                cand = candidates[left_cands[0]]
                row = day_rows[left_rows[0]]
                self.resolutions[(row.name, date)] = Resolution(
                    cand.ticker, cand.market, "date-unique", cand.name,
                    self.name_score(row.name, cand.name))
        return self.resolutions

    # ---------------------------------------------------------------- 단건
    def resolve(self, name: str, on_date: dt.date) -> tuple[str, str] | None:
        found = self.resolve_detail(name, on_date)
        return (found.ticker, found.market) if found else None

    def resolve_detail(self, name: str, on_date: dt.date) -> Resolution | None:
        self.build()
        cached = self.resolutions.get((name, on_date))
        if cached:
            return cached

        norm = normalize_name(name)
        if norm in self.manual:
            ticker, market = self.manual[norm]
            found = Resolution(ticker, market, "manual", name)
            self.resolutions[(name, on_date)] = found
            return found

        # 이름 정확 매칭 / 스팩 표기 매칭
        for table, key, method in ((self.by_norm, norm, "name"),
                                   (self.by_spac, spac_key(name), "spac")):
            if key and key in table:
                pick = self._pick(table[key], on_date)
                found = Resolution(pick.ticker, pick.market, method, pick.name)
                self.resolutions[(name, on_date)] = found
                return found

        # KRX 전종목시세(상장일 기준) — KIND 에 없는 종목 대응
        if self.use_krx_by_date:
            for market in self.markets:
                listings = _krx_listings_on(ymd(on_date), market)
                for listing in listings:
                    self.add(listing)
                for listing in listings:
                    score = self.name_score(name, listing.name)
                    if score >= 0.95:
                        found = Resolution(listing.ticker, listing.market, "krx",
                                           listing.name, score)
                        self.resolutions[(name, on_date)] = found
                        return found

        # 전체 이름 대상 유사도 매칭 (보수적 기준)
        close = difflib.get_close_matches(norm, list(self.by_norm), n=1,
                                          cutoff=self.fuzzy_cutoff)
        if close:
            pick = self._pick(self.by_norm[close[0]], on_date)
            found = Resolution(pick.ticker, pick.market, "fuzzy", pick.name,
                               self.name_score(name, pick.name))
            self.resolutions[(name, on_date)] = found
            return found
        return None

    @staticmethod
    def _pick(candidates: list[Listing], on_date: dt.date) -> Listing:
        exact = [c for c in candidates if c.listing_date == on_date]
        if exact:
            return exact[0]
        kind = [c for c in candidates if c.source == "kind"]
        return (kind or candidates)[0]


# ------------------------------------------------------------------- 시세
def get_stock_ohlcv(ticker: str, start: dt.date, end: dt.date,
                    prefer: str = "krx") -> pd.DataFrame:
    """종목 일봉. 컬럼: 시가/고가/저가/종가/거래량, 인덱스: 날짜.

    prefer="krx"  : 무수정 주가(KRX). 공모가 대비 계산에 적합. 로그인 필요.
    prefer="naver": 수정 주가(네이버). 로그인 없이 조회 가능.
    """
    order = ["krx", "naver"] if prefer == "krx" else ["naver", "krx"]
    problems: list[str] = []
    for source in order:
        try:
            df = stock.get_market_ohlcv_by_date(
                ymd(start), ymd(end), ticker, adjusted=(source == "naver")
            )
            if df is not None and not df.empty:
                return df
            problems.append(f"{source}: 데이터 없음")
        except Exception as exc:
            problems.append(f"{source}: {exc}")
    if all("데이터 없음" in p for p in problems):
        return pd.DataFrame()
    raise RuntimeError(f"{ticker} OHLCV 조회 실패 ({'; '.join(problems)})")


def get_index_ohlcv(index_ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """지수 일봉(KOSPI 1001 / KOSDAQ 2001)."""
    df = stock.get_index_ohlcv_by_date(ymd(start), ymd(end), index_ticker)
    return pd.DataFrame() if df is None else df
