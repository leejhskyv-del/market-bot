import requests, os, json, feedparser, re, time, logging, sys
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

# ✅ NameError 수정 1: CNN_HEADERS 최상단 정의
CNN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
    "Origin": "https://edition.cnn.com",
}

# ✅ NameError 수정: ECON_KEYWORDS 최상단 정의
ECON_KEYWORDS = [
    "Fed", "rate", "inflation", "recession", "GDP", "jobs", "unemployment",
    "tariff", "trade", "bank", "earnings", "default", "yield", "debt",
    "cut", "hike", "pivot", "crash", "rally", "금리", "인플레", "관세", "실업"
]

# ==========================================
# 🔑 환경변수 검증
# ==========================================
def validate_env():
    required = {
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
        "FRED_API_KEY":   os.getenv("FRED_API_KEY"),
        "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN"),
        "CHAT_ID":        os.getenv("CHAT_ID"),
        "GIST_ID":        os.getenv("GIST_ID"),       # 추가
        "GITHUB_TOKEN":   os.getenv("GITHUB_TOKEN"),  # 추가
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
def pct(c, p):  return (c - p) / p * 100 if p and p != 0 else 0
def gap(c, s):  return (c - s) / s * 100 if s and s != 0 else 0
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
    """GitHub Gist에서 이전 점수를 가져옵니다 (서버 리셋에도 안전)."""
    try:
        gist_id = ENV["GIST_ID"]
        token = ENV["GITHUB_TOKEN"]
        url = f"https://api.github.com/gists/{gist_id}"
        headers = {"Authorization": f"token {token}"}
        
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        
        content = res.json()['files']['bot_state.json']['content']
        return json.loads(content)
    except Exception as e:
        log(f"상태 로드 실패: {e}")
    return {}

