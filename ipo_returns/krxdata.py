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
pykrx 1.2.8+ 의 data.krx.co.kr 조회에는 KRX 로그인이 필요하지만, 이 모듈은
로그인이 안 되는 상황에서도 동작한다.

* 종목 일봉: KRX(무수정주가) → 네이버(수정주가)
* 지수 일봉: KRX → 네이버(``api.finance.naver.com``)

따라서 KRX 계정이 없거나 로그인이 막혀도 전체 수집이 가능하다.
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

import ast
import json

import pandas as pd
import requests

from .pykrx_safe import import_stock

stock = import_stock()

KOSPI_INDEX = "1001"
KOSDAQ_INDEX = "2001"
INDEX_TICKERS = {"KOSPI": KOSPI_INDEX, "KOSDAQ": KOSDAQ_INDEX}
MARKETS = ("KOSPI", "KOSDAQ", "KONEX")
# 상장일 기준 KRX 조회는 코스닥부터 (스팩·중소형 신규상장이 대부분)
LOOKUP_MARKETS = ("KOSDAQ", "KOSPI", "KONEX")

# KRX 는 자동화 대량 조회를 감지하면 IP 를 1일 차단한다. 상장일 기준 조회는
# 마지막 수단으로만 쓰고, 총 요청 수에 상한을 둔다.
_krx_lookup_budget = 120
_krx_lookup_used = 0

NAVER_INDEX_URL = "https://api.finance.naver.com/siseJson.naver"
NAVER_INDEX_SYMBOLS = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ"}
NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
}

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


def set_krx_lookup_budget(count: int) -> None:
    """상장일 기준 KRX 조회 요청 수 상한을 설정한다."""
    global _krx_lookup_budget

    _krx_lookup_budget = max(0, count)


def _looks_like_login_failure(exc: Exception) -> bool:
    """KRX 로그인 응답이 JSON 이 아닐 때 나는 예외인가."""
    text = str(exc)
    return "Expecting value" in text or "JSONDecode" in type(exc).__name__


def _guard_krx_login(fn):
    """호출 중 KRX 로그인이 죽으면 KRX 를 끄고 한 번 더 시도한다.

    pykrx 는 네이버 요청 경로에서도 KRX 세션을 갱신하려 들기 때문에,
    KRX 가 차단된 상태에서는 네이버 조회까지 같은 예외로 실패한다.
    """
    from . import pykrx_safe

    try:
        return fn()
    except Exception as exc:
        if not _looks_like_login_failure(exc):
            raise
        pykrx_safe.disable_krx()
        print("      [안내] KRX 로그인이 실패해 KRX 사용을 끕니다 "
              "(이후 네이버로만 조회).")
        return fn()


def krx_login_ok() -> bool:
    """pykrx 가 KRX 로그인 세션을 들고 있는지."""
    from . import pykrx_safe

    return pykrx_safe.KRX_LOGIN_OK


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


NAVER_SEARCH_URLS = (
    "https://m.stock.naver.com/api/search/stock",       # {"stocks":[{itemCode, stockName}]}
    "https://ac.finance.naver.com/ac",                  # 자동완성(구형)
)


def _extract_code_name_pairs(payload: str) -> list[tuple[str, str]]:
    """네이버 검색 응답에서 (6자리 코드, 종목명) 쌍을 최대한 건져낸다."""
    pairs: list[tuple[str, str]] = []
    try:
        data = json.loads(payload)
    except Exception:
        data = None

    def walk(node, parent_code=None):
        if isinstance(node, dict):
            code = node.get("itemCode") or node.get("code") or node.get("cd")
            name = (node.get("stockName") or node.get("name") or node.get("nm")
                    or node.get("korName"))
            if code and name and re.fullmatch(r"[A-Z]?\d{6}", str(code)):
                pairs.append((str(code)[-6:], str(name)))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            flat = [x for x in node if isinstance(x, (str, int))]
            code = next((str(x) for x in flat if re.fullmatch(r"\d{6}", str(x))), None)
            name = next((str(x) for x in flat
                         if isinstance(x, str) and not re.fullmatch(r"\d+", x)), None)
            if code and name:
                pairs.append((code, name))
            for value in node:
                walk(value)

    if data is not None:
        walk(data)
    return pairs


