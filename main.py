import yfinance as yf
import requests
import os
from datetime import datetime

# ==========================================
# 1. 설정 및 경로
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.txt")

# ==========================================
# 2. 데이터 수집 함수
# ==========================================
def get_market_status():
    vix_hist = yf.Ticker("^VIX").history(period="5d")
    vix = round(vix_hist['Close'].iloc[-1], 2)
    prev_vix = round(vix_hist['Close'].iloc[-2], 2)
    vix_change = round((vix - prev_vix) / prev_vix, 3)

    spy_hist = yf.Ticker("SPY").history(period="300d")
    spy = round(spy_hist['Close'].iloc[-1], 2)
    ma200 = round(spy_hist['Close'].rolling(200).mean().iloc[-1], 2)

    tnx = round(yf.Ticker("^TNX").history(period="5d")['Close'].iloc[-1], 2)
    
    qqq_hist = yf.Ticker("QQQ").history(period="300d")
    qqq = round(qqq_hist['Close'].iloc[-1], 2)
    qqq_ma200 = round(qqq_hist['Close'].rolling(200).mean().iloc[-1], 2)

    gld_hist = yf.Ticker("GLD").history(period="100d")
    gld = round(gld_hist['Close'].iloc[-1], 2)
    gld_ma50 = round(gld_hist['Close'].rolling(50).mean().iloc[-1], 2)

    delta = spy_hist['Close'].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - (100 / (1 + (gain / loss)))
    
    peak = spy_hist['Close'].max()
    dd = (spy - peak) / peak * 100
    
    # 티커를 선물(DX=F)로 변경하고, 휴일 대비 period를 5d로 늘림
    try:
        dxy = round(yf.Ticker("DX=F").history(period="5d")['Close'].iloc[-1], 2)
    except:
        dxy = 100.0  # 만약 야후 서버가 또 터지면 임시로 100을 넣어서 프로그램 다운을 방지

    return vix, vix_change, spy, ma200, tnx, qqq, qqq_ma200, gld, gld_ma50, round(rsi.iloc[-1], 1), round(dd, 1), dxy

# 🔥 환율: 1년 평균과 2년 평균을 모두 구합니다.
def get_fx():
    # 2년치(영업일 기준 약 504일)를 구하기 위해 넉넉히 3년 데이터를 가져옵니다.
    data = yf.Ticker("KRW=X").history(period="3y") 
    current = round(data['Close'].iloc[-1], 2)
    avg_1y = round(data['Close'][-252:].mean(), 2)  # 1년(252영업일) 평균
    avg_2y = round(data['Close'][-504:].mean(), 2)  # 2년(504영업일) 평균
    return current, avg_1y, avg_2y

# ==========================================
# 3. 로직 및 판단 함수
# ==========================================
def calculate_score(vix, vix_change, spy, ma200, rsi, dd, tnx, dxy, qqq, qqq_ma200, gld, gld_ma50):
    score = 0
    if vix >= 40: score += 3
    elif vix >= 30: score += 2
    elif vix >= 20: score += 1

    if vix_change >= 0.1: score += 2
    if spy < ma200: score += 2
    if qqq < qqq_ma200: score += 2
    if rsi < 30: score += 1
    elif rsi > 70: score -= 2
    if dd <= -10: score += 2
    if tnx >= 4.5: score += 2
    if dxy >= 105: score += 2
    if gld > gld_ma50: score += 1
    return score

def get_action_text(score):
    if score >= 9: return 4, "💎 공포 구간", "매도 중단 (매수 준비)"
    elif score >= 6: return 3, "🛑 위험 구간", "익절 확대 (20~40%)"
    elif score >= 4: return 2, "⚠️ 시장 경고", "익절 시작 (10~20%)"
    elif score >= 2: return 1, "🟡 시장 주의", "보유"
    else: return 0, "🔥 자동매수 유지", "보유"

# 🔥 환율: 1년, 2년 복합 판단 로직 적용
def get_fx_action(current, avg_1y, avg_2y):
    diff_1y = (current - avg_1y) / avg_1y
    diff_2y = (current - avg_2y) / avg_2y

    if diff_1y >= 0.08 and diff_2y >= 0.10: 
        return "🚨 절대 환전 금지 (초고환율)"
    elif diff_1y <= -0.05 and diff_2y <= -0.05: 
        return "💎 영끌 환전 (초강력 매수)"
    elif diff_1y >= 0.04: 
        return "⚠️ 환전 천천히"
    elif diff_1y <= -0.05: 
        return "✅ 적극 환전"
    else: 
        return "중립"

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return int(f.read().strip())
    except:
        return None

def save_state(score):
    with open(STATE_FILE, "w") as f:
        f.write(str(score))

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.get(url, params={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

# ==========================================
# 4. 실행부
# ==========================================
def main():
    try:
        vix, vix_c, spy, spy_m, tnx, qqq, qqq_m, gld, gld_m, rsi, dd, dxy = get_market_status()
        usd, usd_1y, usd_2y = get_fx() # 변수 3개로 받음
        
        score = calculate_score(vix, vix_c, spy, spy_m, rsi, dd, tnx, dxy, qqq, qqq_m, gld, gld_m)
        level, status, strategy = get_action_text(score)
        
        # 하이브리드 환율 판단 호출
        fx_action = get_fx_action(usd, usd_1y, usd_2y)

        last_score = load_state()
        
        if last_score is None or score != last_score:
            msg = f"""
🤖 **투자 판단 리포트 (Cron)**

🔥 **단계 {level} | 점수 {score}**
👉 상태: {status}
👉 전략: {strategy}

━━━━━━━━━━
💱 **환율 현황**
• 현재가: {usd}원
• 1년평균: {usd_1y}원
• 2년평균: {usd_2y}원
👉 **판단: {fx_action}**

━━━━━━━━━━
📊 **시장 지표**
• VIX: {vix} ({vix_c*100:+.1f}%)
• S&P500: {spy} (200MA: {spy_m})
• 나스닥: {qqq} (200MA: {qqq_m})
• 금(GLD): {gld} (50MA: {gld_m})
• 금리: {tnx}% | 달러: {dxy}
• RSI: {rsi} | 낙폭: {dd:.1f}%
"""
            send_telegram(msg)
            save_state(score)
            print(f"알림 전송 완료 (점수: {score})")
        else:
            print(f"변화 없음 (현재 점수: {score})")

    except Exception as e:
        print(f"오류 발생: {e}")

if __name__ == "__main__":
    main()
