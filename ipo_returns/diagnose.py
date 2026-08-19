"""수집 전 환경 점검.

    python -m ipo_returns.diagnose

38커뮤니케이션 / KIND / KRX 로그인 / 지수·종목 시세를 하나씩 찔러보고
무엇이 막혀 있는지 알려준다.
"""

from __future__ import annotations

import datetime as dt
import sys

from . import ipo38, krxdata


def check(label: str, fn) -> bool:
    try:
        message = fn()
        print(f"  [OK]   {label}: {message}")
        return True
    except Exception as exc:
        print(f"  [실패] {label}: {type(exc).__name__} {exc}")
        return False


def main() -> int:
    print("환경 점검")
    today = dt.date.today()
    results = []

    results.append(check(
        "38커뮤니케이션 신규상장 목록",
        lambda: f"1페이지에서 {len(ipo38.parse_listing_table(ipo38.fetch_page(1)))} 행 파싱",
    ))

    for market in krxdata.MARKETS:
        results.append(check(
            f"KIND 상장법인목록 ({market})",
            lambda m=market: f"{len(krxdata.fetch_kind_listings(m))} 종목",
        ))

    if krxdata.has_krx_credentials():
        print("  [OK]   KRX_ID / KRX_PW 환경변수 설정됨")
    else:
        print("  [경고] KRX_ID / KRX_PW 환경변수 없음 "
              "-> 지수 조회 불가, 종목 시세는 네이버로 대체됨")

    results.append(check(
        "KOSPI 지수 일봉 (pykrx/KRX)",
        lambda: f"{len(krxdata.get_index_ohlcv(krxdata.KOSPI_INDEX, today - dt.timedelta(days=10), today))} 거래일",
    ))
    results.append(check(
        "종목 일봉 (KRX 무수정주가)",
        lambda: f"{len(krxdata.get_stock_ohlcv('005930', today - dt.timedelta(days=10), today, prefer='krx'))} 거래일",
    ))
    results.append(check(
        "종목 일봉 (네이버 수정주가)",
        lambda: f"{len(krxdata.get_stock_ohlcv('005930', today - dt.timedelta(days=10), today, prefer='naver'))} 거래일",
    ))

    print()
    if all(results):
        print("전부 정상입니다. 수집을 진행하세요.")
        return 0
    print("실패 항목이 있습니다. 위 메시지를 확인하세요.\n"
          "  - KIND 가 모두 실패하면 티커 매칭이 크게 나빠집니다(네트워크/차단 확인).\n"
          "  - KRX 만 실패하면 --price-source naver 로도 수집은 가능합니다"
          "(지수 열은 비게 됩니다).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