def save_state(score, stage):
    """현재 점수를 GitHub Gist에 영구 저장합니다."""
    try:
        gist_id = ENV["GIST_ID"]
        token = ENV["GITHUB_TOKEN"]
        url = f"https://api.github.com/gists/{gist_id}"
        headers = {
            "Authorization": f"token {token}",
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
    """(현재, 전일, 200일SMA, 52주고점) 반환"""
    closes = get_yahoo_closes(ticker, range_)
    count  = min(len(closes), 200)
    sma200 = sum(closes[-count:]) / count
    high52 = max(closes[-min(len(closes), 252):])
    return closes[-1], closes[-2], sma200, high52

# ==========================================
# 💱 환율 — 네이버 우선 (토스 환전 기준)
# ==========================================
def get_fx_data():
    fx_c, source = None, "UNKNOWN"

    # 1순위: 네이버
    try:
        res = requests.get("https://finance.naver.com/marketindex/exchangeList.naver",
                           headers=YAHOO_HEADERS, timeout=10)
        res.raise_for_status()
        row = re.search(r"<td class=\"tit\">.*?USD.*?</tr>", res.text, re.DOTALL)
        if row:
            m = re.search(r"<td class=\"sale\">([\d,]+\.?\d*)</td>", row.group())
            if m:
                fx_c, source = float(m.group(1).replace(",", "")), "NAVER"
    except Exception as e:
        log(f"환율 네이버 실패: {e}")

    # 2순위: Yahoo
    if fx_c is None:
        try:
            res = requests.get("https://query2.finance.yahoo.com/v8/finance/chart/KRW=X?interval=1d&range=5d",
                               headers=YAHOO_HEADERS, timeout=10)
            res.raise_for_status()
            closes = [v for v in res.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if v]
            fx_c, source = closes[-1], "YAHOO"
        except Exception as e:
            log(f"환율 Yahoo 실패: {e}")

    # 이력: FRED DEXKOUS (1년/2년 평균용)
    history = None
    try:
        history = get_fred_series("DEXKOUS")
        if fx_c is None:
            fx_c, source = history[-1], "FRED"
    except Exception as e:
        log(f"환율 FRED 이력 실패: {e}")

    if fx_c is None:
        fx_c, source = 1400.0, "DEFAULT"

    if history and len(history) >= 504:
        fx_p  = history[-2]
        fx_1y = sum(history[-252:]) / 252
        fx_2y = sum(history[-504:]) / 504
    else:
        fx_p = fx_1y = fx_2y = fx_c

    return fx_c, fx_p, fx_1y, fx_2y, source

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

# ==========================================
# 😨 공포탐욕지수
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

# ==========================================
# 🏦 매크로 지표
# ==========================================
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
def extract_news_keywords(entries, max_items=6):
    results = []
    for e in entries[:max_items]:
        title   = e.title.strip()
        summary = getattr(e, "summary", "")
        # HTML 태그·엔티티 제거
        summary = re.sub(r'<[^>]+>', ' ', summary)
        summary = re.sub(r'&\w+;', ' ', summary)
        summary = re.sub(r'\s+', ' ', summary).strip()
        sentences = re.split(r'[.!?]', summary)
        key_sents = [s.strip() for s in sentences
                     if any(kw.lower() in s.lower() for kw in ECON_KEYWORDS)]
        context = " / ".join(key_sents[:2]) if key_sents else summary[:80]
        results.append(f"• {title}  [{context}]")
    return "\n".join(results)

# ==========================================
# 🧠 AI 분석
# ==========================================
def get_ai_analysis(news: str, market_summary: dict) -> dict:
    prompt = f"""
당신은 20년 경력의 월스트리트 매크로 전략가입니다.
지수 ETF(SPY, QQQ, SCHD, KOSPI) 투자자를 위해 시장 데이터와 뉴스를 분석하세요.
그리고 추가로 분야별 ETF(QTUM-양자컴퓨터, UFO-우주항공, ARKQ-로봇공학)에 대해서도 시장 데이터와 뉴스를 분석하세요.

[분석 원칙 - 매우 중요]
1. 단일 종목(예: 테슬라, 엔비디아 등)의 개별 소식은 지수 전체를 흔들 정도의 시스템 리스크가 아니면 완전히 배제하세요.
2. 매크로(Fed 금리, 물가 지표, 고용, 환율)와 지수 추세(200일선, 과매수/과매도)에만 집중하세요.
3. '기회 요인'에는 개별 종목 추천이 아닌, 지수 매수 적기나 자산 배분 전략을 제시하세요.

[시장 데이터]
{json.dumps(market_summary, ensure_ascii=False)}

[주요 뉴스]
{news}

[판단 기준]
- VIX>35 + 환율 급등 동시 발생 → 블랙스완
- HY스프레드>6.5% → 신용위기 선행
- DXY 20일 모멘텀+3% 이상 → 신흥국 유동성 경색
- 금+달러 동반 상승 → 디플레 리스크
- 공포탐욕 10 이하 → 역발상 분할매수 구간
- 지수가 200일선 아래 + 호재 뉴스 → 데드캣 의심

[출력: JSON만, 다른 텍스트 없음]
{{"score":<-2~2 정수>,"market_phase":"<국면 한 줄>","top_risks":["<리스크1>","<리스크2>","<리스크3>"],"opportunity":"<기회 요인>","strategy":"<대응 전략 2~3문장>"}}
"""
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.15,
        response_format={"type": "json_object"},
        timeout=30,
    )
    data = json.loads(res.choices[0].message.content.strip())
    data["score"] = max(-2, min(2, float(data.get("score", 0))))
    data.setdefault("market_phase", "분석 중")
    data.setdefault("top_risks",    ["-", "-", "-"])
    data.setdefault("opportunity",  "-")
    data.setdefault("strategy",     "관망")
    return data

