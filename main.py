import requests, os, json, feedparser, re, time, logging
from datetime import datetime, timedelta
from openai import OpenAI

# [기초 설정 생략 - 이전과 동일]
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
def log(msg): print(msg); logging.info(msg)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY"); FRED_API_KEY = os.getenv("FRED_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN"); CHAT_ID = os.getenv("CHAT_ID")
client = OpenAI(api_key=OPENAI_API_KEY)
STATE_FILE = "bot_state.json"

def pct(c, p): return (c - p) / p * 100
def gap(c, s): return (c - s) / s * 100
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f: return json.load(f)
    return {}

def save_state(score, state):
    with open(STATE_FILE, "w") as f: json.dump({"score": score, "state": state}, f)

# ==========================================
# 📊 데이터 수집 (지수, 환율, 금)
# ==========================================
def safe(func, retry=5, delay=60):
    for i in range(retry):
        try:
            res = func()
            if res: return res
        except: pass
        time.sleep(delay)
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
    return (v[-1], v[-2], sum(v[-200:]) / 200) if v else None

def get_fx_dynamic():
    try:
        res = requests.get("https://finance.naver.com/marketindex/exchangeList.naver", headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text
        fx_c = float(re.search(r"<td class=\"sale\">([\d,]+\.\d+)</td>", re.search(r"<td class=\"tit\">.*?USD.*?</tr>", res, re.DOTALL).group()).group(1).replace(",", ""))
    except: fx_c = 1400.0
    v = get_series("DEXKOUS")
    return fx_c, v[-2] if v else fx_c, sum(v[-200:]) / 200 if v else fx_c

def get_gold_full():
    try:
        res = requests.get("https://query2.finance.yahoo.com/v8/finance/chart/GC=F?interval=1d&range=1y", headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
        v = [float(val) for val in res["chart"]["result"][0]["indicators"]["quote"][0]["close"] if val is not None]
        return v[-1], v[-2], v[-20], sum(v[-252:]) / 252
    except: return None

# ==========================================
# 🧠 AI 심층 지시 엔진 (분석 퀄리티 극대화)
# ==========================================
def get_expert_ai_report(news, market_summary):
    # AI에게 단순 요약이 아닌 '판단'을 강요하는 프롬프트
    prompt = f"""
너는 20년 경력의 월스트리트 헤지펀드 거시경제 전략가다. 
아래의 시장 데이터와 최신 뉴스를 결합하여 '블랙스완 리스크'를 진단하라.

[현재 데이터 요약]
{market_summary}

[최신 뉴스 헤드라인]
{news}

분석 가이드라인:
1. 단순 뉴스 요약은 금지한다. 데이터 간의 상관관계를 해석하라. 
   (예: 지수 하락 중인데 VIX와 환율이 폭등한다면 시스템 붕괴 징후로 판단)
2. 현재가 200일 이평선 아래라면 하락 추세 진입으로 간주하고 보수적 스탠스를 취하라.
3. 점수 산정 (-2 ~ +2): 
   - +2: 즉각적인 자산 회수 필요 (금융위기급)
   - 0: 정상적인 시장 노이즈
   - -2: 역사적 저점 매수 기회

반드시 아래 JSON 형식으로만 응답하라:
{{
  "score": int,
  "summary": "시장 국면 한줄 요약 (예: 유동성 장세, 공포 국면 등)",
  "risk": "가장 치명적인 리스크 요인 2가지",
  "strategy": "구체적인 매매 전략 (비중 조절 권고 포함)"
}}
"""
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            temperature=0.3, # 약간의 창의성을 주어 고정된 답변 방지
            response_format={"type":"json_object"}
        )
        return json.loads(r.choices[0].message.content)
    except:
        return {"score": 0, "summary": "분석 엔진 지연", "risk": "데이터 수집 중", "strategy": "관망 유지"}

# ==========================================
# 🧮 점수 산출 로직 (추세 + 속도 + 환율괴리)
# ==========================================
def calc_master_score(spy, qqq, fx_data, vix, dxy, ai_score):
    s = 0
    # 1. 지수 추세(Gap) & 속도(Momentum)
    for idx in [spy, qqq]:
        c, p, sma = idx
        if gap(c, sma) < -3: s += 2 # 200일선 하향 돌파 (장기 추세 붕괴)
        if pct(c, p) < -2.5: s += 2   # 하루 -2.5% 이상 급락 (폭락 속도 가중치 🚨)
    
    # 2. 다이내믹 환율 (SMA 괴리 & 상승 속도)
    fx_c, fx_p, fx_sma = fx_data
    if gap(fx_c, fx_sma) > 4: s += 1  # 평소보다 비싼 달러
    if pct(fx_c, fx_p) > 1.5: s += 1  # 환율 상승 속도가 너무 빠름 (외인 탈출)

    # 3. 공포지수 및 달러인덱스
    if vix > 25: s += 1
    if vix > 35: s += 2
    if vix > 45: s += 4 # 블랙스완 확정
    if dxy > 106: s += 1

    # 4. AI 점수 (0.5 가중치 보정)
    return max(0, min(15, s + (ai_score * 0.5)))

# ==========================================
# 🚀 메인 오퍼레이션
# ==========================================
def main():
    log("📊 퀀텀 마스터피스 리포트 생성 시작")
    spy = safe(lambda:get_index_full("SP500"))
    qqq = safe(lambda:get_index_full("NASDAQCOM"))
    kospi = safe(lambda:get_index_full("KOSPI"))
    fx_data = get_fx_dynamic(); gold = get_gold_full()
    
    if not spy or not qqq or not kospi: return log("⚠️ 지수 데이터 미준비")

    vix_v = safe(lambda:get_series("VIXCLS")); dxy_v = safe(lambda:get_series("DTWEXBGS"))
    vix = vix_v[-1] if vix_v else 22; dxy = dxy_v[-1] if dxy_v else 100
    
    # AI용 뉴스 및 요약 구성
    news = " / ".join([e.title for e in feedparser.parse(requests.get("https://finance.yahoo.com/news/rssindex").text).entries[:5]])
    market_summary = f"S&P500:{spy[0]}, 전일대비:{pct(spy[0],spy[1]):.1f}%, VIX:{vix}, 환율:{fx_data[0]}"
    
    ai = get_expert_ai_report(news, market_summary)
    
    total_score = calc_master_score(spy, qqq, fx_data, vix, dxy, ai['score'])
    panic = (vix > 35 or pct(spy[0], spy[1]) < -4)
    
    stages = [("🟢 공격", "매수"), ("🔵 상승", "유지"), ("🟡 중립", "관망"), ("🟠 경고", "중단"), ("🔴 위험", "축소")]
    st, act = (("💀 패닉", "분할매수") if panic else stages[min(4, int(total_score // 3))])

    prev = load_state()
    diff = total_score - prev.get("score", total_score)
    diff_str = f"{diff:+.1f} {'📈' if diff > 0 else '📉' if diff < 0 else '➖'}"

    msg = f"""🤖 퀀텀 인사이트: 마스터피스 리포트

📌 요약: {ai['summary']}
⚠️ 리스크: {ai['risk']}
📍 전략: {ai['strategy']}

━━━━━━━━━━━━━━━
상태: {st} ({total_score:.1f}) | {diff_str}
액션: {act}

📊 시장 지표
- S&P500: {spy[0]:.0f} ({pct(spy[0], spy[1]):+.2f}%)
- NASDAQ: {qqq[0]:.0f} ({pct(qqq[0], qqq[1]):+.2f}%)
- KOSPI: {kospi[0]:.0f} ({pct(kospi[0], kospi[1]):+.2f}%)
- VIX: {vix:.2f} / 환율: {fx_data[0]:.0f}

🛠 META: {'✅ 정상' if not panic else '🚨 패닉 감지'}
"""
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
    save_state(total_score, st)
    log("✅ 리포트 전송 완료")

if __name__=="__main__":
    main()
