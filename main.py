import requests, os, json, feedparser, re, time, logging, sys, html
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

STATE_FILE  = "bot_state.json"
RETRY_COUNT = 4
RETRY_DELAY = 45

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

CNN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
    "Origin": "https://edition.cnn.com",
}

ECON_KEYWORDS = [
    "Fed", "rate", "inflation", "recession", "GDP", "jobs", "unemployment",
    "tariff", "trade", "bank", "earnings", "default", "yield", "debt",
    "cut", "hike", "pivot", "crash", "rally", "금리", "인플레", "관세", "실업"
]

MACRO_CRITICAL = ["fed", "fomc", "powell", "cpi", "pce", "rate cut", "rate hike", "연준", "파월", "금리", "인플레이션", "물가"]

# ==========================================
# 🔑 환경변수 검증
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

# ==========================================
# 🛠 유틸리티
# ==========================================
def pct(c, p):  return (c - p) / p * 100 if p and abs(p) > 1e-9 else 0
def gap(c, s):  return (c - s) / s * 100 if s and abs(s) > 1e-9 else 0
def arrow(v):   return "▲" if v > 0 else "▼" if v < 0 else "➖"

def safe(func, label="", retry=RETRY_COUNT, delay=RETRY_DELAY):
    for i in range(retry):
        try:
            res = func()
            if res is not None:
                return res
        except requests.exceptions.HTTPError as e:
            log(f"⚠️ [{label}] {i+1}차 실패: {type(e).__name__}: {e}")
            if e.response is not None and e.response.status_code in (401, 403, 418):
                log(f"⚡ [{label}] {e.response.status_code} → 즉시 종료")
                break
        except Exception as e:
            log(f"⚠️ [{label}] {i+1}차 실패: {type(e).__name__}: {e}")
        if i < retry - 1:
            time.sleep(delay)
    log(f"❌ [{label}] 최종 실패")
    return None

def load_state():
    try:
        url = f"https://api.github.com/gists/{ENV['GIST_ID']}"
        headers = {"Authorization": f"token {ENV['GITHUB_TOKEN']}"}
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        content = res.json()['files']['bot_state.json']['content']
        return json.loads(content)
    except Exception as e:
        log(f"상태 로드 실패: {e}")
    return {}

def save_state(score, stage):
    try:
        url = f"https://api.github.com/gists/{ENV['GIST_ID']}"
        headers = {
            "Authorization": f"token {ENV['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github.v3+json"
        }
        data = {
            "files": {
                "bot_state.json": {
                    "content": json.dumps({
                        "score": score, 
                        "stage": stage, 
                        "updated": datetime.now().isoformat()
                    }, ensure_ascii=False)
                }
            }
        }
        res = requests.patch(url, headers=headers, json=data, timeout=10)
        res.raise_for_status()
        log("✅ 점수 상태를 Gist에 안전하게 저장 완료")
    except Exception as e:
        log(f"상태 저장 실패: {e}")

# ==========================================
# 📊 FRED 데이터
# ==========================================
def get_fred_series(series_id, days=1000, min_count=50):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key":   ENV["FRED_API_KEY"],
        "file_type": "json",
        "observation_start": (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d'),
    }
    res = requests.get(url, params=params, timeout=15)
    res.raise_for_status()
    values = [float(o["value"]) for o in res.json().get("observations", []) if o["value"] != "."]
    if len(values) < min_count:
        raise ValueError(f"{series_id}: 데이터 부족 ({len(values)}개 < {min_count})")
    return values

# ==========================================
# 📈 Yahoo Finance 지수 데이터
# ==========================================
def get_yahoo_closes(ticker, range_="2y"):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={range_}"
    res = requests.get(url, headers=YAHOO_HEADERS, timeout=12)
    res.raise_for_status()
    closes = [v for v in res.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if v]
    if len(closes) < 50:
        raise ValueError(f"{ticker}: 데이터 부족 ({len(closes)}개)")
    return closes

