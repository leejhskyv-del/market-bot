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
# 🥇 로깅(Logging) 설정
# ==========================================
logging.basicConfig(
    filename="bot_operation.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8"
)

def log(msg, level="info"):
    print(msg)
    if level == "info": logging.info(msg)
    elif level == "error": logging.error(msg)

log("🚀 퀀텀 인사이트 시스템 구동 시작")

# ==========================================
# 환경 변수
# ==========================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FRED_API_KEY = os.getenv("FRED_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

# ==========================================
# 파라미터 (튜닝 변수)
# ==========================================
VIX_HIGH = 28
VIX_WARN = 22
SPY_OVERHEAT_HIGH = 18
SPY_OVERHEAT_WARN = 12
DXY_WARN = 105
PANIC_VIX = 35
PANIC_SPY_MOMENTUM = -4

# ==========================================
# 🧠 상태 저장 (불사신 로직)
# ==========================================
STATE_FILE = "bot_state.json"

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log(f"상태 파일 로드 실패: {e}", "error")
    return {}

def save_state(score, state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"score": score, "state": state}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"상태 파일 저장 실패: {e}", "error")

# ==========================================
# 유틸리티 함수 모음
# ==========================================
def safe(func, retry=3, delay=1):
    for _ in range(retry):
        try:
            res = func()
            if res: return res
        except: pass
        time.sleep(delay)
    return None

def extract_json(text):
    try: return json.loads(text.strip())
    except:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try: return json.loads(match.group())
            except: pass
    return {"score": 0, "reason": "분석 실패"}

# ==========================================
# 뉴스 & AI 로직
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
        except: continue
    text = " ".join(headlines)
    return text if text else "최근 경제 뉴스 없음"

