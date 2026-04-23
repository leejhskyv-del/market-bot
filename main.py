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
# 2. 뉴스 수집 (안정화)
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
# 3. AI 분석 (한국어 강제)
# ==========================================
def get_ai_score(news_text):

    if not news_text or len(news_text.strip()) < 10:
        return 0, "뉴스 부족", "데이터부족"

    prompt = f"""
너는 금융 시장 분석가다.

반드시 한국어로만 답변해라.
영어 절대 사용하지 마라.

아래 JSON 형식으로만 답변해라.

{{
 "score": int,
 "reason": "한국어 한줄 요약",
 "category": "War/Pandemic/Finance/Macro/Etc"
}}

뉴스:
{news_text}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
            response_format={"type": "json_object"}  # 🔥 핵심 안정화
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
# 5. 퀀트 점수 (-2 포함)
# ==========================================
def calculate_score(spy_return, vix):

    score = 0

    # 🔥 과열 (상승 과도)
    if spy_return > 0.03:
        score = -2

    # 하락
    elif spy_return < -0.03:
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
# 6. 단계 시스템
# ==========================================
def get_stage(score):

    if score <= -2:
        return "🔥 과열", "추격 금지 / 일부 익절"
    elif score >= 8:
        return "🚨 패닉", "강력 매수 구간"
    elif score >= 5:
        return "🔴 위험", "비중 축소"
    elif score >= 3:
        return "🟠 경고", "부분 익절"
    elif score >= 1:
        return "🟡 주의", "관망"
    else:
        return "🟢 정상", "보유"

# ==========================================
# 7. 텔레그램 전송
# ==========================================
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        print("텔레그램 오류:", e)

# ==========================================
# 8. 메인
# ==========================================
def main():

    print("===== BOT START =====")

    news = fetch_news()

    if not news:
        ai_score, ai_reason, category = 0, "뉴스 없음", "없음"
    else:
        ai_score, ai_reason, category = get_ai_score(news)

    spy_return, vix = get_market_data()

    quant_score = calculate_score(spy_return, vix)

    # AI 가중치
    if ai_score >= 3:
        final_ai = 3
    else:
        final_ai = int(round(ai_score * 0.7))

    total_score = quant_score + final_ai

    # 🔥 점수 범위 유지
    total_score = max(-2, total_score)
    total_score = min(10, total_score)

    stage, action = get_stage(total_score)

    msg = f"""
🔥 하이브리드 퀀트 V4.0

{stage}

👉 최종 점수: {total_score}점
👉 전략: {action}

────────────────

📊 시장
• SPY 변화: {spy_return:.2%}
• VIX: {vix:.2f}

────────────────

🧠 AI 분석
• 점수: {ai_score} ({category})
• 의견: {ai_reason}
"""

    print(msg)
    send_telegram(msg)

# ==========================================
if __name__ == "__main__":
    main()
