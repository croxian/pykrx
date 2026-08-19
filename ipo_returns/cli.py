"""신규상장 종목 D+0~D+4 등락률 수집 CLI.

사용 예:
    export KRX_ID=... KRX_PW=...          # pykrx 1.2.8+ KRX 조회용
    python -m ipo_returns.cli --start 20230101 --end 20261231 --out ipo_returns.xlsx
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import pandas as pd

from . import ipo38, krxdata
from .excel_report import write_report
from .pipeline import collect, pct_change_from_prev_close

SPAC_PATTERNS = ("스팩", "기업인수목적")


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
                   help="종목 시세 소스 (krx=무수정주가, naver=수정주가)")
    p.add_argument("--exclude-spac", action="store_true", help="스팩 종목 제외")
    p.add_argument("--limit", type=int, default=0, help="처리 종목 수 제한 (테스트용)")
    p.add_argument("--ticker-map", default=None,
                   help="수동 매핑 CSV (종목명,종목코드[,시장])")
    p.add_argument("--no-krx-fallback", action="store_true",
                   help="티커 매칭 시 KRX 전종목시세 조회를 쓰지 않음 (KIND 만 사용)")
    p.add_argument("--no-verify-listing", action="store_true",
                   help="상장일 이전 시세 검증을 건너뜀")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_path = args.out or f"신규상장_등락률_{args.start:%Y%m%d}_{args.end:%Y%m%d}.xlsx"

    if not krxdata.has_krx_credentials():
        print("[경고] KRX_ID / KRX_PW 환경변수가 없습니다. pykrx 1.2.8+ 는 KRX 조회에 "
              "로그인이 필요하며, 지수(KOSPI/KOSDAQ) 데이터와 종목명→티커 매핑이 "
              "실패할 수 있습니다.", file=sys.stderr)

    print(f"[1/4] 38커뮤니케이션에서 신규상장 목록 수집 "
          f"({args.start} ~ {args.end})")
    ipos = ipo38.collect_ipos(args.start, args.end, max_pages=args.max_pages,
                              html_dir=args.html_dir)
    if args.exclude_spac:
        ipos = [r for r in ipos if not any(p in r.name for p in SPAC_PATTERNS)]
    if args.limit:
        ipos = ipos[: args.limit]
    print(f"      신규상장 {len(ipos)} 건")
    if not ipos:
        print("수집된 신규상장 종목이 없습니다.", file=sys.stderr)
        return 1

    print("[2/5] 티커 매칭 (KIND 상장법인목록 기준)")
    resolver = krxdata.TickerResolver(
        ticker_map_csv=args.ticker_map,
        use_krx_by_date=not args.no_krx_fallback,
    )
    resolver.assign(ipos)
    matched = sum(1 for r in ipos if (r.name, r.listing_date) in resolver.resolutions)
    print(f"      상장일 기준 매칭 {matched}/{len(ipos)} 건 "
          "(나머지는 이름·스팩표기·KRX 조회로 재시도)")

    print("[3/5] KOSPI / KOSDAQ 지수 일봉 수집")
    idx_start = min(r.listing_date for r in ipos) - dt.timedelta(days=15)
    idx_end = max(r.listing_date for r in ipos) + dt.timedelta(days=30)
    index_pct: dict[str, pd.DataFrame] = {}
    for name, ticker in krxdata.INDEX_TICKERS.items():
        try:
            frame = krxdata.get_index_ohlcv(ticker, idx_start, idx_end)
        except Exception as exc:
            print(f"      [경고] {name} 지수 조회 실패: {exc}", file=sys.stderr)
            frame = pd.DataFrame()
        index_pct[name] = pct_change_from_prev_close(frame)
        print(f"      {name}: {len(frame)} 거래일")

    print("[4/5] 종목별 시세 수집 및 등락률 계산")
    main_df, bearish_df, skipped = collect(
        ipos,
        resolve_ticker=resolver.resolve,
        fetch_ohlcv=lambda t, s, e: krxdata.get_stock_ohlcv(
            t, s, e, prefer=args.price_source),
        index_pct=index_pct,
        days=args.days,
        verify_listing=not args.no_verify_listing,
    )
    failed_df = pd.DataFrame(
        [{"종목명": s.name, "상장일": s.listing_date, "사유": s.reason} for s in skipped]
    )
    stocks = main_df["종목코드"].nunique() if not main_df.empty else 0
    print(f"      양봉 종목 {stocks} 개 / {len(main_df)} 행, "
          f"음봉 제외 {len(bearish_df)} 개, 실패 {len(failed_df)} 개")

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
        "수집 일수": f"D+0 ~ D+{args.days - 1}",
        "시세 소스": args.price_source,
        "생성 시각": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_report(out_path, main_df, bearish_df, failed_df, meta, match_df)
    if args.csv:
        main_df.to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"      CSV 저장 -> {args.csv}")
    print("완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
