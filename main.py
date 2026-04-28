import requests, os, json, feedparser, re, time, logging
from datetime import datetime, timedelta
from openai import OpenAI

# ==========================================
# 🥇 인프라 설정 및 로그 (실행 시각 로그 추가)
# ==========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
def log(msg): 
    print(msg)
    logging.info(msg)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY"); FRED_API_KEY = os.getenv("FRED_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN"); CHAT_ID = os.getenv("CHAT_ID")
client = OpenAI(api_key=OPENAI_API_KEY)
STATE_FILE = "bot_state.json"

# [GPT 피드백 반영] ZeroDivisionError 완벽 방어
def pct(c, p): 
    return (c - p) / p * 100 if p and p != 0 else 0

def gap(c, s): 
    return (c - s) / s * 100 if s and s != 0 else 0

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
    except Exception as e: log(f"상태 저장 오류: {e}")

# ==========================================
# 📊 실전형 데이터 엔진 (에러 로깅 강화)
# ==========================================
def safe(func, retry=5, delay=60):
    for i in range(retry):
        try:
            res = func()
            if res: return res
        except Exception as e:
            log(f"⚠️ {i+1}차 재시도 중 오류: {e}")
        if i < retry - 1: time.sleep(delay)
    return None

def get_series(series):
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {"series_id": series, "api_key": FRED_API_KEY, "file_type": "json", "observation_start": (datetime.now()-timedelta(days=1000)).strftime('%Y-%m-%d')}
        res = requests.get(url, params=params, timeout=15).json()
        v = [float(o["value"]) for o in res.get("observations", []) if o["value"] != "."]
        return v if len(v) >= 50 else None
    except Exception as e:
        log(f"데이터 {series} 수집 실패: {e}")
        return None

def get_index_full(series):
    v = get_series(series)
    if not v: return (0, 0, 0) 
    count = min(len(v), 200)
    return (v[-1], v[-2], sum(v[-count:]) / count)

def get_fx_dynamic():
    try:
        res = requests.get("https://finance.naver.com/marketindex/exchangeList.naver", headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text
        row = re.search(r"<td class=\"tit\">.*?USD.*?</tr>", res, re.DOTALL)
        price = re.search(r"<td class=\"sale\">([\d,]+\.\d+)</td>", row.group())
        fx_c = float(price.group(1).replace(",", ""))
    except: fx_c = 1400.0
    v = get_series("DEXKOUS")
    fx_p = v[-2] if v and len(v) > 1 else fx_c
    fx_sma = sum(v[-min(len(v), 200):]) / min(len(v), 200) if v else fx_c
    return fx_c, fx_p, fx_sma

def get_gold_full():
    try:
        res = requests.get("https://query2.finance.yahoo.com/v8/finance/chart/GC=F?interval=1d&range=1y", headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
        v = [float(val) for val in res["chart"]["result"][0]["indicators"]["quote"][0]["close"] if val is not None]
        return (v[-1], v[-2], v[-20], sum(v[-252:]) / 252) if len(v) >= 50 else None
    except: return None

def fetch_news_context():
    try:
        feed = feedparser.parse("https://finance.yahoo.com/news/rssindex")
        news_list = []
        for e in feed.entries[:5]:
            raw_sum = getattr(e, 'summary', '')
            summary = re.sub('<[^<]+?>', '', raw_sum)[:120]
            news_list.append(f"▶ {e.title}: {summary}")
        return "\n".join(news_list)
    except: return "뉴스 수집 지연"

# ==========================================
# 🧠 AI 분석 및 스코어링 (기존 강점 유지)
# ==========================================
def get_expert_ai_analysis(news, market_summary):
    prompt = f"""너는 월스트리트 수석 매크로 전략가다. 지표({market_summary})와 뉴스({news}) 사이의 숨은 위기를 분석하라.
1. 뉴스 헤드라인과 시장 지표(S&P500, VIX, 환율)를 연결하여 해석하라.
2. 시스템 리스크 징후(블랙스완) 포착 시 점수를 가중하라.
3. JSON 형식 엄수: {{"score": int(-2~2), "summary": "한줄국면", "risk": "위기요인2개", "strategy": "대응가이드"}}"""
    try:
        r = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"user","content":prompt}], temperature=0.2, response_format={"type":"json_object"})
        ai_data = json.loads(r.choices[0].message.content)
        ai_data["score"] = max(-2, min(2, float(ai_data.get("score", 0))))
        return ai_data
    except: return {"score": 0, "summary": "AI분석 지연", "risk": "확인중", "strategy": "관망"}

def calc_master_score(spy, qqq, fx_data, vix, dxy, ai_score):
    if spy[0] == 0: return 2.0 # 데이터 지연 시 안전 모드 (낮은 리스크 점수 부여)
    
    s = 0
    for idx in [spy, qqq]:
        c, p, sma = idx
        if c == 0: continue
        if gap(c, sma) < -3: s += 2
        if pct(c, p) < -2.5: s += 2.5
    
    fx_c, fx_p, fx_sma = fx_data
    if gap(fx_c, fx_sma) > 4: s += 1
    if pct(fx_c, fx_p) > 1.5: s += 1.5
    if vix > 25: s += 1
    if vix > 35: s += 2
    if vix > 45: s += 4
    if dxy > 106: s += 1

    log(f"SCORE_LOG: SPY_G={gap(spy[0],spy[2]):.1f}, VIX={vix}, DXY={dxy}, AI={ai_score}")
    return max(0, min(15, s + (ai_score * 0.5)))

