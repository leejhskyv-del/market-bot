import requests, os, json, feedparser, re, time, logging, sys, html
import time
os.environ['TZ'] = 'Asia/Seoul'
time.tzset()

from datetime import datetime, timedelta
from openai import OpenAI

# ==========================================
# ⚙️ 설정 & 상수
# ==========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
def log(msg): logging.info(msg)

UNRATE_THRESHOLD = 4.2
VIX              = {"warn": 25, "danger": 35, "panic": 45}
FX_GAP           = {"caution": 4, "danger": 8}
DXY              = {"warn": 122, "danger": 126}
SPY_PANIC_DROP   = -4.0         # 패닉을 유발하는 서킷브레이커 기준점
SPY_TREND_GAP    = 3.0
SCORE_MAX        = 15.0
HY_SPREAD_WARN   = 4.5
HY_SPREAD_DANGER = 6.5
FG_EXTREME_FEAR  = 20
DXY_MOM_WARN     = 3.0
DRAWDOWN_WARN    = -10.0
DRAWDOWN_DANGER  = -20.0
AI_WEIGHT        = 0.5

RETRY_COUNT = 4
RETRY_DELAY = 15

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
CNN_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
    "Origin": "https://edition.cnn.com",
}

ECON_KEYWORDS = [
    "Fed", "rate", "inflation", "recession", "GDP", "jobs", "unemployment",
    "tariff", "trade", "bank", "earnings", "default", "yield", "debt",
    "cut", "hike", "pivot", "crash", "rally", "금리", "인플레", "관세", "실업",
    "Buffett", "버핏", "Berkshire", "버크셔",
    "Druckenmiller", "드러켄밀러", "Howard Marks", "하워드 막스", "Ray Dalio", "레이 달리오"
]

MACRO_CRITICAL = [
    "fed", "fomc", "powell", "cpi", "pce", "rate cut", "rate hike",
    "연준", "파월", "금리", "인플레이션", "물가",
    "buffett", "버핏", "druckenmiller", "드러켄밀러", "howard marks", "하워드 막스", "ray dalio", "레이 달리오"
]

