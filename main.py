import yfinance as yf
import requests
import os

# ==========================================
# 환경변수
# ==========================================
TELEGRAM_TOKEN = os.getenv("8603001067:AAEtlhd8z5osphG0s-pKkVWJUjol-PA7H9s")
CHAT_ID = os.getenv("8757371550")

# ==========================================
# 기준값
# ==========================================
VIX_L1, VIX_L2, VIX_L3, VIX_L4 = 20, 28, 35, 40
VIX_SPIKE_THRESHOLD = 0.1

# ==========================================
# 데이터 가져오기
# ==========================================
def get_market_status():
    vix_hist = yf.Ticker("^VIX").history(period="2d")
    vix = round(vix_hist['Close'].iloc[-1], 2)
    prev_vix = round(vix_hist['Close'].iloc[-2], 2)

    vix_change = round((vix - prev_vix) / prev_vix, 3)

    spy_hist = yf.Ticker("SPY").history(period="300d")
    spy_price = round(spy_hist['Close'].iloc[-1], 2)
    ma200 = round(spy_hist['Close'].rolling(200).mean().iloc[-1], 2)

    tnx = round(yf.Ticker("^TNX").history(period="1d")['Close'].iloc[-1], 2)

    is_bear = spy_price < ma200

    return vix, vix_change, spy_price, ma200, tnx, is_bear

# ==========================================
# 텔레그램
# ==========================================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.get(url, params={"chat_id": CHAT_ID, "text": msg})

# ==========================================
# 단계 판단
# ==========================================
def get_level(vix, vix_change, is_bear):
    if vix >= VIX_L4:
        return 4
    elif vix >= VIX_L3 and is_bear:
        return 3
    elif vix >= VIX_L2 or is_bear or vix_change >= VIX_SPIKE_THRESHOLD:
        return 2
    elif vix >= VIX_L1:
        return 1
    else:
        return 0

# ==========================================
# 실행 (1회)
# ==========================================
def run_once():
    vix, vix_change, spy, ma200, tnx, is_bear = get_market_status()
    level = get_level(vix, vix_change, is_bear)

    # 기본 정보
    info = f"""
📊 시장 상태
• VIX: {vix} (Δ {vix_change*100:.1f}%)
• SPY: {spy} / 200MA: {ma200}
• 금리: {tnx}%
• 추세: {'🔴 하락' if is_bear else '🟢 상승'}
"""

    # 단계별 메시지
    if level == 4:
        msg = "💎 [4단계: 역발상 구간] 패닉셀링 → 분할매수 검토\n"
    elif level == 3:
        msg = "🚨 [3단계: 극도 위험] 시장 붕괴 구간\n"
    elif level == 2:
        msg = "⚠️ [2단계: 경고] 변동성 확대 / 추세 이탈\n"
    elif level == 1:
        msg = "🔔 [1단계: 주의] 불안 신호 발생\n"
    else:
        msg = "✅ [안정] 시장 정상 상태\n"

    # 급등 별도 표시
    if vix_change >= VIX_SPIKE_THRESHOLD:
        msg = "⚡ VIX 급등 감지!\n" + msg

    send_telegram(msg + info)

# 실행
run_once()
