"""
코인원 자동매매 봇
전략: 이동평균(MA), RSI, 변동성 돌파, MACD, 볼린저밴드 복합 전략
"""

import time
import hmac
import hashlib
import base64
import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

import requests
import numpy as np
import pandas as pd
from dotenv import load_dotenv

# ─────────────────────────────────────────
#  로깅 설정
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

load_dotenv()

# ─────────────────────────────────────────
#  설정값 (config.json 또는 .env 로 변경 가능)
# ─────────────────────────────────────────
CONFIG = {
    "ACCESS_TOKEN": os.getenv("COINONE_ACCESS_TOKEN", "여기에_액세스_토큰"),
    "SECRET_KEY":   os.getenv("COINONE_SECRET_KEY",   "여기에_시크릿_키"),

    # 거래 대상 코인 (소문자)
    "SYMBOL": "btc",

    # 전략 파라미터
    "RSI_PERIOD":        14,
    "RSI_OVERSOLD":      30,      # RSI 이 아래면 매수 신호
    "RSI_OVERBOUGHT":    70,      # RSI 이 위면 매도 신호

    "MA_SHORT":          5,       # 단기 이동평균 봉 수
    "MA_LONG":           20,      # 장기 이동평균 봉 수

    "BB_PERIOD":         20,      # 볼린저밴드 기간
    "BB_STD":            2.0,     # 볼린저밴드 표준편차 배수

    "MACD_FAST":         12,
    "MACD_SLOW":         26,
    "MACD_SIGNAL":       9,

    "VOL_K":             0.5,     # 변동성 돌파 K값 (0.3~0.7 권장)

    # 리스크 관리
    "ORDER_RATIO":       0.3,     # 보유 원화의 최대 몇 % 를 1회 매수에 사용
    "STOP_LOSS_PCT":     0.03,    # 매수가 대비 -3% 손절
    "TAKE_PROFIT_PCT":   0.05,    # 매수가 대비 +5% 익절

    # 봇 루프 주기 (초)
    "LOOP_INTERVAL":     60,

    # 캔들 개수 (분봉)
    "CANDLE_COUNT":      100,
    "CANDLE_INTERVAL":   "1m",    # 1m / 3m / 5m / 15m / 1h / 4h / 1D
}

BASE_URL = "https://api.coinone.co.kr"