def get_yahoo_stats(ticker, range_="2y"):
    closes = get_yahoo_closes(ticker, range_)
    count  = min(len(closes), 200)
    sma200 = sum(closes[-count:]) / count
    high52 = max(closes[-min(len(closes), 252):])
    return closes[-1], closes[-2], sma200, high52

# ==========================================
# 💱 환율 — 야후 단일 소스화
# ==========================================
def get_fx_data():
    fx_errors = []
    yh_c = yh_p = yh_1y = yh_2y = None
    
    try:
        closes = get_yahoo_closes("KRW=X", "2y")
        if closes and len(closes) > 0:
            yh_c = closes[-1]
            yh_p = closes[-2] if len(closes) > 1 else yh_c
            yh_1y = sum(closes[-min(len(closes), 252):]) / min(len(closes), 252)
            yh_2y = sum(closes[-min(len(closes), 504):]) / min(len(closes), 504)
    except Exception as e:
        log(f"환율 야후 실패: {e}")
        fx_errors.append("환율(야후)")

    if yh_c is None:
        yh_c = yh_p = yh_1y = yh_2y = 1400.0

    err_str = "/".join(fx_errors) if fx_errors else None
    return yh_c, yh_p, yh_1y, yh_2y, err_str

# ==========================================
# 🥇 금 & RSI & 드로우다운
# ==========================================
def get_gold_data():
    closes = get_yahoo_closes("GC=F", "1y")
    return closes[-1], closes[-2], closes[-20], sum(closes[-min(len(closes), 252):]) / min(len(closes), 252)

def calc_rsi_wilder(values, period=14):
    if len(values) < period * 2:
        return None
    deltas   = [values[i] - values[i-1] for i in range(1, len(values))]
    gains    = [max(d, 0) for d in deltas]
    losses   = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)

def get_rsi_label(rsi):
    if rsi is None: return "산출 불가"
    if rsi >= 75:   return f"{rsi}  🔴 과매수"
    if rsi >= 60:   return f"{rsi}  🟠 상단"
    if rsi <= 25:   return f"{rsi}  🟢 과매도"
    if rsi <= 40:   return f"{rsi}  🔵 하단"
    return          f"{rsi}  ➖ 중립"

def calc_drawdown(current, high52):
    if not current or not high52 or high52 == 0:
        return None
    return (current - high52) / high52 * 100

def get_drawdown_label(dd):
    if dd is None:             return "산출 불가"
    if dd <= DRAWDOWN_DANGER:  return f"{dd:.1f}%  💀 대형 조정"
    if dd <= DRAWDOWN_WARN:    return f"{dd:.1f}%  🔴 조정 구간"
    if dd <= -5:               return f"{dd:.1f}%  🟠 소폭 하락"
    return                     f"{dd:.1f}%  🟢 고점 근접"

def get_gold_signal(gold):
    if not gold: return "지연"
    c, _, _, sma = gold    
    st_gap = gap(c, sma)   
    if st_gap > 10:   return "🚨 장기 과열"
    elif st_gap > 3:  return "🟠 상승 추세"
    elif st_gap < -5: return "🟢 저점 근접" 
    return "➖ 중립"

# ==========================================
# 😨 공포탐욕지수 & 매크로 지표
# ==========================================
def _fg_label(score):
    if score <= 10:  return "극단적 공포 😱🚨"
    if score <= 25:  return "극단적 공포 😱"
    if score <= 45:  return "공포 😨"
    if score <= 55:  return "중립 😐"
    if score <= 75:  return "탐욕 😏"
    return           "극단적 탐욕 🤑"

def get_fear_greed():
    try:
        res = requests.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                           headers=CNN_HEADERS, timeout=10)
        res.raise_for_status()
        score = round(float(res.json()["fear_and_greed"]["score"]))
        return score, _fg_label(score)
    except Exception as e:
        log(f"F&G CNN 실패: {e} → alternative.me 시도")

    res = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
    res.raise_for_status()
    score = round(float(res.json()["data"][0]["value"]))
    return score, _fg_label(score) + " (alt)"

def get_us10y():
    v = get_fred_series("DGS10", days=60, min_count=5)
    return v[-1], v[-2]

