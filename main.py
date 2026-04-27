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
# 🥇 기초 인프라 (로그 & 환경변수)
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

# Render 환경변수에서 키 로드
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FRED_API_KEY = os.getenv("FRED_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

# ==========================================
# ⚙️ 리스크 임계치 (전략 파라미터)
# ==========================================
VIX_HIGH = 28
VIX_WARN = 22
SPY_OVERHEAT_HIGH = 18
SPY_OVERHEAT_WARN = 12
DXY_WARN = 105
PANIC_VIX = 35
PANIC_MOMENTUM = -4

# ==========================================
# 🧠 상태 저장 (Persistence)
# ==========================================
STATE_FILE = "bot_state.json"

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except: pass
    return {}

def save_state(score, state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"score": score, "state": state}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"상태 저장 실패: {e}", "error")

# ==========================================
# 🛡️ 강화된 데이터 수집 (1분 간격 재시도)
# ==========================================
def safe(func, retry=5, delay=60):
    for i in range(retry):
        try:
            res = func()
            if res: return res
        except Exception as e:
            log(f"⚠️ {i+1}차 시도 중 예외: {e}", "error")
        if i < retry - 1:
            log(f"⏳ 데이터 갱신 대기 중... ({i+1}/5)")
            time.sleep(delay)
    return None

# ==========================================
# 📊 시장 데이터 수집 함수
# ==========================================
def get_series(series):
    try:
        start_date = (datetime.now() - timedelta(days=1000)).strftime('%Y-%m-%d')
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {"series_id": series, "api_key": FRED_API_KEY, "file_type": "json", "observation_start": start_date}
        res = requests.get(url, params=params, timeout=15)
        if res.status_code != 200: return None
        obs = res.json().get("observations", [])
        values = [float(o["value"]) for o in obs if o["value"] != "."]
        return values if len(values) >= 50 else None
    except: return None

def get_index(series):
    v = get_series(series)
    if not v: return None
    # 현재가, 전일가, 200일 이평선 반환
    return v[-1], v[-2], sum(v[-min(200,len(v)):]) / min(200,len(v))

def get_fx_final():
    # 네이버 실시간 환율 시도
    try:
        url = "https://finance.naver.com/marketindex/exchangeList.naver"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text
        row = re.search(r"<td class=\"tit\">.*?USD.*?</tr>", res, re.DOTALL)
        price = re.search(r"<td class=\"sale\">([\d,]+\.\d+)</td>", row.group())
        if price: return float(price.group(1).replace(",", "")), "NAVER"
    except: pass
    
    # 실패 시 FRED 시도
    fx_data = safe(lambda:get_series("DEXKOUS"), retry=2, delay=5)
    if fx_data: return fx_data[-1], "FRED"
    return 1400, "DEFAULT"

def get_gold():
    try:
        url = "https://query2.finance.yahoo.com/v8/finance/chart/GC=F?interval=1d&range=1y"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = res.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        values = [float(v) for v in closes if v is not None]
        return values[-1], values[-2], values[-20], sum(values[-252:]) / 252
    except: return None

# ==========================================
# 🧠 AI 분석 엔진 (0.5 가중치 리스크 판단)
# ==========================================
def fetch_news():
    urls = ["https://feeds.reuters.com/reuters/businessNews", "https://finance.yahoo.com/news/rssindex"]
    headlines = []
    for url in urls:
        try:
            feed = feedparser.parse(requests.get(url, timeout=5).text)
            headlines += [e.title for e in feed.entries[:5]]
        except: continue
    return " ".join(headlines) if headlines else "뉴스 수집 지연"

def get_ai_analysis(news, market_summary):
    prompt = f"""
너는 월스트리트 출신의 거시경제 리스크 분석가다. 아래 지표와 뉴스를 보고 '오늘의 투자 전략'을 JSON으로 작성하라.
데이터: {market_summary}
뉴스: {news}

분석 포인트: 시스템 리스크, 유동성 위기, 블랙스완 가능성 여부.
점수 기준: 강한 악재(+2) ~ 강한 호재(-2).

JSON 형식:
{{
  "score": int,
  "summary": "시장 한줄 요약",
  "risk": "핵심 리스크 2가지",
  "strategy": "투자 행동 제안"
}}
"""
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            response_format={"type":"json_object"}
        )
        return json.loads(r.choices[0].message.content)
    except:
        return {"score": 0, "summary": "분석 지연", "risk": "데이터 수집 중", "strategy": "관망"}