def get_ai(news):
    prompt = f"""
너는 냉철한 거시경제 리스크 분석가다.
최근 경제 뉴스를 기반으로 미국 주식시장에 미치는 영향을 2~3줄 요약하라.

리스크 점수 기준:
- 강한 악재/패닉: +2
- 보통 악재: +1
- 중립: 0
- 보통 호재: -1
- 강한 호재: -2

JSON 형식: {{"score": int, "reason": "간결한 한국어 설명"}}
뉴스: {news}
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
# FRED & 시장 데이터
# ==========================================
def get_series(series):
    try:
        start_date = (datetime.now() - timedelta(days=1000)).strftime('%Y-%m-%d')
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {"series_id": series, "api_key": FRED_API_KEY, "file_type": "json", "observation_start": start_date}
        res = requests.get(url, params=params, timeout=5)
        if res.status_code != 200: return None
        obs = res.json().get("observations", [])
        values = [float(o["value"]) for o in obs if o["value"] != "."]
        return values if len(values) >= 50 else None
    except: return None

def get_index(series):
    v = get_series(series)
    if not v: return None
    return v[-1], v[-2], sum(v[-min(200,len(v)):]) / min(200,len(v))

def get_rate_full():
    data = get_series("DGS10")
    if not data: return None
    c, p = data[-1], data[-2]
    a1 = sum(data[-252:]) / 252 if len(data)>=252 else c
    a2 = sum(data[-500:]) / 500 if len(data)>=500 else a1
    return c, p, a1, a2

def get_fx_current():
    try:
        url = "https://finance.naver.com/marketindex/exchangeList.naver"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5).text
        row = re.search(r"<td class=\"tit\">.*?USD.*?</tr>", res, re.DOTALL)
        if not row: return None
        price = re.search(r"<td class=\"sale\">([\d,]+\.\d+)</td>", row.group())
        if price:
            fx = float(price.group(1).replace(",", ""))
            if 1000 < fx < 2000: return fx
    except: return None

# ==========================================
# 🥈 환율 소스(Source) 추적 로직 추가
# ==========================================
def get_fx_final():
    fx_now = safe(get_fx_current)
    if fx_now: return fx_now, "NAVER"
    
    log("⚠️ 네이버 환율 실패 -> FRED(플랜B) 가동", "error")
    fx_data = safe(lambda:get_series("DEXKOUS"))
    if fx_data: return fx_data[-1], "FRED"
    
    log("🚨 환율 완전 실패 -> 기본값 사용", "error")
    return 1400, "DEFAULT"

# ==========================================
# 🛠 META 상태 생성기
# ==========================================
def build_meta_status(fx_source, vix_data, dxy_data, spy, qqq):
    issues = []
    if fx_source != "NAVER": issues.append("환율대체")
    if not vix_data: issues.append("VIX 오류")
    if not dxy_data: issues.append("DXY 오류")
    if not spy or not qqq: issues.append("지수 오류")
    
    if issues:
        return "⚠️ " + ", ".join(issues)
    else:
        return "✅ 정상"

# ==========================================
# 계산 및 상태 판별
# ==========================================
def pct(c,p): return (c-p)/p*100
def gap(c,s): return (c-s)/s*100
def momentum(c,p): return (c-p)/p*100

def trend_status(g):
    if g > 5: return f"🟢 강상승 (+{g:.1f}%)"
    elif g > 0: return f"🔵 상승 (+{g:.1f}%)"
    elif g > -3: return f"🟡 경계 ({g:.1f}%)"
    else: return f"🔴 하락 ({g:.1f}%)"

def check_panic(vix, spy_m): return vix > PANIC_VIX or spy_m < PANIC_SPY_MOMENTUM

def calc_total(spy_g, qqq_g, spy_m, qqq_m, vix, dxy, rate_tuple, ai_s):
    score = 0
    if spy_g < -3: score += 2
    elif spy_g < -1: score += 1
    if qqq_g < -3: score += 2
    elif qqq_g < -1: score += 1
    if spy_m < -2: score += 1
    if qqq_m < -2: score += 1

    if spy_g > SPY_OVERHEAT_HIGH: score += 2
    elif spy_g > SPY_OVERHEAT_WARN: score += 1

    if vix > VIX_HIGH: score += 2
    elif vix > VIX_WARN: score += 1
    if dxy > DXY_WARN: score += 1

    if rate_tuple:
        c, p, a1, a2 = rate_tuple
        if c > a1 * 1.1: score += 1
        elif c > a1 * 1.05: score += 0.5
        if (c - p) > 0.07: score += 0.5
        if c > a2 * 1.15: score += 0.5

    ai_score = max(-2, min(2, int(round(ai_s * 0.6))))
    panic = check_panic(vix, spy_m)
    if not panic and 3 <= score <= 8: score += ai_score

    return score, panic

def get_stage(score, panic):
    if panic: return "💀 패닉","분할 매수"
    if score <= 2: return "🟢 공격","매수"
    elif score <= 5: return "🔵 상승","매수 유지"
    elif score <= 7: return "🟡 중립","속도 조절"
    elif score <= 10: return "🟠 경고","매수 중단"
    else: return "🔴 위험","비중 축소"

def auto_buy(score, panic):
    if panic: return "🚀 150~200% (분할)"
    if score <= 5: return "✅ 100%"
    elif score <= 7: return "⚠️ 30%"
    else: return "⛔ STOP"

def send(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id":CHAT_ID,"text":msg}, timeout=5)
    except Exception as e:
        log(f"텔레그램 전송 오류: {e}", "error")

# ==========================================
# 메인
# ==========================================
def main():
    log("데이터 수집 시작...")
    
    news = fetch_news()
    ai_s, ai_r = get_ai(news)

    # 데이터 일괄 수집
    spy = safe(lambda:get_index("SP500"))
    qqq = safe(lambda:get_index("NASDAQCOM"))
    vix_data = safe(lambda:get_series("VIXCLS"))
    dxy_data = safe(lambda:get_series("DTWEXBGS"))
    fx_data = safe(lambda:get_series("DEXKOUS"))
    rate_tuple = safe(get_rate_full)
    fx, fx_source = get_fx_final()

    # META 상태 먼저 판별
    meta_status = build_meta_status(fx_source, vix_data, dxy_data, spy, qqq)

    # 지수 실패 시 로직 중단 및 META 상태 즉각 전송
    if not spy or not qqq:
        log("⚠️ 지수 데이터 실패", "error")
        send(f"⚠️ 지수 데이터 수집 실패로 계산 중단\n\n━━━━━━━━━━━━━━━\n🛠 META\n{meta_status}")
        return

    spy_c, spy_p, spy_s = spy
    qqq_c, qqq_p, qqq_s = qqq

    vix = vix_data[-1] if vix_data else 22
    dxy = dxy_data[-1] if dxy_data else 100

    if fx_data:
        fx1 = sum(fx_data[-252:]) / 252
        fx2 = sum(fx_data[-500:]) / 500 if len(fx_data)>=500 else fx1
    else:
        fx1 = fx2 = fx

    spy_g = gap(spy_c, spy_s)
    qqq_g = gap(qqq_c, qqq_s)
    spy_m = momentum(spy_c, spy_p)
    qqq_m = momentum(qqq_c, qqq_p)

    score, panic = calc_total(spy_g, qqq_g, spy_m, qqq_m, vix, dxy, rate_tuple, ai_s)
    st, act = get_stage(score, panic)
    auto = auto_buy(score, panic)
    spy_trend = trend_status(spy_g)
    qqq_trend = trend_status(qqq_g)

    # META 블록이 추가된 최종 메시지
    msg = f"""🤖 퀀텀 인사이트

{ai_r}

상태: {st} ({int(score)}) → {act}
자동매수: {auto}

📊 시장
SP500 {spy_c:.0f} ({pct(spy_c,spy_p):+.2f}%)
→ {spy_trend}

NASDAQ {qqq_c:.0f} ({pct(qqq_c,qqq_p):+.2f}%)
→ {qqq_trend}

⚠️ 리스크
VIX {vix:.2f}

💰 환율
USD/KRW {fx:.0f} ({fx_source})
(1Y {fx1:.0f} / 2Y {fx2:.0f})

━━━━━━━━━━━━━━━
🛠 META
{meta_status}
"""

    prev = load_state()
    prev_score = prev.get("score")
    prev_state = prev.get("state")

    send_flag = False

    if panic:
        send_flag = True
        log("💀 패닉 조건 충족 - 무조건 전송")
    elif prev_state != st:
        send_flag = True
        log(f"상태 변화 감지: {prev_state} -> {st}")
    elif prev_score is not None and abs(score - prev_score) >= 2:
        send_flag = True
        log(f"점수 유의미한 변화: {prev_score} -> {score}")

    if send_flag:
        send(msg)
        save_state(score, st)
        log("✅ 텔레그램 메시지 전송 및 상태 저장 완료")
    else:
        log(f"변화 없음 (현재 상태: {st}, 점수: {score}) - 전송 스킵")

if __name__=="__main__":
    try: main()
    except Exception as e: log(f"치명적 오류 발생: {e}", "error")
