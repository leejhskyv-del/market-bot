import requests
import os
import json
import feedparser
import re
import time
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
# AI
# ==========================================
def get_ai(news):
    prompt = f"""
반드시 한국어로만 작성.
뉴스 기반 시장 영향 2~3줄 요약.

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
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {"series_id": series, "api_key": FRED_API_KEY, "file_type": "json"}

        res = requests.get(url, params=params, timeout=5)
        if res.status_code != 200:
            return None

        data = res.json()
        obs = data.get("observations", [])

        values = [float(o["value"]) for o in obs if o["value"] != "."]

        if len(values) < 50:
            return None

        if not values or all(v == 0 for v in values):
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
    avg_1y = sum(data[-252:]) / 252 if len(data) >= 252 else current
    avg_2y = sum(data[-500:]) / 500 if len(data) >= 500 else avg_1y

    return current, prev, avg_1y, avg_2y

# ==========================================
# 계산
# ==========================================
def pct(c,p): return (c-p)/p*100
def gap(c,s): return (c-s)/s*100
def momentum(c,p): return (c-p)/p*100

# ==========================================
# 패닉
# ==========================================
def check_panic(vix, spy_m):
    return vix > 35 or spy_m < -4

# ==========================================
# 점수 계산
# ==========================================
def calc_total(spy_g, qqq_g, spy_m, qqq_m, vix, dxy, rate_tuple, ai_s):

    score = 0

    # 추세
    if spy_g < -3: score += 2
    elif spy_g < -1: score += 1
    if qqq_g < -3: score += 2
    elif qqq_g < -1: score += 1

    # 모멘텀
    if spy_m < -2: score += 1
    if qqq_m < -2: score += 1

    # VIX (튜닝)
    if vix > 30: score += 2
    elif vix > 24: score += 1

    if dxy > 105: score += 1

    # 금리 자동 보정
    if rate_tuple:
        current, prev, avg_1y, avg_2y = rate_tuple

        if current > avg_1y * 1.1:
            score += 1
        elif current > avg_1y * 1.05:
            score += 0.5

        if (current - prev) > 0.07:
            score += 0.5

        if current > avg_2y * 1.15:
            score += 0.5

    # AI
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
    if score <= 3: return "🟢 공격","매수"
    elif score <= 6: return "🔵 상승","매수 유지"
    elif score <= 8: return "🟡 중립","속도 조절"
    elif score <= 11: return "🟠 경고","매수 중단"
    else: return "🔴 위험","비중 축소"

# ==========================================
# 자동매수
# ==========================================
def auto_buy(score, panic):
    if panic: return "🚀 150~200% (분할)"
    if score <= 6: return "✅ 100%"
    elif score <= 8: return "⚠️ 50%"
    else: return "⛔ STOP"

# ==========================================
# 텔레그램
# ==========================================
def get_last_msg():
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        res = requests.get(url).json()
        msgs = res.get("result", [])
        if not msgs:
            return None
        return msgs[-1]["message"]["text"]
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
    rate_tuple = safe(get_rate_full)

    data_fail = False
    warn = []

    if not vix_data:
        data_fail=True; warn.append("VIX")
    if not dxy_data:
        data_fail=True; warn.append("DXY")
    if not rate_tuple:
        warn.append("금리")

    warn_text = "⚠️ 데이터 오류: " + ", ".join(warn) if warn else ""

    vix = vix_data[-1] if vix_data else 24
    dxy = dxy_data[-1] if dxy_data else 100
    fx_c = fx_data[-1] if fx_data else 1400

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

    if data_fail:
        score = min(score, 8)

    st, act = get_stage(score, panic)
    auto = auto_buy(score, panic)

    msg = f"""🤖 퀀텀 인사이트

{ai_r}

{warn_text}

상태: {st} ({int(score)}) → {act}
자동매수: {auto}

SP500 {spy_c:.0f} ({pct(spy_c,spy_p):+.2f}%)
NASDAQ {qqq_c:.0f} ({pct(qqq_c,qqq_p):+.2f}%)
VIX {vix:.2f}
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
    elif data_fail:
        send_flag = True

    if send_flag:
        send(msg)
        print("전송")
    else:
        print("변화 없음")

if __name__=="__main__":
    main()
