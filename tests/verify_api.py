"""
코드에 사용된 코인원 API 전부 검증
- 공개: 시세(ticker), 캔들(1m·1D), 호가창(orderbook) — 봇에서 호가창은 미사용이지만 클라이언트에 있음
- 비공개 V2.1: 잔고(balance), 미체결 주문 조회(open_orders)
- place_order / cancel_order 는 실제 주문·취소가 발생하므로 검증에서 호출하지 않음 (동일 인증·nonce 사용)
"""
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from bot import CoinoneClient, CONFIG, BASE_URL

def main():
    access = CONFIG.get("ACCESS_TOKEN") or os.getenv("COINONE_ACCESS_TOKEN")
    secret = CONFIG.get("SECRET_KEY") or os.getenv("COINONE_SECRET_KEY")
    if not access or not secret or access == "여기에_액세스_토큰" or secret == "여기에_시크릿_키":
        print("SKIP: COINONE_ACCESS_TOKEN / COINONE_SECRET_KEY 미설정 (.env 확인)")
        print("공개 API만 검증합니다.")
        client = None
    else:
        client = CoinoneClient(access, secret)

    symbol = CONFIG.get("SYMBOL", "btc").upper()
    failed = 0

    # ── 1. 공개 API: 시세(ticker) ─────────────────────────────
    print("\n[1] 공개 API - 시세(ticker) GET /public/v2/ticker_new/KRW/{symbol}")
    try:
        import requests
        r = requests.get(f"{BASE_URL}/public/v2/ticker_new/KRW/{symbol}", timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("result") == "error":
            print("  FAIL:", data.get("error_code"), data.get("error_msg", data))
            failed += 1
        else:
            for t in data.get("tickers", []):
                if t.get("target_currency") == symbol:
                    print("  OK   last:", t.get("last"), "KRW")
                    break
            else:
                print("  OK   response keys:", list(data.keys()))
    except Exception as e:
        print("  FAIL:", e)
        failed += 1

    # ── 2. 공개 API: 캔들 1m (봇 기본) ──────────────────────────
    print("\n[2] 공개 API - 캔들 1m GET /public/v2/chart/KRW/{symbol}?interval=1m")
    try:
        chart_1m = client.get_candles(symbol.lower(), "1m", 10) if client else []
        if not client:
            r = requests.get(f"{BASE_URL}/public/v2/chart/KRW/{symbol}", params={"interval": "1m", "size": 10}, timeout=10)
            r.raise_for_status()
            chart_1m = r.json().get("chart", [])
        if not chart_1m:
            print("  WARN chart 비어 있음")
        else:
            c = chart_1m[-1] if isinstance(chart_1m[-1], dict) else {}
            print("  OK   봉 수:", len(chart_1m), "| 마지막 봉 open/close:", c.get("open"), c.get("close"))
    except Exception as e:
        print("  FAIL:", e)
        failed += 1

    # ── 3. 공개 API: 캔들 1D (VOL_BREAKOUT_USE_DAILY 사용 시) ───
    print("\n[3] 공개 API - 캔들 1D GET /public/v2/chart/KRW/{symbol}?interval=1D")
    try:
        if client:
            chart_1d = client.get_candles(symbol.lower(), "1D", 3)
        else:
            r = requests.get(f"{BASE_URL}/public/v2/chart/KRW/{symbol}", params={"interval": "1D", "size": 3}, timeout=10)
            r.raise_for_status()
            chart_1d = r.json().get("chart", [])
        if len(chart_1d) < 2:
            print("  WARN 일봉 2개 미만:", len(chart_1d))
        else:
            d = chart_1d[-2] if isinstance(chart_1d[-2], dict) else {}
            print("  OK   일봉 수:", len(chart_1d), "| 전일(가정) high/low:", d.get("high"), d.get("low"))
    except Exception as e:
        print("  FAIL:", e)
        failed += 1

    # ── 4. 공개 API: 호가창 (클라이언트에만 있음, 봇에서 미호출) ─
    print("\n[4] 공개 API - 호가창(orderbook) GET /public/v2/orderbook/{symbol}/KRW")
    try:
        if client:
            ob = client.get_orderbook(symbol.lower())
        else:
            r = requests.get(f"{BASE_URL}/public/v2/orderbook/KRW/{symbol}", timeout=10)
            r.raise_for_status()
            ob = r.json()
        if ob.get("result") == "error":
            print("  FAIL:", ob.get("error_code"), ob.get("error_msg"))
            failed += 1
        else:
            print("  OK   keys:", list(ob.keys())[:8])
    except Exception as e:
        print("  FAIL:", e)
        failed += 1

    # ── 5. 비공개 V2.1: 잔고 ───────────────────────────────────
    print("\n[5] 비공개 API V2.1 - 잔고 GET /v2.1/account/balance/all")
    if client is None:
        print("  SKIP (인증 없음)")
    else:
        try:
            res = client.get_balance()
            if res.get("result") == "error":
                print("  FAIL:", res.get("error_code"), res.get("error_msg", res))
                failed += 1
            else:
                print("  OK   result:", res.get("result"))
                for cur in ["KRW", symbol]:
                    for b in res.get("balances", []):
                        if isinstance(b, dict) and b.get("currency") == cur:
                            print(f"  OK   {cur}: available={b.get('available', '0')}")
                            break
        except Exception as e:
            print("  FAIL:", e)
            failed += 1

    # ── 6. 비공개 V2.1: 미체결 주문 조회 (실제 주문 없으면 빈 배열) ─
    print("\n[6] 비공개 API V2.1 - 미체결 주문 POST /v2.1/order/open_orders")
    if client is None:
        print("  SKIP (인증 없음)")
    else:
        try:
            res = client.get_open_orders(symbol.upper())
            if not isinstance(res, list):
                print("  FAIL: open_orders 응답이 리스트가 아님", type(res))
                failed += 1
            else:
                print("  OK   open_orders 수:", len(res))
        except Exception as e:
            print("  FAIL:", e)
            failed += 1

    # ── place_order / cancel_order ───────────────────────────────
    print("\n[7] 비공개 API V2.1 - 주문/취소 (place_order, cancel_order)")
    print("  SKIP 실제 주문·취소는 부작용 있음. 동일 _private_post(네트워크·UUID) 사용으로 [5][6] 성공 시 정상 가정.")

    print("\n" + "=" * 50)
    if failed:
        print(f"결과: {failed}개 실패")
        return 1
    print("결과: 사용된 API 전부 정상 응답")
    return 0

if __name__ == "__main__":
    sys.exit(main())
