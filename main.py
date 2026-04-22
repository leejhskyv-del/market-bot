import yfinance as yf
import requests
import time
import os
from datetime import datetime

# ==========================================
# 1. 환경변수 (Render에서 설정)
# ==========================================
TELEGRAM_TOKEN = os.getenv("8603001067:AAEtlhd8z5osphG0s-pKkVWJUjol-PA7H9s")
CHAT_ID = os.getenv("8757371550")

# ==========================================
# 2. 기준값 설정
# ==========================================
VIX_L1, VIX_L2, VIX_L3, VIX_L4 = 20, 28, 35, 40
VIX_SPIKE_THRESHOLD = 0.1   # 🔥 10% 급등

CNN_LINK = "https://www.cnn.com/markets/fear-and-greed"

# ==========================================
# 3. 데이터 가져오기
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

    return vix, prev_vix, vix_change, spy_price, ma200, tnx, is_bear

# ==========================================
# 4. 텔레그램 전송
# ==========================================
def send_telegram(msg):
    if not msg:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.get(url, params={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

# ==========================================
# 5. 시작 알림
# ==========================================
def send_start_report():
    vix, prev_vix, vix_change, spy, ma200, tnx, is_bear = get_market_status()

    msg = f"""
🚀 **시장 감시 시스템 시작**

📊 현재 상태
• VIX: {vix} (변화율: {vix_change*100:.1f}%)
• SPY: {spy} (200MA: {ma200})
• 금리: {tnx}%
• 추세: {'🔴 하락장' if is_bear else '🟢 상승장'}

이제부터 1시간마다 감시합니다.
"""
    send_telegram(msg)

# ==========================================
# 6. 메인 루프
# ==========================================
def run_monitor():
    last_level = -1

    while True:
        try:
            vix, prev_vix, vix_change, spy, ma200, tnx, is_bear = get_market_status()

            level = 0
            spike = vix_change >= VIX_SPIKE_THRESHOLD

            # 🔥 단계 판정 (개선 버전)
            if vix >= VIX_L4:
                level = 4
            elif vix >= VIX_L3 and is_bear:
                level = 3
            elif vix >= VIX_L2 or is_bear or spike:
                level = 2
            elif vix >= VIX_L1:
                level = 1

            info = f"""

📊 **실시간 상태**
• VIX: {vix} (Δ {vix_change*100:.1f}%)
• SPY: {spy} / 200MA: {ma200}
• 금리: {tnx}%
• 추세: {'🔴 하락' if is_bear else '🟢 상승'}
"""

            msg = ""

            # 🔥 급등 알림 (핵심)
            if spike:
                msg += f"⚡ **VIX 급등 감지! (+{vix_change*100:.1f}%)**\n"

            # 단계 변화 알림
            if level != last_level:
                if level == 4:
                    msg += "💎 [4단계] 패닉 구간 → 역발상 매수 검토\n"
                elif level == 3:
                    msg += "🚨 [3단계] 시장 붕괴 → 리스크 관리 필수\n"
                elif level == 2:
                    msg += "⚠️ [2단계] 경고 구간\n"
                elif level == 1:
                    msg += "🔔 [1단계] 주의 구간\n"
                elif level == 0:
                    msg += "✅ 시장 안정화\n"

            if msg:
                msg += info + f"\n🔗 [CNN 지수 보기]({CNN_LINK})"
                send_telegram(msg)

            last_level = level

            print(f"[{datetime.now()}] VIX:{vix} Δ{vix_change*100:.1f}%")

            time.sleep(3600)

        except Exception as e:
            print("오류:", e)
            time.sleep(60)

# ==========================================
# 실행
# ==========================================
if __name__ == "__main__":
    send_start_report()
    run_monitor()