NEWS_FEEDS = [
    ("Yahoo Finance",  "https://finance.yahoo.com/news/rssindex"),
    ("CNBC 경제",      "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC 전체",      "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch",    "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("WSJ 마켓",       "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
]

# ==========================================
# 🔑 환경변수 & 유틸리티
# ==========================================
def validate_env():
    required = {
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
        "FRED_API_KEY":   os.getenv("FRED_API_KEY"),
        "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN"),
        "CHAT_ID":        os.getenv("CHAT_ID"),
        "GIST_ID":        os.getenv("GIST_ID"),
        "GITHUB_TOKEN":   os.getenv("GITHUB_TOKEN"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        log(f"❌ 환경변수 누락: {', '.join(missing)}")
        sys.exit(1)
    return required

ENV    = validate_env()
client = OpenAI(api_key=ENV["OPENAI_API_KEY"])

def pct(c, p):  return (c - p) / p * 100 if p and abs(p) > 1e-9 else 0
def gap(c, s):  return (c - s) / s * 100 if s and abs(s) > 1e-9 else 0
def arrow(v):   return "▲" if v > 0 else "▼" if v < 0 else "➖"

def safe_float(val, default=0.0):
    try: return float(val)
    except (TypeError, ValueError): return default

def safe(func, label="", retry=RETRY_COUNT, delay=RETRY_DELAY):
    for i in range(retry):
        try:
            res = func()
            if res is not None: return res
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403, 418): break
        except Exception as e:
            log(f"⚠️ [{label}] {i+1}차 실패: {type(e).__name__}: {e}")
        if i < retry - 1: time.sleep(delay)
    log(f"❌ [{label}] 최종 실패")
    return None

# ==========================================
# 💾 상태 관리
# ==========================================
def load_state():
    try:
        url = f"https://api.github.com/gists/{ENV['GIST_ID']}"
        headers = {"Authorization": f"token {ENV['GITHUB_TOKEN']}"}
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        return json.loads(res.json()['files']['bot_state.json']['content'])
    except: 
        return {"score": 0.0, "ism_pmi": 50.0, "ism_date": "2024-01-01", "last_update_id": 0, "history": []}

def save_state(state_data, existing_history, spy_current=None, spy_pct=None, spy_dd=None, vix=None, fg_score=None, dxy=None, hy_spread=None, us10y=None, fx=None):
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        history = existing_history[:] 

        if spy_current and spy_current > 0:
            for h in history:
                h_date = h.get("date")
                if not h_date or not h.get("spy_current"): continue
                try:
                    days_ago = (datetime.strptime(today, '%Y-%m-%d') - datetime.strptime(h_date, '%Y-%m-%d')).days
                    ret = round((spy_current - h["spy_current"]) / h["spy_current"] * 100, 2)
                    if days_ago == 7:  h["spy_1w"] = ret
                    if 28 <= days_ago <= 32: h["spy_1m"] = ret
                    if 88 <= days_ago <= 92: h["spy_3m"] = ret
                except: pass

        history = [h for h in history if h.get("date") != today]
        history.append({
            "date":        today,
            "score":       round(state_data.get("score", 0), 1),
            "stage":       state_data.get("stage", ""),
            "vix":         round(vix, 1) if vix else None,
            "fg":          fg_score,
            "spy_pct":     round(spy_pct, 2) if spy_pct is not None else None,
            "spy_dd":      round(spy_dd, 1) if spy_dd else None,
            "dxy":         round(dxy, 1) if dxy else None,
            "hy_spread":   round(hy_spread, 2) if hy_spread else None,
            "us10y":       round(us10y, 2) if us10y else None,
            "fx":          round(fx, 0) if fx else None,
            "spy_current": round(spy_current, 2) if spy_current else None,
        })
        history = history[-90:]
        
        state_data["history"] = history
        
        url = f"https://api.github.com/gists/{ENV['GIST_ID']}"
        headers = {"Authorization": f"token {ENV['GITHUB_TOKEN']}"}
        payload = {"files": {"bot_state.json": {"content": json.dumps(state_data, ensure_ascii=False)}}}
        requests.patch(url, headers=headers, json=payload, timeout=10)
        log("✅ 상태 저장 완료")
    except Exception as e:
        log(f"⚠️ 상태 저장 실패: {e}")

# ==========================================
# 📊 데이터 수집
# ==========================================
def get_fred_series(series_id, days=1000, min_count=1):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series_id, "api_key": ENV["FRED_API_KEY"], "file_type": "json", "observation_start": (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')}
    res = requests.get(url, params=params, timeout=15)
    res.raise_for_status()
    values = [float(o["value"]) for o in res.json().get("observations", []) if o["value"] != "."]
    if len(values) < min_count: raise ValueError(f"데이터 부족")
    return values

def get_us10y():
    v = get_fred_series("DGS10", days=60, min_count=5)
    return v[-1], v[-2]

def get_hy_spread():
    v = get_fred_series("BAMLH0A0HYM2", days=60, min_count=5)
    return v[-1], v[-2]

def get_unrate():
    v = get_fred_series("UNRATE", days=365, min_count=1)
    return v[-1] if v else 4.0

def get_yahoo_closes(ticker, range_="2y", min_count=20):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={range_}"
    res = requests.get(url, headers=YAHOO_HEADERS, timeout=12)
    res.raise_for_status()
    closes = [v for v in res.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if v is not None]
    if len(closes) < min_count: raise ValueError(f"데이터 부족")
    return closes

def get_yahoo_stats(ticker, range_="2y"):
    closes = get_yahoo_closes(ticker, range_)
    count = min(len(closes), 200)
    year_closes = closes[-253:] if len(closes) >= 253 else closes
    return closes[-1], closes[-2], sum(closes[-count:]) / count, max(year_closes)
    
def get_dxy_momentum(dxy_closes):
    if not dxy_closes or len(dxy_closes) < 21: return None
    return pct(dxy_closes[-1], dxy_closes[-21])

def get_fx_data():
    closes = get_yahoo_closes("KRW=X", "2y")
    return closes[-1], closes[-2], sum(closes[-min(len(closes), 252):]) / min(len(closes), 252), sum(closes[-min(len(closes), 504):]) / min(len(closes), 504)

def get_gold_data():
    closes = get_yahoo_closes("GC=F", "1y")
    return closes[-1], closes[-2], sum(closes[-min(len(closes), 252):]) / min(len(closes), 252)
    
def calc_rsi_wilder(values, period=14):
    if not values or len(values) < period * 2: return None
    deltas = [values[i] - values[i-1] for i in range(1, len(values))]
    gains, losses = [max(d, 0) for d in deltas], [max(-d, 0) for d in deltas]
    avg_gain, avg_loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1) if avg_loss != 0 else 100.0

def get_market_breadth():
    try:
        spy_c = get_yahoo_closes("^GSPC", "1mo", min_count=10)
        rsp_c = get_yahoo_closes("RSP", "1mo", min_count=10)
        if not spy_c or not rsp_c: return "데이터 지연"
        spy_ret = pct(spy_c[-1], spy_c[0])
        rsp_ret = pct(rsp_c[-1], rsp_c[0])
        diff = spy_ret - rsp_ret
        return "✅ 정상" if diff < 2.0 else "⚠️ 시장 왜곡 (소수 종목 편중)"
    except: return "산출 불가"

def get_best_hedge():
    tickers = {"GLD": "금", "TLT": "국채", "UUP": "달러"}
    res = {}
    for t in tickers:
        try:
            c = get_yahoo_closes(t, "3mo")
            res[tickers[t]] = pct(c[-1], c[0])
        except: continue
    if not res: return "산출 불가"
    best = max(res, key=res.get)
    return f"{best} ({res[best]:+.1f}%)"

# ==========================================
# 😨 Fear & Greed 
# ==========================================
def get_fear_greed():
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers_list = [
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36", "Accept": "application/json, text/plain, */*", "Referer": "https://edition.cnn.com/markets/fear-and-greed", "Origin": "https://edition.cnn.com"},
        {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36", "Accept": "application/json, text/plain, */*", "Referer": "https://www.cnn.com/markets/fear-and-greed", "Origin": "https://edition.cnn.com"},
    ]
    for attempt in range(4):
        headers = headers_list[attempt % len(headers_list)]
        try:
            res = requests.get(url, headers=headers, timeout=20)
            if res.status_code == 418: time.sleep(15); continue
            res.raise_for_status()
            data = res.json()
            if "fear_and_greed" in data and isinstance(data["fear_and_greed"], dict): score = round(float(data["fear_and_greed"]["score"]))
            elif "fear_and_greed_historical" in data and data["fear_and_greed_historical"].get("data"): score = round(float(data["fear_and_greed_historical"]["data"][-1]["y"]))
            else: raise ValueError("CNN F&G JSON 구조 변경 감지")

            if score <= 10:  lbl = "극단적 공포 😱🚨"
            elif score <= 25: lbl = "극단적 공포 😱"
            elif score <= 45: lbl = "공포 😨"
            elif score <= 55: lbl = "중립 😐"
            elif score <= 75: lbl = "탐욕 😏"
            else:             lbl = "극단적 탐욕 🤑"
            return score, lbl
        except Exception as e:
            if attempt < 3: time.sleep(10)
    return None, None

def format_index(c, p, sma, _=None):
    if c == 0: return "데이터 지연"
    return f"{c:,.0f}  {arrow(pct(c,p))}{abs(pct(c,p)):.1f}%\n └ 200일선 대비: {gap(c,sma):+.1f}%"

def get_drawdown_label(dd):
    if dd is None: return "산출 불가"
    
    if dd >= 0: return "🔥 신고점 갱신"
    if dd <= DRAWDOWN_DANGER: return f"{dd:.1f}%  💀 대형 조정"
    if dd <= DRAWDOWN_WARN: return f"{dd:.1f}%  🔴 조정 구간"
    if dd <= -5: return f"{dd:.1f}%  🟠 소폭 하락"
    
    return f"{dd:.1f}%  🟢 고점 근접"

def get_rsi_label(rsi):
    if rsi is None: return "산출 불가"
    if rsi >= 75: return f"{rsi}  🔴 과매수"
    if rsi >= 60: return f"{rsi}  🟠 상단"
    if rsi <= 25: return f"{rsi}  🟢 과매도"
    if rsi <= 40: return f"{rsi}  🔵 하단"
    return f"{rsi}  ➖ 중립"

def get_gold_signal(gold):
    if not gold: return "지연"
    st_gap = gap(gold[0], gold[2])
    if st_gap > 10: return "🚨 장기 과열"
    elif st_gap > 3: return "🟠 상승 추세"
    elif st_gap < -5: return "🟢 저점 근접"
    return "➖ 중립"

# ==========================================
# 🧭 매크로 국면 판독
# ==========================================
def get_macro_regime(ism, unrate):
    if ism >= 50.0 and unrate <= UNRATE_THRESHOLD:
        return {"emoji": "🟢", "name": "골디락스 (안정적 성장)", "score_adj": -1.5, "action": "최적 환경. 단기 노이즈 무시 (TQQQ 홀딩 우대)"}
    elif ism >= 50.0 and unrate > UNRATE_THRESHOLD:
        return {"emoji": "🟡", "name": "경기 과열 / 둔화 초기", "score_adj": +0.5, "action": "성장은 유지되나 고용 둔화. 주의 필요"}
    elif ism < 50.0 and unrate <= UNRATE_THRESHOLD:
        return {"emoji": "🟠", "name": "제조업 둔화 (소프트랜딩 대기)", "score_adj": +0.5, "action": "제조업 위축이나 고용이 버팀. 점진적 방어 태세"}
    else:
        return {"emoji": "🔴", "name": "경기 침체 우려 (Recession)", "score_adj": +2.5, "action": "혹한기 진입 가능성. 폭락 위험 극대화 (대피 우선)"}

# ==========================================
# 🧠 AI 분석
# ==========================================
def extract_news_keywords(entries, max_items=8):
    critical, normal = [], []
    for e in entries:
        title = getattr(e, "title", "").strip()
        if not title: continue
        summary = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html.unescape(getattr(e, "summary", "")))).strip()
        key_sents = [s.strip() for s in re.split(r'[.!?]', summary) if any(kw.lower() in s.lower() for kw in ECON_KEYWORDS)]
        context = " / ".join(key_sents[:2]) if key_sents else summary[:80]
        if any(kw in title.lower() for kw in MACRO_CRITICAL): critical.append(f"🚨[핵심 매크로] {title}  [{context}]")
        else: normal.append(f"• {title}  [{context}]")
    return "\n".join((critical + normal)[:max_items])

