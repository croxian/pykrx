"""pykrx 를 안전하게 import 한다.

pykrx 1.2.8+ 는 **import 시점에** KRX 로그인을 시도한다
(``webio.py`` 의 ``_session = build_krx_session()``). KRX 가 로그인 응답으로
JSON 대신 HTML 을 돌려주면 ``resp.json()`` 에서 예외가 그대로 터져
``import pykrx`` 자체가 실패한다:

    requests.exceptions.JSONDecodeError: Expecting value: line 13 column 1

이 모듈은 그 경우 자격증명을 잠시 지운 채(=로그인 시도 없이) 다시 import 해서
패키지는 쓸 수 있게 만든다. 로그인이 없으면 data.krx.co.kr 조회는 실패하지만,
이 프로젝트는 KIND(티커) + 네이버(시세/지수)만으로도 수집할 수 있다.
"""

from __future__ import annotations

import os
import sys

KRX_LOGIN_OK = False
KRX_LOGIN_ERROR: str | None = None


def disable_krx() -> None:
    """이 프로세스에서 KRX 로그인을 완전히 끈다.

    pykrx 는 **네이버 요청을 보낼 때도** ``webio.Get.read`` -> ``get_session()``
    -> ``get_auth_session()`` 을 거치고, 세션이 없는데 KRX_ID/KRX_PW 가 있으면
    그 자리에서 로그인을 시도한다. KRX 가 IP 를 차단한 상태면 로그인 응답이
    HTML 이라 ``JSONDecodeError`` 가 나고, **네이버 시세까지 전부 실패한다.**
    자격증명을 프로세스 환경에서 지워 그 경로를 끊는다.
    """
    global KRX_LOGIN_OK

    os.environ.pop("KRX_ID", None)
    os.environ.pop("KRX_PW", None)
    KRX_LOGIN_OK = False
    try:
        from pykrx.website.comm import auth, webio

        auth.set_auth_session(None)
        webio.set_session(None)
    except Exception:
        pass


def _purge_pykrx_modules() -> None:
    for name in [m for m in sys.modules if m == "pykrx" or m.startswith("pykrx.")]:
        del sys.modules[name]


def import_stock():
    """``pykrx.stock`` 모듈을 반환한다. 실패 시 RuntimeError.

    환경변수 ``IPO_NO_KRX=1`` 이면 로그인 시도 없이 import 한다
    (KRX 가 IP 를 차단한 동안 유용).
    """
    global KRX_LOGIN_OK, KRX_LOGIN_ERROR

    if os.getenv("IPO_NO_KRX") == "1":
        os.environ.pop("KRX_ID", None)
        os.environ.pop("KRX_PW", None)
        from pykrx import stock

        KRX_LOGIN_OK = False
        return stock

    has_credentials = bool(os.getenv("KRX_ID") and os.getenv("KRX_PW"))
    try:
        from pykrx import stock

        KRX_LOGIN_OK = has_credentials
        return stock
    except Exception as exc:
        KRX_LOGIN_ERROR = f"{type(exc).__name__}: {exc}"

    # 로그인 단계에서 죽은 것으로 보고, 자격증명 없이 다시 import.
    # 자격증명은 복원하지 않는다 - 복원하면 요청마다 로그인을 재시도해
    # 네이버 경로까지 같은 예외로 죽는다.
    os.environ.pop("KRX_ID", None)
    os.environ.pop("KRX_PW", None)
    try:
        _purge_pykrx_modules()
        from pykrx import stock

        KRX_LOGIN_OK = False
        print("[경고] KRX 로그인에 실패해 pykrx 가 import 되지 않았습니다 "
              f"({KRX_LOGIN_ERROR}).\n"
              "       KRX 로그인 없이 계속합니다: 티커는 KIND, 시세·지수는 "
              "네이버에서 받습니다.\n"
              "       (KRX 가 로그인 응답으로 HTML 을 돌려주는 상황입니다. "
              "잠시 후 재시도하거나 data.krx.co.kr 에서 직접 로그인해 보세요.)",
              file=sys.stderr)
        return stock
    except Exception as exc:  # pykrx 자체가 망가진 경우
        raise RuntimeError(f"pykrx import 실패: {exc}") from exc
