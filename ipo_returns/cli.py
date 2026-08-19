"""신규상장 종목 D+0~D+4 등락률 수집 CLI.

사용 예:
    export KRX_ID=... KRX_PW=...          # pykrx 1.2.8+ KRX 조회용
    python -m ipo_returns.cli --start 20230101 --end 20261231 --out ipo_returns.xlsx
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

import pandas as pd

from .excel_report import write_report
from .pipeline import collect, pct_change_from_prev_close

SPAC_PATTERNS = ("스팩", "기업인수목적")


def _sleep_then(seconds: float, fn):
    if seconds > 0:
        time.sleep(seconds)
    return fn()


def parse_date(text: str) -> dt.date:
    digits = text.replace("-", "").replace("/", "").strip()
    return dt.datetime.strptime(digits, "%Y%m%d").date()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="신규상장 종목 D+0~D+4 등락률 수집")
    p.add_argument("--start", type=parse_date, default=dt.date(2023, 1, 1),
                   help="상장일 조회 시작 (기본 20230101)")
    p.add_argument("--end", type=parse_date, default=dt.date.today(),
                   help="상장일 조회 종료 (기본 오늘)")
    p.add_argument("--days", type=int, default=5, help="상장일 포함 수집 일수 (기본 5 = D+0~D+4)")
    p.add_argument("--out", default=None, help="엑셀 출력 경로")
    p.add_argument("--csv", default=None, help="CSV 도 함께 저장할 경로")
    p.add_argument("--max-pages", type=int, default=60, help="38 목록 최대 페이지")
    p.add_argument("--html-dir", default=None,
                   help="네트워크 대신 로컬에 저장한 38 페이지(HTML) 디렉터리")
    p.add_argument("--price-source", choices=("krx", "naver"), default="krx",
                   help="종목 시세 소스 (krx=무수정주가, naver=수정주가). "
                        "KRX 로그인이 없으면 자동으로 naver 를 쓴다")
    p.add_argument("--index-source", choices=("auto", "krx", "naver"), default="auto",
                   help="지수 소스 (auto=KRX 로그인 되면 KRX, 아니면 네이버)")
    p.add_argument("--exclude-spac", action="store_true", help="스팩 종목 제외")
    p.add_argument("--limit", type=int, default=0, help="처리 종목 수 제한 (테스트용)")
    p.add_argument("--ticker-map", default=None,
                   help="수동 매핑 CSV (종목명,종목코드[,시장])")
    p.add_argument("--no-krx-fallback", action="store_true",
                   help="티커 매칭 시 KRX 전종목시세 조회를 쓰지 않음 (KIND 만 사용)")
    p.add_argument("--no-verify-listing", action="store_true",
                   help="상장일 이전 시세 검증을 건너뜀")
    p.add_argument("--no-transfer-listing", action="store_true",
                   help="이전상장/재상장 종목을 결과에서 제외")
    p.add_argument("--no-adjust-prices", action="store_true",
                   help="38 시초가 기준 수정주가 보정을 끔")
    p.add_argument("--no-naver-search", action="store_true",
                   help="티커 매칭 시 네이버 종목 검색을 쓰지 않음")
    p.add_argument("--listings-file", action="append", default=[],
                   metavar="시장=경로",
                   help="KIND 에서 직접 받은 상장법인목록 파일 "
                        "(예: KOSPI=kospi.xls). 여러 번 지정 가능")
    p.add_argument("--request-sleep", type=float, default=0.3,
                   help="종목별 시세 요청 사이 대기 (초, 기본 0.3). "
                        "차단 방지를 위해 너무 낮추지 말 것")
    p.add_argument("--no-krx", action="store_true",
                   help="KRX(data.krx.co.kr) 를 아예 쓰지 않음 "
                        "(로그인 시도조차 하지 않는다)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # pykrx 는 import 시점에 KRX 로그인을 시도하므로 옵션 처리 후에 불러온다
    if args.no_krx:
        os.environ["IPO_NO_KRX"] = "1"
    from . import ipo38, krxdata
    out_path = args.out or f"신규상장_등락률_{args.start:%Y%m%d}_{args.end:%Y%m%d}.xlsx"

    if krxdata.krx_login_ok():
        print("KRX 로그인 세션 사용 (무수정주가·지수를 KRX 에서 조회)")
    else:
        print("[안내] KRX 로그인 없이 실행합니다: 티커는 KIND, 종목 시세와 지수는 "
              "네이버에서 받습니다. 종목 시세는 수정주가라 상장 이후 액면분할·"
              "무상증자가 있었던 종목은 상장일 등락률이 실제와 다를 수 있습니다.",
              file=sys.stderr)

    print(f"[1/4] 38커뮤니케이션에서 신규상장 목록 수집 "
          f"({args.start} ~ {args.end})")
    ipos = ipo38.collect_ipos(args.start, args.end, max_pages=args.max_pages,
                              html_dir=args.html_dir)
    if args.exclude_spac:
        ipos = [r for r in ipos if not any(p in r.name for p in SPAC_PATTERNS)]
    if args.limit:
        ipos = ipos[: args.limit]
    with_first = sum(1 for r in ipos if r.first_price)
    print(f"      신규상장 {len(ipos)} 건 (시초가 확보 {with_first} 건)")
    if with_first < len(ipos) * 0.5:
        print("      [경고] 38 목록에서 시초가를 거의 못 읽었습니다. "
              "수정주가 보정과 이전상장 확인이 제한됩니다.", file=sys.stderr)
    if not ipos:
        print("수집된 신규상장 종목이 없습니다.", file=sys.stderr)
        return 1

    print("[2/5] 티커 매칭 (KIND 상장법인목록 기준)")
    resolver = krxdata.TickerResolver(
        ticker_map_csv=args.ticker_map,
        listings_files=args.listings_file,
        use_krx_by_date=not (args.no_krx_fallback or args.no_krx),
        use_naver_search=not args.no_naver_search,
    )
    resolver.assign(ipos)
    matched = sum(1 for r in ipos if (r.name, r.listing_date) in resolver.resolutions)
    print(f"      상장일 기준 매칭 {matched}/{len(ipos)} 건 "
          "(나머지는 이름·스팩표기·KRX 조회로 재시도)")

    print("[3/5] KOSPI / KOSDAQ 지수 일봉 수집")
    idx_start = min(r.listing_date for r in ipos) - dt.timedelta(days=15)
    idx_end = max(r.listing_date for r in ipos) + dt.timedelta(days=30)
    index_pct: dict[str, pd.DataFrame] = {}
    for name in krxdata.INDEX_TICKERS:
        try:
            frame = krxdata.get_index_ohlcv(name, idx_start, idx_end,
                                            source=args.index_source)
        except Exception as exc:
            print(f"      [경고] {name} 지수 조회 실패: {exc}", file=sys.stderr)
            frame = pd.DataFrame()
        index_pct[name] = pct_change_from_prev_close(frame)
        print(f"      {name}: {len(frame)} 거래일")

    print("[4/5] 종목별 시세 수집 및 등락률 계산")
    result = collect(
        ipos,
        resolve_ticker=resolver.resolve,
        fetch_ohlcv=lambda t, s, e: _sleep_then(
            args.request_sleep,
            lambda: krxdata.get_stock_ohlcv(t, s, e, prefer=args.price_source)),
        index_pct=index_pct,
        days=args.days,
        verify_listing=not args.no_verify_listing,
        include_transfer_listing=not args.no_transfer_listing,
        adjust_prices=not args.no_adjust_prices,
        candidates_hint=resolver.same_day_candidates,
    )
    main_df, bearish_df, skipped = result.main, result.bearish, result.skipped
    failed_df = pd.DataFrame(
        [{"종목명": s.name, "상장일": s.listing_date, "사유": s.reason} for s in skipped]
    )
    stocks = main_df["종목코드"].nunique() if not main_df.empty else 0
    print(f"      양봉 종목 {stocks} 개 / {len(main_df)} 행, "
          f"음봉 제외 {len(bearish_df)} 개, 실패 {len(failed_df)} 개")
    if not result.adjustments.empty:
        print(f"      수정주가 보정 {len(result.adjustments)} 종목 "
              "(무상증자·액면분할 등, '수정주가보정' 시트 참고)")
    if not result.transfers.empty:
        print(f"      이전상장/재상장 {len(result.transfers)} 종목 포함 "
              "('이전상장' 시트 참고)")

    match_df = pd.DataFrame(
        [{"38 종목명": name, "상장일": date, "종목코드": res.ticker,
          "시장": res.market, "매칭 종목명": res.matched_name,
          "매칭방식": res.method, "유사도": round(res.score, 3)}
         for (name, date), res in sorted(resolver.resolutions.items(),
                                         key=lambda kv: kv[0][1])]
    )

    print(f"[5/5] 엑셀 저장 -> {out_path}")
    meta = {
        "조회 구간": f"{args.start} ~ {args.end}",
        "신규상장 종목 수": len(ipos),
        "양봉 종목 수": stocks,
        "음봉 제외 종목 수": len(bearish_df),
        "수집 실패 종목 수": len(failed_df),
        "수정주가 보정 종목 수": len(result.adjustments),
        "이전상장 포함 종목 수": len(result.transfers),
        "수집 일수": f"D+0 ~ D+{args.days - 1}",
        "시세 소스": args.price_source if krxdata.krx_login_ok() else "naver",
        "지수 소스": args.index_source,
        "생성 시각": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_report(out_path, main_df, bearish_df, failed_df, meta, match_df,
                 result.adjustments, result.transfers)
    if args.csv:
        main_df.to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"      CSV 저장 -> {args.csv}")
    print("완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
