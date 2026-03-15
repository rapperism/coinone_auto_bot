"""
API 응답 검증 스크립트 (Nonce UUID 및 Private API 정상 여부 확인)
- 공개 API: 시세(ticker)
- 비공개 API V2.1: 잔고(balance) → Nonce UUID 적용 구간
"""
import os
import sys

from dotenv import load_dotenv
load_dotenv()

# bot 모듈에서 클라이언트·설정 사용
from bot import CoinoneClient, CONFIG, log

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

    # 1) 공개 API: 시세
    print("\n[1] 공개 API - 시세(ticker)")
    try:
        from bot import BASE_URL
        import requests
        # 코인원 문서: ticker_new/{quote_currency}/{target_currency} → KRW/BTC
        r = requests.get(f"{BASE_URL}/public/v2/ticker_new/KRW/{symbol}", timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("result") == "error":
            print("  FAIL:", data.get("error_code"), data.get("error_msg", data))
        else:
            tickers = data.get("tickers", [])
            for t in tickers:
                if t.get("target_currency") == symbol:
                    print("  OK   last:", t.get("last"), "KRW")
                    break
            else:
                print("  OK   response keys:", list(data.keys()))
    except Exception as e:
        print("  FAIL:", e)
        return 1

    # 2) 비공개 API V2.1: 잔고 (Nonce UUID 사용)
    if client is None:
        print("\n[2] 비공개 API - 잔고: SKIP (인증 정보 없음)")
        return 0

    print("\n[2] 비공개 API V2.1 - 잔고(balance)")
    try:
        res = client._private_post("/v2.1/account/balance/all", {})
        if res.get("result") == "error":
            print("  FAIL:", res.get("error_code"), res.get("error_msg", res))
            return 1
        print("  OK   result:", res.get("result"))
        if "krw" in res:
            print("  OK   krw balance:", res.get("krw", {}).get("balance", "N/A"))
        return 0
    except Exception as e:
        print("  FAIL:", e)
        return 1

if __name__ == "__main__":
    sys.exit(main())
