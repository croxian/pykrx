"""38커뮤니케이션(www.38.co.kr) 신규상장 종목 크롤러.

신규상장 목록 페이지(``/html/fund/index.htm?o=nw``)에는 종목명 / 공모가 /
상장일이 함께 실려 있다. pykrx 로는 공모가를 얻을 수 없으므로 이 페이지를
공모가·상장일의 원천으로 사용한다.

파서는 표의 **헤더 텍스트**를 읽어 컬럼 위치를 찾아내므로, 38 쪽에서 컬럼
순서가 바뀌거나 컬럼이 추가돼도 계속 동작한다.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import glob
import os
import re
import time
from typing import Iterable, Iterator, Sequence

import requests
from bs4 import BeautifulSoup

BASE_URL = "http://www.38.co.kr/html/fund/index.htm"
DEFAULT_PARAMS = {"o": "nw"}  # o=nw : 신규상장 종목

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "http://www.38.co.kr/html/fund/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 헤더 셀 텍스트 -> 내부 필드
_NAME_KEYS = ("종목명", "기업명", "회사명")
_OFFER_KEYS = ("공모가",)
_LISTING_KEYS = ("상장일", "신규상장일")
_FIRST_KEYS = ("시초가",)


@dataclasses.dataclass
class IpoRow:
    """38커뮤니케이션에서 읽어온 신규상장 1건."""

    name: str
    offer_price: int
    listing_date: dt.date
    first_price: int | None = None

    def as_dict(self) -> dict:
        return {
            "종목명": self.name,
            "공모가": self.offer_price,
            "상장일": self.listing_date,
            "시초가": self.first_price,
        }


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _parse_int(text: str) -> int | None:
    """'12,300 원' -> 12300. 숫자가 없으면 None."""
    digits = re.sub(r"[^0-9]", "", _clean(text))
    if not digits:
        return None
    return int(digits)


def _parse_date(text: str) -> dt.date | None:
    """'2024/03/21', '2024-03-21', '2024.03.21' 모두 허용."""
    m = re.search(r"(\d{4})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})", _clean(text))
    if m:
        y, mm, dd = (int(g) for g in m.groups())
    else:
        m = re.fullmatch(r"(\d{8})", re.sub(r"[^0-9]", "", _clean(text)))
        if not m:
            return None
        raw = m.group(1)
        y, mm, dd = int(raw[:4]), int(raw[4:6]), int(raw[6:])
    try:
        return dt.date(y, mm, dd)
    except ValueError:
        return None


def _match_index(headers: Sequence[str], keys: Iterable[str]) -> int | None:
    for idx, head in enumerate(headers):
        for key in keys:
            if key in head:
                return idx
    return None


def parse_listing_table(html: str) -> list[IpoRow]:
    """38커뮤니케이션 신규상장 페이지 HTML -> IpoRow 목록."""
    soup = BeautifulSoup(html, "lxml")
    rows: list[IpoRow] = []
    seen: set[tuple[str, dt.date]] = set()

    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        header_idx = None
        headers: list[str] = []
        for i, tr in enumerate(trs):
            cells = [_clean(c.get_text()) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            has_name = any(any(k in c for k in _NAME_KEYS) for c in cells)
            has_offer = any(any(k in c for k in _OFFER_KEYS) for c in cells)
            if has_name and has_offer:
                header_idx, headers = i, cells
                break
        if header_idx is None:
            continue

        i_name = _match_index(headers, _NAME_KEYS)
        i_offer = _match_index(headers, _OFFER_KEYS)
        i_listing = _match_index(headers, _LISTING_KEYS)
        i_first = _match_index(headers, _FIRST_KEYS)
        if i_name is None or i_offer is None or i_listing is None:
            continue

        for tr in trs[header_idx + 1 :]:
            cells = tr.find_all(["th", "td"])
            if len(cells) <= max(i_name, i_offer, i_listing):
                continue
            name = _clean(cells[i_name].get_text())
            if not name or any(k in name for k in _NAME_KEYS):
                continue  # 반복 헤더 행
            offer = _parse_int(cells[i_offer].get_text())
            listing = _parse_date(cells[i_listing].get_text())
            if offer is None or not offer or listing is None:
                continue
            key = (name, listing)
            if key in seen:
                continue
            seen.add(key)
            first = (
                _parse_int(cells[i_first].get_text())
                if i_first is not None and len(cells) > i_first
                else None
            )
            rows.append(IpoRow(name=name, offer_price=offer, listing_date=listing,
                               first_price=first))
    return rows


def fetch_page(page: int, session: requests.Session | None = None,
               base_url: str = BASE_URL, timeout: int = 20,
               retries: int = 3) -> str:
    """신규상장 목록 n 페이지의 HTML(문자열)을 반환한다."""
    sess = session or requests.Session()
    params = dict(DEFAULT_PARAMS, page=page)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = sess.get(base_url, params=params, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            # 38 은 EUC-KR(cp949). 응답 헤더가 부정확한 경우가 있어 명시적으로 지정.
            if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
                resp.encoding = "cp949"
            return resp.text
        except Exception as exc:  # 네트워크 오류는 지수 백오프 후 재시도
            last_err = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"38커뮤니케이션 {page} 페이지 요청 실패: {last_err}")


def iter_local_pages(html_dir: str) -> Iterator[str]:
    """로컬에 저장해 둔 38 페이지(*.html)를 순서대로 읽는다."""
    for path in sorted(glob.glob(os.path.join(html_dir, "*.htm*"))):
        with open(path, "rb") as fp:
            raw = fp.read()
        for enc in ("cp949", "utf-8"):
            try:
                yield raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue


def collect_ipos(start: dt.date, end: dt.date, max_pages: int = 60,
                 sleep: float = 0.7, html_dir: str | None = None,
                 base_url: str = BASE_URL, verbose: bool = True) -> list[IpoRow]:
    """[start, end] 구간에 상장한 종목의 (종목명, 공모가, 상장일)을 모은다.

    Args:
        start/end: 상장일 조회 구간
        max_pages: 최대 조회 페이지 수 (목록은 최신 상장일부터 내림차순)
        html_dir : 지정하면 네트워크 대신 로컬에 저장한 HTML 을 파싱한다
    """
    collected: dict[tuple[str, dt.date], IpoRow] = {}

    if html_dir:
        pages: Iterable[str] = iter_local_pages(html_dir)
    else:
        session = requests.Session()
        pages = (fetch_page(p, session=session, base_url=base_url)
                 for p in range(1, max_pages + 1))

    for page_no, html in enumerate(pages, start=1):
        rows = parse_listing_table(html)
        if verbose:
            print(f"  [38] page {page_no}: {len(rows)} 행")
        if not rows:
            break
        for row in rows:
            if start <= row.listing_date <= end:
                collected[(row.name, row.listing_date)] = row
        # 목록이 최신순이므로 페이지 전체가 start 이전이면 더 볼 필요가 없다
        if max(r.listing_date for r in rows) < start:
            break
        if not html_dir:
            time.sleep(sleep)

    return sorted(collected.values(), key=lambda r: (r.listing_date, r.name))
