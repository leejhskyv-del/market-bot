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
# JSON 파싱 안정화
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
# AI 분석 (한국어 강제)
# ==========================================
def get_ai_score(news):
    if not news:
        return 0, "뉴스 없음", "없음"

    prompt = f"""
너는 금융 시장 분석가다.

반드시 한국어로만 답변해라.
영어 절대 사용 금지.

JSON 형식으로만 출력해라.

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

    except Exception as e:
        print("AI ERROR:", e)
        return 0, "AI 오류", "에러"

# ==========================================
# 시장 데이터
# ==========================================
def get_market():
    spy = yf.Ticker("SPY").history(period="5d")["Close"]
    qqq = yf.Ticker("QQQ").history(period="5d")["Close"]
    vix = yf.Ticker("^VIX").history(period="1d")["Close"].iloc[-1]
    dxy = yf.Ticker("DX-Y.NYB").history(period="1d")["Close"].iloc[-1]

    spy_r = (spy.iloc[-1] / spy.iloc[0]) - 1
    qqq_r = (qqq.iloc[-1] / qqq.iloc[0]) - 1

    return spy_r, qqq_r, vix, dxy

# ==========================================
# 환율 (USD/KRW)
# ==========================================
def get_fx():
    data = yf.Ticker("KRW=X").history(period="2y")["Close"]
    current = data.iloc[-1]
    avg_1y = data[-252:].mean()
    avg_2y = data.mean()
    return current, avg_1y, avg_2y

def fx_status(current, avg_1y):
    if current > avg_1y * 1.02:
        return "🔴 고평가 (달러 비쌈)"
    elif current < avg_1y * 0.98:
        return "🟢 저평가 (달러 쌈)"
    else:
        return "🟡 중립"

# ==========================================
# 점수 계산 (-2 포함)
# ==========================================
def calc_score(spy, qqq, vix, dxy):

    # 과열
    if spy > 0.03 and qqq > 0.04:
        return -2

    score = 0

    if spy < -0.03:
        score += 2
    elif spy < -0.01:
        score += 1

    if qqq < -0.04:
        score += 2
    elif qqq < -0.02:
        score += 1

    if vix > 25:
        score += 2
    elif vix > 20:
        score += 1

    if dxy > 105:
        score += 1

    return score

# ==========================================
# 단계 시스템
# ==========================================
def get_stage(score):

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
# 메타 감시자
# ==========================================
def meta(news, ai_score):

    if not news:
        return "⚠️ 뉴스 수집 실패"

    if abs(ai_score) >= 3:
        return "⚠️ AI 강한 신호 감지"

    return "✅ 시스템 정상"

# ==========================================
# 텔레그램
# ==========================================
def send(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

# ==========================================
# 메인 실행
# ==========================================
def main():

    news = fetch_news()
    ai_score, ai_reason, category = get_ai_score(news)

    spy, qqq, vix, dxy = get_market()
    fx, fx1, fx2 = get_fx()

    quant = calc_score(spy, qqq, vix, dxy)

    # AI 가중치
    ai_adj = 3 if ai_score >= 3 else int(ai_score * 0.7)

    total = quant + ai_adj

    # 점수 범위 유지
    total = max(-2, min(10, total))

    stage, action = get_stage(total)
    meta_state = meta(news, ai_score)

    msg = f"""
📊 투자 리포트

🛡️ {meta_state}

────────────────

🔥 {stage}
👉 점수: {total}
👉 전략: {action}

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

🤖 AI 분석
{ai_reason}
"""

    print(msg)
    send(msg)

if __name__ == "__main__":
    main()
