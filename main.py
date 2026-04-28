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
WEIGHT_PER_PCT   = 6.5
HY_SPREAD_WARN   = 4.5
HY_SPREAD_DANGER = 6.5
FG_EXTREME_FEAR  = 10
DXY_MOM_WARN     = 3.0
DRAWDOWN_WARN    = -10.0   # 고점 대비 낙폭 경고
DRAWDOWN_DANGER  = -20.0   # 고점 대비 낙폭 위험
AI_WEIGHT        = 0.5     # AI 분석 비중 강화 (0.35 -> 0.7 -> 0.5 중간값)

STATE_FILE  = "bot_state.json"
RETRY_COUNT = 4
RETRY_DELAY = 45

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

# ==========================================
# 🔑 환경변수 검증
# ==========================================
def validate_env():
    required = {
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
        "FRED_API_KEY":   os.getenv("FRED_API_KEY"),
        "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN"),
        "CHAT_ID":        os.getenv("CHAT_ID"),
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
                log(f"⚡ [{label}] {e.response.status_code} → 재시도 불필요, 즉시 종료")
                break
        except Exception as e:
            log(f"⚠️ [{label}] {i+1}차 실패: {type(e).__name__}: {e}")
        if i < retry - 1:
            time.sleep(delay)
    log(f"❌ [{label}] 최종 실패")
    return None

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log(f"상태 로드 오류: {e}")
    return {}

def save_state(score, stage):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"score": score, "stage": stage, "updated": datetime.now().isoformat()},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"상태 저장 오류: {e}")

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
# 📈 Yahoo Finance 데이터
# ==========================================
def get_yahoo_closes(ticker, range_="2y"):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={range_}"
    res = requests.get(url, headers=YAHOO_HEADERS, timeout=12)
    res.raise_for_status()
    closes = [v for v in res.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if v]
    if len(closes) < 50:
        raise ValueError(f"{ticker}: Yahoo 데이터 부족 ({len(closes)}개)")
    return closes

def get_yahoo_stats(ticker, range_="2y"):
    closes = get_yahoo_closes(ticker, range_)
    count  = min(len(closes), 200)
    sma200 = sum(closes[-count:]) / count
    high52 = max(closes[-min(len(closes), 252):])
    return closes[-1], closes[-2], sma200, high52

# ==========================================
# 💱 환율
# ==========================================
def get_fx_data():
    fx_c, source = None, "UNKNOWN"
    try:
        res = requests.get("https://finance.naver.com/marketindex/exchangeList.naver", headers=YAHOO_HEADERS, timeout=10)
        res.raise_for_status()
        row = re.search(r"<td class=\"tit\">.*?USD.*?</tr>", res.text, re.DOTALL)
        if row:
            m = re.search(r"<td class=\"sale\">([\d,]+\.?\d*)</td>", row.group())
            if m:
                fx_c   = float(m.group(1).replace(",", ""))
                source = "NAVER"
    except Exception as e:
        log(f"환율 네이버 실패: {e}")

    if fx_c is None:
        try:
            res = requests.get("https://query2.finance.yahoo.com/v8/finance/chart/KRW=X?interval=1d&range=5d", headers=YAHOO_HEADERS, timeout=10)
            res.raise_for_status()
            closes = [v for v in res.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if v]
            fx_c, source = closes[-1], "YAHOO"
        except Exception as e:
            log(f"환율 Yahoo 실패: {e}")

    history = None
    try:
        history = get_fred_series("DEXKOUS")
        if fx_c is None:
            fx_c, source = history[-1], "FRED"
    except Exception as e:
        log(f"환율 FRED 이력 실패: {e}")

    if fx_c is None: fx_c, source = 1400.0, "DEFAULT"

    if history and len(history) >= 504:
        fx_p  = history[-2]
        fx_1y = sum(history[-252:]) / 252
        fx_2y = sum(history[-504:]) / 504
    else:
        fx_p = fx_1y = fx_2y = fx_c

    return fx_c, fx_p, fx_1y, fx_2y, source

# ==========================================
# 🥇 금 데이터
# ==========================================
def get_gold_data():
    closes = get_yahoo_closes("GC=F", "1y")
    return closes[-1], closes[-2], closes[-20], sum(closes[-min(len(closes), 252):]) / min(len(closes), 252)

# ==========================================
# 📈 RSI
# ==========================================
def calc_rsi_wilder(values, period=14):
    if len(values) < period * 2: return None
    deltas   = [values[i] - values[i-1] for i in range(1, len(values))]
    gains    = [max(d, 0) for d in deltas]
    losses   = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)

