import requests, os, json, feedparser, re, time, logging, sys, html
from playwright.sync_api import sync_playwright
from datetime import datetime, timedelta
from openai import OpenAI

# ==========================================
# ⚙️ 설정 & 상수
# ==========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
def log(msg): logging.info(msg)

VIX              = {"warn": 25, "danger": 35, "panic": 45}
FX_GAP           = {"caution": 4, "danger": 8}
DXY              = {"warn": 122, "danger": 126}
SPY_DAILY_DROP   = -2.5
SPY_TREND_GAP    = 3.0
SCORE_MAX        = 15.0
HY_SPREAD_WARN   = 4.5
HY_SPREAD_DANGER = 6.5
FG_EXTREME_FEAR  = 10
DXY_MOM_WARN     = 3.0
DRAWDOWN_WARN    = -10.0
DRAWDOWN_DANGER  = -20.0
AI_WEIGHT        = 0.5

RETRY_COUNT = 4
RETRY_DELAY = 45

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
            log(f"⚠️ [{label}] {i+1}차 실패: {type(e).__name__}: {e}")
            if e.response is not None and e.response.status_code in (401, 403, 418): break
        except Exception as e:
            log(f"⚠️ [{label}] {i+1}차 실패: {type(e).__name__}: {e}")
        if i < retry - 1: time.sleep(delay)
    log(f"❌ [{label}] 최종 실패")
    return None

def load_state():
    try:
        url = f"https://api.github.com/gists/{ENV['GIST_ID']}"
        headers = {"Authorization": f"token {ENV['GITHUB_TOKEN']}"}
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        return json.loads(res.json()['files']['bot_state.json']['content'])
    except: return {}

def save_state(score, stage, fg_score=None):
    try:
        url = f"https://api.github.com/gists/{ENV['GIST_ID']}"
        headers = {"Authorization": f"token {ENV['GITHUB_TOKEN']}", "Accept": "application/vnd.github.v3+json"}
        payload = {
            "score": score,
            "stage": stage,
            "updated": datetime.now().isoformat(),
        }
        if fg_score is not None:
            payload["fg_score"] = fg_score
        data = {"files": {"bot_state.json": {"content": json.dumps(payload, ensure_ascii=False)}}}
        requests.patch(url, headers=headers, json=data, timeout=10).raise_for_status()
    except Exception as e: log(f"상태 저장 실패: {e}")

# ==========================================
# 📊 데이터 수집
# ==========================================
def get_fred_series(series_id, days=1000, min_count=50):
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

def get_yahoo_closes(ticker, range_="2y"):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={range_}"
    res = requests.get(url, headers=YAHOO_HEADERS, timeout=12)
    res.raise_for_status()
    closes = [v for v in res.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if v is not None]
    if len(closes) < 50: raise ValueError(f"데이터 부족")
    return closes

def get_yahoo_stats(ticker, range_="2y"):
    closes = get_yahoo_closes(ticker, range_)
    count = min(len(closes), 200)
    return closes[-1], closes[-2], sum(closes[-count:]) / count, max(closes[-min(len(closes), 253):]) if len(closes) > 1 else closes[0]

def get_dxy_momentum(dxy_closes):
    if not dxy_closes or len(dxy_closes) < 21: return None
    return pct(dxy_closes[-1], dxy_closes[-21])

def get_fx_data():
    closes = get_yahoo_closes("KRW=X", "2y")
    yh_c, yh_p = closes[-1], (closes[-2] if len(closes) > 1 else closes[-1])
    return yh_c, yh_p, sum(closes[-min(len(closes), 252):]) / min(len(closes), 252), sum(closes[-min(len(closes), 504):]) / min(len(closes), 504), None

def get_gold_data():
    closes = get_yahoo_closes("GC=F", "1y")
    return closes[-1], closes[-2], closes[-20], sum(closes[-min(len(closes), 252):]) / min(len(closes), 252)

def calc_rsi_wilder(values, period=14):
    if not values or len(values) < period * 2: return None
    deltas = [values[i] - values[i-1] for i in range(1, len(values))]
    gains, losses = [max(d, 0) for d in deltas], [max(-d, 0) for d in deltas]
    avg_gain, avg_loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1) if avg_loss != 0 else 100.0