# ─────────────────────────────────────────
#  코인원 API 클라이언트
# ─────────────────────────────────────────
class CoinoneClient:
    def __init__(self, access_token: str, secret_key: str):
        if not access_token or not isinstance(access_token, str):
            raise ValueError("ACCESS_TOKEN이 비어 있거나 문자열이 아닙니다.")
        if secret_key is None or not isinstance(secret_key, str):
            raise ValueError("SECRET_KEY가 비어 있거나 문자열이 아닙니다.")
        self.access_token = access_token
        self.secret_key   = secret_key.encode()

    def _sign(self, payload: dict) -> dict:
        payload["access_token"] = self.access_token
        payload["nonce"]        = int(time.time() * 1000)
        try:
            encoded = base64.b64encode(json.dumps(payload).encode())
        except (TypeError, ValueError) as e:
            raise ValueError(f"API 서명용 payload 직렬화 실패: {e}") from e
        sig = hmac.new(self.secret_key, encoded, hashlib.sha512).hexdigest()
        return {
            "X-COINONE-PAYLOAD":   encoded.decode(),
            "X-COINONE-SIGNATURE": sig,
            "Content-Type":        "application/json",
        }

    # ── Public API ──────────────────────────
    def get_candles(self, symbol: str, interval: str = "1m", count: int = 100) -> list:
        """캔들(OHLCV) 데이터 조회"""
        url = f"{BASE_URL}/public/v2/chart/{symbol.upper()}/KRW"
        params = {"interval": interval, "count": count}
        try:
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            log.error("캔들 API 요청 실패: %s", e)
            raise RuntimeError(f"캔들 조회 실패: {e}") from e
        except json.JSONDecodeError as e:
            log.error("캔들 API 응답 JSON 파싱 실패: %s", e)
            raise RuntimeError(f"캔들 응답 파싱 실패: {e}") from e
        return data.get("chart", [])

    def get_orderbook(self, symbol: str) -> dict:
        url = f"{BASE_URL}/public/v2/orderbook/{symbol.upper()}/KRW"
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.error("호가창 API 요청 실패: %s", e)
            raise RuntimeError(f"호가창 조회 실패: {e}") from e
        except json.JSONDecodeError as e:
            log.error("호가창 API 응답 JSON 파싱 실패: %s", e)
            raise RuntimeError(f"호가창 응답 파싱 실패: {e}") from e

    def get_ticker(self, symbol: str) -> dict:
        url = f"{BASE_URL}/public/v2/ticker_new/{symbol.upper()}/KRW"
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.error("티커 API 요청 실패: %s", e)
            raise RuntimeError(f"시세 조회 실패: {e}") from e
        except json.JSONDecodeError as e:
            log.error("티커 API 응답 JSON 파싱 실패: %s", e)
            raise RuntimeError(f"시세 응답 파싱 실패: {e}") from e

    # ── Private API ─────────────────────────
    def get_balance(self) -> dict:
        payload = {"request_type": "BALANCE"}
        headers = self._sign(payload)
        r = requests.post(f"{BASE_URL}/v2.1/account/balance/all",
                          data=payload["nonce"], headers=headers, timeout=10)
        # V2.1 방식: payload는 base64로 header에 전달, body 불필요
        # 실제로는 아래 방식 사용
        return self._private_post("/v2.1/account/balance/all", {})

    def _private_post(self, path: str, payload: dict) -> dict:
        payload["access_token"] = self.access_token
        payload["nonce"]        = int(time.time() * 1000)
        try:
            raw = json.dumps(payload, separators=(",", ":"))
        except (TypeError, ValueError) as e:
            raise ValueError(f"Private API payload 직렬화 실패: {e}") from e
        encoded = base64.b64encode(raw.encode()).decode()
        sig     = hmac.new(self.secret_key,
                           encoded.encode(), hashlib.sha512).hexdigest()
        headers = {
            "X-COINONE-PAYLOAD":   encoded,
            "X-COINONE-SIGNATURE": sig,
            "Content-Type":        "application/json",
        }
        try:
            r = requests.post(f"{BASE_URL}{path}",
                              data=raw, headers=headers, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.error("Private API 요청 실패 path=%s: %s", path, e)
            raise RuntimeError(f"Private API 실패 ({path}): {e}") from e
        except json.JSONDecodeError as e:
            log.error("Private API 응답 JSON 파싱 실패: %s", e)
            raise RuntimeError(f"Private API 응답 파싱 실패: {e}") from e

    def get_balance_v2(self) -> dict:
        return self._private_post("/v2/account/balance", {})

    def place_order(self, symbol: str, side: str,
                    qty: float, price: Optional[float] = None,
                    order_type: str = "LIMIT") -> dict:
        """
        side: 'BUY' or 'SELL'
        order_type: 'LIMIT' or 'MARKET'
        """
        payload = {
            "quote_currency": "KRW",
            "target_currency": symbol.upper(),
            "type": order_type,
            "side": side,
            "qty": str(qty),
        }
        if order_type == "LIMIT" and price is not None:
            payload["price"] = str(int(price))

        return self._private_post("/v2.1/order", payload)

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        payload = {
            "order_id": order_id,
            "quote_currency": "KRW",
            "target_currency": symbol.upper(),
        }
        return self._private_post("/v2.1/order/cancel", payload)

    def get_open_orders(self, symbol: str) -> list:
        payload = {
            "quote_currency": "KRW",
            "target_currency": symbol.upper(),
        }
        res = self._private_post("/v2.1/order/open_orders", payload)
        return res.get("open_orders", [])


# ─────────────────────────────────────────
#  기술적 지표 계산
# ─────────────────────────────────────────
class Indicators:

    @staticmethod
    def to_df(candles: list) -> pd.DataFrame:
        if candles is None or not isinstance(candles, list):
            raise ValueError("캔들 데이터가 리스트가 아니거나 None입니다.")
        if len(candles) == 0:
            raise ValueError("캔들 데이터가 비어 있습니다.")
        try:
            df = pd.DataFrame(
                candles,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(f"캔들 DataFrame 생성 실패 (형식 오류): {e}") from e
        for c in ["open", "high", "low", "close", "volume"]:
            try:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            except (KeyError, TypeError) as e:
                raise ValueError(f"캔들 컬럼 변환 실패 ({c}): {e}") from e
        if df["timestamp"].isna().all() or df["close"].isna().all():
            raise ValueError("캔들 필수 컬럼(timestamp, close)에 유효한 값이 없습니다.")
        df.sort_values("timestamp", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    @staticmethod
    def rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def macd(close: pd.Series, fast=12, slow=26, signal=9):
        ema_fast   = close.ewm(span=fast,   adjust=False).mean()
        ema_slow   = close.ewm(span=slow,   adjust=False).mean()
        macd_line  = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        hist       = macd_line - signal_line
        return macd_line, signal_line, hist

    @staticmethod
    def bollinger(close: pd.Series, period=20, std_mult=2.0):
        ma   = close.rolling(period).mean()
        std  = close.rolling(period).std()
        upper = ma + std_mult * std
        lower = ma - std_mult * std
        return upper, ma, lower

    @staticmethod
    def volatility_breakout_target(prev_high: float, prev_low: float,
                                    open_price: float, k: float = 0.5) -> float:
        """변동성 돌파 목표가: 오늘 시가 + (전일 고-저 범위 × K)"""
        return open_price + (prev_high - prev_low) * k


# ─────────────────────────────────────────
#  전략 엔진 (복합 신호)
# ─────────────────────────────────────────
class Strategy:
    """
    매수 신호: 아래 조건 중 3개 이상 충족
      1. MA 골든크로스 (단기 > 장기)
      2. RSI < oversold (과매도)
      3. 현재가 > 변동성 돌파 목표가
      4. MACD 히스토그램 양전환 (직전 음수 → 현재 양수)
      5. 현재가 볼린저밴드 하단 접촉 이후 반등

    매도 신호: 아래 조건 중 2개 이상 충족
      1. MA 데드크로스 (단기 < 장기)
      2. RSI > overbought (과매수)
      3. MACD 히스토그램 음전환
      4. 현재가 볼린저밴드 상단 돌파 후 되돌림
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def evaluate(self, df: pd.DataFrame) -> str:
        """'BUY' / 'SELL' / 'HOLD' 반환"""
        if df is None or len(df) < 2:
            raise ValueError("전략 평가를 위해 최소 2개 이상의 캔들 행이 필요합니다.")
        required_keys = (
            "RSI_PERIOD", "MA_SHORT", "MA_LONG", "MACD_FAST", "MACD_SLOW",
            "MACD_SIGNAL", "BB_PERIOD", "BB_STD", "VOL_K",
            "RSI_OVERSOLD", "RSI_OVERBOUGHT",
        )
        for k in required_keys:
            if k not in self.cfg:
                raise KeyError(f"전략 설정에 필수 키가 없습니다: {k}")
        cfg = self.cfg
        c   = df["close"]

        rsi_s         = Indicators.rsi(c, cfg["RSI_PERIOD"])
        ma_short      = c.ewm(span=cfg["MA_SHORT"],  adjust=False).mean()
        ma_long       = c.ewm(span=cfg["MA_LONG"],   adjust=False).mean()
        macd, sig, hist = Indicators.macd(c, cfg["MACD_FAST"], cfg["MACD_SLOW"], cfg["MACD_SIGNAL"])
        bb_upper, bb_mid, bb_lower = Indicators.bollinger(c, cfg["BB_PERIOD"], cfg["BB_STD"])

        last  = df.iloc[-1]
        prev  = df.iloc[-2]
        price = last["close"]

        vol_target = Indicators.volatility_breakout_target(
            prev["high"], prev["low"], last["open"], cfg["VOL_K"]
        )

        # ── 매수 신호 점수 ──────────────────────
        buy_score = 0
        buy_reasons = []

        if ma_short.iloc[-1] > ma_long.iloc[-1] and ma_short.iloc[-2] <= ma_long.iloc[-2]:
            buy_score += 1
            buy_reasons.append("MA 골든크로스")

        if rsi_s.iloc[-1] < cfg["RSI_OVERSOLD"]:
            buy_score += 1
            buy_reasons.append(f"RSI 과매도({rsi_s.iloc[-1]:.1f})")

        if price > vol_target:
            buy_score += 1
            buy_reasons.append(f"변동성돌파(목표가:{vol_target:,.0f})")

        if hist.iloc[-2] < 0 < hist.iloc[-1]:
            buy_score += 1
            buy_reasons.append("MACD 히스토그램 양전환")

        if prev["close"] <= bb_lower.iloc[-2] and price > bb_lower.iloc[-1]:
            buy_score += 1
            buy_reasons.append("볼린저밴드 하단 반등")

        # ── 매도 신호 점수 ──────────────────────
        sell_score = 0
        sell_reasons = []

        if ma_short.iloc[-1] < ma_long.iloc[-1] and ma_short.iloc[-2] >= ma_long.iloc[-2]:
            sell_score += 1
            sell_reasons.append("MA 데드크로스")

        if rsi_s.iloc[-1] > cfg["RSI_OVERBOUGHT"]:
            sell_score += 1
            sell_reasons.append(f"RSI 과매수({rsi_s.iloc[-1]:.1f})")

        if hist.iloc[-2] > 0 > hist.iloc[-1]:
            sell_score += 1
            sell_reasons.append("MACD 히스토그램 음전환")

        if prev["close"] >= bb_upper.iloc[-2] and price < bb_upper.iloc[-1]:
            sell_score += 1
            sell_reasons.append("볼린저밴드 상단 되돌림")

        log.info(f"매수점수={buy_score}/5 {buy_reasons} | 매도점수={sell_score}/4 {sell_reasons}")

        if buy_score >= 3:
            return "BUY"
        if sell_score >= 2:
            return "SELL"
        return "HOLD"


# ─────────────────────────────────────────
#  리스크 관리 / 포지션 추적
# ─────────────────────────────────────────
class RiskManager:
    def __init__(self, cfg: dict):
        self.cfg         = cfg
        self.entry_price : Optional[float] = None  # 매수 진입가
        self.holding_qty : float = 0.0

    def set_position(self, price: float, qty: float):
        self.entry_price = price
        self.holding_qty = qty
        log.info(f"포지션 진입: {price:,.0f}원 × {qty} = {price*qty:,.0f}원")

    def clear_position(self):
        self.entry_price = None
        self.holding_qty = 0.0

    def should_stop_loss(self, current_price: float) -> bool:
        if self.entry_price is None or self.entry_price <= 0:
            return False
        loss_rate = (current_price - self.entry_price) / self.entry_price
        return loss_rate <= -self.cfg["STOP_LOSS_PCT"]

    def should_take_profit(self, current_price: float) -> bool:
        if self.entry_price is None or self.entry_price <= 0:
            return False
        profit_rate = (current_price - self.entry_price) / self.entry_price
        return profit_rate >= self.cfg["TAKE_PROFIT_PCT"]

    def calc_buy_qty(self, balance_krw: float, current_price: float) -> float:
        """매수 수량 계산 (원화 잔고의 ORDER_RATIO 비율)"""
        if current_price is None or current_price <= 0:
            raise ValueError("current_price는 0보다 큰 값이어야 합니다.")
        budget = balance_krw * self.cfg["ORDER_RATIO"]
        qty    = budget / current_price
        return round(qty, 6)


# ─────────────────────────────────────────
#  메인 봇
# ─────────────────────────────────────────
class TradingBot:
    def __init__(self, cfg: dict):
        self.cfg      = cfg
        self.client   = CoinoneClient(cfg["ACCESS_TOKEN"], cfg["SECRET_KEY"])
        self.strategy = Strategy(cfg)
        self.risk     = RiskManager(cfg)
        self.symbol   = cfg["SYMBOL"]

    def get_price(self) -> float:
        try:
            ticker = self.client.get_ticker(self.symbol)
        except (RuntimeError, requests.RequestException) as e:
            raise RuntimeError(f"시세 조회 실패: {e}") from e
        if not isinstance(ticker, dict):
            raise ValueError("시세 응답 형식이 올바르지 않습니다.")
        for t in ticker.get("tickers", []):
            if not isinstance(t, dict):
                continue
            if t.get("target_currency", "").upper() == self.symbol.upper():
                last = t.get("last")
                if last is None:
                    raise ValueError(f"시세 필드 없음 (target_currency={self.symbol})")
                try:
                    return float(last)
                except (TypeError, ValueError) as e:
                    raise ValueError(f"시세 값 변환 실패 (last={last!r}): {e}") from e
        raise ValueError(f"시세 조회 실패: 해당 코인 없음 (symbol={self.symbol})")

    def get_krw_balance(self) -> float:
        try:
            res = self.client.get_balance_v2()
        except (RuntimeError, requests.RequestException) as e:
            raise RuntimeError(f"원화 잔고 조회 실패: {e}") from e
        if not isinstance(res, dict):
            raise ValueError("잔고 응답 형식이 올바르지 않습니다.")
        krw = res.get("krw")
        if not isinstance(krw, dict):
            return 0.0
        try:
            return float(krw.get("avail", 0))
        except (TypeError, ValueError):
            return 0.0

    def get_coin_balance(self) -> float:
        try:
            res = self.client.get_balance_v2()
        except (RuntimeError, requests.RequestException) as e:
            raise RuntimeError(f"코인 잔고 조회 실패: {e}") from e
        if not isinstance(res, dict):
            raise ValueError("잔고 응답 형식이 올바르지 않습니다.")
        # API는 대문자 통화 코드를 반환할 수 있음
        coin_balance = res.get(self.symbol) or res.get(self.symbol.upper())
        if not isinstance(coin_balance, dict):
            return 0.0
        try:
            return float(coin_balance.get("avail", 0))
        except (TypeError, ValueError):
            return 0.0

    def run(self):
        log.info("=" * 50)
        log.info(f"코인원 자동매매 봇 시작 | 대상: {self.symbol.upper()}/KRW")
        log.info("=" * 50)

        while True:
            try:
                self._loop()
            except KeyboardInterrupt:
                log.info("봇 종료 (사용자 중단)")
                break
            except Exception as e:
                log.error(f"루프 오류: {e}", exc_info=True)

            time.sleep(self.cfg["LOOP_INTERVAL"])

    def _loop(self):
        now = datetime.now().strftime("%H:%M:%S")

        # 1. 캔들 데이터 조회 및 지표 계산
        candles = self.client.get_candles(
            self.symbol, self.cfg["CANDLE_INTERVAL"], self.cfg["CANDLE_COUNT"]
        )
        if len(candles) < self.cfg["MA_LONG"] + 5:
            log.warning("캔들 데이터 부족, 대기 중...")
            return

        df = Indicators.to_df(candles)
        if len(df) < 2:
            log.warning("캔들 변환 후 행 수 부족, 대기 중...")
            return

        # 2. 현재가
        price = float(df.iloc[-1]["close"])
        log.info(f"[{now}] {self.symbol.upper()} 현재가: {price:,.0f} KRW")

        # 3. 손절/익절 체크 (포지션 보유 중일 때)
        if self.risk.entry_price is not None and self.risk.entry_price > 0:
            if self.risk.should_stop_loss(price):
                log.warning(f"⛔ 손절 실행! 진입가:{self.risk.entry_price:,.0f} 현재:{price:,.0f}")
                self._sell_all(price, reason="손절")
                return
            if self.risk.should_take_profit(price):
                log.info(f"✅ 익절 실행! 진입가:{self.risk.entry_price:,.0f} 현재:{price:,.0f}")
                self._sell_all(price, reason="익절")
                return

        # 4. 전략 신호 평가
        signal = self.strategy.evaluate(df)
        log.info(f"전략 신호: {signal}")

        # 5. 매매 실행
        if signal == "BUY" and self.risk.entry_price is None:
            self._buy(price)
        elif signal == "SELL" and self.risk.holding_qty > 0:
            self._sell_all(price, reason="전략 신호")

    def _buy(self, price: float):
        try:
            krw = self.get_krw_balance()
            qty = self.risk.calc_buy_qty(krw, price)

            if qty * price < 1000:  # 코인원 최소 주문 금액 체크
                log.warning(f"잔고 부족: {krw:,.0f} KRW")
                return

            # 지정가 매수 (현재가 기준 0.05% 위 → 빠른 체결)
            limit_price = round(price * 1.0005)
            log.info(f"📈 매수 주문: {qty} {self.symbol.upper()} @ {limit_price:,.0f} KRW")

            # ⚠️  실제 주문 실행 시 아래 주석을 해제하세요
            res = self.client.place_order(self.symbol, "BUY", qty, limit_price)
            log.info(f"주문 결과: {res}")

            # log.info("[시뮬레이션] 실제 주문은 코드 주석 해제 후 실행")
            self.risk.set_position(price, qty)

        except Exception as e:
            log.error(f"매수 실패: {e}")

    def _sell_all(self, price: float, reason: str = ""):
        try:
            qty = self.get_coin_balance()
            if qty <= 0:
                qty = self.risk.holding_qty
            if qty <= 0:
                log.warning("매도할 코인 없음")
                self.risk.clear_position()
                return

            limit_price = round(price * 0.9995)
            log.info(f"📉 매도 주문({reason}): {qty} {self.symbol.upper()} @ {limit_price:,.0f} KRW")

            # ⚠️  실제 주문 실행 시 아래 주석을 해제하세요
            res = self.client.place_order(self.symbol, "SELL", qty, limit_price)
            log.info(f"주문 결과: {res}")

            # log.info("[시뮬레이션] 실제 주문은 코드 주석 해제 후 실행")
            if self.risk.entry_price and self.risk.entry_price > 0:
                pnl = (price - self.risk.entry_price) * qty
                pnl_pct = (price - self.risk.entry_price) / self.risk.entry_price * 100
                log.info(f"손익: {pnl:+,.0f} KRW ({pnl_pct:+.2f}%)")
            self.risk.clear_position()

        except Exception as e:
            log.error(f"매도 실패: {e}")


# ─────────────────────────────────────────
#  엔트리포인트
# ─────────────────────────────────────────
def _validate_config(cfg: dict) -> None:
    """시작 전 필수 설정 검증."""
    if not cfg.get("ACCESS_TOKEN") or cfg.get("ACCESS_TOKEN") == "여기에_액세스_토큰":
        raise ValueError(
            "COINONE_ACCESS_TOKEN이 설정되지 않았습니다. .env 또는 환경변수를 확인하세요."
        )
    if not cfg.get("SECRET_KEY") or cfg.get("SECRET_KEY") == "여기에_시크릿_키":
        raise ValueError(
            "COINONE_SECRET_KEY가 설정되지 않았습니다. .env 또는 환경변수를 확인하세요."
        )
    if not isinstance(cfg.get("SECRET_KEY"), str):
        raise ValueError("SECRET_KEY는 문자열이어야 합니다.")


if __name__ == "__main__":
    try:
        _validate_config(CONFIG)
        bot = TradingBot(CONFIG)
        bot.run()
    except ValueError as e:
        log.error("설정 오류: %s", e)
        sys.exit(1)
    except Exception as e:
        log.error("봇 시작 실패: %s", e, exc_info=True)
        sys.exit(1)
