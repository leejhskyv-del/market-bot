import yfinance as yf
import requests
import os
import json
import feedparser
import re
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
# 뉴스
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
# AI (보조)
# ==========================================
def get_ai_score(news):
    if not news:
        return 0, "뉴스 없음", "없음"

    prompt = f"""
너는 금융 분석가다.
반드시 한국어 JSON만 출력해라.

{{
 "score": int,
 "reason": "한줄 요약",
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
# 시장 데이터 + 전일 + 200일선
# ==========================================
def get_market():
    spy_data = yf.Ticker("SPY").history(period="1y")["Close"]
    qqq_data = yf.Ticker("QQQ").history(period="1y")["Close"]
    vix_data = yf.Ticker("^VIX").history(period="5d")["Close"]

    spy_cur = spy_data.iloc[-1]
    spy_prev = spy_data.iloc[-2]

    qqq_cur = qqq_data.iloc[-1]
    qqq_prev = qqq_data.iloc[-2]

    vix = vix_data.iloc[-1]
    vix_prev = vix_data.iloc[-2]

    spy_r = (spy_cur / spy_data.iloc[-5]) - 1
    qqq_r = (qqq_cur / qqq_data.iloc[-5]) - 1

    spy_sma200 = spy_data.rolling(200).mean().iloc[-1]
    qqq_sma200 = qqq_data.rolling(200).mean().iloc[-1]

    spy_below = spy_cur < spy_sma200
    qqq_below = qqq_cur < qqq_sma200

    dxy = yf.Ticker("DX-Y.NYB").history(period="1d")["Close"].iloc[-1]

    return (
        spy_r, qqq_r, vix, dxy,
        spy_below, qqq_below,
        spy_cur, spy_prev,
        qqq_cur, qqq_prev,
        vix, vix_prev
    )

# ==========================================
# 환율 (1Y / 2Y 유지)
# ==========================================
def get_fx():
    data = yf.Ticker("KRW=X").history(period="2y")["Close"]

    current = data.iloc[-1]
    avg_1y = data[-252:].mean()
    avg_2y = data.mean()

    return current, avg_1y, avg_2y

def fx_status(cur, avg1):
    if cur > avg1 * 1.02:
        return "🔴 고평가"
    elif cur < avg1 * 0.98:
        return "🟢 저평가"
    else:
        return "🟡 중립"

# ==========================================
# 퀀트 점수 (핵심 유지)
# ==========================================
def calc_quant(spy, qqq, vix, dxy, spy_below, qqq_below):

    if spy > 0.03 and qqq > 0.04:
        return -2

    score = 0

    if spy < -0.03: score += 2
    elif spy < -0.01: score += 1

    if qqq < -0.04: score += 2
    elif qqq < -0.02: score += 1

    # 200일선
    if spy_below and qqq_below:
        score += 2
    elif spy_below or qqq_below:
        score += 1

    if vix > 25: score += 2
    elif vix > 20: score += 1

    if dxy > 105: score += 1

    return score

# ==========================================
# AI 보조 적용
# ==========================================
def apply_ai(q, ai):
    if ai >= 3:
        return q + 3
    return q + int(round(ai * 0.7))

# ==========================================
# 단계 (초입 표시 포함)
# ==========================================
def stage(score):

    if score <= -2:
        return "🔥 과열", "추격 금지"
    elif score >= 8:
        return "🚨 패닉", "강력 매수"
    elif score >= 5:
        return "🔴 위험", "비중 축소"
    elif score >= 3:
        label = "🟠 경고"
        if score == 3:
            label += " (초입)"
        return label, "부분 익절"
    elif score >= 1:
        return "🟡 주의", "관망"
    else:
        return "🟢 정상", "보유"

# ==========================================
# 메타 감시
# ==========================================
def meta(news, ai):
    if not news:
        return "⚠️ 뉴스 없음"
    if abs(ai) >= 3:
        return "⚠️ AI 강한 신호"
    return "✅ 정상"

# ==========================================
# 보조 함수
# ==========================================
def change_pct(cur, prev):
    return ((cur - prev) / prev) * 100

def sma_status(below):
    return "🔴 200일선 아래" if below else "🟢 200일선 위"

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

    news = fetch_news()
    ai_s, ai_r, _ = get_ai_score(news)

    (
        spy_r, qqq_r, vix, dxy,
        spy_b, qqq_b,
        spy_cur, spy_prev,
        qqq_cur, qqq_prev,
        vix_cur, vix_prev
    ) = get_market()

    fx, fx1, fx2 = get_fx()

    quant = calc_quant(spy_r, qqq_r, vix, dxy, spy_b, qqq_b)
    total = apply_ai(quant, ai_s)

    total = max(-2, min(10, total))

    st, act = stage(total)
    meta_s = meta(news, ai_s)

    msg = f"""
🤖 퀀텀 인사이트 봇

📊 투자 리포트

🛡️ {meta_s}

────────────────

🔥 {st}
👉 점수: {total}
👉 전략: {act}

────────────────

📊 시장 변화 (전일 대비)

• SPY: {spy_cur:.2f} ({change_pct(spy_cur, spy_prev):+.2f}%)
• QQQ: {qqq_cur:.2f} ({change_pct(qqq_cur, qqq_prev):+.2f}%)
• VIX: {vix_cur:.2f} ({change_pct(vix_cur, vix_prev):+.2f}%)

────────────────

📉 추세

• SPY: {sma_status(spy_b)}
• QQQ: {sma_status(qqq_b)}

────────────────

💱 환율

• 현재: {fx:.2f}
• 1Y: {fx1:.2f}
• 2Y: {fx2:.2f}
• 상태: {fx_status(fx, fx1)}

────────────────

🤖 AI 보조

{ai_r}
"""

    print(msg)
    send(msg)

if __name__ == "__main__":
    main()