def get_rsi_label(rsi):
    if rsi is None: return "산출 불가"
    if rsi >= 75:   return f"{rsi}  🔴 과매수"
    if rsi >= 60:   return f"{rsi}  🟠 상단"
    if rsi <= 25:   return f"{rsi}  🟢 과매도"
    if rsi <= 40:   return f"{rsi}  🔵 하단"
    return          f"{rsi}  ➖ 중립"

# ==========================================
# 📉 드로우다운
# ==========================================
def calc_drawdown(current, high52):
    if not current or not high52 or high52 == 0: return None
    return (current - high52) / high52 * 100

def get_drawdown_label(dd):
    if dd is None:        return "산출 불가"
    if dd <= DRAWDOWN_DANGER: return f"{dd:.1f}%  💀 대형 조정"
    if dd <= DRAWDOWN_WARN:   return f"{dd:.1f}%  🔴 조정 구간"
    if dd <= -5:              return f"{dd:.1f}%  🟠 소폭 하락"
    return                    f"{dd:.1f}%  🟢 고점 근접"

# ==========================================
# 😨 공포탐욕지수
# ==========================================
CNN_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://edition.cnn.com/markets/fear-and-greed",
    "Origin":          "https://edition.cnn.com",
}

def _fg_label(score):
    if score <= 10:  return "극단적 공포 😱🚨"
    if score <= 25:  return "극단적 공포 😱"
    if score <= 45:  return "공포 😨"
    if score <= 55:  return "중립 😐"
    if score <= 75:  return "탐욕 😏"
    return           "극단적 탐욕 🤑"

def get_fear_greed():
    try:
        res = requests.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata", headers=CNN_HEADERS, timeout=10)
        res.raise_for_status()
        score = round(float(res.json()["fear_and_greed"]["score"]))
        return score, _fg_label(score)
    except Exception as e:
        log(f"F&G CNN 실패: {e} → alternative.me 시도")

    try:
        res = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        res.raise_for_status()
        score = round(float(res.json()["data"][0]["value"]))
        return score, _fg_label(score) + " (alt)"
    except Exception as e:
        log(f"F&G alternative.me 실패: {e}")
        return None, None

# ==========================================
# 🏦 기타 매크로
# ==========================================
def get_us10y():
    v = get_fred_series("DGS10", days=60, min_count=5)
    return v[-1], v[-2]

def get_hy_spread():
    v = get_fred_series("BAMLH0A0HYM2", days=60, min_count=5)
    return v[-1], v[-2]

def get_dxy_momentum(dxy_series):
    if not dxy_series or len(dxy_series) < 21: return None
    return pct(dxy_series[-1], dxy_series[-21])

# ==========================================
# 📰 뉴스 키워드 추출 (HTML 태그 제거 추가)
# ==========================================
ECON_KEYWORDS = [
    "Fed", "rate", "inflation", "recession", "GDP", "jobs", "unemployment",
    "tariff", "trade", "bank", "earnings", "default", "yield", "debt",
    "cut", "hike", "pivot", "crash", "rally", "금리", "인플레", "관세", "실업"
]

def extract_news_keywords(entries, max_items=6):
    results = []
    for e in entries[:max_items]:
        title     = e.title.strip()
        summary   = getattr(e, "summary", "")
        summary   = re.sub(r'<[^>]+>', '', summary) # HTML 태그 제거
        sentences = re.split(r'[.!?]', summary)
        key_sents = [s.strip() for s in sentences
                     if any(kw.lower() in s.lower() for kw in ECON_KEYWORDS)]
        context   = " / ".join(key_sents[:2]) if key_sents else summary[:80]
        results.append(f"• {title}  [{context}]")
    return "\n".join(results)

