# 코인원 자동매매 봇

## 📦 설치

```bash
pip install -r requirements.txt
```

## 🔑 API 키 설정

1. 코인원 웹사이트 로그인
2. 하단 footer > Open API > 통합 API 관리 > 개인용 API
3. [새로운 키 발급] 클릭
4. 권한 선택: **잔고 조회 + 주문 권한** 체크
5. `.env.example` → `.env` 로 복사 후 키 입력

```bash
cp .env.example .env
# .env 파일 열어서 토큰/시크릿 입력
```

## ⚙️ 설정 변경 (bot.py 상단 CONFIG 딕셔너리)

| 항목 | 기본값 | 설명 |
|------|--------|------|
| SYMBOL | btc | 거래 코인 (btc/eth/xrp 등) |
| ORDER_RATIO | 0.3 | 원화 잔고의 30%씩 매수 |
| STOP_LOSS_PCT | 0.03 | 3% 손절 |
| TAKE_PROFIT_PCT | 0.05 | 5% 익절 |
| LOOP_INTERVAL | 60 | 60초마다 신호 체크 |
| CANDLE_INTERVAL | 1m | 1분봉 기준 |
| VOL_K | 0.5 | 변동성 돌파 K값 |

## 🚀 실행 (시뮬레이션 모드)

기본값은 **시뮬레이션 모드**입니다. 실제 주문이 나가지 않습니다.

```bash
python bot.py
```

## ⚠️ 실제 매매 활성화 방법

`bot.py`에서 `_buy()` 와 `_sell_all()` 메서드 내부의 주석을 해제하세요:

```python
# 주석 해제 전 (시뮬레이션)
# res = self.client.place_order(self.symbol, "BUY", qty, limit_price)

# 주석 해제 후 (실제 매매)
res = self.client.place_order(self.symbol, "BUY", qty, limit_price)
```

## 📊 전략 설명

### 복합 신호 방식 (5개 지표 조합)

**매수 신호** — 아래 중 3개 이상 충족 시 매수
1. 이동평균 골든크로스 (단기 EMA5 > 장기 EMA20)
2. RSI 과매도 (RSI < 30)
3. 변동성 돌파 (현재가 > 시가 + 전일범위×K)
4. MACD 히스토그램 양전환 (음→양)
5. 볼린저밴드 하단 반등

**매도 신호** — 아래 중 2개 이상 충족 시 매도
1. 이동평균 데드크로스 (단기 < 장기)
2. RSI 과매수 (RSI > 70)
3. MACD 히스토그램 음전환 (양→음)
4. 볼린저밴드 상단 되돌림

**리스크 관리 (전략 신호보다 우선)**
- 손절: 진입가 대비 -3% 시 즉시 매도
- 익절: 진입가 대비 +5% 시 즉시 매도

## ⚠️ 주의사항

- **투자 손실에 대한 책임은 전적으로 본인에게 있습니다**
- 반드시 시뮬레이션 모드로 충분히 검증 후 실 매매 전환하세요
- 코인원 API 수수료: Maker/Taker 각 0.02%
- PC가 켜져 있어야 봇이 동작합니다 (상시 구동 시 서버/VPS 권장)
- 소액으로 테스트 후 금액을 늘리세요
