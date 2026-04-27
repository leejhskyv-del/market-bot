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
# 🥇 인프라 세팅 (로그 & 환경변수)
# ==========================================
logging.basicConfig(filename="bot_operation.log", level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", encoding="utf-8")
def log(msg, level="info"):
    print(msg); logging.info(msg) if level == "info" else logging.error(msg)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FRED_API_KEY = os.getenv("FRED_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
client = OpenAI(api_key=OPENAI_API_KEY)

STATE_FILE = "bot_state.json"

# ==========================================
# 🛡️ 데이터 수집 및 상태 관리
# ==========================================
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: pass
    return {}

def save_state(score, state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"score": score, "state": state}, f, ensure_ascii=False, indent=2)
    except: pass

def safe(func, retry=5, delay=60):
    for i in range(retry):
        try:
            res = func()
            if res: return res
        except Exception as e: log(f"⚠️ {i+1}차 시도 에러: {e}", "error")
        if i < retry - 1:
            log(f"⏳ 데이터 갱신 대기 중... ({i+1}/5)"); time.sleep(delay)
    return None

def get_series(series):
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {"series_id": series, "api_key": FRED_API_KEY, "file_type": "json", "observation_start": (datetime.now() - timedelta(days=1000)).strftime('%Y-%m-%d')}
        res = requests.get(url, params=params, timeout=15).json()
        v = [float(o["value"]) for o in res.get("observations", []) if o["value"] != "."]
        return v if len(v) >= 50 else None
    except: return None

def get_index(series):
    v = get_series(series)
    return (v[-1], v[-2], sum(v[-min(200,len(v)):]) / min(200,len(v))) if v else None

def get_fx_final():
    try:
        url = "https://finance.naver.com/marketindex/exchangeList.naver"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text
        price = re.search(r"<td class=\"sale\">([\d,]+\.\d+)</td>", re.search(r"<td class=\"tit\">.*?USD.*?</tr>", res, re.DOTALL).group())
        if price: return float(price.group(1).replace(",", "")), "NAVER"
    except: pass
    fx_v = safe(lambda:get_series("DEXKOUS"), retry=2, delay=5)
    return (fx_v[-1], "FRED") if fx_v else (1400, "DEFAULT")

def get_gold():
    try:
        res = requests.get("https://query2.finance.yahoo.com/v8/finance/chart/GC=F?interval=1d&range=1y", headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
        v = [float(val) for val in res["chart"]["result"][0]["indicators"]["quote"][0]["close"] if val is not None]
        return v[-1], v[-2], v[-20], sum(v[-252:]) / 252
    except: return None

# ==========================================
# 🧠 AI 분석 (전략가 모드 & 0.5 가중치)
# ==========================================
def get_ai_analysis(news, market_summary):
    prompt = f"너는 거시경제 분석가다. 지표({market_summary})와 뉴스({news})를 보고 JSON 리포트하라. 점수(+2~-2), summary(요약), risk(요인), strategy(전략). 반드시 JSON 형식."
    try:
        r = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"user","content":prompt}], response_format={"type":"json_object"})
        return json.loads(r.choices[0].message.content)
    except: return {"score": 0, "summary": "분석 지연", "risk": "확인 중", "strategy": "관망"}

def calc_final_score(spy_g, spy_m, vix, dxy, ai_score):
    s = 0
    if spy_g < -1: s += 1
    if spy_g < -3: s += 2
    if spy_m < -4: s += 2
    if vix > 22: s += 1
    if vix > 28: s += 2
    if vix > 45: s += 4 # 블랙스완 방어막
    if dxy > 105: s += 1
    return max(0, min(15, s + (ai_score * 0.5))) # AI 0.5 가중치 적용

def get_stage(s, p):
    if p: return "💀 패닉", "분할 매수"
    stages = [("🟢 공격", "매수"), ("🔵 상승", "매수 유지"), ("🟡 중립", "속도 조절"), ("🟠 경고", "매수 중단"), ("🔴 위험", "비중 축소")]
    return stages[min(4, int(s // 3))]

# ==========================================
# 🚀 메인 프로세스
# ==========================================
def pct(c,p): return (c-p)/p*100
def gap(c,s): return (c-s)/s*100

def main():
    log("📊 퀀텀 인사이트 리포트 생성 시작")
    spy = safe(lambda:get_index("SP500")); qqq = safe(lambda:get_index("NASDAQCOM")); kospi = safe(lambda:get_index("KOSPI"))
    
    if not spy or not qqq or not kospi:
        log("⚠️ 지수 데이터 미준비로 종료", "info"); return

    vix_v = safe(lambda:get_series("VIXCLS")); dxy_v = safe(lambda:get_series("DTWEXBGS"))
    fx, fx_s = get_fx_final(); gold = get_gold()
    spy_c, spy_p, spy_s = spy; vix = vix_v[-1] if vix_v else 22; dxy = dxy_v[-1] if dxy_v else 100
    
    news = " ".join([e.title for e in feedparser.parse(requests.get("https://finance.yahoo.com/news/rssindex").text).entries[:5]])
    ai = get_ai_analysis(news, f"SP500:{spy_c}, VIX:{vix}, DXY:{dxy}")
    
    total_score = calc_final_score(gap(spy_c, spy_s), pct(spy_c, spy_p), vix, dxy, ai['score'])
    st, act = get_stage(total_score, (vix > 35 or pct(spy_c, spy_p) < -4))

    # 어제 점수 비교 로직
    prev = load_state()
    diff = total_score - prev.get("score", total_score)
    diff_str = f"{diff:+.1f}"
    diff_icon = "📈" if diff > 0 else "📉" if diff < 0 else "➖"

    msg = f"""🤖 퀀텀 인사이트: 데일리 리포트

📌 요약: {ai['summary']}
⚠️ 리스크: {ai['risk']}
📍 전략: {ai['strategy']}

━━━━━━━━━━━━━━━
상태: {st} ({total_score:.1f}) {diff_icon} {diff_str}
→ {act}

📊 시장 현황
- S&P500: {spy_c:.0f} ({pct(spy_c, spy_p):+.2f}%)
- NASDAQ: {qqq_c:.0f} ({pct(qqq_c, qqq_p):+.2f}%)
- KOSPI: {kospi_c:.0f} ({pct(kospi_c, kospi_p):+.2f}%)
- VIX: {vix:.2f} / 환율: {fx:.0f}
- 금: {f"{gold[0]:.0f}" if gold else "지연"}

🛠 META: {'✅ 정상' if vix < 35 else '🚨 패닉 감지'}
"""
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
    save_state(total_score, st)
    log("✅ 데일리 리포트 전송 완료")

if __name__=="__main__":
    try: main()
    except Exception as e: log(f"오류: {e}", "error")
