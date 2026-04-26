import requests
import os
import json
import feedparser
import re
import time
import logging
from datetime import datetime, timedelta
from openai import OpenAI

# ==========================================
# 🔧 환경 변수
# ==========================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FRED_API_KEY = os.getenv("FRED_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

# ==========================================
# 📜 로깅
# ==========================================
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def log(msg, level="info"):
    print(msg)
    if level == "error":
        logging.error(msg)
    else:
        logging.info(msg)

# ==========================================
# ⚙️ 파라미터
# ==========================================
VIX_HIGH = 28
VIX_MID = 22
SPY_OVERHEAT_HIGH = 18
SPY_OVERHEAT_MID = 12

# ==========================================
# 🛡 safe 호출
# ==========================================
def safe(func, retry=3, delay=1):
    for _ in range(retry):
        try:
            res = func()
            if res:
                return res
        except Exception as e:
            log(f"재시도 오류: {e}", "error")
        time.sleep(delay)
    return None

# ==========================================
# 📊 FRED
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

        log(f"🔍 FRED 호출 시작: {series}")
        res = requests.get(url, params=params, timeout=5)

        log(f"📡 {series} 상태코드: {res.status_code}")

        if res.status_code != 200:
            log(f"❌ {series} 응답 오류: {res.text[:200]}", "error")
            return None

        data = res.json()
        obs = data.get("observations", [])

        log(f"📊 {series} obs 개수: {len(obs)}")

        values = [float(o["value"]) for o in obs if o["value"] != "."]

        log(f"✅ {series} 유효 데이터 개수: {len(values)}")

        if len(values) < 50:
            log(f"⚠️ {series} 데이터 부족 (<50)", "error")
            return None

        return values

    except Exception as e:
        log(f"🚨 {series} 예외 발생: {e}", "error")
        return None

# ==========================================
# 📈 지수
# ==========================================
def get_index(series):
    v = get_series(series)
    if not v:
        return None
    return v[-1], v[-2], sum(v[-200:]) / 200

# ==========================================
# 💰 환율 (네이버 + fallback)
# ==========================================
def get_fx_current():
    try:
        url = "https://finance.naver.com/marketindex/exchangeList.naver"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=5).text

        row = re.search(r"<td class=\"tit\">.*?USD.*?</tr>", res, re.DOTALL)
        if not row:
            return None

        price = re.search(r"<td class=\"sale\">([\d,]+\.\d+)</td>", row.group())
        if price:
            return float(price.group(1).replace(",", ""))
    except:
        return None

def get_fx():
    fx = safe(get_fx_current)
    if fx:
        return fx, "NAVER"

    log("⚠️ 환율 네이버 실패 → FRED 대체", "error")
    fx_data = safe(lambda:get_series("DEXKOUS"))
    if fx_data:
        return fx_data[-1], "FRED"

    return 1400, "DEFAULT"

# ==========================================
# 🪙 금 (Yahoo + FRED fallback)
# ==========================================
def get_gold():
    try:
        url = "https://query2.finance.yahoo.com/v8/finance/chart/GC=F?interval=1d&range=1y"
        res = requests.get(url, timeout=5)
        if res.status_code != 200:
            return None

        data = res.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        values = [float(v) for v in closes if v is not None]

        if len(values) < 10:
            return None

        c = values[-1]
        p1 = values[-2] if len(values)>=2 else c
        p20 = values[-20] if len(values)>=20 else c
        avg = sum(values)/len(values)

        return c, p1, p20, avg
    except:
        return None

def get_gold_final():
    gold = safe(get_gold)
    if gold:
        return gold, "YAHOO"

    log("⚠️ 금 Yahoo 실패 → FRED 대체", "error")
    data = safe(lambda:get_series("GOLDPMGBD228NLBM"))
    if data:
        c = data[-1]
        p1 = data[-2] if len(data)>=2 else c
        p20 = data[-20] if len(data)>=20 else c
        avg = sum(data[-min(252,len(data)):]) / min(252,len(data))
        return (c,p1,p20,avg), "FRED"

    return None, "FAIL"

# ==========================================
# 🧠 계산
# ==========================================
def pct(c,p): return (c-p)/p*100
def gap(c,s): return (c-s)/s*100

def calc_score(spy_g, qqq_g, vix):
    s=0
    if spy_g<-3: s+=2
    elif spy_g<-1: s+=1

    if qqq_g<-3: s+=2
    elif qqq_g<-1: s+=1

    if spy_g>SPY_OVERHEAT_HIGH: s+=2
    elif spy_g>SPY_OVERHEAT_MID: s+=1

    if vix>VIX_HIGH: s+=2
    elif vix>VIX_MID: s+=1

    return s

# ==========================================
# 📩 텔레그램
# ==========================================
def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id":CHAT_ID,"text":msg},
            timeout=5
        )
    except Exception as e:
        log(f"텔레그램 오류: {e}", "error")

# ==========================================
# 🚀 메인
# ==========================================
def main():
    log("🚀 시스템 시작")
    log("데이터 수집 시작...")

    if not FRED_API_KEY:
        log("🚨 FRED_API_KEY 없음!", "error")
    else:
        log(f"🔑 FRED_API_KEY 확인: {FRED_API_KEY[:5]}***")

    spy = safe(lambda:get_index("SP500"))
    qqq = safe(lambda:get_index("NASDAQCOM"))

    if not spy or not qqq:
        send("⚠️ 지수 데이터 실패 (FRED/API 문제)")
        return

    spy_c, spy_p, spy_s = spy
    qqq_c, qqq_p, qqq_s = qqq

    vix_data = safe(lambda:get_series("VIXCLS"))
    vix = vix_data[-1] if vix_data else 22

    fx, fx_src = get_fx()
    gold, gold_src = get_gold_final()

    spy_g = gap(spy_c, spy_s)
    qqq_g = gap(qqq_c, qqq_s)

    score = calc_score(spy_g, qqq_g, vix)
    state = "🟢 정상" if score < 3 else "🟡 주의"

    msg = f"""🤖 퀀텀 인사이트

상태: {state} ({score})

📊 SP500 {spy_c:.0f} ({pct(spy_c,spy_p):+.2f}%)
📊 NASDAQ {qqq_c:.0f} ({pct(qqq_c,qqq_p):+.2f}%)

⚠️ VIX {vix:.2f}

💰 환율 {fx:.0f} ({fx_src})
🪙 금 {gold_src}
"""

    send(msg)

if __name__=="__main__":
    main()