def get_hy_spread():
    v = get_fred_series("BAMLH0A0HYM2", days=60, min_count=5)
    return v[-1], v[-2]

def get_dxy_momentum(dxy_series):
    if not dxy_series or len(dxy_series) < 21:
        return None
    return pct(dxy_series[-1], dxy_series[-21])

# ==========================================
# 📰 뉴스 키워드 추출
# ==========================================
def extract_news_keywords(entries, max_items=8):
    critical_news = []
    normal_news = []
    
    for e in entries:
        title   = e.title.strip()
        summary = getattr(e, "summary", "")
        summary = html.unescape(summary)
        summary = re.sub(r'<[^>]+>', ' ', summary)
        summary = re.sub(r'\s+', ' ', summary).strip()
        
        sentences = re.split(r'[.!?]', summary)
        key_sents = [s.strip() for s in sentences
                     if any(kw.lower() in s.lower() for kw in ECON_KEYWORDS)]
        context = " / ".join(key_sents[:2]) if key_sents else summary[:80]
        
        is_critical = any(kw in title.lower() for kw in MACRO_CRITICAL)
        
        if is_critical:
            critical_news.append(f"🚨[핵심 매크로] {title}  [{context}]")
        else:
            normal_news.append(f"• {title}  [{context}]")

    return "\n".join((critical_news + normal_news)[:max_items])

# ==========================================
# 🧠 AI 분석 (궁극의 비서 버전)
# ==========================================
def get_ai_analysis(news: str, market_summary: dict) -> dict:
    prompt = f"""
당신은 월스트리트 최고 수준의 퀀트 매크로 전략가이며, 현재 '매일 기계적으로 지수(VOO, QQQ)를 모아가며, 포트폴리오의 일부를 미래 전략산업(QTUM, UFO, NASA, ARKQ 등)에 위성 투자(Satellite)하는 투자자'를 전담 보좌하는 수석 비서입니다.

[분석 원칙 - 매우 중요]
1. [매크로 최우선] 개별 기업 뉴스는 철저히 배제하세요. 제공된 뉴스 중 '🚨[핵심 매크로]' 태그가 붙은 연준(Fed), 금리, 물가 데이터에 집중하여 시장의 큰 자금 흐름(Risk-On/Off)을 진단하세요.
2. [팩트 기반 상관관계] 전달받은 [시장 데이터]의 수치와 상태값(안정/위험 등 꼬리표)을 절대적으로 신뢰하세요. 달러, 국채금리, VIX, 하이일드 스프레드, 금 가격의 조합이 현재 증시에 어떤 심리적/구조적 영향을 미치는지 'macro_correlation'에 2~3문장으로 통찰하세요.
3. [대응 전략 - 템포 조절] 이 봇은 위험도에 따라 현금을 20%→40%→70%→100%로 자동 가속 방어합니다. 당신이 직접 매도 비율을 지시할 필요가 없습니다. 대신 "추세가 굳건하니 맘 편히 일일 적립을 유지하십시오" 또는 "핵심 지지선 이탈 및 과열이 보이니 방어(현금화) 기제 발동에 대비해 관망할 때입니다"라고 투자자의 '마음가짐과 템포'에 집중하여 'strategy' 항목에 조언하세요.
4. [미래 전략산업 진단] 현재의 매크로 환경(국채금리, 유동성, 위험 선호도 등)이 초고위험 성장주인 미래 전략산업(우주항공, 로봇, 양자 등)에 우호적인 환경인지, 아니면 보수적으로 접근해야 할 때인지를 분석하여 그 결과를 'opportunity' 항목에 1~2문장으로 작성하세요.
5. [언어 - 필수] 모든 분석 결과는 반드시 자연스럽고 전문적인 **한국어(Korean)**로 출력하세요. (JSON key는 영문 유지)

[시장 데이터]
{json.dumps(market_summary, ensure_ascii=False)}

[주요 뉴스]
{news}

[출력: JSON만, 다른 텍스트 없음]
{{"score":<-2~2 정수>,"market_phase":"<국면 한 줄>","top_risks":["<리스크1>","<리스크2>","<리스크3>"],"opportunity":"<기회 요인>","strategy":"<매일 적립하는 투자자를 위한 템포 조절 및 멘탈 관리 조언>","macro_correlation":"<지표 간 상관관계 기반 시장 진단 2~3문장>"}}
"""
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.25,
        response_format={"type": "json_object"},
        timeout=30,
    )
    data = json.loads(res.choices[0].message.content.strip())
    data["score"] = max(-2, min(2, float(data.get("score", 0))))
    data.setdefault("market_phase", "분석 중")
    data.setdefault("top_risks",    ["-", "-", "-"])
    data.setdefault("opportunity",  "-")
    data.setdefault("strategy",     "관망")
    data.setdefault("macro_correlation", "상관관계 분석 지연")
    return data

