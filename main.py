import yfinance as yf
import requests
import os

# ==========================================
# 설정
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.txt")

# ==========================================
# 안전 데이터 함수
# ==========================================
def safe_get_price(ticker, period="5d"):
    try:
        data = yf.Ticker(ticker).history(period=period)
        if data.empty:
            return None
        return round(data['Close'].iloc[-1], 2)
    except:
        return None

# ==========================================
# 데이터 수집
# ==========================================
def get_market_status():
    # VIX
    vix_hist = yf.Ticker("^VIX").history(period="5d")
    vix = round(vix_hist['Close'].iloc[-1], 2)
    prev_vix = round(vix_hist['Close'].iloc[-2], 2)
    vix_change = round((vix - prev_vix) / prev_vix, 3)

    # SPY
    spy_hist = yf.Ticker("SPY").history(period="300d")
    spy = round(spy_hist['Close'].iloc[-1], 2)
    ma200 = round(spy_hist['Close'].rolling(200).mean().iloc[-1], 2)

    # QQQ
    qqq_hist = yf.Ticker("QQQ").history(period="300d")
    qqq = round(qqq_hist['Close'].iloc[-1], 2)
    qqq_ma200 = round(qqq_hist['Close'].rolling(200).mean().iloc[-1], 2)

    # GOLD
    gld_hist = yf.Ticker("GLD").history(period="100d")
    gld = round(gld_hist['Close'].iloc[-1], 2)
    gld_ma50 = round(gld_hist['Close'].rolling(50).mean().iloc[-1], 2)

    # RSI
    delta = spy_hist['Close'].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - (100 / (1 + (gain / loss)))

    # 낙폭 (1년 기준)
    recent_peak = spy_hist['Close'][-252:].max()
    dd = (spy - recent_peak) / recent_peak * 100

    # 🔥 금리 (중립값 처리)
    tnx_raw = safe_get_price("^TNX")
    tnx = tnx_raw if tnx_raw else 4.0

    # 🔥 DXY (이중 방어 + 중립값)
    dxy_raw = safe_get_price("DX-Y.NYB")
    if not dxy_raw:
        dxy_raw = safe_get_price("DX=F")
    dxy = dxy_raw if dxy_raw else 100.0

    return (
        vix,
        vix_change,
        spy,
        ma200,
        tnx,
        qqq,
        qqq_ma200,
        gld,
        gld_ma50,
        round(rsi.iloc[-1], 1),
        round(dd, 1),
        dxy
    )

# ==========================================
# 환율
# ==========================================
def get_fx():
    try:
        data = yf.Ticker("KRW=X").history(period="3y")
        current = round(data['Close'].iloc[-1], 2)
        avg_1y = round(data['Close'][-252:].mean(), 2)
        avg_2y = round(data['Close'][-504:].mean(), 2)
        return current, avg_1y, avg_2y
    except:
        return None, None, None

def get_fx_action(current, avg_1y, avg_2y):
    if current is None:
        return "데이터 없음"

    diff_1y = (current - avg_1y) / avg_1y
    diff_2y = (current - avg_2y) / avg_2y

    if diff_1y >= 0.08 and diff_2y >= 0.10:
        return "🚨 환전 금지"
    elif diff_1y <= -0.05 and diff_2y <= -0.05:
        return "💎 적극 환전"
    elif diff_1y >= 0.04:
        return "⚠️ 환전 천천히"
    elif diff_1y <= -0.05:
        return "✅ 환전 기회"
    else:
        return "중립"

# ==========================================
# 점수 계산
# ==========================================
def calculate_score(vix, vix_change, spy, ma200, rsi, dd, tnx, dxy, qqq, qqq_ma200, gld, gld_ma50):
    score = 0

    if vix >= 40: score += 3
    elif vix >= 30: score += 2
    elif vix >= 20: score += 1

    if vix_change >= 0.1: score += 2

    if spy < ma200: score += 2
    if qqq < qqq_ma200: score += 1

    if rsi < 30: score += 1
    elif rsi > 70: score -= 2

    if dd <= -10: score += 2
    if tnx >= 4.5: score += 2
    if dxy >= 105: score += 2
    if gld > gld_ma50: score += 1

    return int(round(score))

def get_action(score):
    if score >= 9: return 4, "💎 공포", "매수 준비"
    elif score >= 6: return 3, "🛑 위험", "익절 확대"
    elif score >= 4: return 2, "⚠️ 경고", "익절 시작"
    elif score >= 2: return 1, "🟡 주의", "보유"
    else: return 0, "🔥 정상", "보유"

# ==========================================
# 상태 저장
# ==========================================
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return int(float(f.read().strip()))
    except:
        return None

def save_state(score):
    with open(STATE_FILE, "w") as f:
        f.write(str(int(score)))

# ==========================================
# 텔레그램
# ==========================================
def send(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.get(url, params={"chat_id": CHAT_ID, "text": msg})

# ==========================================
# 실행
# ==========================================
def main():
    vix, vix_c, spy, spy_m, tnx, qqq, qqq_m, gld, gld_m, rsi, dd, dxy = get_market_status()
    usd, usd_1y, usd_2y = get_fx()

    score = calculate_score(vix, vix_c, spy, spy_m, rsi, dd, tnx, dxy, qqq, qqq_m, gld, gld_m)
    level, status, strategy = get_action(score)

    fx_action = get_fx_action(usd, usd_1y, usd_2y)

    last_score = load_state()

    # 🔥 Cron 대응 (첫 실행 무시)
    if last_score is None:
        save_state(score)
        return

    # 🔥 패닉 감지
    crash = (score - last_score >= 3)
    panic = (vix >= 45 or crash)

    if score != last_score:

        panic_text = "💀 패닉 감지\n" if panic else ""

        # 표시용 값 (N/A 처리)
        tnx_display = tnx if tnx != 4.0 else "N/A"
        dxy_display = dxy if dxy != 100.0 else "N/A"

       msg = f"""
{panic_text}🤖 투자 리포트

━━━━━━━━━━━━━━━━━━
🔥 단계 {level} | 점수 {score}
📍 상태: {status}
🎯 전략: {strategy}

━━━━━━━━━━━━━━━━━━
💱 환율
💲 현재: {usd}
📊 1Y: {usd_1y} | 2Y: {usd_2y}
👉 {fx_action}

━━━━━━━━━━━━━━━━━━
📊 시장 핵심
📉 VIX: {vix} ({vix_c*100:+.1f}%)
📈 SPY: {spy} / 200MA {spy_m}
🚀 QQQ: {qqq} / 200MA {qqq_m}

━━━━━━━━━━━━━━━━━━
🧠 리스크 분석
💰 금리: {tnx_display}%
💵 달러: {dxy_display}
🥇 금: {gld}

━━━━━━━━━━━━━━━━━━
⏱ 타이밍
🔥 RSI: {rsi}
📉 낙폭: {dd}%

━━━━━━━━━━━━━━━━━━
"""

        send(msg)
        save_state(score)

if __name__ == "__main__":
    main()
