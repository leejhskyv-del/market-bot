import requests
import os
import json
import feedparser
import re
import time
from datetime import datetime, timedelta
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
# 안전 호출
# ==========================================
def safe(func, retry=3, delay=1):
    for _ in range(retry):
        try:
            res = func()
            if res:
                return res
        except:
            pass
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
    return {"score": 0, "reason": "분석 실패"}

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

    text = " ".join(headlines)
    return text if text else "최근 경제 뉴스 없음"

# ==========================================
# AI (리스크 기준 명확화)
# ==========================================
def get_ai(news):
    prompt = f"""
반드시 한국어로만 작성.

뉴스 기반 시장 영향 2~3줄 요약.

점수(score) 기준:
- 심각한 악재/공포: +2
- 일반적 악재: +1
- 중립: 0
- 일반적 호재: -1
- 강력한 호재: -2

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
        return 0, "AI 오류"

# ==========================================
# FRED
# ==========================================
def get_series(series):
    try:
        start_date = (datetime.now() - timedelta(days=1000)).strftime('%Y-%m-%d')

        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "observation_start": start_date
        }

        res = requests.get(url, params=params, timeout=5)
        if res.status_code != 200:
            return None

        data = res.json()
        obs = data.get("observations", [])

        values = [float(o["value"]) for o in obs if o["value"] != "."]

        if len(values) < 50:
            return None

        return values
    except:
        return None

# ==========================================
# 지수
# ==========================================
def get_index(series):
    v = get_series(series)
    if not v:
        return None
    return v[-1], v[-2], sum(v[-min(200,len(v)):]) / min(200,len(v))

# ==========================================
# 금리 자동 보정
# ==========================================
def get_rate_full():
    data = get_series("DGS10")
    if not data:
        return None

    current = data[-1]
    prev = data[-2]
    avg_1y = sum(data[-252:]) / 252 if len(data)>=252 else current
    avg_2y = sum(data[-500:]) / 500 if len(data)>=500 else avg_1y

    return current, prev, avg_1y, avg_2y

# ==========================================
# 환율 (네이버 + FRED 평균)
# ==========================================
def get_fx_current():
    try:
        url = "https://finance.naver.com/marketindex/exchangeDetail.naver?marketindexCd=FX_USDKRW"
        res = requests.get(url, timeout=5).text
        price = re.search(r"(\d{3,4}\.\d{2})", res)
        return float(price.group(1)) if price else None
    except:
        return None

# ==========================================
# 계산
# ==========================================
def pct(c,p): return (c-p)/p*100
def gap(c,s): return (c-s)/s*100
def momentum(c,p): return (c-p)/p*100

def trend_status(g):
    if g > 5: return f"🟢 강상승 (+{g:.1f}%)"
    elif g > 0: return f"🔵 상승 (+{g:.1f}%)"
    elif g > -3: return f"🟡 경계 ({g:.1f}%)"
    else: return f"🔴 하락 ({g:.1f}%)"

# ==========================================
# 패닉
# ==========================================
def check_panic(vix, spy_m):
    return vix > 35 or spy_m < -4

# ==========================================
# 점수 계산 (리스크형)
# ==========================================
def calc_total(spy_g, qqq_g, spy_m, qqq_m, vix, dxy, rate_tuple, ai_s):

    score = 0

    if spy_g < -3: score += 2
    elif spy_g < -1: score += 1
    if qqq_g < -3: score += 2
    elif qqq_g < -1: score += 1

    if spy_m < -2: score += 1
    if qqq_m < -2: score += 1

    if vix > 28: score += 2
    elif vix > 22: score += 1

    if dxy > 105: score += 1

    if rate_tuple:
        c, p, a1, a2 = rate_tuple

        if c > a1 * 1.1: score += 1
        elif c > a1 * 1.05: score += 0.5

        if (c - p) > 0.07: score += 0.5

        if c > a2 * 1.15: score += 0.5

    ai_score = int(round(ai_s * 0.6))
    ai_score = max(-2, min(2, ai_score))

    panic = check_panic(vix, spy_m)

    if not panic and 3 <= score <= 8:
        score += ai_score

    return score, panic

# ==========================================
# 상태
# ==========================================
def get_stage(score, panic):
    if panic: return "💀 패닉","분할 매수"
    if score <= 2: return "🟢 공격","매수"
    elif score <= 5: return "🔵 상승","매수 유지"
    elif score <= 7: return "🟡 중립","속도 조절"
    elif score <= 10: return "🟠 경고","매수 중단"
    else: return "🔴 위험","비중 축소"

# ==========================================
# 자동매수
# ==========================================
def auto_buy(score, panic):
    if panic: return "🚀 150~200% (분할)"
    if score <= 5: return "✅ 100%"
    elif score <= 7: return "⚠️ 30%"
    else: return "⛔ STOP"

# ==========================================
# 텔레그램
# ==========================================
def get_last_msg():
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        res = requests.get(url).json()
        msgs = res.get("result", [])
        return msgs[-1]["message"]["text"] if msgs else None
    except:
        return None

def parse_prev(text):
    try:
        score = int(re.search(r"\((\d+)\)", text).group(1))
        state = re.search(r"상태: (.+?) \(", text).group(1)
        return score, state
    except:
        return None, None

def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id":CHAT_ID,"text":msg},
            timeout=5
        )
    except:
        pass

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

    vix_data = safe(lambda:get_series("VIXCLS"))
    dxy_data = safe(lambda:get_series("DTWEXBGS"))
    fx_data = safe(lambda:get_series("DEXKOUS"))
    fx_current = safe(get_fx_current)
    rate_tuple = safe(get_rate_full)

    vix = vix_data[-1] if vix_data else 22
    dxy = dxy_data[-1] if dxy_data else 100

    if fx_data:
        fx1 = sum(fx_data[-252:]) / 252
        fx2 = sum(fx_data[-500:]) / 500 if len(fx_data)>=500 else fx1
    else:
        fx1 = fx2 = fx_current if fx_current else 1400

    fx = fx_current if fx_current else fx1

    spy_g = gap(spy_c, spy_s)
    qqq_g = gap(qqq_c, qqq_s)

    spy_m = momentum(spy_c, spy_p)
    qqq_m = momentum(qqq_c, qqq_p)

    score, panic = calc_total(
        spy_g, qqq_g,
        spy_m, qqq_m,
        vix, dxy,
        rate_tuple,
        ai_s
    )

    st, act = get_stage(score, panic)
    auto = auto_buy(score, panic)

    spy_trend = trend_status(spy_g)
    qqq_trend = trend_status(qqq_g)

    msg = f"""🤖 퀀텀 인사이트

{ai_r}

상태: {st} ({int(score)}) → {act}
자동매수: {auto}

📊 시장
SP500 {spy_c:.0f} ({pct(spy_c,spy_p):+.2f}%)
→ {spy_trend}

NASDAQ {qqq_c:.0f} ({pct(qqq_c,qqq_p):+.2f}%)
→ {qqq_trend}

🌪 리스크
VIX {vix:.2f}

💱 환율
USD/KRW {fx:.0f}
(1Y {fx1:.0f} / 2Y {fx2:.0f})
"""

    prev_text = get_last_msg()
    prev_score, prev_state = parse_prev(prev_text) if prev_text else (None, None)

    send_flag = False

    if panic:
        send_flag = True
    elif prev_state != st:
        send_flag = True
    elif prev_score is not None and abs(score - prev_score) >= 2:
        send_flag = True

    if send_flag:
        send(msg)
        print("전송")
    else:
        print("변화 없음")

if __name__=="__main__":
    main()