# ==========================================
# 🎯 종합 위험 점수
# ==========================================
def calc_risk_score(spy, qqq, kospi, fx_data, vix, dxy_series, ai_score,
                    us10y, fg_score, hy_spread, spy_dd):
    if spy[0] == 0:
        return 2.0
    s   = 0.0
    dxy = dxy_series[-1] if dxy_series else 118.0

    # S&P500 신호
    if spy[0] > 0:
        if gap(spy[0], spy[2]) < -SPY_TREND_GAP: s += 2.0
        if pct(spy[0], spy[1]) < SPY_DAILY_DROP:  s += 2.5

    # NASDAQ 신호
    if qqq[0] > 0 and gap(qqq[0], qqq[2]) < -SPY_TREND_GAP:
        s += 1.5

    # ✅ KOSPI 신호
    c_kos = kospi[0] if kospi else 0
    sma_kos = kospi[2] if kospi else 0
    if c_kos > 0 and sma_kos > 0:
        kos_gap = gap(c_kos, sma_kos)
        if kos_gap < -4.0:   s += 1.5
        elif kos_gap < -2.0: s += 1.0

    # 드로우다운
    if spy_dd is not None:
        if spy_dd <= DRAWDOWN_DANGER: s += 2.5
        elif spy_dd <= DRAWDOWN_WARN: s += 1.0

    # 환율
    fx_c, fx_p, fx_1y, fx_2y, _ = fx_data
    fg_2y = gap(fx_c, fx_2y)
    if fg_2y > FX_GAP["danger"]:    s += 2.0
    elif fg_2y > FX_GAP["caution"]: s += 1.0
    if pct(fx_c, fx_p) > 1.5:       s += 1.5

    # VIX
    if vix > VIX["panic"]:    s += 4.0
    elif vix > VIX["danger"]: s += 2.0
    elif vix > VIX["warn"]:   s += 1.0

    # DXY 절대값 + 속도
    if dxy > DXY["danger"]:    s += 1.5
    elif dxy > DXY["warn"]:    s += 0.5
    dxy_mom = get_dxy_momentum(dxy_series)
    if dxy_mom and dxy_mom > DXY_MOM_WARN:
        s += 2.0 if dxy_mom > DXY_MOM_WARN * 1.5 else 1.0

    # 국채금리
    if us10y and us10y[0] and us10y[1]:
        rc = us10y[0] - us10y[1]
        if rc > 0.2:   s += 2.0
        elif rc > 0.1: s += 1.0

    # 하이일드 스프레드
    if hy_spread and hy_spread[0]:
        hys = hy_spread[0]
        if hys > HY_SPREAD_DANGER:  s += 3.0
        elif hys > HY_SPREAD_WARN:  s += 1.5
        if hy_spread[1] and (hys - hy_spread[1]) > 0.3:
            s += 1.0

    # 공포탐욕
    if fg_score is not None:
        if fg_score > 80:               s += 1.0
        elif fg_score < FG_EXTREME_FEAR: s -= 1.5

    # AI 보정
    s += ai_score * AI_WEIGHT

    return max(0.0, min(SCORE_MAX, s))

# ==========================================
# 📋 보조 분석
# ==========================================
def get_gold_signal(gold):
    if not gold: return "지연"
    
    # c: 현재가, sma: 1년(252일) 장기 평균선
    c, _, _, sma = gold    
    
    # 현재가가 장기 평균선 대비 얼마나 떨어져 있는지(%) 계산
    st_gap = gap(c, sma)   
    
    # 기준점은 장기 추세에 맞게 조금 더 넓게 잡아줍니다
    if st_gap > 10:   return "🚨 장기 과열"
    elif st_gap > 3:  return "🟠 상승 추세"
    elif st_gap < -5: return "🟢 저점 근접" # 평균선보다 5% 이상 하락했을 때만 알림
    return "➖ 중립"

def get_macro_comment(gold, dxy, spy_daily, us10y, hy_spread, dxy_mom):
    comments = []
    if gold:
        g = pct(gold[0], gold[2])
        if g > 2 and dxy > DXY["warn"]:  comments.append("금·달러 동반↑ → 안전자산 쏠림 🚨")
        elif g > 2 and spy_daily > 0:    comments.append("금·주식 동반↑ → 유동성 장세 💸")
        elif g < -2 and spy_daily > 0:   comments.append("금↓·주식↑ → Risk-On 🟢")
        elif g < -2 and spy_daily < -2:  comments.append("전방위 패닉셀 💀")
    if us10y and us10y[0]:
        if us10y[0] > 4.8:   comments.append(f"10Y {us10y[0]:.2f}% → 고금리 부담 ⚠️")
        elif us10y[0] < 3.8: comments.append(f"10Y {us10y[0]:.2f}% → 금리 완화 기대 🟢")
    if hy_spread and hy_spread[0] > HY_SPREAD_WARN:
        comments.append(f"HY스프레드 {hy_spread[0]:.1f}% → 신용위험 선행 🔴")
    if dxy_mom and dxy_mom > DXY_MOM_WARN:
        comments.append(f"DXY 20일 +{dxy_mom:.1f}% → 신흥국 유동성 경색 ⚠️")
    return " | ".join(comments) if comments else "뚜렷한 매크로 시그널 없음 ➖"

