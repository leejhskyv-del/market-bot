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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ALPHA_API_KEY = os.getenv("ALPHA_API_KEY")
FRED_API_KEY = os.getenv("FRED_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

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
        "https://feeds.reuters.com/reuters/businessNews",
        "https://finance.yahoo.com/news/rssindex"
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
# AI (2~3줄)
# ==========================================
def get_ai_score(news):
    if not news:
        return 0, "뉴스 없음", "없음"

    prompt = f"""
너는 금융 매크로 분석가다.

뉴스를 보고 시장 영향을 2~3줄로 요약해라.

형식(JSON):
{{
 "score": int,
 "reason": "2~3줄 요약",
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
# Alpha 주식 데이터
# ==========================================
def get_stock(symbol):
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "apikey": ALPHA_API_KEY
    }

    r = requests.get(url)
    data = r.json()

    ts = data.get("Time Series (Daily)", {})
    dates = list(ts.keys())

    if len(dates) < 2:
        return None

    cur = float(ts[dates[0]]["4. close"])
    prev = float(ts[dates[1]]["4. close"])

    closes = [float(ts[d]["4. close"]) for d in dates[:200]]
    sma200 = sum(closes) / len(closes)

    return cur, prev, sma200

# ==========================================
# FRED 데이터
# ==========================================
def get_fred(series):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series,
        "api_key": FRED_API_KEY,
        "file_type": "json"
    }

    r = requests.get(url, params=params)
    data = r.json()

    obs = data.get("observations", [])
    values = [float(o["value"]) for o in obs if o["value"] != "."]

    return values[-1]

# ==========================================
# 환율 (Alpha)
# ==========================================
def get_fx():
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "FX_DAILY",
        "from_symbol": "USD",
        "to_symbol": "KRW",
        "apikey": ALPHA_API_KEY
    }

    r = requests.get(url)
    data = r.json()

    ts = data.get("Time Series FX (Daily)", {})
    dates = list(ts.keys())

    closes = [float(ts[d]["4. close"]) for d in dates[:500]]

    cur = closes[0]
    avg_1y = sum(closes[:252]) / 252
    avg_2y = sum(closes[:500]) / 500

    return cur, avg_1y, avg_2y

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

    hour = datetime.now().hour

    news = fetch_news()
    ai_score, ai_reason, _ = get_ai_score(news)

    spy = get_stock("SPY")
    qqq = get_stock("QQQ")
    vix = get_stock("^VIX")

    if not spy or not qqq or not vix:
        send("⚠️ 데이터 수집 실패 (API)")
        return

    spy_cur, spy_prev, spy_sma = spy
    qqq_cur, qqq_prev, qqq_sma = qqq
    vix_cur, vix_prev, _ = vix

    dxy = get_fred("DTWEXBGS")
    fx, fx1, fx2 = get_fx()

    spy_gap = sma_gap(spy_cur, spy_sma)
    qqq_gap = sma_gap(qqq_cur, qqq_sma)

    spy_sig = sma_signal(spy_gap)
    qqq_sig = sma_signal(qqq_gap)

    quant_score = calc_score(spy_gap, qqq_gap, vix_cur, dxy)
    total_score = quant_score + int(round(ai_score * 0.7))
    total_score = max(-2, min(10, total_score))

    st, act = stage(total_score)

    # 알림 제어
    send_flag = False

    if total_score >= 5:
        send_flag = True
    elif total_score >= 3:
        if hour % 3 == 0:
            send_flag = True
    else:
        if hour % 6 == 0:
            send_flag = True

    if 22 <= hour or hour <= 5:
        if total_score >= 3:
            send_flag = True

    if not send_flag:
        return

    msg = f"""🤖 퀀텀 인사이트 봇
🤖 AI
{ai_reason}

🛡️ 상태: {st} ({total_score}점) → {act}

📊 시장
SPY {spy_cur:.2f} ({change_pct(spy_cur, spy_prev):+.2f}%) {spy_sig} {spy_gap:+.1f}%
QQQ {qqq_cur:.2f} ({change_pct(qqq_cur, qqq_prev):+.2f}%) {qqq_sig} {qqq_gap:+.1f}%
VIX {vix_cur:.2f} ({change_pct(vix_cur, vix_prev):+.2f}%)

💱 환율 {fx:.0f} (1Y {fx1:.0f} / 2Y {fx2:.0f}, {"고평가" if fx > fx1 else "저평가"})
"""

    print(msg)
    send(msg)

if __name__ == "__main__":
    main()