def naver_search_ticker(name: str, timeout: int = 15) -> list[tuple[str, str]]:
    """네이버 금융 검색으로 (티커, 종목명) 후보를 얻는다. 실패하면 빈 목록."""
    for url in NAVER_SEARCH_URLS:
        params = ({"query": name, "pageSize": 10} if "m.stock" in url else
                  {"q": name, "q_enc": "utf-8", "st": 111, "r_format": "json",
                   "r_enc": "utf-8", "r_unicode": 0, "t_koreng": 1, "r_lt": 111})
        try:
            resp = requests.get(url, params=params, headers=NAVER_HEADERS,
                                timeout=timeout)
            resp.raise_for_status()
            pairs = _extract_code_name_pairs(resp.text)
            if pairs:
                return pairs
        except Exception:
            continue
    return []


def load_listings_file(path: str, market: str = "미상") -> list[Listing]:
    """KIND 에서 직접 내려받은 상장법인목록 파일을 읽는다.

    KIND 의 '상장법인목록' 다운로드는 확장자가 .xls 이지만 실제로는 HTML 표다.
    .xlsx / .csv 도 받는다. 필요한 열: 회사명, 종목코드, 상장일.
    """
    lowered = path.lower()
    if lowered.endswith(".csv"):
        df = pd.read_csv(path, dtype={"종목코드": str}, encoding="utf-8-sig")
    elif lowered.endswith(".xlsx") or lowered.endswith(".xlsm"):
        df = pd.read_excel(path, dtype={"종목코드": str})
    else:  # KIND 다운로드(.xls) = HTML 표
        with open(path, "rb") as fp:
            raw = fp.read()
        for encoding in ("euc-kr", "utf-8"):
            try:
                df = pd.read_html(io.StringIO(raw.decode(encoding)),
                                  converters={"종목코드": str})[0]
                break
            except (UnicodeDecodeError, ValueError):
                df = None
        if df is None:
            raise RuntimeError(f"상장법인목록 파일을 읽지 못했습니다: {path}")

    listings = []
    for _, row in df.iterrows():
        ticker = str(row.get("종목코드", "")).strip().zfill(6)
        name = str(row.get("회사명", row.get("종목명", ""))).strip()
        if not re.fullmatch(r"\d{6}", ticker) or not name:
            continue
        listings.append(Listing(ticker, name, str(row.get("시장", market)),
                                _parse_date(row.get("상장일")), "file"))
    return listings


@functools.lru_cache(maxsize=1024)
def _krx_listings_on(date_str: str, market: str) -> tuple[Listing, ...]:
    """상장일 기준 KRX 전종목 (티커, 종목명). 실패하면 빈 튜플."""
    global _krx_lookup_used

    if not krx_login_ok():
        return ()
    if _krx_lookup_used >= _krx_lookup_budget:
        if _krx_lookup_used == _krx_lookup_budget:
            _krx_lookup_used += 1
            print("      [안내] 상장일 기준 KRX 조회 상한에 도달해 중단합니다 "
                  "(--krx-lookup-budget 로 조정).")
        return ()
    _krx_lookup_used += 1
    time.sleep(1.0)  # KRX 차단 회피: 상장일 조회는 천천히
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


@functools.lru_cache(maxsize=1024)
def krx_new_listings_on(date_str: str, market: str) -> tuple[Listing, ...]:
    """그 날 **새로 생긴** 티커들.

    상장일의 전종목 목록에서 직전 거래일 목록을 빼면 그날 신규 상장한 종목만
    남는다. 이름을 맞출 필요가 없어 표기 차이(스팩·사명 변경)에 영향을 받지
    않고, 지금은 합병·상장폐지된 종목도 그대로 잡힌다.

    다만 KRX 조회라 로그인이 필요하고 요청 상한(:func:`set_krx_lookup_budget`)의
    적용을 받는다.
    """
    today = _krx_listings_on(date_str, market)
    if not today:
        return ()
    date = dt.datetime.strptime(date_str, "%Y%m%d").date()
    for back in range(1, 6):
        previous = _krx_listings_on(ymd(date - dt.timedelta(days=back)), market)
        if previous:
            known = {listing.ticker for listing in previous}
            return tuple(l for l in today if l.ticker not in known)
    return ()


