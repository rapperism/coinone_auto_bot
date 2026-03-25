"""
코인원 자동매매 봇
전략: 이동평균(MA), RSI, 변동성 돌파, MACD, 볼린저밴드 복합 전략
"""

import time
import uuid
import hmac
import math
import hashlib
import base64
import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Optional

import requests
import numpy as np
import pandas as pd
from dotenv import load_dotenv

# ─────────────────────────────────────────
#  로깅 설정 (일 단위 로테이션, 30일 초과 백업 자동 삭제)
# ─────────────────────────────────────────
_log_dir = "logs"
os.makedirs(_log_dir, exist_ok=True)
_log_fmt = "%(asctime)s [%(levelname)s] %(message)s"
_file_handler = logging.handlers.TimedRotatingFileHandler(
    os.path.join(_log_dir, "bot.log"),
    when="midnight",      # 자정 기준 일 단위
    interval=1,
    backupCount=30,       # 30일 지난 백업 자동 삭제
    encoding="utf-8",
)
_file_handler.suffix = "%Y-%m-%d"
logging.basicConfig(
    level=logging.INFO,
    format=_log_fmt,
    handlers=[
        logging.StreamHandler(sys.stdout),
        _file_handler,
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
    "VOL_BREAKOUT_USE_DAILY": True,  # True면 변동성 돌파 목표가를 일봉 전일 고·저 + 당일 시가로 계산 (클래식 전략)

    # 신호 완화: True면 골든/데드크로스·MACD 전환 "순간"뿐 아니라 "단기>장기 유지", "히스토그램 양수 유지"도 1점
    "RELAX_CROSS_SIGNALS": True,

    # 리스크 관리
    "ORDER_RATIO":       0.3,     # 보유 원화의 최대 몇 % 를 1회 매수에 사용
    "USE_STOP_LOSS":     False,   # False면 손절 미사용(소액·수수료만 깎이는 손절 회피 등)
    "STOP_LOSS_PCT":     0.03,    # USE_STOP_LOSS True일 때만: 매수가 대비 -3% 손절
    "TAKE_PROFIT_PCT":   0.05,    # 매수가 대비 +5% 익절
    "TAKER_FEE_PCT":     0.02,    # 코인원 API 체결 수수료 0.02% (매수·매도 각각). 수수료 감안 손익·손실 매도 보류에 사용

    # 포지션 보유 중 신호가 BUY가 아니고(HOLD/SELL), 현재가가 수수료 손익분기 이상이면 청산.
    # BUY만 뜨다가 가격이 분기 아래로 내려온 뒤 SELL이 나오면 매도 보류에 걸리는 구간을 줄임.
    "EXIT_WHEN_NOT_BUY_ABOVE_BE": True,
    # True면 전략 SELL만 손익분기 미만에서도 매도 시도(실제 손실 가능). False 권장.
    "STRATEGY_SELL_ALLOW_BELOW_BREAKEVEN": False,

    # 봇 루프 주기 (초)
    "LOOP_INTERVAL":     60,

    # 캔들 개수 (분봉)
    "CANDLE_COUNT":      100,
    "CANDLE_INTERVAL":   "1m",    # 1m / 3m / 5m / 15m / 1h / 4h / 1D
}

BASE_URL = "https://api.coinone.co.kr"

# 공개 GET: SSL/연결 끊김 등 일시 오류 시 재시도 횟수·간격
PUBLIC_HTTP_RETRIES = 3
PUBLIC_HTTP_BACKOFF_SEC = 0.45
_TRANSIENT_HTTP_CODES = frozenset({429, 502, 503, 504})


class TransientAPIError(Exception):
    """SSL·연결·타임아웃·일부 5xx 등 일시적 장애. 메인 루프가 주기적으로 재시도."""


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
        self._range_price_units_cache: dict[str, list] = {}
        self._market_constraints_cache: dict[str, dict] = {}

    def _public_get(
        self,
        api_label: str,
        url: str,
        *,
        params: Optional[dict] = None,
        timeout: float = 10,
    ):
        """공개 GET. 일시적 네트워크/SSL/5xx(일부)는 재시도 후 TransientAPIError."""
        last: Optional[BaseException] = None
        for attempt in range(PUBLIC_HTTP_RETRIES):
            try:
                r = requests.get(url, params=params, timeout=timeout)
                r.raise_for_status()
                return r
            except (
                requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ChunkedEncodingError,
            ) as e:
                last = e
                if attempt + 1 < PUBLIC_HTTP_RETRIES:
                    time.sleep(PUBLIC_HTTP_BACKOFF_SEC * (2**attempt))
                    continue
                break
            except requests.exceptions.HTTPError as e:
                last = e
                code = e.response.status_code if e.response is not None else 0
                if code in _TRANSIENT_HTTP_CODES and attempt + 1 < PUBLIC_HTTP_RETRIES:
                    time.sleep(PUBLIC_HTTP_BACKOFF_SEC * (2**attempt))
                    continue
                log.error("%s HTTP 오류: %s", api_label, e)
                raise RuntimeError(f"{api_label} 조회 실패: {e}") from e
            except requests.exceptions.RequestException as e:
                log.error("%s 요청 실패: %s", api_label, e)
                raise RuntimeError(f"{api_label} 조회 실패: {e}") from e
        hint = type(last).__name__ if last else "Unknown"
        raise TransientAPIError(
            f"{api_label} {PUBLIC_HTTP_RETRIES}회 재시도 후 실패 ({hint})"
        ) from last

    def _sign(self, payload: dict) -> dict:
        payload["access_token"] = self.access_token
        payload["nonce"]        = str(uuid.uuid4())
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
        """캔들(OHLCV) 데이터 조회. API 경로: quote_currency/target_currency (문서 기준)."""
        # 코인원 문서: /public/v2/chart/{quote_currency}/{target_currency} (예: KRW/BTC)
        url = f"{BASE_URL}/public/v2/chart/KRW/{symbol.upper()}"
        params = {"interval": interval, "size": min(max(1, count), 500)}  # API는 size, 1~500
        try:
            r = self._public_get("캔들", url, params=params)
            data = r.json()
        except TransientAPIError:
            raise
        except json.JSONDecodeError as e:
            log.error("캔들 API 응답 JSON 파싱 실패: %s", e)
            raise RuntimeError(f"캔들 응답 파싱 실패: {e}") from e
        chart = data.get("chart", [])
        if data.get("result") == "error" and not chart:
            log.warning("캔들 API result=error (경로/파라미터 확인): %s", data.get("error_code", data))
        # API는 target_volume/quote_volume 반환 → volume 컬럼으로 통일
        for c in chart:
            if "volume" not in c and "target_volume" in c:
                c["volume"] = c["target_volume"]
        return chart

    def get_orderbook(self, symbol: str) -> dict:
        # 코인원 문서: orderbook/{quote_currency}/{target_currency} → KRW/BTC
        url = f"{BASE_URL}/public/v2/orderbook/KRW/{symbol.upper()}"
        try:
            r = self._public_get("호가창", url)
            return r.json()
        except TransientAPIError:
            raise
        except json.JSONDecodeError as e:
            log.error("호가창 API 응답 JSON 파싱 실패: %s", e)
            raise RuntimeError(f"호가창 응답 파싱 실패: {e}") from e

    def get_ticker(self, symbol: str) -> dict:
        url = f"{BASE_URL}/public/v2/ticker_new/{symbol.upper()}/KRW"
        try:
            r = self._public_get("티커", url)
            return r.json()
        except TransientAPIError:
            raise
        except json.JSONDecodeError as e:
            log.error("티커 API 응답 JSON 파싱 실패: %s", e)
            raise RuntimeError(f"시세 응답 파싱 실패: {e}") from e

    def get_range_price_units(self, symbol: str) -> list:
        """KRW 마켓 호가 단위 구간 (오류 310 방지). GET /public/v2/range_units/KRW/{symbol}"""
        sym = symbol.upper()
        if sym in self._range_price_units_cache:
            return self._range_price_units_cache[sym]
        url = f"{BASE_URL}/public/v2/range_units/KRW/{sym}"
        try:
            r = self._public_get("호가 단위", url)
            data = r.json()
        except TransientAPIError:
            raise
        except json.JSONDecodeError as e:
            log.error("호가 단위 API JSON 파싱 실패: %s", e)
            raise RuntimeError(f"호가 단위 응답 파싱 실패: {e}") from e
        if data.get("result") != "success":
            raise RuntimeError(f"호가 단위 API 오류: {data}")
        rows = data.get("range_price_units") or []
        if not rows:
            raise RuntimeError("range_price_units 가 비어 있습니다.")
        self._range_price_units_cache[sym] = rows
        return rows

    @staticmethod
    def _price_unit_for_krw(price: float, rows: list) -> float:
        p = float(price)
        for row in rows:
            rmin = float(row["range_min"])
            rmax = float(row["next_range_min"])
            u = float(row["price_unit"])
            if rmin <= p < rmax:
                return u
        return float(rows[-1]["price_unit"])

    @staticmethod
    def _snap_krw_limit_price(price: float, side: str, unit: float) -> float:
        if unit <= 0:
            raise ValueError("price_unit 이 0 이하입니다.")
        q = float(price) / unit
        if side.upper() == "BUY":
            q = math.ceil(q - 1e-12)
        else:
            q = math.floor(q + 1e-12)
        return q * unit

    @staticmethod
    def _format_limit_price_str(snapped: float, unit: float) -> str:
        if unit >= 1:
            return str(int(round(snapped)))
        text = f"{snapped:.10f}".rstrip("0").rstrip(".")
        return text if text else "0"

    def get_market_constraints(self, symbol: str) -> dict:
        """종목별 최소 주문금액·수량단위 (오류 306 방지). GET /public/v2/markets/KRW/{symbol}"""
        sym = symbol.upper()
        if sym in self._market_constraints_cache:
            return self._market_constraints_cache[sym]
        url = f"{BASE_URL}/public/v2/markets/KRW/{sym}"
        try:
            r = self._public_get("마켓 정보", url)
            data = r.json()
        except TransientAPIError:
            raise
        except json.JSONDecodeError as e:
            log.error("마켓 정보 API JSON 파싱 실패: %s", e)
            raise RuntimeError(f"마켓 정보 응답 파싱 실패: {e}") from e
        if data.get("result") != "success":
            raise RuntimeError(f"마켓 정보 API 오류: {data}")
        markets = data.get("markets") or []
        if not markets:
            raise RuntimeError("markets 배열이 비어 있습니다.")
        row = markets[0]
        mc = {
            "min_order_amount": float(row["min_order_amount"]),
            "min_qty": float(row["min_qty"]),
            "qty_unit": float(row["qty_unit"]),
            "qty_unit_str": str(row["qty_unit"]),
            "max_order_amount": float(row["max_order_amount"]),
        }
        self._market_constraints_cache[sym] = mc
        return mc

    def preview_snapped_limit_krw(self, symbol: str, side: str, raw_price: float) -> float:
        """주문 전 총액 검증용 지정가 스냅(실제 주문과 동일 규칙)."""
        rows = self.get_range_price_units(symbol)
        unit = self._price_unit_for_krw(float(raw_price), rows)
        return float(self._snap_krw_limit_price(float(raw_price), side, unit))

    @staticmethod
    def _floor_qty_string(qty: float, qty_unit_str: str) -> str:
        """qty_unit 배수로 내린 수량을 과학적 표기 없는 문자열로 (API qty 필드용)."""
        unit = Decimal(qty_unit_str)
        qd = Decimal(str(qty))
        if qd <= 0:
            return "0"
        n = (qd / unit).to_integral_value(rounding=ROUND_FLOOR)
        qn = n * unit
        if qn <= 0:
            return "0"
        s = format(qn, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s if s else "0"

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
        payload["nonce"]        = str(uuid.uuid4())
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
                    order_type: str = "LIMIT",
                    post_only: bool = False) -> dict:
        """
        side: 'BUY' or 'SELL'
        order_type: 'LIMIT' or 'MARKET'
        post_only: LIMIT 전용. True면 메이커만(즉시 체결 시 주문 거절). 기본 False.
        """
        mc = self.get_market_constraints(symbol)
        qty_s = self._floor_qty_string(qty, mc["qty_unit_str"])
        if qty_s == "0":
            raise ValueError(
                f"주문 수량이 qty_unit({mc['qty_unit_str']})으로 내림하면 0입니다."
            )
        payload = {
            "quote_currency": "KRW",
            "target_currency": symbol.upper(),
            "type": order_type,
            "side": side,
            "qty": qty_s,
        }
        if order_type == "LIMIT" and price is not None:
            rows = self.get_range_price_units(symbol)
            unit = self._price_unit_for_krw(float(price), rows)
            snapped = self._snap_krw_limit_price(float(price), side, unit)
            wanted = int(round(float(price)))
            if unit >= 1:
                got = int(round(snapped))
                if wanted != got:
                    log.info("지정가 호가단위 스냅: %s → %s KRW (unit=%s)", wanted, got, unit)
            payload["price"] = self._format_limit_price_str(snapped, unit)
            payload["post_only"] = post_only

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

    def evaluate(self, df: pd.DataFrame, daily_candles: Optional[list] = None) -> str:
        """'BUY' / 'SELL' / 'HOLD' 반환. daily_candles 있으면 변동성 돌파는 일봉(전일 고·저+당일 시가) 기준."""
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

        # 변동성 돌파 목표가: 일봉 옵션 사용 시 전일 고·저 + 당일 시가 (클래식)
        if cfg.get("VOL_BREAKOUT_USE_DAILY") and daily_candles and len(daily_candles) >= 2:
            # daily_candles: [ ..., 전일(마감), 당일(진행중) ]
            d_prev = daily_candles[-2]
            d_today = daily_candles[-1]
            prev_high = float(d_prev.get("high", d_prev[2] if isinstance(d_prev, (list, tuple)) else 0))
            prev_low  = float(d_prev.get("low",  d_prev[3] if isinstance(d_prev, (list, tuple)) else 0))
            today_open = float(d_today.get("open", d_today[1] if isinstance(d_today, (list, tuple)) else 0))
            vol_target = Indicators.volatility_breakout_target(
                prev_high, prev_low, today_open, cfg["VOL_K"]
            )
        else:
            vol_target = Indicators.volatility_breakout_target(
                prev["high"], prev["low"], last["open"], cfg["VOL_K"]
            )

        relax = cfg.get("RELAX_CROSS_SIGNALS", False)

        # ── 매수 신호 점수 ──────────────────────
        buy_score = 0
        buy_reasons = []

        if relax:
            if ma_short.iloc[-1] > ma_long.iloc[-1]:
                buy_score += 1
                buy_reasons.append("MA 단기>장기(상승추세)")
        else:
            if ma_short.iloc[-1] > ma_long.iloc[-1] and ma_short.iloc[-2] <= ma_long.iloc[-2]:
                buy_score += 1
                buy_reasons.append("MA 골든크로스")

        if rsi_s.iloc[-1] < cfg["RSI_OVERSOLD"]:
            buy_score += 1
            buy_reasons.append(f"RSI 과매도({rsi_s.iloc[-1]:.1f})")

        if price > vol_target:
            buy_score += 1
            buy_reasons.append(f"변동성돌파(목표가:{vol_target:,.0f})")

        if relax:
            if hist.iloc[-1] > 0:
                buy_score += 1
                buy_reasons.append("MACD 히스토그램 양수")
        else:
            if hist.iloc[-2] < 0 < hist.iloc[-1]:
                buy_score += 1
                buy_reasons.append("MACD 히스토그램 양전환")

        if prev["close"] <= bb_lower.iloc[-2] and price > bb_lower.iloc[-1]:
            buy_score += 1
            buy_reasons.append("볼린저밴드 하단 반등")

        # ── 매도 신호 점수 ──────────────────────
        sell_score = 0
        sell_reasons = []

        if relax:
            if ma_short.iloc[-1] < ma_long.iloc[-1]:
                sell_score += 1
                sell_reasons.append("MA 단기<장기(하락추세)")
        else:
            if ma_short.iloc[-1] < ma_long.iloc[-1] and ma_short.iloc[-2] >= ma_long.iloc[-2]:
                sell_score += 1
                sell_reasons.append("MA 데드크로스")

        if rsi_s.iloc[-1] > cfg["RSI_OVERBOUGHT"]:
            sell_score += 1
            sell_reasons.append(f"RSI 과매수({rsi_s.iloc[-1]:.1f})")

        if relax:
            if hist.iloc[-1] < 0:
                sell_score += 1
                sell_reasons.append("MACD 히스토그램 음수")
        else:
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

    def break_even_price(self, entry: float) -> float:
        """수수료 감안 시 손익 0이 되는 매도가. 이 가격 미만으로 매도하면 통장 기준 손실."""
        fee = self.cfg.get("TAKER_FEE_PCT", 0.02) / 100.0
        return entry * (1 + fee) / (1 - fee)

    def pnl_after_fee(self, entry: float, sell_price: float, qty: float) -> tuple[float, float]:
        """수수료 반영 손익(KRW)과 수익률(%) 반환."""
        fee = self.cfg.get("TAKER_FEE_PCT", 0.02) / 100.0
        cost = entry * qty * (1 + fee)
        proceeds = sell_price * qty * (1 - fee)
        pnl_krw = proceeds - cost
        pnl_pct = (pnl_krw / cost) * 100 if cost > 0 else 0.0
        return pnl_krw, pnl_pct

    def calc_buy_qty(
        self, balance_krw: float, current_price: float, qty_unit_str: str
    ) -> float:
        """매수 수량: 잔고×ORDER_RATIO 범위에서 qty_unit 배수로 내림 (거래소 규칙)."""
        if current_price is None or current_price <= 0:
            raise ValueError("current_price는 0보다 큰 값이어야 합니다.")
        budget = Decimal(str(balance_krw)) * Decimal(str(self.cfg["ORDER_RATIO"]))
        unit = Decimal(qty_unit_str)
        price = Decimal(str(current_price))
        raw = budget / price
        n = (raw / unit).to_integral_value(rounding=ROUND_FLOOR)
        return float(n * unit)


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
        """V2.1 잔고 API 사용. balances 배열에서 currency=KRW인 항목의 available 사용."""
        try:
            res = self.client.get_balance()
        except (RuntimeError, requests.RequestException) as e:
            raise RuntimeError(f"원화 잔고 조회 실패: {e}") from e
        if not isinstance(res, dict) or res.get("result") == "error":
            return 0.0
        for b in res.get("balances", []):
            if not isinstance(b, dict):
                continue
            if b.get("currency") == "KRW":
                try:
                    return float(b.get("available", 0))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def get_coin_balance(self) -> float:
        """V2.1 잔고 API 사용. balances 배열에서 해당 코인 항목의 available 사용."""
        try:
            res = self.client.get_balance()
        except (RuntimeError, requests.RequestException) as e:
            raise RuntimeError(f"코인 잔고 조회 실패: {e}") from e
        if not isinstance(res, dict) or res.get("result") == "error":
            return 0.0
        sym_upper = self.symbol.upper()
        for b in res.get("balances", []):
            if not isinstance(b, dict):
                continue
            if b.get("currency") == sym_upper:
                try:
                    return float(b.get("available", 0))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def get_currency_balance(self, symbol: str) -> Optional[dict]:
        """
        V2.1 잔고 한 줄. average_price=거래소 평단, total=가용+주문잠금.
        문서: available+limit 이 전체 잔고.
        """
        try:
            res = self.get_balance()
        except (RuntimeError, requests.RequestException) as e:
            log.warning("잔고 API 실패(get_currency_balance): %s", e)
            return None
        if not isinstance(res, dict) or res.get("result") != "success":
            return None
        sym = symbol.upper()
        for b in res.get("balances", []):
            if not isinstance(b, dict) or b.get("currency") != sym:
                continue
            try:
                av = float(b.get("available", 0) or 0)
                lm = float(b.get("limit", 0) or 0)
                ap = b.get("average_price")
                if ap is None or ap == "":
                    avg = 0.0
                else:
                    avg = float(ap)
            except (TypeError, ValueError):
                return None
            return {
                "available": av,
                "limit": lm,
                "total": av + lm,
                "average_price": avg,
            }
        return None

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
            except TransientAPIError as e:
                log.warning(
                    "일시적 API/네트워크 오류 — %s초 후 재시도 | %s",
                    self.cfg["LOOP_INTERVAL"],
                    e,
                )
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

        # 2b. 거래소 잔고·평단 동기화 (재시작 후에도 포지션 인식, 손익분기는 average_price 기준)
        self._sync_position_from_exchange(price)

        # 3. 손절/익절 체크 (포지션 보유 중일 때)
        if self.risk.entry_price is not None and self.risk.entry_price > 0:
            if self.cfg.get("USE_STOP_LOSS", False) and self.risk.should_stop_loss(price):
                log.warning(f"⛔ 손절 실행! 진입가:{self.risk.entry_price:,.0f} 현재:{price:,.0f}")
                self._sell_all(price, reason="손절")
                return
            if self.risk.should_take_profit(price):
                log.info(f"✅ 익절 실행! 진입가:{self.risk.entry_price:,.0f} 현재:{price:,.0f}")
                self._sell_all(price, reason="익절")
                return

        # 4. 전략 신호 평가 (변동성 돌파 일봉 사용 시 전일 고·저+당일 시가로 목표가 계산)
        daily_candles = None
        if self.cfg.get("VOL_BREAKOUT_USE_DAILY"):
            try:
                daily_candles = self.client.get_candles(self.symbol, "1D", 3)
            except Exception as e:
                log.warning("일봉 조회 실패(변동성 돌파는 분봉 기준으로 계산): %s", e)
        signal = self.strategy.evaluate(df, daily_candles=daily_candles)
        log.info(f"전략 신호: {signal}")

        # 5. 매매 실행 (보유는 거래소 수량 기준 — 메모리만으로는 재시작 시 중복 매수)
        in_pos = (
            self.risk.holding_qty > 0
            and self.risk.entry_price is not None
            and self.risk.entry_price > 0
        )

        if signal == "BUY" and not in_pos:
            self._buy(price)
        elif (
            self.cfg.get("EXIT_WHEN_NOT_BUY_ABOVE_BE", True)
            and in_pos
            and signal != "BUY"
            and price >= self.risk.break_even_price(self.risk.entry_price)
        ):
            self._sell_all(price, reason="약세·수수료분기 이상 청산")
        elif signal == "SELL" and in_pos:
            self._sell_all(price, reason="전략 신호")

    def _sync_position_from_exchange(self, mark_price: float) -> None:
        row = self.client.get_currency_balance(self.symbol)
        if row is None:
            return
        try:
            mc = self.client.get_market_constraints(self.symbol)
            min_q = float(mc["min_qty"])
        except (RuntimeError, KeyError, TypeError, ValueError):
            min_q = 1e-8
        total = row["total"]
        if total < min_q:
            if self.risk.entry_price is not None or self.risk.holding_qty > 0:
                self.risk.clear_position()
                log.info("거래소 코인 잔고 없음 → 포지션 추적 초기화")
            return

        old_ep = self.risk.entry_price
        old_h = self.risk.holding_qty
        avg = row["average_price"]
        if avg > 0:
            self.risk.entry_price = float(avg)
        elif self.risk.entry_price is None or self.risk.entry_price <= 0:
            self.risk.entry_price = float(mark_price)
            log.warning(
                "거래소 average_price 없음·0 — 손익분기는 현재가 근사 (%s원)",
                f"{mark_price:,.0f}",
            )
        self.risk.holding_qty = total

        ep_changed = old_ep is None or abs((old_ep or 0) - self.risk.entry_price) > 100
        qty_changed = abs(old_h - total) > 1e-10
        if ep_changed or qty_changed:
            log.info(
                "거래소 동기화 | 평균매수가 %s원 | 보유 %s BTC (가용 %s + 잠금 %s)",
                f"{self.risk.entry_price:,.0f}",
                f"{total}",
                f"{row['available']}",
                f"{row['limit']}",
            )

    def _buy(self, price: float):
        try:
            mc = self.client.get_market_constraints(self.symbol)
            min_q = float(mc["min_qty"])
            if self.risk.holding_qty >= min_q:
                log.info(
                    "이미 코인 보유 중(거래소 동기화 기준) — 추가 매수 생략",
                )
                return

            krw = self.get_krw_balance()
            qty = self.risk.calc_buy_qty(krw, price, mc["qty_unit_str"])
            min_krw = mc["min_order_amount"]

            limit_price = round(price * 1.0005)
            snapped = self.client.preview_snapped_limit_krw(
                self.symbol, "BUY", float(limit_price)
            )
            notional = qty * snapped
            if notional < min_krw:
                unit = Decimal(mc["qty_unit_str"])
                snapped_d = Decimal(str(snapped))
                min_amt = Decimal(str(min_krw))
                min_q = min_amt / snapped_d
                n = (min_q / unit).to_integral_value(rounding=ROUND_CEILING)
                qn = n * unit
                while qn * snapped_d < min_amt:
                    n += 1
                    qn = n * unit
                qty_up = float(qn)
                max_q = float(
                    (Decimal(str(krw)) / snapped_d / unit).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                    * unit
                )
                if qty_up <= max_q:
                    qty = qty_up
                    notional = qty * snapped
                    log.info(
                        "최소 주문 금액 %s원 충족을 위해 수량 조정 → %s BTC (총액 약 %s원)",
                        f"{min_krw:,.0f}",
                        self.client._floor_qty_string(qty, mc["qty_unit_str"]),
                        f"{notional:,.0f}",
                    )
                else:
                    log.warning(
                        "주문 총액이 거래소 최소 %s원 미만입니다 (현재 약 %s원, 잔고 %s원). "
                        "ORDER_RATIO·잔고를 늘리세요.",
                        f"{min_krw:,.0f}",
                        f"{notional:,.0f}",
                        f"{krw:,.0f}",
                    )
                    return

            if qty <= 0:
                log.warning(f"잔고 부족: {krw:,.0f} KRW")
                return

            # 지정가 매수 (현재가 기준 0.05% 위 → 빠른 체결)
            log.info(
                f"📈 매수 주문: {self.client._floor_qty_string(qty, mc['qty_unit_str'])} "
                f"{self.symbol.upper()} @ {limit_price:,.0f} KRW"
            )

            # ⚠️  실제 주문 실행 시 아래 주석을 해제하세요
            res = self.client.place_order(self.symbol, "BUY", qty, limit_price)
            log.info(f"주문 결과: {res}")

            if res.get("result") == "error":
                log.warning("매수 주문 실패: %s - %s", res.get("error_code"), res.get("error_msg"))
                return
            self.risk.set_position(price, qty)
            self._sync_position_from_exchange(price)

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

            # 수수료 감안 시 손실이면 매도 보류 (손절·익절·명시 옵션은 예외)
            if self.risk.entry_price and self.risk.entry_price > 0:
                be_price = self.risk.break_even_price(self.risk.entry_price)
                allow_below = (
                    reason == "전략 신호"
                    and self.cfg.get("STRATEGY_SELL_ALLOW_BELOW_BREAKEVEN", False)
                )
                force_risk = reason in ("손절", "익절")
                if (
                    price < be_price
                    and not force_risk
                    and not allow_below
                ):
                    log.warning(
                        "수수료 감안 시 손실이라 매도 보류 (현재가 %s < 손익분기 %s)",
                        f"{price:,.0f}", f"{be_price:,.0f}",
                    )
                    return

            limit_price = round(price * 0.9995)
            mc = self.client.get_market_constraints(self.symbol)
            snapped = self.client.preview_snapped_limit_krw(
                self.symbol, "SELL", float(limit_price)
            )
            qty_s = self.client._floor_qty_string(qty, mc["qty_unit_str"])
            if qty_s == "0":
                log.warning("매도 수량이 호가 단위로 내림하면 0입니다.")
                return
            qty_ord = float(Decimal(qty_s))
            sell_notional = qty_ord * snapped
            if sell_notional < mc["min_order_amount"]:
                log.warning(
                    "매도 총액 %s원 < 거래소 최소 %s원 — 체결 불가로 주문 생략.",
                    f"{sell_notional:,.0f}",
                    f"{mc['min_order_amount']:,.0f}",
                )
                return

            log.info(
                f"📉 매도 주문({reason}): {qty_s} {self.symbol.upper()} @ {limit_price:,.0f} KRW"
            )

            # ⚠️  실제 주문 실행 시 아래 주석을 해제하세요
            res = self.client.place_order(self.symbol, "SELL", qty_ord, limit_price)
            log.info(f"주문 결과: {res}")

            if res.get("result") == "error":
                log.warning("매도 주문 실패: %s - %s", res.get("error_code"), res.get("error_msg"))
                return
            if self.risk.entry_price and self.risk.entry_price > 0:
                pnl_krw, pnl_pct = self.risk.pnl_after_fee(
                    self.risk.entry_price, price, qty_ord
                )
                log.info(f"손익(수수료 반영): {pnl_krw:+,.0f} KRW ({pnl_pct:+.2f}%)")
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
