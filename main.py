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
# AI 분석 (보조 역할)
# ==========================================
def get_ai_score(news):
    if not news:
        return 0, "뉴스 없음", "없음"

    prompt = f"""
너는 금융 분석가다.
반드시 한국어 JSON만 출력.

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
# 시장 데이터 + 200일선
# ==========================================
def get_market():
    spy_data = yf.Ticker("SPY").history(period="1y")["Close"]
    qqq_data = yf.Ticker("QQQ").history(period="1y")["Close"]

    spy_cur = spy_data.iloc[-1]
    qqq_cur = qqq_data.iloc[-1]

    spy_r = (spy_cur / spy_data.iloc[-5]) - 1
    qqq_r = (qqq_cur / qqq_data.iloc[-5]) - 1

    spy_sma200 = spy_data.rolling(200).mean().iloc[-1]
    qqq_sma200 = qqq_data.rolling(200).mean().iloc[-1]

    spy_below = spy_cur < spy_sma200
    qqq_below = qqq_cur < qqq_sma200

    vix = yf.Ticker("^VIX").history(period="1d")["Close"].iloc[-1]
    dxy = yf.Ticker("DX-Y.NYB").history(period="1d")["Close"].iloc[-1]

    return spy_r, qqq_r, vix, dxy, spy_below, qqq_below

# ==========================================
# 환율 (1년 / 2년 평균 포함)
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
# 퀀트 점수 (핵심 시스템)
# ==========================================
def calc_quant(spy, qqq, vix, dxy, spy_below, qqq_below):

    # 과열
    if spy > 0.03 and qqq > 0.04:
        return -2

    score = 0

    # 단기
    if spy < -0.03: score += 2
    elif spy < -0.01: score += 1

    if qqq < -0.04: score += 2
    elif qqq < -0.02: score += 1

    # 200일선 (완화형)
    if spy_below and qqq_below:
        score += 2
    elif spy_below or qqq_below:
        score += 1

    # 변동성
    if vix > 25: score += 2
    elif vix > 20: score += 1

    # 달러
    if dxy > 105: score += 1

    return score

# ==========================================
# AI 적용 (보조)
# ==========================================
def apply_ai(q, ai):
    if ai >= 3:
        return q + 3
    return q + int(round(ai * 0.7))

# ==========================================
# 단계
# ==========================================
def stage(score):
    if score <= -2: return "🔥 과열", "추격 금지"
    if score >= 8: return "🚨 패닉", "강력 매수"
    if score >= 5: return "🔴 위험", "비중 축소"
    if score >= 3: return "🟠 경고", "부분 익절"
    if score >= 1: return "🟡 주의", "관망"
    return "🟢 정상", "보유"

# ==========================================
# 메타 감시
# ==========================================
def meta(news, ai):
    if not news: return "⚠️ 뉴스 없음"
    if abs(ai) >= 3: return "⚠️ AI 강한 신호"
    return "✅ 정상"

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

    spy, qqq, vix, dxy, spy_b, qqq_b = get_market()
    fx, fx1, fx2 = get_fx()

    quant = calc_quant(spy, qqq, vix, dxy, spy_b, qqq_b)
    total = apply_ai(quant, ai_s)

    total = max(-2, min(10, total))

    st, act = stage(total)
    meta_s = meta(news, ai_s)

    msg = f"""
📊 투자 리포트

🛡️ {meta_s}

────────────────

🔥 {st}
👉 점수: {total}
👉 전략: {act}

────────────────

📊 시장
• SPY: {spy:.2%}
• QQQ: {qqq:.2%}
• VIX: {vix:.2f}
• DXY: {dxy:.2f}

────────────────

💱 환율
• 현재: {fx:.2f}
• 1Y: {fx1:.2f}
• 2Y: {fx2:.2f}
• 상태: {fx_status(fx, fx1)}

────────────────

🤖 AI (보조)
{ai_r}
"""

    print(msg)
    send(msg)

if __name__ == "__main__":
    main()