def get_ai_analysis(news: str, market_summary: dict) -> dict:
    prompt = f"""당신은 월스트리트 최고 수준의 퀀트 매크로 전략가이며, 현재 '매일 기계적으로 지수(VOO, QQQ)를 모아가며, 포트폴리오의 일부를 미래 전략산업(QTUM, UFO, NASA, ARKQ 등)에 위성 투자(Satellite)하는 투자자'를 전담 보좌하는 수석 비서입니다.

[분석 원칙 - 매우 중요]
1. [매크로 최우선] 뉴스 중 '🚨[핵심 매크로]' 연준, 금리 데이터에 집중하여 시장의 흐름 진단.
2. [상관관계] 전달받은 데이터 수치를 맹신하지 말고 증시에 미치는 영향을 'macro_correlation'에 통찰력 있게 작성. (추세 판단 필수)
3. [대응 전략] 비율(%) 숫자 금지. 투자자의 심리적 템포와 마음가짐 중심으로 'strategy' 작성.
4. [미래 전략산업] 매크로 환경이 우주/로봇/양자에 우호적인지 'opportunity'에 1~2문장 진단.
5. [거장 시그널 분리] 워런 버핏, 드러켄밀러 등 거장의 발언이 있다면 'guru_score'(-0.5~+0.5) 부여 및 'guru_insight' 요약. 없으면 0.0. 일반 리스크는 'macro_score'(-1.5~1.5) 부여.

[시장 데이터]
{json.dumps(market_summary, ensure_ascii=False)}
[주요 뉴스]
{news}

[출력: JSON만]
{{"macro_score": <실수>, "guru_score": <실수>, "guru_insight": "<거장뷰>", "market_phase": "<국면>", "top_risks": ["<1>","<2>","<3>"], "opportunity": "<미래산업>", "strategy": "<조언>", "macro_correlation": "<진단>"}}"""
    res = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.25, response_format={"type": "json_object"}, timeout=30)
    data = json.loads(res.choices[0].message.content.strip())
    macro = max(-1.5, min(1.5, safe_float(data.get("macro_score", 0.0))))
    guru  = max(-0.5, min(0.5, safe_float(data.get("guru_score", 0.0))))
    data["score"] = macro + guru
    risks = data.get("top_risks", [])
    data["top_risks"] = ((risks if isinstance(risks, list) else [str(risks)]) + ["-", "-", "-"])[:3]
    for k, v in {"market_phase": "분석 중", "opportunity": "-", "strategy": "관망", "macro_correlation": "지연", "guru_insight": "특이사항 없음"}.items():
        data.setdefault(k, v)
    return data