# ==========================================
# 🧠 AI 분석
# ==========================================
def get_ai_analysis(news: str, market_summary: dict) -> dict:
    prompt = f"""
당신은 20년 경력의 월스트리트 매크로 전략가입니다.
시장 데이터와 뉴스를 분석해 투자 리스크를 진단하세요.

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
{{"score":<-2~2 정수>,"market_phase":"<국면 한 줄>","top_risks":["<리스크1>","<리스크2>"],"opportunity":"<기회 요인>","strategy":"<대응 전략 2~3문장>"}}
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
    return data

# ==========================================
# 🎯 종합 위험 점수 (코스피 점수 반영 제외)
# ==========================================
def calc_risk_score(spy, qqq, fx_data, vix, dxy_series, ai_score, us10y, fg_score, hy_spread, spy_dd):
    if spy[0] == 0: return 2.0
    s   = 0.0
    dxy = dxy_series[-1] if dxy_series else 118.0

    # 지수 신호
    c_spy, p_spy, sma_spy, _ = spy
    if c_spy > 0:
        if gap(c_spy, sma_spy) < -SPY_TREND_GAP: s += 2.0
        if pct(c_spy, p_spy)   < SPY_DAILY_DROP:  s += 2.5
    c_qqq, _, sma_qqq, _ = qqq
    if c_qqq > 0 and gap(c_qqq, sma_qqq) < -SPY_TREND_GAP:
        s += 1.5

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
    if dxy_mom is not None:
        if dxy_mom > DXY_MOM_WARN * 1.5: s += 2.0
        elif dxy_mom > DXY_MOM_WARN:      s += 1.0

    # 국채금리
    if us10y and us10y[0] and us10y[1]:
        rc = us10y[0] - us10y[1]
        if rc > 0.2:   s += 2.0
        elif rc > 0.1: s += 1.0

    # 하이일드 스프레드
    if hy_spread and hy_spread[0]:
        hys = hy_spread[0]
        if hys > HY_SPREAD_DANGER:   s += 3.0
        elif hys > HY_SPREAD_WARN:    s += 1.5
        if hy_spread[1] and (hys - hy_spread[1]) > 0.3:
            s += 1.0

    # 공포탐욕
    if fg_score is not None:
        if fg_score > 80:                  s += 1.0
        elif fg_score < FG_EXTREME_FEAR:   s -= 1.5

    # AI 보정 (0.5: 매크로 해석력 강화)
    s += ai_score * AI_WEIGHT

    return max(0.0, min(SCORE_MAX, s))

# ==========================================
# 📋 보조 분석 로직
# ==========================================
def get_gold_signal(gold):
    if not gold: return "지연"
    c, _, p20, _ = gold
    st_gap = gap(c, p20)
    if st_gap > 5:    return "🚨 단기 과열"
    elif st_gap > 2:  return "🟠 상승 추세"
    elif st_gap < -3: return "🟢 저점 근접"
    return "➖ 중립"

def get_macro_comment(gold, dxy, spy_daily, us10y, hy_spread, dxy_mom):
    comments = []
    if gold:
        g = pct(gold[0], gold[2])
        if g > 2 and dxy > DXY["warn"]:   comments.append("금·달러 동반↑ → 안전자산 쏠림 🚨")
        elif g > 2 and spy_daily > 0:      comments.append("금·주식 동반↑ → 유동성 장세 💸")
        elif g < -2 and spy_daily > 0:     comments.append("금↓·주식↑ → Risk-On 🟢")
        elif g < -2 and spy_daily < -2:    comments.append("전방위 패닉셀 💀")
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
    return f"{c:,.0f}  {arrow(pct(c,p))}{abs(pct(c,p)):.1f}%  │  200일선 {gap(c,sma):+.1f}%"

# ==========================================
# 🚀 메인
# ==========================================
def main():
    log(f"📊 퀀텀 v4 가동 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")

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
    vix     = vix_series[-1] if vix_series else 22.0
    dxy     = dxy_series[-1] if dxy_series else 118.0
    dxy_mom = get_dxy_momentum(dxy_series)

    spy_closes = safe(lambda: get_yahoo_closes("^GSPC", "2y"), "RSI소스")
    rsi        = calc_rsi_wilder(spy_closes) if spy_closes else None
    spy_dd     = calc_drawdown(spy_raw[0], spy_raw[3]) if spy_raw[0] else None

    try:
        feed = feedparser.parse("https://finance.yahoo.com/news/rssindex")
        news_context = extract_news_keywords(feed.entries)
    except: news_context = "뉴스 수집 실패"

    market_summary = {
        "SP500":     {"현재": spy_raw[0], "전일대비%": round(pct(spy_raw[0], spy_raw[1]), 2), "200일선대비%": round(gap(spy_raw[0], spy_raw[2]), 2), "고점대비%": round(spy_dd, 1) if spy_dd else None},
        "NASDAQ":    {"현재": qqq_raw[0], "전일대비%": round(pct(qqq_raw[0], qqq_raw[1]), 2)},
        "VIX":       vix,
        "DXY":       {"현재": dxy, "20일모멘텀%": round(dxy_mom, 2) if dxy_mom else None},
        "USD_KRW":   fx_data[0],
        "US10Y금리": us10y[0],
        "HY스프레드": hy_spread[0] if hy_spread else None,
        "공포탐욕":   fg_score,
        "금현재가":   gold[0] if gold else None,
        "RSI_SP500": rsi,
    }
    try: ai = get_ai_analysis(news_context, market_summary)
    except: ai = {"score": 0, "market_phase": "AI 일시 지연", "top_risks": ["분석 불가"], "opportunity": "-", "strategy": "관망"}

    total_score = calc_risk_score(spy_raw, qqq_raw, fx_data, vix, dxy_series, ai["score"], us10y, fg_score, hy_spread, spy_dd)
    weight = round(max(0, min(100, 100 - total_score * WEIGHT_PER_PCT)))

    is_panic = vix > VIX["danger"] or (spy_raw[0] > 0 and pct(spy_raw[0], spy_raw[1]) < -4)
    is_extreme_fear = fg_score is not None and fg_score < FG_EXTREME_FEAR

    stages = [("🟢 공격적 매수", "주식 80% 이상"), ("🔵 적극적 유지", "주식 60~80%"), ("🟡 중립 관망", "주식 40~60%"), ("🟠 방어적 축소", "주식 20~40%"), ("🔴 위험 회피", "주식 20% 이하")]
    if is_panic: stage_label, stage_action = "💀 패닉 구간", "현금 확보 + 분할매수 대기"
    else: stage_label, stage_action = stages[min(4, int(total_score // 3))]

    prev = load_state()
    prev_stage = prev.get("stage", stage_label)
    score_diff = total_score - prev.get("score", total_score)
    diff_str = f"{score_diff:+.1f} {arrow(score_diff)}"

    # 알림 섹션 강화
    alerts = []
    if prev_stage != stage_label: alerts.append(f"📢 국면 변화: {prev_stage} → {stage_label}")
    if is_extreme_fear: alerts.append(f"😱 극단적 공포 감지 (F&G={fg_score}) → 분할매수 검토")
    alert_msg = "\n" + "\n".join(alerts) + "\n" if alerts else ""

    fx_c, _, fx_1y, fx_2y, fx_src = fx_data
    fx_2y_gap = gap(fx_c, fx_2y)
    fx_status = "⚠️ 역사적 고점" if fx_2y_gap > FX_GAP["danger"] else "🟠 주의" if fx_2y_gap > FX_GAP["caution"] else "✅ 정상"
    dxy_status = "✅" if dxy < DXY["warn"] else "⚠️" if dxy < DXY["danger"] else "🚨"

    if hy_spread and hy_spread[0]:
        hy_str = f"{hy_spread[0]:.2f}% {'🔴' if hy_spread[0] > HY_SPREAD_DANGER else '🟠' if hy_spread[0] > HY_SPREAD_WARN else '🟢'}"
    else: hy_str = "지연"

    msg = f"""🤖 퀀텀 인사이트 v4 | {datetime.now().strftime("%Y.%m.%d %H:%M")}