def get_gold_signal(gold):
    if not gold: return "데이터 부족"
    c, p1, p20, avg = gold
    st_gap, lt_gap = gap(c, p20), gap(c, avg)
    if st_gap > 3 and lt_gap > 10: return "🚨 과열"
    elif st_gap > 2: return "⚠️ 경계"
    elif st_gap < -2: return "🟢 안정"
    return "➖ 중립"

def get_macro_insight(gold, dxy, spy_mom):
    if not gold: return "💡 매크로: 데이터 부족"
    g_ch = pct(gold[0], gold[2])
    if g_ch > 2 and dxy > 106: return "💡 매크로: 금·달러 동반 상승 (안전자산 쏠림 🚨)"
    elif g_ch > 2 and spy_mom > 0: return "💡 매크로: 금·지수 동반 상승 (유동성 장세 💸)"
    elif g_ch < -2 and spy_mom > 0: return "💡 매크로: 금 하락·지수 상승 (Risk-On 🟢)"
    elif g_ch < -2 and spy_mom < -2: return "💡 매크로: 전방위 패닉셀 발생 💀"
    return "💡 매크로: 뚜렷한 쏠림 없음 ➖"

# ==========================================
# 🚀 메인 오퍼레이션
# ==========================================
def main():
    log(f"📊 퀀텀 인사이트 가동 시작 (시각: {datetime.now()})")
    
    # 데이터 수집
    spy = safe(lambda:get_index_full("SP500")) or (0, 0, 0)
    qqq = safe(lambda:get_index_full("NASDAQCOM")) or (0, 0, 0)
    kospi = safe(lambda:get_index_full("KOSPI")) or (0, 0, 0)
    fx_data = get_fx_dynamic(); gold = get_gold_full()
    
    vix_v = safe(lambda:get_series("VIXCLS")); dxy_v = safe(lambda:get_series("DTWEXBGS"))
    vix = vix_v[-1] if vix_v else 22; dxy = dxy_v[-1] if dxy_v else 100
    
    ai = get_expert_ai_analysis(fetch_news_context(), f"SP500:{spy[0]}, VIX:{vix}, FX:{fx_data[0]}")
    total_score = calc_master_score(spy, qqq, fx_data, vix, dxy, ai['score'])
    
    status_icon = "✅ 정상" if spy[0] > 0 else "⚠️ 데이터 수집 지연 (최신화 대기 중)"
    panic = (vix > 35 or pct(spy[0], spy[1]) < -4) if spy[0] > 0 else False
    
    stages = [("🟢 공격", "매수"), ("🔵 상승", "유지"), ("🟡 중립", "관망"), ("🟠 경고", "중단"), ("🔴 위험", "축소")]
    st, act = (("💀 패닉", "분할매수") if panic else stages[min(4, int(total_score // 3))])
    pos_ratio = round(max(0, min(100, 100 - total_score * 6.5)))

    # [GPT 피드백 반영] 지연 시 출력 깔끔하게 수정
    spy_str = f"{spy[0]:.0f} ({pct(spy[0], spy[1]):+.2f}%)" if spy[0] > 0 else "지연"
    qqq_str = f"{qqq[0]:.0f} ({pct(qqq[0], qqq[1]):+.2f}%)" if qqq[0] > 0 else "지연"
    kos_str = f"{kospi[0]:.0f} ({pct(kospi[0], kospi[1]):+.2f}%)" if kospi[0] > 0 else "지연"

    prev = load_state()
    diff = total_score - prev.get("score", total_score)
    diff_str = f"{diff:+.1f} {'📈' if diff > 0 else '📉' if diff < 0 else '➖'}"

    msg = f"""🤖 퀀텀 인사이트: 데일리 리포트

📌 현재 상황: {status_icon}
 요약: {ai['summary']}
⚠️ 리스크: {ai['risk']}
📍 권장 비중: {pos_ratio}% ({act})

━━━━━━━━━━━━━━━
상태: {st} ({total_score:.1f}) | {diff_str}

📊 주요 지표
- S&P500: {spy_str}
- NASDAQ: {qqq_str}
- KOSPI: {kos_str}
- VIX: {vix:.2f} / 환율: {fx_data[0]:.0f}
- 금: {f"{gold[0]:.0f} → {get_gold_signal(gold)}" if gold else "지연"}

{get_macro_insight(gold, dxy, pct(spy[0], spy[1]))}

🛠 META: {'✅ 정상' if not panic else '🚨 패닉 감지 (방어 모드)'}
"""
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg}, timeout=10).raise_for_status()
    except Exception as e: log(f"텔레그램 전송 실패: {e}")
    
    save_state(total_score, st)
    log("✅ 리포트 전송 프로세스 완료")

if __name__=="__main__":
    main()
