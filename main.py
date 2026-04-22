import yfinance as yf
import requests
import os
import pandas as pd

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

VIX_L1, VIX_L2, VIX_L3, VIX_L4 = 20, 28, 35, 40
VIX_SPIKE_THRESHOLD = 0.1

# ==========================================
# 데이터 수집
# ==========================================
def get_market_status():
    vix_hist = yf.Ticker("^VIX").history(period="2d")
    vix = round(vix_hist['Close'].iloc[-1], 2)
    prev_vix = round(vix_hist['Close'].iloc[-2], 2)
    vix_change = round((vix - prev_vix) / prev_vix, 3)

    spy_hist = yf.Ticker("SPY").history(period="300d")
    spy = round(spy_hist['Close'].iloc[-1], 2)
    ma200 = round(spy_hist['Close'].rolling(200).mean().iloc[-1], 2)

    tnx = round(yf.Ticker("^TNX").history(period="1d")['Close'].iloc[-1], 2)

    return vix, vix_change, spy, ma200, tnx

# ==========================================
# RSI
# ==========================================
def get_rsi():
    data = yf.Ticker("SPY").history(period="100d")
    delta = data['Close'].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi.iloc[-1], 1)

# ==========================================
# 낙폭
# ==========================================
def get_drawdown():
    data = yf.Ticker("SPY").history(period="1y")
    peak = data['Close'].max()
    current = data['Close'].iloc[-1]
    return round((current - peak) / peak * 100, 1)

# ==========================================
# 달러 인덱스
# ==========================================
def get_dxy():
    return round(yf.Ticker("DX-Y.NYB").history(period="1d")['Close'].iloc[-1], 2)

# ==========================================
# 해석 함수
# ==========================================
def get_vix_zone(vix):
    if vix >= 40:
        return "40+ (패닉)"
    elif vix >= 35:
        return "35~40 (극도위험)"
    elif vix >= 28:
        return "28~35 (경고)"
    elif vix >= 20:
        return "20~28 (주의)"
    else:
        return "0~20 (안정)"

def get_spy_status(spy, ma200):
    diff = round((spy - ma200) / ma200 * 100, 1)
    return f"{'🔴 하락추세' if spy < ma200 else '🟢 상승추세'} ({diff}%)"

def get_rate_status(tnx):
    if tnx >= 4.5:
        return "🚨 매우 높음"
    elif tnx >= 4.0:
        return "⚠️ 높음"
    elif tnx >= 3.0:
        return "🟡 보통"
    else:
        return "🟢 낮음"

def get_rsi_status(rsi):
    if rsi < 30:
        return "💎 과매도 (매수 기회)"
    elif rsi > 70:
        return "🔥 과열"
    else:
        return "중립"

def get_dd_status(dd):
    if dd <= -20:
        return "💎 베어마켓"
    elif dd <= -10:
        return "⚠️ 조정"
    elif dd <= -5:
        return "주의 조정"
    else:
        return "정상"

def get_dxy_status(dxy):
    if dxy > 105:
        return "🚨 달러 강세 (위험자산 압박)"
    elif dxy > 100:
        return "⚠️ 강세"
    else:
        return "🟢 안정"

def get_level(vix, vix_change, spy, ma200):
    is_bear = spy < ma200
    if vix >= 40:
        return 4
    elif vix >= 35 and is_bear:
        return 3
    elif vix >= 28 or is_bear or vix_change >= 0.1:
        return 2
    elif vix >= 20:
        return 1
    else:
        return 0

def get_action(level):
    return [
        "포지션 유지 / 분할매수 가능",
        "신규 매수 천천히",
        "비중 축소 검토",
        "현금 비중 확대",
        "분할매수 시작 (공포)"
    ][level]

# ==========================================
# 텔레그램
# ==========================================
def send(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.get(url, params={"chat_id": CHAT_ID, "text": msg})

# ==========================================
# 실행
# ==========================================
def run():
    vix, vix_change, spy, ma200, tnx = get_market_status()
    rsi = get_rsi()
    dd = get_drawdown()
    dxy = get_dxy()

    level = get_level(vix, vix_change, spy, ma200)

    msg = f"""
📊 시장 종합 리포트

🔥 단계: {level}단계
👉 전략: {get_action(level)}

[변동성]
VIX: {vix} (Δ {vix_change*100:.1f}%)
→ {get_vix_zone(vix)}

[추세]
SPY: {spy} / 200MA: {ma200}
→ {get_spy_status(spy, ma200)}

[금리]
10Y: {tnx}%
→ {get_rate_status(tnx)}

[타이밍]
RSI: {rsi}
→ {get_rsi_status(rsi)}

낙폭: {dd}%
→ {get_dd_status(dd)}

[환경]
달러: {dxy}
→ {get_dxy_status(dxy)}
"""

    if vix_change >= VIX_SPIKE_THRESHOLD:
        msg = "⚡ VIX 급등 감지!\n" + msg

    send(msg)

run()