# ==========================================
# 🎯 위험 점수 산출
# ==========================================
def calc_risk_score(spy, qqq, kospi, fx_data, vix, vix_trend, dxy, dxy_mom, ai_score, us10y, fg_score, hy_spread, spy_dd, gold, rsi, is_recovering, regime_adj, is_bull, breadth_status, recent_score_jump):
    s = 0.0
        
    if spy[0] > 0:
        if gap(spy[0], spy[2]) < -SPY_TREND_GAP: s += 2.0
        elif gap(spy[0], spy[2]) < 0: s += 1.0
        if pct(spy[0], spy[1]) <= SPY_PANIC_DROP: s += 3.0

    if qqq[0] > 0:
        if gap(qqq[0], qqq[2]) < -SPY_TREND_GAP: s += 1.5
        elif gap(qqq[0], qqq[2]) < 0: s += 0.5

    if kospi and kospi[0] > 0 and kospi[2] > 0:
        kos_gap = gap(kospi[0], kospi[2])
        if kos_gap < -4.0: s += 1.5
        elif kos_gap < -2.0: s += 1.0

    if spy_dd is not None:
        if spy_dd <= DRAWDOWN_DANGER: s += 2.5
        elif spy_dd <= -15.0: s += 2.0
        elif spy_dd <= DRAWDOWN_WARN: s += 1.0

    if rsi is not None:
        if rsi > 75: s += 1.0
        elif rsi < 30: s -= 0.5

    if gap(fx_data[0], fx_data[3]) > FX_GAP["danger"]: s += 2.0
    elif gap(fx_data[0], fx_data[3]) > FX_GAP["caution"]: s += 1.0
    if pct(fx_data[0], fx_data[1]) > 2.0: s += 1.0

    if vix is not None:
        if vix_trend >= 10 and vix >= VIX["warn"]: s += 1.0
        elif vix_trend <= -10: s -= 0.5

        if vix >= VIX["panic"]: s += 4.0
        elif vix >= 30: s += 1.5
        elif vix >= VIX["warn"]: s += 1.0

    if dxy > DXY["danger"]: s += 1.5
    elif dxy > DXY["warn"]: s += 0.5
    if dxy_mom and dxy_mom > DXY_MOM_WARN: s += 2.0 if dxy_mom > DXY_MOM_WARN * 1.5 else 1.0

    if us10y and us10y[0] and us10y[1]:
        if us10y[0] - us10y[1] > 0.15: s += 1.5

    if hy_spread and hy_spread[0]:
        hys = hy_spread[0]
        if hys > HY_SPREAD_DANGER: s += 3.0
        elif hys > HY_SPREAD_WARN: s += 1.5
        if hy_spread[1] and (hys - hy_spread[1]) > 0.3: s += 1.0

    if fg_score is not None:
        if fg_score > 80: s += 1.0
        elif fg_score < FG_EXTREME_FEAR:
            s -= 1.5 if vix is not None and vix < VIX["warn"] else 0.5

    if gold and gold[2] > 0:
        gold_gap = gap(gold[0], gold[2])
        if gold_gap > 10: s += 1.5
        elif gold_gap > 5: s += 0.5
        if dxy > 122 and gold_gap > 5: s += 1.5

    if is_recovering:
        s -= 1.6
        log("✨ V자 회복 모멘텀 감지: 위험점수 -1.6 적용")
        
    if is_bull:
        s -= 1.0
        log("🔥 강세장 필터 가동: 리스크 점수 완화 (-1.0)")

    if breadth_status == "⚠️ 시장 왜곡 (소수 종목 편중)":
        s += 0.8
        log("⚠️ 시장 왜곡 감지: 위험점수 +0.8 가산")

    if recent_score_jump:
        s += 0.5
        log("⚡ 점수 급등 모멘텀 감지: 위험점수 +0.5 가산")

    s += (ai_score * AI_WEIGHT)
    s += regime_adj 
    
    return max(0.0, min(SCORE_MAX, s))

