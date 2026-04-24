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
FRED_API_KEY = os.getenv("FRED_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

# ==========================================
# 공통
# ==========================================
def safe(func, retry=3):
    for _ in range(retry):
        try:
            r = func()
            if r:
                return r
        except:
            pass
        time.sleep(1)
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
    return {"score": 0, "reason": "파싱 실패", "category": ""}

# ==========================================
# 뉴스
# ==========================================
def fetch_news():
    urls = [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://finance.yahoo.com/news/rssindex"
    ]
    headlines = []

    for url in urls:
        try:
            r = requests.get(url, timeout=5)
            feed = feedparser.parse(r.text)
            headlines += [e.title for e in feed.entries[:5]]
        except:
            continue

    return " ".join(headlines)

# ==========================================
# AI (한국어 강제)
# ==========================================
def get_ai(news):
    prompt = f"""
반드시 한국어만 사용하라.

뉴스를 보고 시장 영향 2~3줄 요약

JSON:
{{"score": int, "reason": ""}}

{news}
"""
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            temperature=0,
            response_format={"type":"json_object"}
        )
        d = extract_json(r.choices[0].message.content)
        return d.get("score",0), d.get("reason","")
    except:
        return 0,"AI 오류"

# ==========================================
# FRED 데이터
# ==========================================
def get_series(series):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series,
        "api_key": FRED_API_KEY,
        "file_type": "json"
    }
    data = requests.get(url, params=params).json()
    obs = data.get("observations", [])
    return [float(o["value"]) for o in obs if o["value"] != "."]

# ==========================================
# 지수 처리
# ==========================================
def get_index(series):
    v = get_series(series)
    if len(v) < 200:
        return None
    cur = v[-1]
    prev = v[-2]
    sma = sum(v[-200:]) / 200
    return cur, prev, sma

# ==========================================
# 계산
# ==========================================
def pct(c,p): return (c-p)/p*100
def gap(c,s): return (c-s)/s*100
def sig(g): return "🟢" if g>3 else "🟡" if g>0 else "🔴"

# ==========================================
# 점수
# ==========================================
def calc(spy_g,qqq_g,vix,dxy):
    s=0
    if spy_g<-3: s+=2
    elif spy_g<-1: s+=1
    if qqq_g<-3: s+=2
    elif qqq_g<-1: s+=1
    if vix>25: s+=2
    elif vix>20: s+=1
    if dxy>105: s+=1
    return s

def stage(s):
    if s>=5: return "🔴 위험","비중 축소"
    elif s>=3: return "🟠 경고","부분 익절"
    elif s>=1: return "🟡 주의","관망"
    else: return "🟢 정상","보유"

# ==========================================
# 텔레그램
# ==========================================
def send(m):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={"chat_id":CHAT_ID,"text":m}
    )

# ==========================================
# 메인
# ==========================================
def main():

    news = fetch_news()
    ai_s, ai_r = get_ai(news)

    spy = safe(lambda:get_index("SP500"))
    qqq = safe(lambda:get_index("NASDAQCOM"))

    if not spy or not qqq:
        send("⚠️ 지수 데이터 실패")
        return

    spy_c, spy_p, spy_s = spy
    qqq_c, qqq_p, qqq_s = qqq

    vix = get_series("VIXCLS")[-1]
    dxy = get_series("DTWEXBGS")[-1]

    fx = get_series("DEXKOUS")
    fx_c = fx[-1]
    fx1 = sum(fx[-252:])/252
    fx2 = sum(fx[-500:])/500

    spy_g = gap(spy_c,spy_s)
    qqq_g = gap(qqq_c,qqq_s)

    score = calc(spy_g,qqq_g,vix,dxy)
    score += int(round(ai_s*0.7))
    score = max(0,min(10,score))

    st,act = stage(score)

    msg=f"""🤖 퀀텀 인사이트

{ai_r}

상태: {st} ({score}) → {act}

SPY {spy_c:.2f} ({pct(spy_c,spy_p):+.2f}%) {sig(spy_g)}
QQQ {qqq_c:.2f} ({pct(qqq_c,qqq_p):+.2f}%) {sig(qqq_g)}
VIX {vix:.2f}

환율 {fx_c:.0f} (1Y {fx1:.0f} / 2Y {fx2:.0f})
"""

    print(msg)
    send(msg)

if __name__=="__main__":
    main()