# ==========================================
# 😨 Fear & Greed (CNN 전용)
# ==========================================
def get_fear_greed():
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

    headers_list = [
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://edition.cnn.com/markets/fear-and-greed",
            "Origin": "https://edition.cnn.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        },
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.cnn.com/markets/fear-and-greed",
            "Origin": "https://edition.cnn.com",
        },
    ]

    for attempt in range(4):
        headers = headers_list[attempt % len(headers_list)]
        try:
            res = requests.get(url, headers=headers, timeout=20)

            if res.status_code == 418:
                log(f"⚠️ [F&G CNN] {attempt+1}차 418 Teapot → 봇 차단, 헤더 교체 후 재시도")
                time.sleep(15)
                continue

            res.raise_for_status()
            data = res.json()
            if "fear_and_greed" in data and isinstance(data["fear_and_greed"], dict):
                score = round(float(data["fear_and_greed"]["score"]))
            elif "fear_and_greed_historical" in data and data["fear_and_greed_historical"].get("data"):
                score = round(float(data["fear_and_greed_historical"]["data"][-1]["y"]))
            else:
                raise ValueError("CNN F&G JSON 구조 변경 감지")

            if score <= 10:  lbl = "극단적 공포 😱🚨"
            elif score <= 25: lbl = "극단적 공포 😱"
            elif score <= 45: lbl = "공포 😨"
            elif score <= 55: lbl = "중립 😐"
            elif score <= 75: lbl = "탐욕 😏"
            else:             lbl = "극단적 탐욕 🤑"

            log(f"✅ [F&G CNN] {attempt+1}차 성공: {score}")
            return score, lbl

        except Exception as e:
            log(f"⚠️ [F&G CNN] {attempt+1}차 실패: {type(e).__name__}: {e}")
            if attempt < 3:
                time.sleep(10)

    log("❌ [F&G CNN] 4회 모두 실패 → 캐시 사용")
    return None, None

def format_index(c, p, sma, _=None):
    if c == 0: return "데이터 지연"
    return f"{c:,.0f}  {arrow(pct(c,p))}{abs(pct(c,p)):.1f}%\n └ 200일선 대비: {gap(c,sma):+.1f}%"

def get_drawdown_label(dd):
    if dd is None: return "산출 불가"
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
    st_gap = gap(gold[0], gold[3])
    if st_gap > 10: return "🚨 장기 과열"
    elif st_gap > 3: return "🟠 상승 추세"
    elif st_gap < -5: return "🟢 저점 근접"
    return "➖ 중립"

# ==========================================
# 🧠 AI 분석
# ==========================================
def extract_news_keywords(entries, max_items=8):
    critical, normal = [], []
    for e in entries:
        title = getattr(e, "title", "").strip()
        if not title:
            continue
        summary = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html.unescape(getattr(e, "summary", "")))).strip()
        key_sents = [s.strip() for s in re.split(r'[.!?]', summary) if any(kw.lower() in s.lower() for kw in ECON_KEYWORDS)]
        context = " / ".join(key_sents[:2]) if key_sents else summary[:80]
        if any(kw in title.lower() for kw in MACRO_CRITICAL): critical.append(f"🚨[핵심 매크로] {title}  [{context}]")
        else: normal.append(f"• {title}  [{context}]")
    return "\n".join((critical + normal)[:max_items])