class TickerResolver:
    """38 종목명 -> (티커, 시장).

    상장일이 같은 종목끼리 먼저 짝을 지어(assign) 매칭한다. 같은 날 상장하는
    종목은 보통 1~3 개뿐이라, 이름 표기가 달라도(사명 변경, 스팩 합병 후 개명)
    정확히 이어붙일 수 있다.
    """

    def __init__(self, markets: tuple[str, ...] = MARKETS, use_kind: bool = True,
                 use_krx_by_date: bool = True, ticker_map_csv: str | None = None,
                 listings_files: "list[str] | None" = None,
                 use_naver_search: bool = True,
                 fuzzy_cutoff: float = 0.86, verbose: bool = True):
        self.markets = markets
        self.use_kind = use_kind
        self.listings_files = listings_files or []
        self.use_naver_search = use_naver_search
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
        total = 0

        # 미리 받아둔 상장법인목록 파일 (KRX 차단 시 대안)
        for spec in self.listings_files:
            market, _, path = spec.partition("=")
            if not path:
                market, path = "미상", spec
            listings = load_listings_file(path, market)
            for listing in listings:
                self.add(listing)
            total += len(listings)
            if self.verbose:
                print(f"      파일 {path} ({market}): {len(listings)} 종목")

        if not self.use_kind:
            return
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
        # 숫자가 다르면 다른 회사다: '디비금융스팩11호' != '디비금융스팩12호'
        if re.findall(r"\d+", norm_a) != re.findall(r"\d+", norm_b):
            return 0.0
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

        # KRX 상장일 스냅샷 — KIND 에 없는 종목(합병·상폐된 스팩 등) 대응
        if self.use_krx_by_date:
            for market in LOOKUP_MARKETS:
                fresh = krx_new_listings_on(ymd(on_date), market)
                if not fresh:
                    continue
                for listing in fresh:
                    self.add(listing)
                claimed = {r.ticker for r in self.resolutions.values()}
                free = [l for l in fresh if l.ticker not in claimed]
                scored = sorted(((self.name_score(name, l.name), l) for l in free),
                                key=lambda pair: pair[0], reverse=True)
                if scored and scored[0][0] >= 0.5:
                    score, listing = scored[0]
                    found = Resolution(listing.ticker, listing.market,
                                       "krx-new", listing.name, score)
                    self.resolutions[(name, on_date)] = found
                    return found
                if len(free) == 1:
                    # 그날 그 시장에서 새로 생긴 티커가 하나뿐이면 그것이다
                    listing = free[0]
                    found = Resolution(listing.ticker, listing.market,
                                       "krx-new-unique", listing.name,
                                       self.name_score(name, listing.name))
                    self.resolutions[(name, on_date)] = found
                    return found

        # 네이버 금융 검색 (KIND 목록에 없는 스팩·리츠 등 대응)
        if self.use_naver_search:
            for ticker, naver_name in naver_search_ticker(name):
                score = self.name_score(name, naver_name)
                if score >= 0.9:
                    listing = Listing(ticker, naver_name, "미상", None, "naver")
                    self.add(listing)
                    found = Resolution(ticker, "미상", "naver", naver_name, score)
                    self.resolutions[(name, on_date)] = found
                    return found

        # 전체 이름 대상 유사도 매칭 (보수적 기준)
        close = [key for key in difflib.get_close_matches(
            norm, list(self.by_norm), n=3, cutoff=self.fuzzy_cutoff)
            if re.findall(r"\d+", key) == re.findall(r"\d+", norm)]
        if close:
            pick = self._pick(self.by_norm[close[0]], on_date)
            found = Resolution(pick.ticker, pick.market, "fuzzy", pick.name,
                               self.name_score(name, pick.name))
            self.resolutions[(name, on_date)] = found
            return found
        return None

    def same_day_candidates(self, on_date: dt.date) -> str:
        """진단용: 그 날 상장한 것으로 알려진 종목들."""
        return ", ".join(f"{c.name}({c.ticker})" for c in self.by_date.get(on_date, []))

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

    prefer="krx"  : 무수정 주가(KRX). 공모가 대비 계산에 정확. 로그인 필요.
    prefer="naver": 수정 주가(네이버). 로그인 없이 조회 가능.

    KRX 로그인이 없으면 KRX 시도를 건너뛴다(수백 번의 헛된 요청 방지).
    """
    order = ["krx", "naver"] if prefer == "krx" else ["naver", "krx"]
    if not krx_login_ok():
        order = [src for src in order if src != "krx"] or ["naver"]

    problems: list[str] = []
    for source in order:
        try:
            df = _guard_krx_login(lambda src=source: stock.get_market_ohlcv_by_date(
                ymd(start), ymd(end), ticker, adjusted=(src == "naver")
            ))
            if df is not None and not df.empty:
                return df
            problems.append(f"{source}: 데이터 없음")
        except Exception as exc:
            problems.append(f"{source}: {exc}")
    if all("데이터 없음" in p for p in problems):
        return pd.DataFrame()
    raise RuntimeError(f"{ticker} OHLCV 조회 실패 ({'; '.join(problems)})")


def get_index_ohlcv_naver(market: str, start: dt.date, end: dt.date,
                          timeout: int = 30, retries: int = 3) -> pd.DataFrame:
    """네이버 지수 일봉 (로그인 불필요). market: KOSPI / KOSDAQ."""
    params = {
        "symbol": NAVER_INDEX_SYMBOLS[market],
        "requestType": 1,
        "startTime": ymd(start),
        "endTime": ymd(end),
        "timeframe": "day",
    }
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(NAVER_INDEX_URL, params=params,
                                headers=NAVER_HEADERS, timeout=timeout)
            resp.raise_for_status()
            return parse_naver_index(resp.text)
        except Exception as exc:
            last_err = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"네이버 {market} 지수 조회 실패: {last_err}")


def parse_naver_index(text: str) -> pd.DataFrame:
    """siseJson.naver 응답(파이썬 리터럴 형태의 2차원 배열)을 DataFrame 으로."""
    rows = ast.literal_eval(text.strip())
    if not rows or len(rows) < 2:
        return pd.DataFrame()
    header = [str(h).strip() for h in rows[0]]
    df = pd.DataFrame(rows[1:], columns=header)
    df = df.rename(columns={"날짜": "날짜", "시가": "시가", "고가": "고가",
                            "저가": "저가", "종가": "종가", "거래량": "거래량"})
    df["날짜"] = pd.to_datetime(df["날짜"].astype(str), format="%Y%m%d")
    df = df.set_index("날짜")
    for col in ("시가", "고가", "저가", "종가"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    keep = [c for c in ("시가", "고가", "저가", "종가", "거래량") if c in df.columns]
    return df[keep].dropna(subset=["종가"]).sort_index()


def get_index_ohlcv(market: str, start: dt.date, end: dt.date,
                    source: str = "auto") -> pd.DataFrame:
    """지수 일봉. market 은 'KOSPI' / 'KOSDAQ' (또는 지수 티커 1001 / 2001).

    source="auto" 면 KRX 로그인이 살아 있을 때만 KRX 를 쓰고, 아니면 네이버.
    """
    market = {v: k for k, v in INDEX_TICKERS.items()}.get(market, market)
    problems: list[str] = []

    if source in ("auto", "krx") and (source == "krx" or krx_login_ok()):
        try:
            df = _guard_krx_login(lambda: stock.get_index_ohlcv_by_date(
                ymd(start), ymd(end), INDEX_TICKERS[market]))
            if df is not None and not df.empty:
                return df
            problems.append("krx: 데이터 없음")
        except Exception as exc:
            problems.append(f"krx: {exc}")

    if source in ("auto", "naver"):
        try:
            df = get_index_ohlcv_naver(market, start, end)
            if not df.empty:
                return df
            problems.append("naver: 데이터 없음")
        except Exception as exc:
            problems.append(f"naver: {exc}")

    raise RuntimeError(f"{market} 지수 조회 실패 ({'; '.join(problems)})")
