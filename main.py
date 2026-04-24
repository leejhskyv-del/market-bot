import requests
import os
import json
import feedparser
import re
import time
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
# 공통 유틸
# ==========================================
def safe_request(func, retry=3, delay=2):
    for i in range(retry):
        try:
            data = func()
            if data:
                return data
        except Exception as e:
            print("재시도 에러:", e)
        time.sleep(delay)
    return None

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
# AI
# ==========================================
def get_ai_score(news):
    if not news:
        return 0, "뉴스 없음", "없음"

    prompt = f"""
뉴스를 보고 시장 영향 2~3줄 요약
JSON:
{{"score": int, "reason": "", "category": ""}}
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
# Alpha
# ==========================================
def get_stock(symbol):
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "apikey": ALPHA_API_KEY
    }

    # 🚨 여기가 수정된 핵심 포인트입니다! (params=params 추가)
    data = requests.get(url, params=params).json()

    if "Time Series (Daily)" not in data:
        print(f"{symbol} 오류:", data)
        return None

    ts = data["Time Series (Daily)"]
    dates = list(ts.keys())

    cur = float(ts[dates[0]]["4. close"])
    prev = float(ts[dates[1]]["4. close"])

    closes = [float(ts[d]["4. close"]) for d in dates[:200]]
    sma200 = sum(closes) / len(closes)

    return cur, prev, sma200

def get_fx():
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "FX_DAILY",
        "from_symbol": "USD",
        "to_symbol": "KRW",
        "apikey": ALPHA_API_KEY
    }

    # 🚨 여기도 완벽하게 수정했습니다! (params=params 추가)
    data = requests.get(url, params=params).json()

    if "Time Series FX (Daily)" not in data:
        print("환율 오류:", data)
        return None

    ts = data["Time Series FX (Daily)"]
    dates = list(ts.keys())

    closes = [float(ts[d]["4. close"]) for d in dates[:500]]

    cur = closes[0]
    avg1 = sum(closes[:252]) / 252
    avg2 = sum(closes[:500]) / 500

    return cur, avg1, avg2

# ==========================================
# FRED
# ==========================================
def get_fred(series):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series,
        "api_key": FRED_API_KEY,
        "file_type": "json"
    }

    data = requests.get(url, params=params).json()
    obs = data.get("observations", [])
    values = [float(o["value"]) for o in obs if o["value"] != "."]

    if len(values) >= 2:
        return values[-1], values[-2]
    return 0, 0

# ==========================================
# 계산
# ==========================================
def pct(cur, prev):
    return ((cur - prev) / prev) * 100

def gap(cur, sma):
    return ((cur - sma) / sma) * 100

def signal(g):
    return "🟢" if g > 3 else "🟡" if g > 0 else "🔴"

# ==========================================
# 점수
# ==========================================
def calc(spy_g, qqq_g, vix, dxy):
    score = 0

    if spy_g < -3: score += 2
    elif spy_g < -1: score += 1

    if qqq_g < -3: score += 2
    elif qqq_g < -1: score += 1

    if vix > 25: score += 2
    elif vix > 20: score += 1

    if dxy > 105: score += 1

    return score

def stage(s):
    if s >= 5: return "🔴 위험", "비중 축소"
    elif s >= 3: return "🟠 경고", "부분 익절"
    elif s >= 1: return "🟡 주의", "관망"
    else: return "🟢 정상", "보유"

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

    spy = safe_request(lambda: get_stock("SPY"))
    time.sleep(2)

    qqq = safe_request(lambda: get_stock("QQQ"))
    time.sleep(2)

    fx = safe_request(get_fx)

    if not spy or not qqq:
        send("⚠️ 핵심 지표 실패")
        return

    if not fx:
        fx = (0, 0, 0)

    spy_c, spy_p, spy_s = spy
    qqq_c, qqq_p, qqq_s = qqq
    fx_c, fx1, fx2 = fx

    vix_c, vix_p = get_fred("VIXCLS")
    dxy_c, _ = get_fred("DTWEXBGS")

    spy_g = gap(spy_c, spy_s)
    qqq_g = gap(qqq_c, qqq_s)

    score = calc(spy_g, qqq_g, vix_c, dxy_c)
    score += int(round(ai_score * 0.7))
    score = max(0, min(10, score))

    st, act = stage(score)

    msg = f"""🤖 퀀텀 인사이트
{ai_reason}

상태: {st} ({score}) → {act}

SPY {spy_c:.2f} ({pct(spy_c, spy_p):+.2f}%) {signal(spy_g)}
QQQ {qqq_c:.2f} ({pct(qqq_c, qqq_p):+.2f}%) {signal(qqq_g)}
VIX {vix_c:.2f} ({pct(vix_c, vix_p):+.2f}%)

환율 {fx_c:.0f} (1Y {fx1:.0f} / 2Y {fx2:.0f})
"""

    print(msg)
    send(msg)

if __name__ == "__main__":
    main()