def get_ai_analysis(news: str, market_summary: dict) -> dict:
    prompt = f"""
당신은 월스트리트 최고 수준의 퀀트 매크로 전략가이며, 현재 '매일 기계적으로 지수(VOO, QQQ)를 모아가며, 포트폴리오의 일부를 미래 전략산업(QTUM, UFO, NASA, ARKQ 등)에 위성 투자(Satellite)하는 투자자'를 전담 보좌하는 수석 비서입니다.

[분석 원칙 - 매우 중요]
1. [매크로 최우선] 뉴스 중 '🚨[핵심 매크로]' 연준, 금리 데이터에 집중하여 시장의 흐름(Risk-On/Off) 진단.
2. [상관관계] 전달받은 데이터 수치를 맹신하고 증시에 미치는 영향을 'macro_correlation'에 통찰력 있게 작성.
3. [대응 전략] 비율(%) 숫자 금지. 투자자의 심리적 템포와 마음가짐 중심으로 'strategy' 작성.
4. [미래 전략산업] 매크로 환경이 우주/로봇/양자에 우호적인지 'opportunity'에 1~2문장 진단.
5. [거장 시그널 분리] 워런 버핏, 드러켄밀러 등 거장의 발언이 있다면 'guru_score'(-0.5~+0.5) 부여 및 'guru_insight' 요약. 없으면 0.0. 일반 리스크는 'macro_score'(-1.5~1.5) 부여.
6. [언어] 반드시 한국어(Korean) 출력.

[시장 데이터]
{json.dumps(market_summary, ensure_ascii=False)}

[주요 뉴스]
{news}

[출력: JSON만]
{{"macro_score": <실수>, "guru_score": <실수>, "guru_insight": "<거장뷰>", "market_phase": "<국면>", "top_risks": ["<1>","<2>","<3>"], "opportunity": "<미래산업>", "strategy": "<조언>", "macro_correlation": "<진단>"}}
"""
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
# 🎯 위험 점수 산출 (v9.0 Professional)
# ==========================================
def calc_risk_score(spy, qqq, kospi, fx_data, vix, vix_trend, dxy, dxy_mom, 
                    ai_score, us10y, fg_score, hy_spread, spy_dd, gold, rsi, 
                    is_recovering=False):
    s = 0.0
    
    # 1. 강세장 판독 필터
    is_bull_market = False
    if (spy[0] > 0 and qqq[0] > 0 and spy[2] > 0 and qqq[2] > 0):
        is_bull_market = (spy[0] > spy[2] and qqq[0] > qqq[2] and vix < 20)
        
    # 2. 지수 추세 및 변동성
    if spy[0] > 0:
        spy_gap = gap(spy[0], spy[2])
        if spy_gap < -SPY_TREND_GAP: s += 2.0
        elif spy_gap < 0: s += 1.0
        if pct(spy[0], spy[1]) <= -4.0: s += 3.0

    if qqq[0] > 0:
        qqq_gap = gap(qqq[0], qqq[2])
        if qqq_gap < -SPY_TREND_GAP: s += 1.5
        elif qqq_gap < 0: s += 0.5

    if kospi and kospi[0] > 0 and kospi[2] > 0:
        kos_gap = gap(kospi[0], kospi[2])
        if kos_gap < -4.0: s += 1.5
        elif kos_gap < -2.0: s += 1.0

    # 3. 드로다운(MDD) 및 RSI
    if spy_dd is not None:
        if spy_dd <= DRAWDOWN_DANGER: s += 2.5
        elif spy_dd <= -15.0: s += 2.0
        elif spy_dd <= DRAWDOWN_WARN: s += 1.0

    if rsi is not None:
        if rsi > 75: s += 1.0
        elif rsi < 30: s -= 0.5

    # 4. 환율 리스크
    fx_c, fx_p, fx_1y, fx_2y, _ = fx_data
    if gap(fx_c, fx_2y) > FX_GAP["danger"]: s += 2.0
    elif gap(fx_c, fx_2y) > FX_GAP["caution"]: s += 1.0
    if pct(fx_c, fx_p) > 2.0: s += 1.0

    # 5. VIX 추세 및 절대값
    if vix_trend >= 10 and vix >= VIX["warn"]: s += 1.0
    elif vix_trend <= -10: s -= 0.5

    if vix >= VIX["panic"]: s += 4.0
    elif vix >= 30: s += 1.5
    elif vix >= VIX["warn"]: s += 1.0

    # 6. 매크로 (달러/금리/하이일드)
    if dxy > DXY["danger"]: s += 1.5
    elif dxy > DXY["warn"]: s += 0.5
    if dxy_mom and dxy_mom > DXY_MOM_WARN: s += 2.0 if dxy_mom > DXY_MOM_WARN * 1.5 else 1.0

    if us10y and us10y[0] and us10y[1]:
        rc = us10y[0] - us10y[1]
        if rc > 0.15: s += 1.5

    if hy_spread and hy_spread[0]:
        hys = hy_spread[0]
        if hys > HY_SPREAD_DANGER: s += 3.0
        elif hys > HY_SPREAD_WARN: s += 1.5
        if hy_spread[1] and (hys - hy_spread[1]) > 0.3: s += 1.0

    # 7. 공포탐욕 지수 역발상
    if fg_score is not None:
        if fg_score > 80: s += 1.0
        elif fg_score < FG_EXTREME_FEAR:
            s -= 1.5 if vix < VIX["warn"] else 0.5

    # 8. 안전자산 (금)
    if gold and gold[3] > 0:
        gold_gap = gap(gold[0], gold[3])
        if gold_gap > 10: s += 1.5
        elif gold_gap > 5: s += 0.5
        if dxy > 122 and gold_gap > 5: s += 1.5

    # 9. 보너스 섹션
    if is_recovering:
        s -= 1.6
        log("✨ V자 회복 모멘텀 감지: 위험점수 -1.6 적용")

    if is_bull_market:
        s -= 1.0
        log("🔥 강세장 필터 가동: 리스크 점수 완화 (-1.0)")

    # 10. AI 분석
    ai_score_clamped = max(-1.5, min(1.5, ai_score))
    s += (ai_score_clamped * AI_WEIGHT)

    return max(0.0, min(SCORE_MAX, s))

