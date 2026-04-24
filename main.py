import yfinance as yf
import requests
import os
import json
import feedparser
import re
from datetime import datetime
from openai import OpenAI

# ==========================================
# 환경 변수
# ==========================================
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ==========================================
# JSON 파싱
# ==========================================
def extract_json(text):
    try:
        return json.loads(text.strip())
    except:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except:
                pass
    return {"score": 0, "reason": "파싱 실패", "category": "기타"}

# ==========================================
# 뉴스 수집
# ==========================================
def fetch_news():
    urls = [
        "https://finance.yahoo.com/news/rssindex",
        "https://feeds.reuters.com/reuters/businessNews"
    ]
    headers = {"User-Agent": "Mozilla/5.0"}
    headlines = []

    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=5)
            feed = feedparser.parse(r.text)
            headlines += [e.title for e in feed.entries[:5]]
        except:
            continue

    return " ".join(headlines)

# ==========================================
# AI 분석 (2~3줄 요약)
# ==========================================
def get_ai_score(news):
    if not news:
        return 0, "뉴스 없음", "없음"

    prompt = f"""
너는 금융 매크로 분석가다.

아래 뉴스들을 보고 시장에 영향을 주는 핵심을
"2~3줄로 한국어 요약"하라.

그리고 점수도 함께 반환하라.

형식(JSON만 출력):
{{
 "score": int,
 "reason": "줄바꿈 포함 2~3줄 요약",
 "category": "Macro"
}}

뉴스:
{news}
"""

    try:
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"}
        )
        data = extract_json(res.choices[0].message.content)
        return data["score"], data["reason"], data["category"]
    except:
        return 0, "AI 오류", "에러"

# ==========================================
# 시장 데이터
# ==========================================
def get_market():
    spy_data = yf.Ticker("SPY").history(period="1y")["Close"]
    qqq_data = yf.Ticker("QQQ").history(period="1y")["Close"]
    vix_data = yf.Ticker("^VIX").history(period="5d")["Close"]

    spy_cur = spy_data.iloc[-1]
    spy_prev = spy_data.iloc[-2]

    qqq_cur = qqq_data.iloc[-1]
    qqq_prev = qqq_data.iloc[-2]

    vix_cur = vix_data.iloc[-1]
    vix_prev = vix_data.iloc[-2]

    spy_sma = spy_data.rolling(200).mean().iloc[-1]
    qqq_sma = qqq_data.rolling(200).mean().iloc[-1]

    dxy = yf.Ticker("DX-Y.NYB").history(period="1d")["Close"].iloc[-1]

    return spy_cur, spy_prev, spy_sma, qqq_cur, qqq_prev, qqq_sma, vix_cur, vix_prev, dxy

# ==========================================
# 환율 (1년 + 2년 평균)
# ==========================================
def get_fx():
    data = yf.Ticker("KRW=X").history(period="2y")["Close"]
    current = data.iloc[-1]
    avg_1y = data[-252:].mean()
    avg_2y = data.mean()
    return current, avg_1y, avg_2y

def fx_status(cur, avg1):
    if cur > avg1 * 1.02:
        return "고평가"
    elif cur < avg1 * 0.98:
        return "저평가"
    else:
        return "중립"

# ==========================================
# 계산
# ==========================================
def change_pct(cur, prev):
    return ((cur - prev) / prev) * 100

def sma_gap(cur, sma):
    return ((cur - sma) / sma) * 100

def sma_signal(gap):
    if gap > 3:
        return "🟢"
    elif gap > 0:
        return "🟡"
    else:
        return "🔴"

# ==========================================
# 점수
# ==========================================
def calc_score(spy_gap, qqq_gap, vix, dxy):

    if spy_gap > 5 and qqq_gap > 5:
        return -2

    score = 0

    if spy_gap < -3: score += 2
    elif spy_gap < -1: score += 1

    if qqq_gap < -3: score += 2
    elif qqq_gap < -1: score += 1

    if vix > 25: score += 2
    elif vix > 20: score += 1

    if dxy > 105: score += 1

    return score

def stage(score):
    if score <= -2:
        return "🔥 과열", "추격 금지"
    elif score >= 8:
        return "🚨 패닉", "강력 매수"
    elif score >= 5:
        return "🔴 위험", "비중 축소"
    elif score >= 3:
        return "🟠 경고", "부분 익절"
    elif score >= 1:
        return "🟡 주의", "관망"
    else:
        return "🟢 정상", "보유"

# ==========================================
# 텔레그램
# ==========================================
def send(msg):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": msg}
    )

# ==========================================
# 메인
# ==========================================
def main():

    now = datetime.now()
    hour = now.hour

    news = fetch_news()
    ai_score, ai_reason, _ = get_ai_score(news)

    spy_cur, spy_prev, spy_sma, qqq_cur, qqq_prev, qqq_sma, vix_cur, vix_prev, dxy = get_market()
    fx, fx1, fx2 = get_fx()

    spy_gap = sma_gap(spy_cur, spy_sma)
    qqq_gap = sma_gap(qqq_cur, qqq_sma)

    spy_sig = sma_signal(spy_gap)
    qqq_sig = sma_signal(qqq_gap)

    quant_score = calc_score(spy_gap, qqq_gap, vix_cur, dxy)
    total_score = quant_score + int(round(ai_score * 0.7))
    total_score = max(-2, min(10, total_score))

    st, act = stage(total_score)

    # ==========================================
    # 알림 제어 (핵심🔥)
    # ==========================================
    send_flag = False

    if total_score >= 5:
        send_flag = True

    elif total_score >= 3:
        if hour % 3 == 0:
            send_flag = True

    else:
        if hour % 6 == 0:
            send_flag = True

    # 미국장 시간 보정
    if 22 <= hour or hour <= 5:
        if total_score >= 3:
            send_flag = True

    if not send_flag:
        return

    # ==========================================
    # 메시지
    # ==========================================
    msg = f"""🤖 퀀텀 인사이트 봇
🤖 AI
{ai_reason}

🛡️ 상태: {st} ({total_score}점) → {act}

📊 시장
SPY {spy_cur:.2f} ({change_pct(spy_cur, spy_prev):+.2f}%) {spy_sig} {spy_gap:+.1f}%
QQQ {qqq_cur:.2f} ({change_pct(qqq_cur, qqq_prev):+.2f}%) {qqq_sig} {qqq_gap:+.1f}%
VIX {vix_cur:.2f} ({change_pct(vix_cur, vix_prev):+.2f}%)

💱 환율 {fx:.0f} (1Y {fx1:.0f} / 2Y {fx2:.0f}, {fx_status(fx, fx1)})
"""

    print(msg)
    send(msg)

if __name__ == "__main__":
    main()
