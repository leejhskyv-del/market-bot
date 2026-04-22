import yfinance as yf
import requests
import os
import pandas as pd

# ==========================================
# 환경변수
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

VIX_SPIKE_THRESHOLD = 0.1

# ==========================================
# 데이터 수집
# ==========================================
def get_market_status():
    vix_hist = yf.Ticker("^VIX").history(period="5d")

    if len(vix_hist) < 2:
        raise Exception("VIX 데이터 부족")

    vix = round(vix_hist['Close'].iloc[-1], 2)
    prev_vix = round(vix_hist['Close'].iloc[-2], 2)
    vix_change = round((vix - prev_vix) / prev_vix, 3)

    spy_hist = yf.Ticker("SPY").history(period="300d")

    if len(spy_hist) < 200:
        raise Exception("SPY 데이터 부족")

    spy = round(spy_hist['Close'].iloc[-1], 2)
    ma200 = round(spy_hist['Close'].rolling(200).mean().iloc[-1], 2)

    tnx = round(yf.Ticker("^TNX").history(period="5d")['Close'].iloc[-1], 2)

    return vix, vix_change, spy, ma200, tnx

# ==========================================
# RSI
# ==========================================
def get_rsi():
    data = yf.Ticker("SPY").history(period="100d")

    if len(data) < 20:
        return 50

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

    if len(data) == 0:
        return 0

    peak = data['Close'].max()
    current = data['Close'].iloc[-1]

    return round((current - peak) / peak * 100, 1)

# ==========================================
# 달러 (DXY)
# ==========================================
def get_dxy():
    try:
        return round(yf.Ticker("DX-Y.NYB").history(period="1d")['Close'].iloc[-1], 2)
    except:
        return 100

# ==========================================
# 원달러 환율
# ==========================================
def get_usdkrw():
    try:
        return round(yf.Ticker("KRW=X").history(period="1d")['Close'].iloc[-1], 2)
    except:
        return 1300

def get_fx_status(usdkrw):
    if usdkrw >= 1400:
        return "🚨 매우 높음 (환전 비추천)"
    elif usdkrw >= 1350:
        return "⚠️ 높음 (환전 주의)"
    elif usdkrw >= 1300:
        return "중립"
    else:
        return "💎 낮음 (환전 기회)"

# ==========================================
# 점수 시스템
# ==========================================
def calculate_score(vix, vix_change, spy, ma200, rsi, dd, tnx, dxy):
    score = 0
    details = []

    if vix >= 40:
        score += 3
        details.append("VIX 패닉 +3")
    elif vix >= 30:
        score += 2
        details.append("VIX 상승 +2")
    elif vix >= 20:
        score += 1
        details.append("VIX 주의 +1")

    if vix_change >= 0.1:
        score += 2
        details.append("VIX 급등 +2")

    if spy < ma200:
        score += 2
        details.append("하락추세 +2")

    if rsi < 30:
        score += 1
        details.append("과매도 +1")
    elif rsi > 70:
        score -= 1
        details.append("과열 -1")

    if dd <= -20:
        score += 3
        details.append("베어마켓 +3")
    elif dd <= -10:
        score += 2
        details.append("조정 +2")
    elif dd <= -5:
        score += 1
        details.append("약조정 +1")

    if tnx >= 4.5:
        score += 2
        details.append("금리위험 +2")
    elif tnx >= 4.0:
        score += 1
        details.append("금리부담 +1")

    if dxy >= 105:
        score += 2
        details.append("달러강세 +2")
    elif dxy >= 100:
        score += 1
        details.append("달러상승 +1")

    return score, details

def score_to_level(score):
    if score >= 9:
        return 4
    elif score >= 6:
        return 3
    elif score >= 4:
        return 2
    elif score >= 2:
        return 1
    else:
        return 0

# ==========================================
# 투자 판단
# ==========================================
def get_invest_ratio(level):
    return {0:1.0, 1:0.7, 2:0.4, 3:0.2, 4:1.3}[level]

def get_action(level):
    return [
        "적극 매수",
        "분할매수 유지",
        "비중 축소 / 일부 익절",
        "현금 확보 / 방어",
        "공포 매수 (강하게)"
    ][level]

def get_sell_signal(level):
    if level >= 3:
        return "수익난 종목 일부 익절 (20~40%)"
    elif level == 2:
        return "일부 익절 고려"
    else:
        return "보유 유지"

# ==========================================
# 텔레그램
# ==========================================
def send(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("토큰 없음")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.get(url, params={"chat_id": CHAT_ID, "text": msg})

# ==========================================
# 실행
# ==========================================
def run():
    try:
        vix, vix_change, spy, ma200, tnx = get_market_status()
        rsi = get_rsi()
        dd = get_drawdown()
        dxy = get_dxy()
        usdkrw = get_usdkrw()

        score, details = calculate_score(vix, vix_change, spy, ma200, rsi, dd, tnx, dxy)
        level = score_to_level(score)

        ratio = get_invest_ratio(level)
        action = get_action(level)
        sell = get_sell_signal(level)

        detail_text = "\n".join(details)

        msg = (
        "🤖 자동 투자 판단 시스템\n\n"

        f"🔥 단계: {level}단계 | 점수: {score}\n"
        f"👉 행동: {action}\n"
        f"👉 매수 비중: {int(ratio*100)}%\n"
        f"👉 매도 전략: {sell}\n\n"

        "[판단 근거]\n"
        f"{detail_text}\n\n"

        "[시장 지표]\n"
        f"VIX: {vix} (Δ {vix_change*100:.1f}%)\n"
        f"SPY: {spy} / 200MA: {ma200}\n"
        f"금리: {tnx}% | RSI: {rsi}\n"
        f"낙폭: {dd}%\n"
        f"달러(DXY): {dxy}\n"
        f"환율: {usdkrw} → {get_fx_status(usdkrw)}"
        )

        if vix_change >= VIX_SPIKE_THRESHOLD:
            msg = "⚡ VIX 급등 감지!\n\n" + msg

        send(msg)

    except Exception as e:
        send(f"❌ 에러 발생: {e}")

run()