# ==========================================
# 🎨 이미지 생성 (v10.0 풀버전 인포그래픽)
# ==========================================
def generate_card_image(total_score, stage_label, ai, weight, diff_str, date_str,
                        spy_raw, qqq_raw, kospi_raw, spy_dd, rsi,
                        fx_data, fg_score, fg_label, vix, dxy, dxy_mom, us10y, hy_eval, gold,
                        sys_status_msg, bullish_suffix):
    
    score_pct = (total_score / 15.0) * 100
    
    # 핀테크 스타일 컬러셋
    if total_score < 7:
        bar_gradient = "linear-gradient(90deg, #00F260 0%, #0575E6 100%)"
        accent_color = "#00F260"
        bg_glow = "rgba(0, 242, 96, 0.15)"
    elif total_score < 11:
        bar_gradient = "linear-gradient(90deg, #FDC830 0%, #F37335 100%)"
        accent_color = "#FDC830"
        bg_glow = "rgba(253, 200, 48, 0.15)"
    else:
        bar_gradient = "linear-gradient(90deg, #FF416C 0%, #FF4B2B 100%)"
        accent_color = "#FF416C"
        bg_glow = "rgba(255, 65, 108, 0.15)"

    # 데이터 포맷팅 헬퍼
    def fmt_pct(val): return f"{val:+.1f}%" if val else "0.0%"
    def get_color(val): return "#00F260" if val and val > 0 else "#FF416C" if val and val < 0 else "#FFFFFF"
    
    spy_p = pct(spy_raw[0], spy_raw[1])
    qqq_p = pct(qqq_raw[0], qqq_raw[1])
    kos_p = pct(kospi_raw[0], kospi_raw[1])

    risks = ai.get('top_risks', ["-", "-", "-"])
    
    html_content = f"""
    <html><head><meta charset="utf-8">
    <style>
      @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
      * {{ margin:0; padding:0; box-sizing:border-box; font-family: 'Pretendard', sans-serif; }}
      body {{ background-color: #0B0E14; padding: 20px; width: 480px; color: #FFFFFF; letter-spacing: -0.3px; }}
      
      .dashboard {{ background: #131722; border-radius: 20px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); border: 1px solid rgba(255,255,255,0.05); overflow: hidden; }}
      
      .header {{ border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 16px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: flex-end; }}
      .brand {{ font-size: 20px; font-weight: 800; background: linear-gradient(90deg, #fff, #aaa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
      .time {{ font-size: 12px; color: #787B86; }}
      
      .score-wrap {{ text-align: center; margin-bottom: 20px; padding: 20px; background: rgba(0,0,0,0.2); border-radius: 16px; border: 1px solid rgba(255,255,255,0.03); }}
      .score-title {{ font-size: 13px; color: #787B86; font-weight: 600; margin-bottom: 4px; }}
      .score-val {{ font-size: 42px; font-weight: 800; color: {accent_color}; line-height: 1; text-shadow: 0 0 20px {bg_glow}; margin-bottom: 4px; }}
      .score-diff {{ font-size: 13px; color: #B2B5BE; }}
      
      .bar-container {{ height: 8px; background: #2A2E39; border-radius: 4px; margin: 12px 0 8px; overflow: hidden; }}
      .bar-fill {{ height: 100%; width: {score_pct}%; background: {bar_gradient}; border-radius: 4px; box-shadow: 0 0 10px {accent_color}; }}
      
      .status-badge {{ display: inline-block; padding: 6px 14px; border-radius: 20px; font-size: 14px; font-weight: 700; background: {bg_glow}; color: {accent_color}; margin-top: 10px; }}
      
      .grid-3 {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 20px; }}
      .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 20px; }}
      
      .card {{ background: #1E222D; border-radius: 12px; padding: 14px; border: 1px solid rgba(255,255,255,0.04); }}
      .c-lbl {{ font-size: 11px; color: #787B86; margin-bottom: 4px; text-transform: uppercase; }}
      .c-val {{ font-size: 16px; font-weight: 700; color: #D1D4DC; }}
      .c-sub {{ font-size: 11px; margin-top: 2px; }}
      
      .section-title {{ font-size: 14px; font-weight: 700; color: #FFFFFF; margin: 24px 0 12px; border-left: 3px solid {accent_color}; padding-left: 8px; }}
      
      .text-box {{ background: rgba(30, 34, 45, 0.5); border-radius: 12px; padding: 16px; font-size: 13px; color: #B2B5BE; line-height: 1.6; border: 1px solid rgba(255,255,255,0.03); margin-bottom: 12px; }}
      .text-box strong {{ color: #D1D4DC; }}
      
      .ul-list {{ list-style-type: none; }}
      .ul-list li {{ margin-bottom: 6px; position: relative; padding-left: 14px; }}
      .ul-list li::before {{ content: '•'; position: absolute; left: 0; color: {accent_color}; }}
      
      .footer {{ text-align: center; font-size: 11px; color: #50535E; margin-top: 20px; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 16px; }}
    </style>
    </head><body>
    
    <div class="dashboard">
      <div class="header">
        <div class="brand">QUANTUM INSIGHT</div>
        <div class="time">{date_str}</div>
      </div>

      <div class="score-wrap">
        <div class="score-title">시장 위험도 및 포지션</div>
        <div class="score-val">{total_score:.1f}</div>
        <div class="score-diff">전일 대비 {diff_str}  |  주식 {weight}% · 현금 {100-weight}%</div>
        <div class="bar-container"><div class="bar-fill"></div></div>
        <div class="status-badge">{stage_label}{bullish_suffix}</div>
      </div>

      <div class="section-title">📊 핵심 지수 현황</div>
      <div class="grid-3">
        <div class="card">
          <div class="c-lbl">S&P 500</div>
          <div class="c-val">{spy_raw[0]:,.0f}</div>
          <div class="c-sub" style="color:{get_color(spy_p)}">{fmt_pct(spy_p)}</div>
        </div>
        <div class="card">
          <div class="c-lbl">NASDAQ</div>
          <div class="c-val">{qqq_raw[0]:,.0f}</div>
          <div class="c-sub" style="color:{get_color(qqq_p)}">{fmt_pct(qqq_p)}</div>
        </div>
        <div class="card">
          <div class="c-lbl">KOSPI</div>
          <div class="c-val">{kospi_raw[0]:,.0f}</div>
          <div class="c-sub" style="color:{get_color(kos_p)}">{fmt_pct(kos_p)}</div>
        </div>
      </div>

      <div class="section-title">🧭 매크로 레이더</div>
      <div class="grid-2">
        <div class="card"><div class="c-lbl">USD/KRW 환율</div><div class="c-val">{fx_data[0]:,.0f}원</div></div>
        <div class="card"><div class="c-lbl">VIX 변동성</div><div class="c-val">{vix:.2f}</div></div>
        <div class="card"><div class="c-lbl">공포탐욕 지수</div><div class="c-val">{fg_score if fg_score is not None else '지연'}</div></div>
        <div class="card"><div class="c-lbl">미 10년물 금리</div><div class="c-val">{f"{us10y[0]:.2f}%" if us10y and us10y[0] else '지연'}</div></div>
        <div class="card"><div class="c-lbl">달러 인덱스</div><div class="c-val">{dxy:.1f}</div></div>
        <div class="card"><div class="c-lbl">하이일드 스프레드</div><div class="c-val">{hy_eval.split(' ')[0]}</div></div>
      </div>

      <div class="section-title">⚠️ Top 3 거시경제 리스크</div>
      <div class="text-box">
        <ul class="ul-list">
          <li>{risks[0] if len(risks) > 0 else '-'}</li>
          <li>{risks[1] if len(risks) > 1 else '-'}</li>
          <li>{risks[2] if len(risks) > 2 else '-'}</li>
        </ul>
      </div>

      <div class="section-title">💡 퀀텀 심층 분석 & 전략</div>
      <div class="text-box">
        <strong>[매크로 진단]</strong><br>{ai.get('macro_correlation', '-').replace(chr(10), '<br>')}<br><br>
        <strong>[미래 산업 기회]</strong><br>{ai.get('opportunity', '-').replace(chr(10), '<br>')}<br><br>
        <strong>[거장 시그널]</strong><br>{ai.get('guru_insight', '-').replace(chr(10), '<br>')}<br><br>
        <strong>[대응 전략]</strong><br><span style="color:#FFF;">{ai.get('strategy', '-').replace(chr(10), '<br>')}</span>
      </div>

      <div class="footer">
        {sys_status_msg}<br>
        Generated by Quantum AI Engine v10.0
      </div>
    </div>
    
    </body></html>
    """
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 480, "height": 800}) # height는 넉넉하게
            page.set_content(html_content)
            page.wait_for_timeout(1000) # 렌더링 대기 시간 약간 증가
            path = "/tmp/quantum_full_dashboard.png"
            page.screenshot(path=path, full_page=True, omit_background=True)
            browser.close()
            return path
    except Exception as e:
        log(f"카드 이미지 생성 실패: {e}")
        return None