def calc_trend(history):
    if not history or len(history) < 2: return None
    scores = [h["score"] for h in history if "score" in h]
    if not scores: return None
    def avg(lst): return round(sum(lst) / len(lst), 1) if lst else None
    avg7  = avg(scores[-7:])
    avg30 = avg(scores[-30:])
    avg90 = avg(scores[-90:])
    trend = "📉 개선 중" if avg7 and avg30 and avg7 < avg30 else ("📈 악화 중" if avg7 and avg30 and avg7 > avg30 + 1.5 else "➖ 횡보")
    max_score = max(scores[-90:]) if len(scores) >= 1 else None
    min_score = min(scores[-90:]) if len(scores) >= 1 else None
    max_date = next((h["date"] for h in reversed(history) if h.get("score") == max_score), "-")
    min_date = next((h["date"] for h in reversed(history) if h.get("score") == min_score), "-")
    return {"avg7": avg7, "avg30": avg30, "avg90": avg90, "trend": trend, "max_score": max_score, "max_date": max_date, "min_score": min_score, "min_date": min_date}

# ==========================================
# 🚀 메인 실행부
# ==========================================
def main():
    log("📊 퀀텀 하이브리드 v10.3 가동 시작")
    
    state = load_state()
    prev_score = state.get("score", 0.0)
    current_ism = state.get("ism_pmi", 50.0)
    ism_date = state.get("ism_date", "2024-01-01")
    last_update_id = state.get("last_update_id", 0)
    history = state.get("history", [])

    new_ism = None
    try:
        url = f"https://api.telegram.org/bot{ENV['TELEGRAM_TOKEN']}/getUpdates?offset={last_update_id + 1}"
        res = requests.get(url, timeout=10).json()
        if res.get("ok") and res["result"]:
            for item in res["result"]:
                update_id = item["update_id"]
                if update_id > last_update_id: last_update_id = update_id
                msg_text = item.get("message", {}).get("text", "").upper()
                if msg_text.startswith("ISM "):
                    try: new_ism = float(msg_text.replace("ISM", "").strip())
                    except: pass
    except Exception as e: log(f"텔레그램 명령 확인 실패: {e}")

    if new_ism is not None:
        current_ism = new_ism
        ism_date = datetime.now().strftime("%Y-%m-%d")

    days_since_update = 0
    try: days_since_update = (datetime.now() - datetime.strptime(ism_date, "%Y-%m-%d")).days
    except: pass

    api_errors = []

    spy_closes = safe(lambda: get_yahoo_closes("^GSPC", "2y"), "SPY")
    if spy_closes:
        count = min(len(spy_closes), 200)
        year_closes = spy_closes[-253:] if len(spy_closes) >= 253 else spy_closes
        spy_raw = (spy_closes[-1], spy_closes[-2], sum(spy_closes[-count:]) / count, max(year_closes))
        rsi = calc_rsi_wilder(spy_closes)
        spy_dd = ((spy_raw[0] - spy_raw[3]) / spy_raw[3] * 100) if spy_raw[0] and spy_raw[3] else None
    else:
        spy_raw = (0, 0, 0, 0)
        rsi = None
        spy_dd = None
        api_errors.append("SPY")

    qqq_raw = safe(lambda: get_yahoo_stats("^IXIC"), "QQQ")
    if not qqq_raw: qqq_raw = (0,0,0,0); api_errors.append("QQQ")
    kospi_raw = safe(lambda: get_yahoo_stats("^KS11"), "KOSPI")
    if not kospi_raw: kospi_raw = (0,0,0,0); api_errors.append("KOSPI")

    fx_data = safe(lambda: get_fx_data(), "FX")
    if not fx_data: fx_data = (1400.0, 1400.0, 1400.0, 1400.0); api_errors.append("FX")
    gold = safe(lambda: get_gold_data(), "GOLD")
    if not gold: api_errors.append("GOLD")
    us10y   = safe(lambda: get_us10y(), "10Y")
    if not us10y: us10y = (None, None); api_errors.append("10Y")
    hy_spread = safe(lambda: get_hy_spread(), "HY")
    if not hy_spread: hy_spread = (None, None); api_errors.append("HY스프레드")
    unrate  = safe(lambda: get_unrate(), "실업률")
    if not unrate: unrate = 4.0; api_errors.append("실업률")

    breadth_status = get_market_breadth()

    _fg = get_fear_greed()
    fg_score, fg_label = _fg if _fg != (None, None) else (None, None)
    if fg_score is None:
        fg_score = state.get("fg_score")
        if fg_score is not None: fg_label = "(전일 캐시)"
        else: api_errors.append("공포탐욕")

    vix_closes = safe(lambda: get_yahoo_closes("^VIX", "6mo", min_count=10), "VIX")
    vix = vix_closes[-1] if vix_closes else None 
    vix_trend = pct(vix_closes[-1], vix_closes[-5]) if vix_closes and len(vix_closes) >= 5 else 0.0
    if not vix_closes: api_errors.append("VIX")

    dxy_closes = safe(lambda: get_yahoo_closes("DX-Y.NYB", "6mo", min_count=10), "DXY")
    dxy = dxy_closes[-1] if dxy_closes else 118.0
    dxy_mom = get_dxy_momentum(dxy_closes) if dxy_closes else None
    if not dxy_closes: api_errors.append("DXY")

    is_recovering = False
    if (spy_closes and len(spy_closes) >= 6 and vix_closes and len(vix_closes) >= 10):
        try:
            is_rebounding = all(spy_closes[i] > spy_closes[i-1] for i in range(-5, 0))
            vix_max = max(vix_closes[-10:])
            vix_cooling = pct(vix, vix_max) <= -15.0
            if is_rebounding and vix_cooling and spy_dd is not None and spy_dd <= -10.0: 
                is_recovering = True
        except: pass

    regime_info = get_macro_regime(current_ism, unrate)
    trend = calc_trend(history)

    recent_score_jump = False
    if len(history) >= 3:
        recent_scores = [h["score"] for h in history[-3:]]
        if recent_scores[-1] - recent_scores[0] >= 2.0:
            recent_score_jump = True

    trend_section = ""
    if trend:
        trend_section = f"""\n━━━━━━━━━━━━━━━━━━\n📊 위험 점수 추이 (90일)\n ├ 7일 평균 : {trend['avg7']}\n ├ 30일 평균: {trend['avg30']}\n └ 90일 평균: {trend['avg90']}  {trend['trend']}\n⚡ 90일 최고: {trend['max_score']}  ({trend['max_date']})\n⚡ 90일 최저: {trend['min_score']}  ({trend['min_date']})"""

    all_entries, seen_titles = [], set()
    for source_name, feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                if title and title not in seen_titles:
                    seen_titles.add(title)
                    all_entries.append(entry)
        except: pass

    news_context = extract_news_keywords(all_entries) if all_entries else "뉴스 수집 실패"
    
    market_summary = {
        "SP500_Drop": spy_dd, "VIX": vix, "DXY": dxy, "UNRATE": unrate,
        "HY_Spread": hy_spread[0] if hy_spread else None,
        "Trend": trend["trend"] if trend else None
    }
    
    ai = get_ai_analysis(news_context, market_summary) if news_context != "뉴스 수집 실패" else {"score":0.5, "market_phase":"지연", "opportunity": "-", "guru_insight": "없음", "top_risks":["-","-","-"], "strategy":"대기", "macro_correlation":"-"}

    is_bull = (spy_raw[0] > 0 and qqq_raw[0] > 0 and gap(spy_raw[0], spy_raw[2]) > 3 and gap(qqq_raw[0], qqq_raw[2]) > 3 and vix is not None and vix < 20)

    total_score = calc_risk_score(spy_raw, qqq_raw, kospi_raw, fx_data, vix, vix_trend, dxy, dxy_mom, ai["score"], us10y, fg_score, hy_spread, spy_dd, gold, rsi, is_recovering, regime_info["score_adj"], is_bull, breadth_status, recent_score_jump)
    
    best_hedge_display = get_best_hedge() if total_score >= 13 else "안전 (위험 13점 이상 시 자동 산출)"

    is_panic = ((vix is not None and vix >= VIX["panic"]) or (spy_raw[0] > 0 and pct(spy_raw[0], spy_raw[1]) <= SPY_PANIC_DROP))
    is_extreme_fear = fg_score is not None and fg_score < FG_EXTREME_FEAR

    raw_score = total_score
    diff_str = f"{(raw_score - prev_score):+.1f}"

    # --------------------------------------------------
    # ▼ [v10.3 핵심] 히스테리시스 + 추세 융합 엔진 ▼
    # --------------------------------------------------
    avg7 = trend["avg7"] if trend and trend["avg7"] is not None else raw_score
    decision_score = raw_score
    whipsaw_alert = ""

    # 클라우드 조언 반영: 실제 기록된 점수가 최소 7개 이상일 때만 작동
    scores_available = len([h for h in history if "score" in h])

    if not is_panic and raw_score < 13.0 and scores_available >= 7:
        boundaries = [3.0, 7.0, 11.0]
        buffer = 0.5
        
        for b in boundaries:
            if b - buffer <= raw_score <= b + buffer:
                if raw_score >= b and avg7 < b:
                    decision_score = b - 0.1 
                    whipsaw_alert = f"\n\n🛡️ [휩소 방어] 일시적 점수 상승({raw_score:.1f})이나, 7일 추세({avg7:.1f}) 안정으로 매도 지침을 유보합니다."
                    log(f"🛡️ 휩소 방어: 점수 {raw_score} -> {decision_score} 보정 (avg7: {avg7})")
                
                elif raw_score < b and avg7 >= b:
                    decision_score = b + 0.1
                    whipsaw_alert = f"\n\n🛡️ [휩소 방어] 일시적 점수 하락({raw_score:.1f})이나, 7일 추세({avg7:.1f}) 위험 잔존으로 방어 태세를 유지합니다."
                    log(f"🛡️ 휩소 방어: 점수 {raw_score} -> {decision_score} 보정 (avg7: {avg7})")
                
                # 클라우드 조언 반영: 조건에 걸려 보정되었다면 즉시 루프 탈출
                break
    # --------------------------------------------------

    if is_panic:
        stage_label, weight = "💀 패닉 구간", 0
        sell_idx, sell_div = "100% (전량)", "50% (절반 유지)"
        stage_action = "기존 자산 현금화 대피 (배당 파이프라인 절반 유지)"
    elif decision_score < 3:  
        stage_label, weight = "🟢 공격적 매수", 100
        sell_idx, sell_div = "0%", "0%"
        stage_action = "주식 비중 100% 유지 및 추가 매수(수량 확보)"
    elif decision_score < 7:  
        stage_label, weight = "🔵 적극적 유지", 80
        sell_idx, sell_div = "20%", "10%"
        stage_action = "1차 수익 실현 (잔파도 무시, 20%만 현금화)"
    elif decision_score < 11: 
        stage_label, weight = "🟡 부분 방어",   50
        sell_idx, sell_div = "50%", "20%"
        stage_action = "2차 수익 실현 (본격 하락 대비, 누적 50% 현금화)"
    elif decision_score < 13:
        stage_label, weight = "🟠 적극적 축소", 20
        sell_idx, sell_div = "80%", "30%"
        stage_action = "3차 수익 실현 (위기 직전, 누적 80% 현금화)"
    else:                  
        stage_label, weight = "🔴 위험 회피",   0
        sell_idx, sell_div = "100% (전량)", "50% (절반 유지)"
        stage_action = "대피 및 폭풍우 관망 (배당으로 멘탈 방어)"

    bullish_suffix = "  🔥 강세장" if is_bull else ""
    
    special_alert = ""
    if is_panic:
        special_alert = "\n\n🚨 ⚡ [블랙스완 감지] 일일 -4% 이상 폭락 (서킷브레이커)!\n▶ 시장에 돌발 패닉이 발생했습니다. 묻지도 따지지도 말고 즉시 100% 대피하십시오."
    elif decision_score >= 13:
        special_alert = "\n\n🚨 💀 [긴급 대피 시그널] 매크로 경제 붕괴(퍼펙트 스톰) 확정!\n▶ 거시 경제 지표가 최악을 가리키고 있습니다. 모든 자산을 현금화하고 관망하십시오."
    elif is_recovering:
        special_alert = "\n\n🚀 🚨 [특별 시그널] V자 폭발적 반등 포착!\n▶ 하락장 종료 확정! 대피해둔 100%의 현금을 TQQQ에 집중 투입할 절호의 타이밍입니다."
    elif decision_score == 0 and spy_dd is not None and spy_dd >= 0:
        special_alert = "\n\n🌈 ✨ [골디락스 시그널] 완벽한 대세 상승장 진입!\n▶ 리스크 제로 구간입니다. 신고가를 경신 중이니 TQQQ의 복리 폭발력을 편안하게 누리십시오."
    
    special_alert += whipsaw_alert

    fx_2y_gap = gap(fx_data[0], fx_data[3])
    fx_1y_gap = gap(fx_data[0], fx_data[2])

    if fx_2y_gap > FX_GAP["danger"]:
        fx_status = "🚨 역사적 고점권"
    elif fx_2y_gap > FX_GAP["caution"]:
        fx_status = "⚠️ 2년 평균 상회 (주의)"
    elif fx_1y_gap > FX_GAP["caution"]:
        fx_status = "🟠 1년 평균 상회"
    else:
        fx_status = "✅ 정상 범위"
        
    vix_eval_str = "🚨" if vix is not None and vix > VIX["danger"] else ("⚠️" if vix is not None and vix > VIX["warn"] else "✅")
    dxy_status = "✅" if dxy < DXY["warn"] else "⚠️" if dxy < DXY["danger"] else "🚨"
    dxy_mom_str = f"  20일 {dxy_mom:+.1f}% {'🚨' if dxy_mom and dxy_mom > DXY_MOM_WARN else ''}" if dxy_mom else ""
    hy_eval = f"{hy_spread[0]:.2f}% ({'위험' if hy_spread[0] > HY_SPREAD_DANGER else '주의' if hy_spread[0] > HY_SPREAD_WARN else '안정'})" if hy_spread[0] else "지연"
    extreme_fear_alert = f"\n🔔 극단적 공포 감지 (F&G={fg_score})\n   → 역발상 분할매수 검토 구간\n" if is_extreme_fear else ""

    msg_header = f"🤖 퀀텀 인사이트 v10.3  |  {datetime.now().strftime('%Y.%m.%d %H:%M')}"
    if new_ism is not None:
        msg_header += f"\n\n✅ [업데이트 완료] 텔레그램 명령으로 ISM 지수가 {current_ism}로 갱신되었습니다!"
    elif days_since_update > 35:
        msg_header += f"\n\n🚨🚨 [경고] ISM 지수가 너무 오래되었습니다! (마지막 갱신: {days_since_update}일 전)\n채팅창에 'ISM 50.2' 형식으로 최신 수치를 보내주세요! 🚨🚨"

    sys_status_msg = f"⚠️ 데이터 지연 ({', '.join(api_errors)})" if api_errors else "✅ 정상"
    if is_panic: sys_status_msg = f"🚨 패닉 감지 | {sys_status_msg}"

    msg = f"""{msg_header}
━━━━━━━━━━━━━━━━━━
🌍 거시 경제 국면 (매크로 내비게이션)
 ├ ISM 제조업: {current_ism} / 미국 실업률: {unrate}%
 ├ 현재 국면  : {regime_info['emoji']} {regime_info['name']}
 └ 시스템 보정: {regime_info['action']} (위험점수 {regime_info['score_adj']:+.1f}점 조절)
━━━━━━━━━━━━━━━━━━
📌 시장 국면
{ai['market_phase']}{bullish_suffix}

⚠️ 핵심 리스크
① {ai['top_risks'][0]}
② {ai['top_risks'][1]}
③ {ai['top_risks'][2]}

💡 기회 요인 (미래 산업 진단)
{ai['opportunity']}

🧙‍♂️ 거장 시그널
{ai['guru_insight']}

🧭 대응 전략
{ai['strategy']}
{extreme_fear_alert}
🧭 추가 시장 진단
 ├ 시장 폭(Breadth): {breadth_status}
 └ 위기 시 최적 대피처: {best_hedge_display}
━━━━━━━━━━━━━━━━━━
📊 위험 점수: {raw_score:.1f} / 15.0 ({diff_str}){trend_section}
🎯 자산 배분: 주식 {weight}%  |  현금 {100-weight}%
📢 매도 지침 (현재 수량 기준):
 ├ 📈 지수/성장(QQQ, SPY): 【 {sell_idx} 】
 └ 💰 배당/인컴(SCHD, JEPI): 【 {sell_div} 】

🚦 국면: {stage_label}
📋 행동: {stage_action}{special_alert}
━━━━━━━━━━━━━━━━━━
📈 주요 지표

S&P 500  : {format_index(*spy_raw)}
 └ 52주 고점 대비: {get_drawdown_label(spy_dd)}
NASDAQ   : {format_index(*qqq_raw)}
KOSPI    : {format_index(*kospi_raw)}
RSI(S&P) : {get_rsi_label(rsi)}

💵 환율 (USD/KRW)
{fx_data[0]:,.0f}원  {fx_status}
 ├ 1년 평균: {fx_data[2]:,.0f}원  ({gap(fx_data[0], fx_data[2]):+.1f}%)
 └ 2년 평균: {fx_data[3]:,.0f}원  ({fx_2y_gap:+.1f}%)

😨 공포탐욕  : {f"{fg_score}  {fg_label}" if fg_score is not None else "지연"}
📊 VIX      : {f"{vix:.2f}" if vix is not None else "지연"}  {vix_eval_str}
💲 달러인덱스: {dxy:.1f}  {dxy_status}{dxy_mom_str}
🏦 미 10Y금리: {f"{us10y[0]:.2f}%" if us10y and us10y[0] else "지연"}
📉 HY스프레드: {hy_eval}
🥇 금        : {f"{gold[0]:,.0f}  {get_gold_signal(gold)}" if gold else "지연"}
━━━━━━━━━━━━━━━━━━
💡 매크로 지표 심층 분석 (AI)
{ai['macro_correlation']}
━━━━━━━━━━━━━━━━━━
🛠 시스템: {sys_status_msg}
"""

    def split_message(text, max_len=3900):
        parts = []
        while len(text) > max_len:
            split_at = text.rfind('\n', 0, max_len)
            if split_at == -1: 
                split_at = max_len
            parts.append(text[:split_at])
            text = text[split_at:].lstrip()
        if text: 
            parts.append(text)
        return parts

    for chunk in split_message(msg):
        for _ in range(3):
            try:
                requests.post(
                    f"https://api.telegram.org/bot{ENV['TELEGRAM_TOKEN']}/sendMessage",
                    data={"chat_id": ENV["CHAT_ID"], "text": chunk}, timeout=15
                ).raise_for_status()
                break 
            except Exception as e:
                time.sleep(2) 
        else:
            log("❌ 텔레그램 메시지 전송 최종 실패")

    today_str = datetime.now().strftime('%Y-%m-%d')
    daily_log = [e for e in state.get("daily_log", []) if e.get("date") != today_str]
    daily_log.append({"date": today_str, "score": round(raw_score, 1), "phase": stage_label})

    new_state_data = {
        "score": raw_score,
        "stage": stage_label,
        "ism_pmi": current_ism,
        "ism_date": ism_date,
        "last_update_id": last_update_id,
        "fg_score": fg_score,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "daily_log": daily_log[-365:]
    }

    save_state(new_state_data, 
               existing_history=history, 
               spy_current=spy_raw[0], 
               spy_pct=pct(spy_raw[0], spy_raw[1]), 
               spy_dd=spy_dd, 
               vix=vix, 
               fg_score=fg_score, 
               dxy=dxy, 
               hy_spread=hy_spread[0] if hy_spread else None, 
               us10y=us10y[0] if us10y else None, 
               fx=fx_data[0])
    
    log(f"✅ v10.3 완료 | 국면={regime_info['name']} | 산출점수={raw_score:.1f}")

if __name__ == "__main__":
    main()
