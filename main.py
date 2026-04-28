import requests, os, json, feedparser, re, time, logging
from datetime import datetime, timedelta
from openai import OpenAI

# ==========================================
# 🥇 인프라 설정
# ==========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
def log(msg): print(msg); logging.info(msg)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY"); FRED_API_KEY = os.getenv("FRED_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN"); CHAT_ID = os.getenv("CHAT_ID")
client = OpenAI(api_key=OPENAI_API_KEY)
STATE_FILE = "bot_state.json"

# [방어 로직] ZeroDivision 방지
def pct(c, p): return (c - p) / p * 100 if p and p != 0 else 0
def gap(c, s): return (c - s) / s * 100 if s and s != 0 else 0

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
# 📊 데이터 엔진 (1년/2년 평균가 완전 유지)
# ==========================================
def safe(func, retry=5, delay=60):
    for i in range(retry):
        try:
            res = func()
            if res: return res
        except Exception as e: log(f"⚠️ {i+1}차 시도 오류: {e}")
        if i < retry - 1: time.sleep(delay)
    return None

def get_series(series):
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {"series_id": series, "api_key": FRED_API_KEY, "file_type": "json", "observation_start": (datetime.now()-timedelta(days=1000)).strftime('%Y-%m-%d')}
        res = requests.get(url, params=params, timeout=15).json()
        v = [float(o["value"]) for o in res.get("observations", []) if o["value"] != "."]
        return v if len(v) >= 50 else None
    except: return None

def get_index_full(series):
    v = get_series(series)
    if not v: return (0, 0, 0)
    count = min(len(v), 200)
    return (v[-1], v[-2], sum(v[-count:]) / count)

def get_fx_final():
    source = "NAVER"
    try:
        res = requests.get("https://finance.naver.com/marketindex/exchangeList.naver", headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text
        row = re.search(r"<td class=\"tit\">.*?USD.*?</tr>", res, re.DOTALL)
        price = re.search(r"<td class=\"sale\">([\d,]+\.\d+)</td>", row.group())
        fx_c = float(price.group(1).replace(",", ""))
    except: 
        fx_c = 1400.0; source = "DEFAULT"
    
    v = get_series("DEXKOUS")
    if v:
        fx_p = v[-2] if len(v) > 1 else fx_c
        sma_1y = sum(v[-min(len(v), 252):]) / min(len(v), 252)
        sma_2y = sum(v[-min(len(v), 504):]) / min(len(v), 504)
        return fx_c, fx_p, sma_1y, sma_2y, source
    return fx_c, fx_c, fx_c, fx_c, source

def get_gold_full():
    try:
        res = requests.get("https://query2.finance.yahoo.com/v8/finance/chart/GC=F?interval=1d&range=1y", headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
        v = [float(val) for val in res["chart"]["result"][0]["indicators"]["quote"][0]["close"] if val is not None]
        return (v[-1], v[-2], v[-20], sum(v[-252:]) / 252) if len(v) >= 50 else None
    except: return None

# ==========================================
# 🧠 AI 심층 분석 엔진 (풀 프롬프트 복구)
# ==========================================
def get_expert_ai_analysis(news, market_summary):
    prompt = f"""너는 20년 경력의 월스트리트 수석 매크로 전략가다. 
지표({market_summary})와 뉴스({news}) 사이의 숨은 상관관계를 해석하여 시스템 리스크를 진단하라.
1. 단순 뉴스 요약은 금지한다. 지수 하락 중 환율/VIX 폭등 시 블랙스완 징후로 판단하라.
2. 현재가 200일선 아래라면 하락 추세로 간주하고 호재 뉴스를 '데드캣 바운스'로 의심하라.
3. 시스템 리스크나 금융위기 징후 포착 시 즉각 경고하라.
4. JSON 형식 엄수: {{"score": int(-2~2), "summary": "시장국면", "risk": "위기요인2개", "strategy": "대응가이드"}}"""
    try:
        r = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"user","content":prompt}], temperature=0.2, response_format={"type":"json_object"})
        ai_data = json.loads(r.choices[0].message.content)
        ai_data["score"] = max(-2, min(2, float(ai_data.get("score", 0))))
        return ai_data
    except: return {"score": 0, "summary": "AI 지연", "risk": "확인중", "strategy": "관망"}

def calc_master_score(spy, qqq, fx_data, vix, dxy, ai_score):
    if spy[0] == 0: return 2.0
    s = 0
    for idx in [spy, qqq]:
        c, p, sma = idx
        if c == 0: continue
        if gap(c, sma) < -3: s += 2
        if pct(c, p) < -2.5: s += 2.5 
    
    fx_c, fx_p, fx_1y, fx_2y, _ = fx_data
    if gap(fx_c, fx_1y) > 4: s += 1
    if pct(fx_c, fx_p) > 1.5: s += 1.5
    if vix > 25: s += 1
    if vix > 35: s += 2
    if vix > 45: s += 4
    if dxy > 125: s += 1 
    return max(0, min(15, s + (ai_score * 0.5)))

def get_gold_signal(gold):
    if not gold: return "지연"
    c, p1, p20, avg = gold
    st_gap = gap(c, p20)
    if st_gap > 3: return "🚨 과열"
    elif st_gap < -2: return "🟢 안정"
    return "➖ 중립"