# ==========================================
# 🚀 메인 실행부
# ==========================================
def main():
    log("📊 퀀텀 v10.0 가동 시작")
    api_errors = []

    spy_raw = safe(lambda: get_yahoo_stats("^GSPC"), "SPY")
    if not spy_raw: spy_raw = (0, 0, 0, 0); api_errors.append("SPY")

    qqq_raw = safe(lambda: get_yahoo_stats("^IXIC"), "QQQ")
    if not qqq_raw: qqq_raw = (0, 0, 0, 0); api_errors.append("QQQ")

    kospi_raw = safe(lambda: get_yahoo_stats("^KS11"), "KOSPI")
    if not kospi_raw: kospi_raw = (0, 0, 0, 0); api_errors.append("KOSPI")

    fx_data = safe(lambda: get_fx_data(), "FX")
    if not fx_data: fx_data = (1400.0, 1400.0, 1400.0, 1400.0, None); api_errors.append("FX")

    gold = safe(lambda: get_gold_data(), "GOLD")
    if not gold: api_errors.append("GOLD")

    us10y = safe(lambda: get_us10y(), "10Y")
    if not us10y: us10y = (None, None); api_errors.append("10Y")

    hy_spread = safe(lambda: get_hy_spread(), "HY")
    if not hy_spread: hy_spread = (None, None); api_errors.append("HY스프레드")

    # 🔹 상태 1회만 로드 (중복 호출 방지)
    state = load_state()

    # ── Fear & Greed: CNN 전용, 실패 시 Gist 캐시 사용 ──
    _fg = get_fear_greed()
    fg_score, fg_label = _fg if _fg != (None, None) else (None, None)

    if fg_score is None:
        prev_fg = state.get("fg_score")
        if prev_fg is not None:
            fg_score = prev_fg
            fg_label = "(전일 캐시)"
            log(f"⚠️ F&G CNN 실패 → 전일 캐시값 사용: {fg_score}")
            api_errors.append("공포탐욕(캐시)")
        else:
            api_errors.append("공포탐욕")

    vix_closes = safe(lambda: get_yahoo_closes("^VIX", "6mo"), "VIX")
    vix = vix_closes[-1] if vix_closes else 22.0
    vix_trend = pct(vix_closes[-1], vix_closes[-5]) if vix_closes and len(vix_closes) >= 5 else 0.0
    if not vix_closes: api_errors.append("VIX")

    dxy_closes = safe(lambda: get_yahoo_closes("DX-Y.NYB", "6mo"), "DXY")
    dxy = dxy_closes[-1] if dxy_closes else 118.0
    dxy_mom = get_dxy_momentum(dxy_closes) if dxy_closes else None
    if not dxy_closes: api_errors.append("DXY")

    spy_closes = safe(lambda: get_yahoo_closes("^GSPC", "2y"), "RSI")
    rsi = calc_rsi_wilder(spy_closes) if spy_closes else None
    spy_dd = ((spy_raw[0] - spy_raw[3]) / spy_raw[3] * 100) if spy_raw[0] and spy_raw[3] else None

    # 여러 소스에서 뉴스를 모아 중복 제거 후 분석
    all_entries = []
    seen_titles = set()
    news_source_errors = []

    for source_name, feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            count = 0
            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                if title and title not in seen_titles:
                    seen_titles.add(title)
                    all_entries.append(entry)
                    count += 1
            log(f"✅ [{source_name}] {count}건 수집")
        except Exception as e:
            log(f"⚠️ [{source_name}] 뉴스 수집 실패: {e}")
            news_source_errors.append(source_name)

    if not all_entries:
        news_context = "뉴스 수집 실패"
        api_errors.append("뉴스(전체 실패)")
    else:
        if news_source_errors:
            log(f"⚠️ 일부 뉴스 소스 실패: {', '.join(news_source_errors)}")
        news_context = extract_news_keywords(all_entries)

    hy_eval = "지연"
    if hy_spread and hy_spread[0]:
        hy_eval = "위험" if hy_spread[0] > HY_SPREAD_DANGER else ("주의" if hy_spread[0] > HY_SPREAD_WARN else "안정")
        hy_eval = f"{hy_spread[0]:.2f}% ({hy_eval})"

    vix_eval = "위험" if vix > VIX["danger"] else ("주의" if vix > VIX["warn"] else "안정")

    market_summary = {
        "SP500":      {"현재": spy_raw[0], "전일대비%": round(pct(spy_raw[0], spy_raw[1]), 2), "200일선대비%": round(gap(spy_raw[0], spy_raw[2]), 2), "고점대비%": round(spy_dd, 1) if spy_dd else None},
        "NASDAQ":     {"현재": qqq_raw[0], "전일대비%": round(pct(qqq_raw[0], qqq_raw[1]), 2)},
        "KOSPI":      {"현재": kospi_raw[0], "200일선대비%": round(gap(kospi_raw[0], kospi_raw[2]), 2) if kospi_raw[0] else None},
        "VIX":        f"{vix:.2f} ({vix_eval})",
        "DXY":        {"현재": dxy, "20일모멘텀%": round(dxy_mom, 2) if dxy_mom else None},
        "USD_KRW":    fx_data[0],
        "US10Y금리":  us10y[0],
        "HY스프레드": hy_eval,
        "공포탐욕":   fg_score,
        "금현재가":   f"{gold[0]:.0f} ({get_gold_signal(gold)})" if gold else None,
        "RSI_SP500":  rsi,
    }

    def _clean(v):
        if isinstance(v, dict): return {k2: v2 for k2, v2 in v.items() if v2 is not None}
        return v
    market_summary = {k: _clean(v) for k, v in market_summary.items() if v is not None}

    if news_context == "뉴스 수집 실패":
        ai = {"score": 0.5, "market_phase": "데이터 지연 관망", "top_risks": ["-","-","-"], "opportunity": "-", "strategy": "보수적 운용 필요", "macro_correlation": "분석 지연", "guru_insight": "없음"}
    else:
        try: 
            ai = get_ai_analysis(news_context, market_summary)
        except Exception as e:
            log(f"AI 분석 실패: {e}")
            ai = {"score": 0.5, "market_phase": "AI 지연", "top_risks": ["-","-","-"], 
                  "opportunity": "-", "strategy": "보수적 운용 필요", 
                  "macro_correlation": "지연", "guru_insight": "없음"}
            api_errors.append("AI응답")

    # ==================== V자 반등 회복 판단 ====================
    is_recovering = False
    if (spy_closes and len(spy_closes) >= 6 and 
        vix_closes and len(vix_closes) >= 10):
        try:
            is_rebounding = all(spy_closes[i] > spy_closes[i-1] for i in range(-5, 0))
            vix_max = max(vix_closes[-10:])
            vix_cooling = pct(vix, vix_max) <= -15.0
            if is_rebounding and vix_cooling:
                is_recovering = True
                log("🔥 V자 회복 신호 감지 (5일 상승 + VIX 15%↓)")
        except Exception as e:
            log(f"⚠️ V자 회복 판단 중 오류: {e}")
    # ===========================================================

    total_score = calc_risk_score(spy_raw, qqq_raw, kospi_raw, fx_data, vix,
                                 vix_trend, dxy, dxy_mom, ai["score"], us10y,
                                 fg_score, hy_spread, spy_dd, gold, rsi,
                                 is_recovering)

    # VIX 45(Panic) 이상 또는 하루 -4.0% 폭락 시에만 '현금화 대피' 가동
    is_panic = vix >= VIX["panic"] or (spy_raw[0] > 0 and pct(spy_raw[0], spy_raw[1]) <= -4.0)
    is_extreme_fear = fg_score is not None and fg_score < FG_EXTREME_FEAR

    if is_panic:
        stage_label, weight, stage_action = "💀 패닉 구간", 0, "기존 자산 100% 현금화 대피 (현금 방어 유지 + 극단 구간에서만 분할 접근)"
        total_score = SCORE_MAX
    elif total_score < 3:  
        stage_label, weight, stage_action = "🟢 공격적 매수", 100, "주식 비중 100% 유지 및 수익 극대화"
    elif total_score < 7:  
        stage_label, weight, stage_action = "🔵 적극적 유지", 80,  "1차 수익 실현 및 방어 (자산 20% 현금화)"
    elif total_score < 11: 
        stage_label, weight, stage_action = "🟡 부분 방어",   60,  "2차 추가 수익 실현 (자산 40% 현금화)"
    elif total_score < 13:
        stage_label, weight, stage_action = "🟠 적극적 축소", 30,  "보수적 운영 (자산 70% 현금화)"
    else:                 
        stage_label, weight, stage_action = "🔴 위험 회피",   0,   "대피 및 폭풍우 관망 (100% 현금화)"

    prev_stage = state.get("stage", stage_label)
    prev_score = state.get("score", total_score)
    diff_str = f"{(total_score - prev_score):+.1f}"

    stage_change_alert = f"📢📢 국면 변화 감지!\n   {prev_stage}  →  {stage_label}\n━━━━━━━━━━━━━━━━━━\n" if prev_stage != stage_label else ""
    bullish_suffix = "  🔥 강세장" if spy_raw[0] > 0 and gap(spy_raw[0], spy_raw[2]) > 3 and qqq_raw[0] > 0 and gap(qqq_raw[0], qqq_raw[2]) > 3 else ""

    sys_status_msg = f"⚠️ 데이터 지연 ({', '.join(api_errors)})" if api_errors else "✅ 정상"
    if is_panic: sys_status_msg = f"🚨 패닉 감지 | {sys_status_msg}"

    # ----- [맨 밑바닥 교체할 부분 시작] -----
    
    date_str = datetime.now().strftime('%Y.%m.%d %H:%M')
    
    # 만약 이미지 생성이 실패할 때를 대비한 텍스트 백업 (기존과 동일)
    msg = f"""🤖 퀀텀 인사이트 v10.0  |  {date_str}
━━━━━━━━━━━━━━━━━━
{stage_change_alert}📌 시장 국면: {ai['market_phase']}{bullish_suffix}
🚦 권장 행동: {stage_action}

📈 주요 지표
S&P 500  : {format_index(*spy_raw)}
NASDAQ   : {format_index(*qqq_raw)}
KOSPI    : {format_index(*kospi_raw)}
VIX      : {vix:.2f}
USD/KRW  : {fx_data[0]:,.0f}원
공포탐욕 : {fg_score if fg_score is not None else '지연'}

💡 매크로 심층 분석
{ai['macro_correlation']}
🛠 시스템: {sys_status_msg}
"""

    # 🔥 모든 데이터를 담아서 '풀버전' 이미지 생성 요청!
    image_path = generate_card_image(
        total_score, stage_label, ai, weight, diff_str, date_str,
        spy_raw, qqq_raw, kospi_raw, spy_dd, rsi,
        fx_data, fg_score, fg_label, vix, dxy, dxy_mom, us10y, hy_eval, gold,
        sys_status_msg, bullish_suffix
    )

    # 3. 텔레그램으로 전송하기 (이미지만 깔끔하게!)
    if image_path: 
        with open(image_path, "rb") as f:
            for _ in range(3):
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{ENV['TELEGRAM_TOKEN']}/sendPhoto",
                        data={"chat_id": ENV["CHAT_ID"]}, # caption(텍스트) 뺐습니다! 사진만 갑니다.
                        files={"photo": f},
                        timeout=20,
                    ).raise_for_status()
                    # 🎯 사진 전송에 성공하면 여기서 끝! 텍스트는 보내지 않습니다.
                    break 
                except Exception as e:
                    log(f"⚠️ 이미지 전송 실패: {e}")
                    time.sleep(2)
    else:
        # 혹시라도 이미지 생성에 실패했다면? 그때만 텍스트 리포트 전송
        log("⚠️ 이미지 생성 실패, 텍스트 리포트 백업을 전송합니다.")
        def split_message(text, max_len=3900):
            return [text[i:i+max_len] for i in range(0, len(text), max_len)]
        for chunk in split_message(msg):
            requests.post(
                f"https://api.telegram.org/bot{ENV['TELEGRAM_TOKEN']}/sendMessage",
                data={"chat_id": ENV["CHAT_ID"], "text": chunk},
                timeout=15,
            )

    save_state(total_score, stage_label, fg_score)
    log(f"✅ 완료 | 점수={total_score:.1f} | 국면={stage_label}")
    # ----- [여기까지 끝] -----

if __name__ == "__main__":
    main()
