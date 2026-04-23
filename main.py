import yfinance as yf
import requests
import os
import json
import feedparser
import re
from openai import OpenAI

# ==========================================
# 0. 환경 변수
# ==========================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

# ==========================================
# 1. JSON 파싱 안정화
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
# 2. 뉴스 수집 (안정화 버전)
# ==========================================
def fetch_news():
    headlines = []

    urls = [
        "https://finance.yahoo.com/news/rssindex",
        "https://feeds.reuters.com/reuters/businessNews"
    ]

    headers = {"User-Agent": "Mozilla/5.0"}

    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=5)
            feed = feedparser.parse(r.text)
            headlines += [e.title for e in feed.entries[:5]]
        except:
            continue

    return " ".join(headlines)

# ==========================================
# 3. AI 분석
# ==========================================
def get_ai_score(news_text):

    if not news_text or len(news_text.strip()) < 10:
        return 0, "뉴스 부족", "데이터부족"

    prompt = f"""
Return ONLY JSON.

{{
 "score": int,
 "reason": "Korean short sentence",
 "category": "War/Pandemic/Finance/Macro/Etc"
}}

News:
{news_text}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200
        )

        raw = response.choices[0].message.content

        print("\n=== AI RESPONSE ===")
        print(raw)
        print("===================\n")

        result = extract_json(raw)

        return (
            int(result.get("score", 0)),
            result.get("reason", "분석 실패"),
            result.get("category", "미분류")
        )

    except Exception as e:
        print("AI ERROR:", e)
        return 0, "AI 오류", "에러"

# ==========================================
# 4. 시장 데이터
# ==========================================
def get_market_data():
    try:
        spy = yf.Ticker("SPY").history(period="5d")["Close"]
        vix = yf.Ticker("^VIX").history(period="1d")["Close"].iloc[-1]

        spy_return = (spy.iloc[-1] / spy.iloc[0]) - 1

        return spy_return, vix
    except:
        return 0, 0

# ==========================================
# 5. 퀀트 점수
# ==========================================
def calculate_score(spy_return, vix):

    score = 0

    # 하락
    if spy_return < -0.03:
        score += 2
    elif spy_return < -0.01:
        score += 1

    # 변동성
    if vix > 25:
        score += 2
    elif vix > 20:
        score += 1

    return score

# ==========================================
# 6. 텔레그램 전송
# ==========================================
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        print("텔레그램 오류:", e)

# ==========================================
# 7. 메인
# ==========================================
def main():

    print("===== BOT START =====")

    news = fetch_news()

    ai_score, ai_reason, category = get_ai_score(news)

    spy_return, vix = get_market_data()

    quant_score = calculate_score(spy_return, vix)

    # AI 가중치
    if ai_score >= 3:
        final_ai = 3
    else:
        final_ai = int(round(ai_score * 0.7))

    total_score = quant_score + final_ai

    # 안전 캡
    total_score = min(total_score, 10)

    msg = f"""
📊 시장 리포트

지수 변화: {spy_return:.2%}
VIX: {vix:.2f}

퀀트 점수: {quant_score}
AI 점수: {ai_score} ({category})

최종 점수: {total_score}

🧠 AI 의견:
{ai_reason}
"""

    print(msg)
    send_telegram(msg)

# ==========================================
if __name__ == "__main__":
    main()