# ==========================================
# 🧮 종합 리스크 스코어링 (3중 방어막)
# ==========================================
def calc_final_score(spy_g, spy_m, vix, dxy, ai_score):
    score = 0
    # 1단계: 지표 기반 (하드 데이터)
    if spy_g < -1: score += 1
    if spy_g < -3: score += 2
    if spy_m < -4: score += 2  # 급락 모멘텀
    
    if vix > VIX_WARN: score += 1
    if vix > VIX_HIGH: score += 2
    if vix > 45: score += 4    # 블랙스완 임계치
    
    if dxy > DXY_WARN: score += 1
    if spy_g > SPY_OVERHEAT_WARN: score += 1

    # 2단계: AI 분석 기반 (소프트 데이터 - 0.5 가중치 적용)
    score += (ai_score * 0.5)
    
    return max(0, min(15, score))

def get_stage(s, p):
    if p: return "💀 패닉", "분할 매수"
    if s <= 2: return "🟢 공격", "매수"
    elif s <= 5: return "🔵 상승", "매수 유지"
    elif s <= 7: return "🟡 중립", "속도 조절"
    elif s <= 10: return "🟠 경고", "매수 중단"
    else: return "🔴 위험", "비중 축소"

# ==========================================
# 🚀 메인 프로세스
# ==========================================
def pct(c,p): return (c-p)/p*100
def gap(c,s): return (c-s)/s*100
def send(msg):
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except: pass

def main():
    log("📊 퀀텀 인사이트 리포트 생성 시작")
    
    # 1. 핵심 지수 수집 (1분 간격 재시도)
    spy = safe(lambda:get_index("SP500"), retry=5, delay=60)
    qqq = safe(lambda:get_index("NASDAQCOM"), retry=5, delay=60)
    kospi = safe(lambda:get_index("KOSPI"), retry=5, delay=60)

    if not spy or not qqq or not kospi:
        log("⚠️ 지수 데이터 미준비로 종료", "info"); return

    # 2. 보조 데이터 수집
    vix_data = safe(lambda:get_series("VIXCLS"), retry=2, delay=5)
    dxy_data = safe(lambda:get_series("DTWEXBGS"), retry=2, delay=5)
    fx, fx_source = get_fx_final()
    gold_data = get_gold()
    
    spy_c, spy_p, spy_s = spy
    vix = vix_data[-1] if vix_data else 22
    dxy = dxy_data[-1] if dxy_data else 100
    
    # 3. AI 심층 분석
    news = fetch_news()
    market_summary = f"S&P500: {spy_c}(200MA격차 {gap(spy_c, spy_s):.1f}%), VIX: {vix}, DXY: {dxy}, 환율: {fx}"
    ai = get_ai_analysis(news, market_summary)

    # 4. 점수 및 상태 판별 (AI 0.5 가중치 적용)
    total_score = calc_final_score(gap(spy_c, spy_s), pct(spy_c, spy_p), vix, dxy, ai['score'])
    panic_signal = (vix > PANIC_VIX or pct(spy_c, spy_p) < PANIC_MOMENTUM)
    st, act = get_stage(total_score, panic_signal)

    # 5. 리포트 UI 구성
    msg = f"""🤖 퀀텀 인사이트: 데일리 리포트

📌 요약: {ai['summary']}

⚠️ 리스크 요인:
{ai['risk']}

📍 전략 제안:
{ai['strategy']}

━━━━━━━━━━━━━━━
상태: {st} ({total_score:.1f}) → {act}

📊 시장 현황
- S&P500: {spy_c:.0f} ({pct(spy_c, spy_p):+.2f}%)
- NASDAQ: {qqq_c:.0f} ({pct(qqq_c, qqq_p):+.2f}%)
- KOSPI: {kospi_c:.0f} ({pct(kospi_c, kospi_p):+.2f}%)
- VIX: {vix:.2f} / 환율: {fx:.0f}
- 금: {f"{gold_data[0]:.0f}" if gold_data else "수집지연"}

🛠 META: {'✅ 정상' if not panic_signal else '🚨 패닉 감지'}
"""
    # 6. 전송 및 상태 저장
    prev = load_state()
    # 데일리 리포트는 매일 아침 전송하되, 상태 변화가 클 때만 기록
    send(msg)
    save_state(total_score, st)
    log("✅ 데일리 리포트 전송 완료")

if __name__=="__main__":
    try: main()
    except Exception as e: log(f"치명적 오류: {e}", "error")