━━━━━━━━━━━━━━━━━━━━━━━━━
📌 시장 국면: {ai['market_phase']} {alert_msg}
⚠️ 리스크: {', '.join(ai['top_risks'])}
💡 기회: {ai['opportunity']}
🧭 전략: {ai['strategy']}

📊 위험 점수: {total_score:.1f} / 15 ({diff_str})
🎯 주식 권장: {weight}% | 국면: {stage_label}
━━━━━━━━━━━━━━━━━━━━━━━━━
📈 주요 지표
S&P 500  : {format_index(*spy_raw)}
└ 고점대비: {get_drawdown_label(spy_dd)}
NASDAQ   : {format_index(*qqq_raw)}
KOSPI    : {format_index(*kospi_raw)}
RSI(S&P) : {get_rsi_label(rsi)}

💵 환율 (USD/KRW) [{fx_src}]
{fx_c:,.0f}원 {fx_status} (2년평균 대비 {fx_2y_gap:+.1f}%)

😨 공포탐욕: {f"{fg_score} {fg_label}" if fg_score else "지연"}
📊 VIX     : {vix:.2f} {'🚨' if vix > VIX['danger'] else '✅'}
💲 달러인덱스: {dxy:.1f} {dxy_status}
🏦 미 10Y금리: {f"{us10y[0]:.2f}%" if us10y[0] else "지연"}
📉 HY스프레드: {hy_str}
🥇 금       : {f"{gold[0]:,.0f} {get_gold_signal(gold)}" if gold else "지연"}

💡 {get_macro_comment(gold, dxy, pct(spy_raw[0], spy_raw[1]), us10y, hy_spread, dxy_mom)}
"""
    try:
        requests.post(f"https://api.telegram.org/bot{ENV['TELEGRAM_TOKEN']}/sendMessage", data={"chat_id": ENV["CHAT_ID"], "text": msg}, timeout=15)
        log("✅ 전송 완료")
    except Exception as e: log(f"❌ 전송 실패: {e}")

    save_state(total_score, stage_label)

if __name__ == "__main__":
    main()