# ==========================================
# 🎯 종합 위험 점수
# ==========================================
def calc_risk_score(spy, qqq, kospi, fx_data, vix, dxy_closes, dxy_mom, ai_score,
                    us10y, fg_score, hy_spread, spy_dd, gold):
    if spy[0] == 0:
        return 2.0
    s   = 0.0
    dxy = dxy_closes[-1] if dxy_closes else 118.0

    if spy[0] > 0:
        spy_gap = gap(spy[0], spy[2])
        if spy_gap < -SPY_TREND_GAP: s += 2.0
        elif spy_gap < 0:            s += 1.0
        
        if pct(spy[0], spy[1]) < SPY_DAILY_DROP:  s += 2.5

    if qqq[0] > 0:
        qqq_gap = gap(qqq[0], qqq[2])
        if qqq_gap < -SPY_TREND_GAP: s += 1.5
        elif qqq_gap < 0:            s += 0.5

    c_kos = kospi[0] if kospi else 0
    sma_kos = kospi[2] if kospi else 0
    if c_kos > 0 and sma_kos > 0:
        kos_gap = gap(c_kos, sma_kos)
        if kos_gap < -4.0:   s += 1.5
        elif kos_gap < -2.0: s += 1.0

    if spy_dd is not None:
        if spy_dd <= DRAWDOWN_DANGER: s += 2.5
        elif spy_dd <= DRAWDOWN_WARN: s += 1.0

    fx_c, fx_p, fx_1y, fx_2y, _ = fx_data
    fg_2y = gap(fx_c, fx_2y)
    if fg_2y > FX_GAP["danger"]:    s += 2.0
    elif fg_2y > FX_GAP["caution"]: s += 1.0
    if pct(fx_c, fx_p) > 1.5:       s += 1.5

    if vix > VIX["panic"]:    s += 4.0
    elif vix > VIX["danger"]: s += 2.0
    elif vix > VIX["warn"]:   s += 1.0

    if dxy > DXY["danger"]:    s += 1.5
    elif dxy > DXY["warn"]:    s += 0.5

    if dxy_mom and dxy_mom > DXY_MOM_WARN:
        s += 2.0 if dxy_mom > DXY_MOM_WARN * 1.5 else 1.0

    if us10y and us10y[0] and us10y[1]:
        rc = us10y[0] - us10y[1]
        if rc > 0.2:   s += 2.0
        elif rc > 0.1: s += 1.0

    if hy_spread and hy_spread[0]:
        hys = hy_spread[0]
        if hys > HY_SPREAD_DANGER:  s += 3.0
        elif hys > HY_SPREAD_WARN:  s += 1.5
        if hy_spread[1] and (hys - hy_spread[1]) > 0.3:
            s += 1.0

    if fg_score is not None:
        if fg_score > 80:                s += 1.0
        elif fg_score < FG_EXTREME_FEAR: s -= 1.5

    if gold and gold[3] > 0:
        gold_gap = gap(gold[0], gold[3])
        if gold_gap > 10:   s += 1.5 
        elif gold_gap > 5:  s += 0.5 

    s += ai_score * AI_WEIGHT
    return max(0.0, min(SCORE_MAX, s))

def format_index(c, p, sma, _=None):
    if c == 0: return "데이터 지연"
    return f"{c:,.0f}  {arrow(pct(c,p))}{abs(pct(c,p)):.1f}%\n └ 200일선 대비: {gap(c,sma):+.1f}%"