def get_macro_insight(gold, dxy, spy_mom):
    if not gold: return "💡 매크로: 데이터 부족"
    g_ch = pct(gold[0], gold[2])
    if g_ch > 2 and dxy > 125: return "💡 매크로: 금·달러 동반 상승 (안전자산 쏠림 🚨)"
    elif g_ch > 2 and spy_mom > 0: return "💡 매크로: 금·지수 동반 상승 (유동성 장세 💸)"
    elif g_ch < -2 and spy_mom > 0: return "💡 매크로: 금 하락·지수 상승 (Risk-On 🟢)"
    elif g_ch < -2 and spy_mom < -2: return "💡 매크로: 전방위 패닉셀 발생 💀"
    return "💡 매크로: 뚜렷한 쏠림 없음 ➖"

# ==========================================
# 🚀 메인 오퍼레이션 (지표 표시 로직 완전 복구)
# ==========================================
def main():
    log(f"📊 퀀텀 가동 ({datetime.now()})")
    spy = safe(lambda:get_index_full("SP500")) or (0, 0, 0)
    qqq = safe(lambda:get_index_full("NASDAQCOM")) or (0, 0, 0)
    kospi = safe(lambda:get_index_full("KOSPI")) or (0, 0, 0)
    
    fx_data = get_fx_final() 
    gold = get_gold_full()
    
    vix_v = safe(lambda:get_series("VIXCLS")); dxy_v = safe(lambda:get_series("DTWEXBGS"))
    vix = vix_v[-1] if vix_v else 22; dxy = dxy_v[-1] if dxy_v else 118.0
    
    dxy_status = "✅ 정상" if dxy < 122 else "⚠️ 경계" if dxy < 126 else "🚨 위험"
    
    feed = feedparser.parse("https://finance.yahoo.com/news/rssindex")
    news_context = "\n".join([f"▶ {e.title}: {getattr(e, 'summary', '')[:100]}" for e in feed.entries[:5]])
    
    ai = get_expert_ai_analysis(news_context, f"SP500:{spy[0]}, VIX:{vix}, FX:{fx_data[0]}")
    total_score = calc_master_score(spy, qqq, fx_data, vix, dxy, ai['score'])
    
    # [복구 및 강화] 지표 옆에 200일선 대비 위치(▲/▼) 표시 함수
    def format_idx_msg(idx):
        c, p, sma = idx
        if c == 0: return "지연"
        g = gap(c, sma)
        icon = "▲" if g > 0 else "▼"
        return f"{c:.0f} ({pct(c, p):+.2f}%) | 200일선 대비 {g:+.1f}% {icon}"

    spy_str = format_idx_msg(spy)
    qqq_str = format_idx_msg(qqq)
    kos_str = format_idx_msg(kospi)
    
    # [강상승 로직 유지]
    trend_suffix = ""
    if spy[0] > 0 and qqq[0] > 0:
        if gap(spy[0], spy[2]) > 3 and gap(qqq[0], qqq[2]) > 3: trend_suffix = " (🔥 강상승)"
    
    # [환율 판단 로직 세분화] 대표님 요청 반영
    fx_c, fx_p, fx_1y, fx_2y, fx_s = fx_data
    fx_gap_2y = gap(fx_c, fx_2y)
    if fx_gap_2y > 8: fx_status = "⚠️ 역사적 고점"
    elif fx_gap_2y > 4: fx_status = "🟠 환율 높음 (주의)"
    else: fx_status = "✅ 정상범위"
    
    panic = (vix > 35 or pct(spy[0], spy[1]) < -4) if spy[0] > 0 else False
    stages = [("🟢 공격", "매수"), ("🔵 상승", "유지"), ("🟡 중립", "관망"), ("🟠 경고", "중단"), ("🔴 위험", "축소")]
    st, act = (("💀 패닉", "분할매수") if panic else stages[min(4, int(total_score // 3))])
    
    prev = load_state()
    diff = total_score - prev.get("score", total_score)
    diff_str = f"{diff:+.1f} {'📈' if diff > 0 else '📉' if diff < 0 else '➖'}"

    msg = f"""🤖 퀀텀 인사이트: 데일리 리포트

📌 요약: {ai['summary']}
⚠️ 리스크: {ai['risk']}
📍 권장 비중: {round(max(0, min(100, 100 - total_score * 6.5)))}% ({act})

━━━━━━━━━━━━━━━
상태: {st}{trend_suffix} (점수: {total_score:.1f}) | {diff_str}

📊 주요 지표
- S&P500: {spy_str}
- NASDAQ: {qqq_str}
- KOSPI: {kos_str}
- 환율: {fx_c:,.0f}원 [{fx_s}] ({fx_status})
  └ 1년 평균: {fx_1y:,.0f}원 (대비 {gap(fx_c, fx_1y):+.1f}%)
  └ 2년 평균: {fx_2y:,.0f}원 (대비 {gap(fx_c, fx_2y):+.1f}%)
- VIX: {vix:.2f} / 달러인덱스: {dxy:.1f} ({dxy_status})
- 금: {f"{gold[0]:.0f} → {get_gold_signal(gold)}" if gold else "지연"}

{get_macro_insight(gold, dxy, pct(spy[0], spy[1]))}

🛠 META: {'✅ 정상' if not panic else '🚨 패닉 감지'}
"""
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg}, timeout=10).raise_for_status()
    except Exception as e: log(f"전송 실패: {e}")
    
    save_state(total_score, st)
    log("✅ 리포트 전송 완료")

if __name__=="__main__":
    main()