def format_index(c, p, sma, _=None):
    if c == 0: return "데이터 지연"
    return f"{c:,.0f}  {arrow(pct(c,p))}{abs(pct(c,p)):.1f}%\n └ 200일선 대비: {gap(c,sma):+.1f}%"

# ==========================================
# 🚀 메인
# ==========================================
def main():
    log(f"📊 퀀텀 v5.2 가동 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")

    # 데이터 수집
    spy_raw   = safe(lambda: get_yahoo_stats("^GSPC"), "SPY")   or (0, 0, 0, 0)
    qqq_raw   = safe(lambda: get_yahoo_stats("^IXIC"), "QQQ")   or (0, 0, 0, 0)
    kospi_raw = safe(lambda: get_yahoo_stats("^KS11"), "KOSPI") or (0, 0, 0, 0)
    fx_data   = safe(lambda: get_fx_data(),            "FX")    or (1400, 1400, 1400, 1400, "DEFAULT")
    gold      = safe(lambda: get_gold_data(),          "GOLD")
    us10y     = safe(lambda: get_us10y(),              "10Y")   or (None, None)
    hy_spread = safe(lambda: get_hy_spread(),          "HY")
    fg_score, fg_label = safe(lambda: get_fear_greed(), "F&G") or (None, None)

    vix_series = safe(lambda: get_fred_series("VIXCLS"),   "VIX")
    dxy_series = safe(lambda: get_fred_series("DTWEXBGS"), "DXY")
    vix      = vix_series[-1] if vix_series else 22.0
    dxy      = dxy_series[-1] if dxy_series else 118.0
    dxy_mom = get_dxy_momentum(dxy_series)

    spy_closes = safe(lambda: get_yahoo_closes("^GSPC", "2y"), "RSI소스")
    rsi    = calc_rsi_wilder(spy_closes) if spy_closes else None
    spy_dd = calc_drawdown(spy_raw[0], spy_raw[3]) if spy_raw[0] else None

    # 뉴스
    try:
        feed         = feedparser.parse("https://finance.yahoo.com/news/rssindex")
        news_context = extract_news_keywords(feed.entries)
    except Exception as e:
        log(f"뉴스 수집 실패: {e}")
        news_context = "뉴스 수집 실패"

    # AI 분석
    market_summary = {
        "SP500":      {"현재": spy_raw[0], "전일대비%": round(pct(spy_raw[0], spy_raw[1]), 2),
                       "200일선대비%": round(gap(spy_raw[0], spy_raw[2]), 2), "고점대비%": round(spy_dd, 1) if spy_dd else None},
        "NASDAQ":     {"현재": qqq_raw[0], "전일대비%": round(pct(qqq_raw[0], qqq_raw[1]), 2)},
        "KOSPI":      {"현재": kospi_raw[0], "200일선대비%": round(gap(kospi_raw[0], kospi_raw[2]), 2) if kospi_raw[0] else None},
        "VIX":        vix,
        "DXY":        {"현재": dxy, "20일모멘텀%": round(dxy_mom, 2) if dxy_mom else None},
        "USD_KRW":    fx_data[0],
        "US10Y금리":  us10y[0],
        "HY스프레드": hy_spread[0] if hy_spread else None,
        "공포탐욕":   fg_score,
        "금현재가":   gold[0] if gold else None,
        "RSI_SP500":  rsi,
    }
    try:
        ai = get_ai_analysis(news_context, market_summary)
    except Exception as e:
        log(f"AI 분석 실패: {e}")
        ai = {"score": 0, "market_phase": "AI 일시 지연", "top_risks": ["-", "-", "-"], "opportunity": "-", "strategy": "관망"}

    # 점수 & 국면
    total_score = calc_risk_score(
        spy_raw, qqq_raw, kospi_raw, fx_data, vix, dxy_series,
        ai["score"], us10y, fg_score, hy_spread, spy_dd
    )

    if total_score < 3:
        stage_label, weight = "🟢 공격적 매수", 100
        stage_action = "주식 비중 최대 유지 및 수익 극대화"
    elif total_score < 6:
        stage_label, weight = "🔵 적극적 유지", 70
        stage_action = "1차 수익 실현 (자산의 30% 현금화)"
    elif total_score < 9:
        stage_label, weight = "🟡 중립 관망", 40
        stage_action = "보수적 운영 (자산의 60% 현금화 완료)"
    elif total_score < 12:
        stage_label, weight = "🟠 방어적 축소", 10
        stage_action = "최소 비중 유지 (자산의 90% 현금화 완료)"
    else:
        stage_label, weight = "🔴 위험 회피", 0
        stage_action = "전량 대피 및 폭풍우 관망"

    # ✅ NameError 수정 2: is_extreme_fear 정의 위치 수정
    is_panic        = vix > VIX["danger"] or (spy_raw[0] > 0 and pct(spy_raw[0], spy_raw[1]) < -4)
    is_extreme_fear = fg_score is not None and fg_score < FG_EXTREME_FEAR

    if is_panic:
        stage_label  = "💀 패닉 구간"
        stage_action = "현금 투입 시작 (평소 분할매수액의 2배 증액)"

    # 전일 비교
    prev        = load_state()
    prev_score  = prev.get("score", total_score)
    prev_stage  = prev.get("stage", stage_label)
    score_diff  = total_score - prev_score
    diff_str    = f"{score_diff:+.1f} {arrow(score_diff)}"

    # ✅ 국면 변화 알림 — 메시지 최상단 배치
    stage_change_alert = (
        f"📢📢 국면 변화 감지!\n   {prev_stage}  →  {stage_label}\n━━━━━━━━━━━━━━━━━━\n"
        if prev_stage != stage_label else ""
    )
    extreme_fear_alert = (
        f"\n🔔 극단적 공포 감지 (F&G={fg_score})\n   → 분할매수 검토 구간\n"
        if is_extreme_fear else ""
    )
    bullish_suffix = (
        "  🔥 강세장"
        if spy_raw[0] > 0 and gap(spy_raw[0], spy_raw[2]) > 3
        and qqq_raw[0] > 0 and gap(qqq_raw[0], qqq_raw[2]) > 3
        else ""
    )

    # 표시용 가공
    fx_c, fx_p, fx_1y, fx_2y, fx_src = fx_data
    fx_2y_gap = gap(fx_c, fx_2y)
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

    # 메시지 — 국면 변화 알림이 맨 위
    msg = f"""🤖 퀀텀 인사이트 v5.2  |  {now_str}
━━━━━━━━━━━━━━━━━━
{stage_change_alert}📌 시장 국면
{ai['market_phase']}{bullish_suffix}

⚠️ 핵심 리스크
① {ai['top_risks'][0] if len(ai['top_risks']) > 0 else '-'}
② {ai['top_risks'][1] if len(ai['top_risks']) > 1 else '-'}
③ {ai['top_risks'][2] if len(ai['top_risks']) > 2 else '-'}

💡 기회 요인
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

💵 환율 (USD/KRW)  [{fx_src}]
{fx_c:,.0f}원  {fx_status}
 ├ 1년 평균: {fx_1y:,.0f}원  ({gap(fx_c, fx_1y):+.1f}%)
 └ 2년 평균: {fx_2y:,.0f}원  ({fx_2y_gap:+.1f}%)

😨 공포탐욕  : {f"{fg_score}  {fg_label}" if fg_score else "지연"}
📊 VIX      : {vix:.2f}  {"🚨" if vix > VIX["danger"] else ("⚠️" if vix > VIX["warn"] else "✅")}
💲 달러인덱스: {dxy:.1f}  {dxy_status}{dxy_mom_str}
🏦 미 10Y금리: {f"{us10y[0]:.2f}%" if us10y and us10y[0] else "지연"}  {f"({us10y[0]-us10y[1]:+.2f}%p)" if us10y and us10y[0] and us10y[1] else ""}
📉 HY스프레드: {hy_str}
🥇 금        : {f"{gold[0]:,.0f}  {get_gold_signal(gold)}" if gold else "지연"}

━━━━━━━━━━━━━━━━━━
💡 {get_macro_comment(gold, dxy, pct(spy_raw[0], spy_raw[1]) if spy_raw[0] else 0, us10y, hy_spread, dxy_mom)}

🛠 시스템: {"🚨 패닉 감지" if is_panic else "✅ 정상"}
"""

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{ENV['TELEGRAM_TOKEN']}/sendMessage",
            data={"chat_id": ENV["CHAT_ID"], "text": msg},
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