# ==========================================
# 🚀 메인
# ==========================================
def main():
    log(f"📊 퀀텀 v5.5 가동 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    api_errors = [] 

    spy_raw   = safe(lambda: get_yahoo_stats("^GSPC"), "SPY")
    if not spy_raw: spy_raw = (0, 0, 0, 0); api_errors.append("SPY")

    qqq_raw   = safe(lambda: get_yahoo_stats("^IXIC"), "QQQ")
    if not qqq_raw: qqq_raw = (0, 0, 0, 0); api_errors.append("QQQ")

    kospi_raw = safe(lambda: get_yahoo_stats("^KS11"), "KOSPI")
    if not kospi_raw: kospi_raw = (0, 0, 0, 0); api_errors.append("KOSPI")

    fx_data   = safe(lambda: get_fx_data(), "FX")
    if not fx_data: 
        fx_data = (1400, 1400, 1400, 1400, "환율전체")
    yh_c, yh_p, yh_1y, yh_2y, fx_err = fx_data
    if fx_err: api_errors.append(fx_err)

    gold      = safe(lambda: get_gold_data(), "GOLD")
    if not gold: api_errors.append("GOLD")

    us10y     = safe(lambda: get_us10y(), "10Y")
    if not us10y: us10y = (None, None); api_errors.append("10Y")

    hy_spread = safe(lambda: get_hy_spread(), "HY")
    if not hy_spread: hy_spread = (None, None); api_errors.append("HY스프레드")

    fg_score, fg_label = safe(lambda: get_fear_greed(), "F&G") or (None, None)
    if not fg_score: api_errors.append("공포탐욕")

    vix_closes = safe(lambda: get_yahoo_closes("^VIX", "3m"), "VIX")
    vix = vix_closes[-1] if vix_closes else 22.0
    if not vix_closes: api_errors.append("VIX")

    dxy_closes = safe(lambda: get_yahoo_closes("DX-Y.NYB", "3m"), "DXY")
    dxy = dxy_closes[-1] if dxy_closes else 118.0
    dxy_mom = get_dxy_momentum(dxy_closes)
    if not dxy_closes: api_errors.append("DXY")

    spy_closes = safe(lambda: get_yahoo_closes("^GSPC", "2y"), "RSI소스")
    rsi    = calc_rsi_wilder(spy_closes) if spy_closes else None
    spy_dd = calc_drawdown(spy_raw[0], spy_raw[3]) if spy_raw[0] else None

    try:
        feed         = feedparser.parse("https://finance.yahoo.com/news/rssindex")
        news_context = extract_news_keywords(feed.entries)
    except Exception as e:
        log(f"뉴스 수집 실패: {e}")
        news_context = "뉴스 수집 실패"
        api_errors.append("뉴스")

    hy_eval = "지연"
    if hy_spread and hy_spread[0]:
        hy_eval = "위험" if hy_spread[0] > HY_SPREAD_DANGER else ("주의" if hy_spread[0] > HY_SPREAD_WARN else "안정")
        hy_eval = f"{hy_spread[0]:.2f}% ({hy_eval})"

    vix_eval = "위험" if vix > VIX["danger"] else ("주의" if vix > VIX["warn"] else "안정")

    market_summary = {
        "SP500":      {"현재": spy_raw[0], "전일대비%": round(pct(spy_raw[0], spy_raw[1]), 2),
                       "200일선대비%": round(gap(spy_raw[0], spy_raw[2]), 2), "고점대비%": round(spy_dd, 1) if spy_dd else None},
        "NASDAQ":     {"현재": qqq_raw[0], "전일대비%": round(pct(qqq_raw[0], qqq_raw[1]), 2)},
        "KOSPI":      {"현재": kospi_raw[0], "200일선대비%": round(gap(kospi_raw[0], kospi_raw[2]), 2) if kospi_raw[0] else None},
        "VIX":        f"{vix:.2f} ({vix_eval})",
        "DXY":        {"현재": dxy, "20일모멘텀%": round(dxy_mom, 2) if dxy_mom else None},
        "USD_KRW":    yh_c,
        "US10Y금리":  us10y[0],
        "HY스프레드": hy_eval,
        "공포탐욕":   fg_score,
        "금현재가":   f"{gold[0]:.0f} ({get_gold_signal(gold)})" if gold else None,
        "RSI_SP500":  rsi,
    }

    # 💡 [추가 반영] 중첩 딕셔너리 내부의 None까지 완벽하게 제거하는 필터 함수
    def _clean(v):
        if isinstance(v, dict):
            return {k2: v2 for k2, v2 in v.items() if v2 is not None}
        return v

    market_summary = {k: _clean(v) for k, v in market_summary.items() if v is not None}
    
    try:
        ai = get_ai_analysis(news_context, market_summary)
    except Exception as e:
        log(f"AI 분석 실패: {e}")
        ai = {"score": 0, "market_phase": "AI 일시 지연", "top_risks": ["-", "-", "-"], "opportunity": "-", "strategy": "관망", "macro_correlation": "분석 지연"}
        api_errors.append("AI응답")

    total_score = calc_risk_score(
        spy_raw, qqq_raw, kospi_raw, fx_data, vix, dxy_closes, dxy_mom, ai["score"],
        us10y, fg_score, hy_spread, spy_dd, gold
    )

    if total_score < 3:
        stage_label, weight = "🟢 공격적 매수", 100
        stage_action = "주식 비중 100% 유지 및 수익 극대화"
    elif total_score < 6:
        stage_label, weight = "🔵 적극적 유지", 80
        stage_action = "1차 수익 실현 및 방어 (자산의 20% 현금화)"
    elif total_score < 9:
        stage_label, weight = "🟡 부분 방어", 60
        stage_action = "2차 추가 수익 실현 (자산의 40% 현금화 완료)"
    elif total_score < 12:
        stage_label, weight = "🟠 적극적 축소", 30
        stage_action = "보수적 운영 (자산의 70% 현금화 완료)"
    else:
        stage_label, weight = "🔴 위험 회피", 0
        stage_action = "대피 및 폭풍우 관망 (100% 현금화)"

    is_panic        = vix > VIX["danger"] or (spy_raw[0] > 0 and pct(spy_raw[0], spy_raw[1]) < -4)
    is_extreme_fear = fg_score is not None and fg_score < FG_EXTREME_FEAR

    if is_panic:
        stage_label  = "💀 패닉 구간"
        weight       = 0  
        stage_action = "기존 자산 100% 현금화 대피 + 신규 적립액 2배수 바닥 줍기"
        
    prev        = load_state()
    prev_score  = prev.get("score", total_score)
    prev_stage  = prev.get("stage", stage_label)
    score_diff  = total_score - prev_score
    diff_str    = f"{score_diff:+.1f} {arrow(score_diff)}"

    stage_change_alert = (
        f"📢📢 국면 변화 감지!\n   {prev_stage}  →  {stage_label}\n━━━━━━━━━━━━━━━━━━\n"
        if prev_stage != stage_label else ""
    )
    extreme_fear_alert = (
        f"\n🔔 극단적 공포 감지 (F&G={fg_score})\n   → 역발상 분할매수 검토 구간\n"
        if is_extreme_fear else ""
    )
    bullish_suffix = (
        "  🔥 강세장"
        if spy_raw[0] > 0 and gap(spy_raw[0], spy_raw[2]) > 3
        and qqq_raw[0] > 0 and gap(qqq_raw[0], qqq_raw[2]) > 3
        else ""
    )

    fx_2y_gap = gap(yh_c, yh_2y)
    fx_status  = ("⚠️ 역사적 고점권" if fx_2y_gap > FX_GAP["danger"]
                  else "🟠 주의 수준" if fx_2y_gap > FX_GAP["caution"]
                  else "✅ 정상 범위")
    dxy_status  = "✅" if dxy < DXY["warn"] else ("⚠️" if dxy < DXY["danger"] else "🚨")
    dxy_mom_str = (f"  20일 {dxy_mom:+.1f}% {'🚨' if dxy_mom > DXY_MOM_WARN else ''}"
                   if dxy_mom else "")

    if hy_spread and hy_spread[0]:
        hys     = hy_spread[0]
        hy_chg  = (hys - hy_spread[1]) if hy_spread[1] else 0
        hy_icon = "🔴" if hys > HY_SPREAD_DANGER else ("🟠" if hys > HY_SPREAD_WARN else "🟢")
        hy_str  = f"{hys:.2f}%  {hy_icon}  ({hy_chg:+.2f}%p)"
    else:
        hy_str = "지연"

    now_str = datetime.now().strftime("%Y.%m.%d %H:%M")

    sys_status_msg = "✅ 정상"
    if api_errors:
        sys_status_msg = f"⚠️ 데이터 지연 ({', '.join(api_errors)})"
    if is_panic:
        sys_status_msg = f"🚨 패닉 감지 | {sys_status_msg}"

    msg = f"""🤖 퀀텀 인사이트 v5.5  |  {now_str}
━━━━━━━━━━━━━━━━━━
{stage_change_alert}📌 시장 국면
{ai['market_phase']}{bullish_suffix}

⚠️ 핵심 리스크
① {ai['top_risks'][0] if len(ai['top_risks']) > 0 else '-'}
② {ai['top_risks'][1] if len(ai['top_risks']) > 1 else '-'}
③ {ai['top_risks'][2] if len(ai['top_risks']) > 2 else '-'}

💡 기회 요인 (미래 산업 진단)
{ai['opportunity']}

🧭 대응 전략
{ai['strategy']}
{extreme_fear_alert}
━━━━━━━━━━━━━━━━━━
📊 위험 점수: {total_score:.1f} / 15.0  ({diff_str} vs 전일)
🎯 주식 권장: {weight}%  |  현금: {100-weight}%
🚦 국면: {stage_label}
📋 행동: {stage_action}

━━━━━━━━━━━━━━━━━━
📈 주요 지표

S&P 500  : {format_index(*spy_raw)}
 └ 52주 고점 대비: {get_drawdown_label(spy_dd)}
NASDAQ   : {format_index(*qqq_raw)}
KOSPI    : {format_index(*kospi_raw)}
RSI(S&P) : {get_rsi_label(rsi)}

💵 환율 (USD/KRW)
{yh_c:,.0f}원  {fx_status}
 ├ 1년 평균: {yh_1y:,.0f}원  ({gap(yh_c, yh_1y):+.1f}%)
 └ 2년 평균: {yh_2y:,.0f}원  ({fx_2y_gap:+.1f}%)

😨 공포탐욕  : {f"{fg_score}  {fg_label}" if fg_score else "지연"}
📊 VIX      : {vix:.2f}  {"🚨" if vix > VIX["danger"] else ("⚠️" if vix > VIX["warn"] else "✅")}
💲 달러인덱스: {dxy:.1f}  {dxy_status}{dxy_mom_str}
🏦 미 10Y금리: {f"{us10y[0]:.2f}%" if us10y and us10y[0] else "지연"}  {f"({us10y[0]-us10y[1]:+.2f}%p)" if us10y and us10y[0] and us10y[1] else ""}
📉 HY스프레드: {hy_str}
🥇 금        : {f"{gold[0]:,.0f}  {get_gold_signal(gold)}" if gold else "지연"}

━━━━━━━━━━━━━━━━━━
💡 매크로 지표 심층 분석 (AI)
{ai['macro_correlation']}

🛠 시스템: {sys_status_msg}
"""

    msg_final = msg[:4000] + "\n...[메시지 길이 제한으로 절사됨]" if len(msg) > 4000 else msg

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{ENV['TELEGRAM_TOKEN']}/sendMessage",
            data={"chat_id": ENV["CHAT_ID"], "text": msg_final}, 
            timeout=15,
        )
        resp.raise_for_status()
        log("✅ 전송 완료")
    except Exception as e:
        log(f"❌ 전송 실패: {e}")

    save_state(total_score, stage_label)
    log(f"✅ 완료 | 점수={total_score:.1f} | 국면={stage_label}")

if __name__ == "__main__":
    main()
