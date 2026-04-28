import requests, os, json, feedparser, re, time, logging, sys
from datetime import datetime, timedelta
from openai import OpenAI

# ==========================================
# ⚙️ 설정 & 상수
# ==========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
def log(msg): logging.info(msg)

VIX   = {"warn": 25, "danger": 35, "panic": 45}
FX_GAP = {"caution": 4, "danger": 8}
DXY   = {"warn": 122, "danger": 126}
SPY_DAILY_DROP  = -2.5
SPY_TREND_GAP   = 3.0
SCORE_MAX       = 15.0
WEIGHT_PER_PCT  = 6.5
HY_SPREAD_WARN  = 4.5   # 하이일드 스프레드 경고 기준 (%)
HY_SPREAD_DANGER = 6.5  # 위험 기준
FG_EXTREME_FEAR = 10    # 극단적 공포 → 분할매수 시그널
DXY_MOMENTUM_WARN = 3.0 # 20일 대비 상승률 경고 기준

STATE_FILE   = "bot_state.json"
RETRY_COUNT  = 4
RETRY_DELAY  = 45

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
            json.dump({"score": score, "stage": stage, "updated": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"상태 저장 오류: {e}")

# ==========================================
# 📊 FRED 데이터
# ==========================================
def get_fred_series(series_id, days=1000):
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
    if len(values) < 50:
        raise ValueError(f"{series_id}: 데이터 부족 ({len(values)}개)")
    return values

def get_index_stats(series_id):
    v = get_fred_series(series_id)
    count = min(len(v), 200)
    return v[-1], v[-2], sum(v[-count:]) / count

# ==========================================
# 💱 환율 — 네이버 우선 (토스 환전 기준)
# ==========================================
def get_fx_data():
    fx_c, source = None, "UNKNOWN"

    # 1순위: 네이버 (토스 환전 기준)
    try:
        res = requests.get(
            "https://finance.naver.com/marketindex/exchangeList.naver",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        res.raise_for_status()
        # USD/KRW 행 추출
        row = re.search(r"<td class=\"tit\">.*?USD.*?</tr>", res.text, re.DOTALL)
        if row:
            price_match = re.search(r"<td class=\"sale\">([\d,]+\.?\d*)</td>", row.group())
            if price_match:
                fx_c  = float(price_match.group(1).replace(",", ""))
                source = "NAVER"
    except Exception as e:
        log(f"환율 네이버 실패: {e}")

    # 2순위: Yahoo Finance
    if fx_c is None:
        try:
            res = requests.get(
                "https://query2.finance.yahoo.com/v8/finance/chart/KRW=X?interval=1d&range=5d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10
            )
            res.raise_for_status()
            closes = [v for v in res.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if v]
            fx_c   = closes[-1]
            source = "YAHOO"
        except Exception as e:
            log(f"환율 Yahoo 실패: {e}")

    # 이력 데이터: FRED DEXKOUS (1년/2년 평균 계산용)
    history = None
    try:
        history = get_fred_series("DEXKOUS")
        if fx_c is None:
            fx_c   = history[-1]
            source = "FRED"
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
# 🥇 금 데이터
# ==========================================
def get_gold_data():
    res = requests.get(
        "https://query2.finance.yahoo.com/v8/finance/chart/GC=F?interval=1d&range=1y",
        headers={"User-Agent": "Mozilla/5.0"}, timeout=10
    )
    res.raise_for_status()
    closes = [v for v in res.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if v]
    if len(closes) < 50:
        raise ValueError("금 데이터 부족")
    return closes[-1], closes[-2], closes[-20], sum(closes[-min(len(closes), 252):]) / min(len(closes), 252)

# ==========================================
# 📈 RSI — 와일더(Wells Wilder) EMA 방식
# ==========================================
def calc_rsi_wilder(values, period=14):
    """HTS/MTS 표준인 와일더 지수이동평균 RSI."""
    if len(values) < period * 2:
        return None
    deltas = [values[i] - values[i-1] for i in range(1, len(values))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    # 초기 단순평균
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # 와일더 스무딩 (이후 구간)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)

def get_rsi_label(rsi):
    if rsi is None:  return "산출 불가"
    if rsi >= 75:    return f"{rsi}  🔴 과매수"
    if rsi >= 60:    return f"{rsi}  🟠 상단"
    if rsi <= 25:    return f"{rsi}  🟢 과매도"
    if rsi <= 40:    return f"{rsi}  🔵 하단"
    return           f"{rsi}  ➖ 중립"

# ==========================================
# 😨 공포탐욕지수 (CNN)
# ==========================================
def get_fear_greed():
    res = requests.get(
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
        headers={"User-Agent": "Mozilla/5.0"}, timeout=10
    )
    res.raise_for_status()
    score = round(float(res.json()["fear_and_greed"]["score"]))
    if score <= 10:    label = "극단적 공포 😱🚨"
    elif score <= 25:  label = "극단적 공포 😱"
    elif score <= 45:  label = "공포 😨"
    elif score <= 55:  label = "중립 😐"
    elif score <= 75:  label = "탐욕 😏"
    else:              label = "극단적 탐욕 🤑"
    return score, label

# ==========================================
# 🏦 미 10년물 금리
# ==========================================
def get_us10y():
    v = get_fred_series("DGS10", days=30)
    return v[-1], v[-2]

# ==========================================
# 📉 하이일드 채권 스프레드 (선행 위기 지표)
# ==========================================
def get_hy_spread():
    """
    BAMLH0A0HYM2: ICE BofA US High Yield Option-Adjusted Spread
    주식보다 먼저 위기 신호 포착.
    """
    v = get_fred_series("BAMLH0A0HYM2", days=60)
    return v[-1], v[-2]   # (현재, 전일)

# ==========================================
# 💵 DXY 모멘텀 (20일 상승 속도)
# ==========================================
def get_dxy_momentum(dxy_series):
    """DXY 20일 대비 변화율 — 절대값보다 속도가 위기를 먼저 알린다."""
    if not dxy_series or len(dxy_series) < 21:
        return None
    return pct(dxy_series[-1], dxy_series[-21])

# ==========================================
# 📰 뉴스 키워드 추출 (AI 프롬프트 최적화)
# ==========================================
def extract_news_keywords(entries, max_items=6):
    """
    헤드라인 전체 + 핵심 경제 키워드를 뽑아 AI에 전달.
    요약문 120자 잘라쓰기보다 키워드 밀도가 높아 AI 판단이 정확해진다.
    """
    ECON_KEYWORDS = [
        "Fed", "rate", "inflation", "recession", "GDP", "jobs", "unemployment",
        "tariff", "trade", "bank", "earnings", "default", "yield", "debt",
        "cut", "hike", "pivot", "crash", "rally", "금리", "인플레", "관세"
    ]
    results = []
    for e in entries[:max_items]:
        title   = e.title.strip()
        summary = getattr(e, "summary", "")
        # 요약에서 키워드 포함 문장만 추출
        sentences = re.split(r'[.!?]', summary)
        key_sents = [s.strip() for s in sentences if any(kw.lower() in s.lower() for kw in ECON_KEYWORDS)]
        context = " / ".join(key_sents[:2]) if key_sents else summary[:80]
        results.append(f"• {title}  [{context}]")
    return "\n".join(results)

# ==========================================
# 🧠 AI 분석 (고도화 프롬프트)
# ==========================================
def get_ai_analysis(news: str, market_summary: dict) -> dict:
    summary_str = json.dumps(market_summary, ensure_ascii=False)
    prompt = f"""
당신은 20년 경력의 월스트리트 매크로 전략가입니다.
아래 시장 데이터와 뉴스를 바탕으로 투자 리스크를 분석하세요.

[시장 데이터]
{summary_str}

[주요 뉴스 & 핵심 키워드]
{news}

[판단 기준]
- 지수가 200일선 아래일 때 호재는 데드캣 바운스로 의심
- VIX > 35 + 환율 급등 동시 발생 시 블랙스완 시나리오
- 하이일드 스프레드 > 6.5%는 신용위기 선행 신호
- DXY 20일 급등(+3% 이상)은 신흥국 유동성 경색 신호
- 금·달러 동반 상승은 디플레이션 리스크
- 공포탐욕지수 10 이하는 역발상 분할매수 검토 구간
- RSI 과매도(≤25) + 200일선 근접은 기술적 반등 가능성

[출력 형식 - JSON만, 다른 텍스트 없음]
{{
  "score": <-2~2 정수, 양수=위험 증가, 음수=위험 감소>,
  "market_phase": "<현재 시장 국면 한 줄>",
  "top_risks": ["<리스크1>", "<리스크2>"],
  "opportunity": "<반등 또는 기회 요인>",
  "strategy": "<이 국면에서의 구체적 대응 전략 2~3문장>"
}}
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.15,
        response_format={"type": "json_object"},
        timeout=30,
    )
    data = json.loads(response.choices[0].message.content.strip())
    data["score"] = max(-2, min(2, float(data.get("score", 0))))
    data.setdefault("market_phase", "분석 중")
    data.setdefault("top_risks",    ["데이터 부족", "재시도 필요"])
    data.setdefault("opportunity",  "확인 중")
    data.setdefault("strategy",     "관망")
    return data

# ==========================================
# 🎯 종합 위험 점수
# ==========================================
def calc_risk_score(spy, qqq, fx_data, vix, dxy_series, ai_score, us10y, fg_score, hy_spread):
    if spy[0] == 0:
        return 2.0
    s = 0.0
    dxy = dxy_series[-1] if dxy_series else 118.0

    # ── 지수 신호 ──────────────────────
    c_spy, p_spy, sma_spy = spy
    if c_spy > 0:
        if gap(c_spy, sma_spy) < -SPY_TREND_GAP: s += 2.0
        if pct(c_spy, p_spy)   < SPY_DAILY_DROP:  s += 2.5
    c_qqq, _, sma_qqq = qqq
    if c_qqq > 0 and gap(c_qqq, sma_qqq) < -SPY_TREND_GAP:
        s += 1.5

    # ── 환율 신호 ──────────────────────
    fx_c, fx_p, fx_1y, fx_2y, _ = fx_data
    fg_2y = gap(fx_c, fx_2y)
    if fg_2y > FX_GAP["danger"]:   s += 2.0
    elif fg_2y > FX_GAP["caution"]: s += 1.0
    if pct(fx_c, fx_p) > 1.5:      s += 1.5

    # ── VIX 신호 ───────────────────────
    if vix > VIX["panic"]:   s += 4.0
    elif vix > VIX["danger"]: s += 2.0
    elif vix > VIX["warn"]:   s += 1.0

    # ── DXY 절대값 + 속도 ──────────────
    if dxy > DXY["danger"]:   s += 1.5
    elif dxy > DXY["warn"]:   s += 0.5
    dxy_mom = get_dxy_momentum(dxy_series)
    if dxy_mom is not None:
        if dxy_mom > DXY_MOMENTUM_WARN * 1.5: s += 2.0   # 급등
        elif dxy_mom > DXY_MOMENTUM_WARN:      s += 1.0   # 경계

    # ── 국채금리 ───────────────────────
    if us10y and us10y[0] and us10y[1]:
        rate_chg = us10y[0] - us10y[1]
        if rate_chg > 0.2:   s += 2.0
        elif rate_chg > 0.1: s += 1.0

    # ── 하이일드 스프레드 (선행지표) ───
    if hy_spread and hy_spread[0]:
        hys = hy_spread[0]
        if hys > HY_SPREAD_DANGER:  s += 3.0
        elif hys > HY_SPREAD_WARN:   s += 1.5
        # 스프레드 1일 급등도 반영
        if hy_spread[1] and (hys - hy_spread[1]) > 0.3:
            s += 1.0

    # ── 공포탐욕 ───────────────────────
    if fg_score is not None:
        if fg_score > 80:                s += 1.0   # 극단적 탐욕 = 고점 경고
        elif fg_score < FG_EXTREME_FEAR: s -= 1.5   # 극단적 공포 = 역발상 기회

    # ── AI 보정 ────────────────────────
    s += ai_score * 0.6

    return max(0.0, min(SCORE_MAX, s))

# ==========================================
# 📋 보조 분석
# ==========================================
def get_gold_signal(gold):
    if not gold: return "지연"
    c, p1, p20, avg = gold
    st_gap = gap(c, p20)
    if st_gap > 5:    return "🚨 단기 과열"
    elif st_gap > 2:  return "🟠 상승 추세"
    elif st_gap < -3: return "🟢 저점 근접"
    return "➖ 중립"

def get_macro_comment(gold, dxy, spy_daily, us10y, hy_spread, dxy_mom):
    comments = []
    if gold:
        g_pct = pct(gold[0], gold[2])
        if g_pct > 2 and dxy > DXY["warn"]:
            comments.append("금·달러 동반↑ → 안전자산 쏠림 🚨")
        elif g_pct > 2 and spy_daily > 0:
            comments.append("금·주식 동반↑ → 유동성 장세 💸")
        elif g_pct < -2 and spy_daily > 0:
            comments.append("금↓·주식↑ → Risk-On 🟢")
        elif g_pct < -2 and spy_daily < -2:
            comments.append("전방위 패닉셀 💀")
    if us10y and us10y[0]:
        if us10y[0] > 4.8:   comments.append(f"10Y {us10y[0]:.2f}% → 고금리 부담 ⚠️")
        elif us10y[0] < 3.8: comments.append(f"10Y {us10y[0]:.2f}% → 금리 완화 기대 🟢")
    if hy_spread and hy_spread[0] > HY_SPREAD_WARN:
        comments.append(f"HY스프레드 {hy_spread[0]:.1f}% → 신용 위험 선행 🔴")
    if dxy_mom is not None and dxy_mom > DXY_MOMENTUM_WARN:
        comments.append(f"DXY 20일 +{dxy_mom:.1f}% 급등 → 신흥국 유동성 경색 위험 ⚠️")
    return " | ".join(comments) if comments else "뚜렷한 매크로 시그널 없음 ➖"

def format_index(c, p, sma):
    if c == 0: return "데이터 지연"
    g = gap(c, sma)
    d = pct(c, p)
    return f"{c:,.0f}  {arrow(d)}{abs(d):.1f}%  │  200일선 {g:+.1f}%"

# ==========================================
# 🚀 메인
# ==========================================
def main():
    log(f"📊 퀀텀 v3 가동 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")

    # ── 데이터 수집 ──────────────────────────────
    spy_raw   = safe(lambda: get_index_stats("SP500"),     "SPY")   or (0, 0, 0)
    qqq_raw   = safe(lambda: get_index_stats("NASDAQCOM"), "QQQ")   or (0, 0, 0)
    kospi_raw = safe(lambda: get_index_stats("KOSPI"),     "KOSPI") or (0, 0, 0)
    fx_data   = safe(lambda: get_fx_data(),                "FX")    or (1400, 1400, 1400, 1400, "DEFAULT")
    gold      = safe(lambda: get_gold_data(),              "GOLD")
    us10y     = safe(lambda: get_us10y(),                  "10Y")   or (None, None)
    hy_spread = safe(lambda: get_hy_spread(),              "HY스프레드")
    fg_score, fg_label = safe(lambda: get_fear_greed(),    "F&G")   or (None, None)

    vix_series = safe(lambda: get_fred_series("VIXCLS"),   "VIX")
    dxy_series = safe(lambda: get_fred_series("DTWEXBGS"), "DXY")
    sp_series  = safe(lambda: get_fred_series("SP500"),    "RSI")

    vix     = vix_series[-1] if vix_series else 22.0
    dxy     = dxy_series[-1] if dxy_series else 118.0
    rsi     = calc_rsi_wilder(sp_series) if sp_series else None
    dxy_mom = get_dxy_momentum(dxy_series)

    # ── 뉴스 수집 & 키워드 추출 ─────────────────
    try:
        feed         = feedparser.parse("https://finance.yahoo.com/news/rssindex")
        news_context = extract_news_keywords(feed.entries)
    except Exception as e:
        log(f"뉴스 수집 실패: {e}")
        news_context = "뉴스 수집 실패"

    # ── AI 분석 ─────────────────────────────────
    market_summary = {
        "SP500":      {"현재": spy_raw[0], "전일대비%": round(pct(spy_raw[0], spy_raw[1]), 2), "200일선대비%": round(gap(spy_raw[0], spy_raw[2]), 2)},
        "NASDAQ":     {"현재": qqq_raw[0], "전일대비%": round(pct(qqq_raw[0], qqq_raw[1]), 2)},
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
        ai = {"score": 0, "market_phase": "AI 일시 지연", "top_risks": ["분석 불가"], "opportunity": "-", "strategy": "관망 유지"}

    # ── 점수 & 국면 ──────────────────────────────
    total_score = calc_risk_score(spy_raw, qqq_raw, fx_data, vix, dxy_series, ai["score"], us10y, fg_score, hy_spread)
    weight      = round(max(0, min(100, 100 - total_score * WEIGHT_PER_PCT)))

    is_panic          = (vix > VIX["danger"] or (spy_raw[0] > 0 and pct(spy_raw[0], spy_raw[1]) < -4))
    is_extreme_fear   = (fg_score is not None and fg_score < FG_EXTREME_FEAR)

    stages = [
        ("🟢 공격적 매수", "주식 80% 이상"),
        ("🔵 적극적 유지", "주식 60~80%"),
        ("🟡 중립 관망",   "주식 40~60%"),
        ("🟠 방어적 축소", "주식 20~40%"),
        ("🔴 위험 회피",   "주식 20% 이하"),
    ]
    if is_panic:
        stage_label, stage_action = "💀 패닉 구간", "현금 확보 + 분할매수 대기"
    else:
        idx = min(4, int(total_score // 3))
        stage_label, stage_action = stages[idx]

    # ── 전일 비교 ────────────────────────────────
    prev       = load_state()
    score_diff = total_score - prev.get("score", total_score)
    diff_str   = f"{score_diff:+.1f} {arrow(score_diff)}"

    # ── 강세장 여부 ──────────────────────────────
    bullish_suffix = ""
    if spy_raw[0] > 0 and qqq_raw[0] > 0:
        if gap(spy_raw[0], spy_raw[2]) > 3 and gap(qqq_raw[0], qqq_raw[2]) > 3:
            bullish_suffix = "  🔥 강세장"

    # ── 환율 상태 ────────────────────────────────
    fx_c, fx_p, fx_1y, fx_2y, fx_src = fx_data
    fx_2y_gap = gap(fx_c, fx_2y)
    if fx_2y_gap > FX_GAP["danger"]:    fx_status = "⚠️ 역사적 고점권"
    elif fx_2y_gap > FX_GAP["caution"]: fx_status = "🟠 주의 수준"
    else:                                 fx_status = "✅ 정상 범위"

    dxy_status   = "✅" if dxy < DXY["warn"] else ("⚠️" if dxy < DXY["danger"] else "🚨")
    dxy_mom_str  = f"  20일 모멘텀 {dxy_mom:+.1f}% {'🚨' if dxy_mom and dxy_mom > DXY_MOMENTUM_WARN else ''}" if dxy_mom else ""

    # ── 하이일드 스프레드 표시 ───────────────────
    if hy_spread and hy_spread[0]:
        hys     = hy_spread[0]
        hy_chg  = (hys - hy_spread[1]) if hy_spread[1] else 0
        hy_icon = "🔴" if hys > HY_SPREAD_DANGER else ("🟠" if hys > HY_SPREAD_WARN else "🟢")
        hy_str  = f"{hys:.2f}%  {hy_icon}  (전일대비 {hy_chg:+.2f}%p)"
    else:
        hy_str = "지연"

    # ── 극단적 공포 특별 알림 ────────────────────
    extreme_fear_alert = ""
    if is_extreme_fear:
        extreme_fear_alert = f"\n🔔 극단적 공포 감지 (F&G={fg_score}) → 분할매수 검토 구간\n"

    # ── 메시지 ───────────────────────────────────
    now_str = datetime.now().strftime("%Y.%m.%d %H:%M")
    msg = f"""🤖 퀀텀 인사이트 v3  |  {now_str}
━━━━━━━━━━━━━━━━━━━━━━━━━
📌 시장 국면
{ai['market_phase']}{bullish_suffix}

⚠️ 핵심 리스크
① {ai['top_risks'][0] if len(ai['top_risks']) > 0 else '-'}
② {ai['top_risks'][1] if len(ai['top_risks']) > 1 else '-'}

💡 기회 요인
{ai['opportunity']}

🧭 대응 전략
{ai['strategy']}
{extreme_fear_alert}
━━━━━━━━━━━━━━━━━━━━━━━━━
📊 위험 점수: {total_score:.1f} / {SCORE_MAX:.0f}  ({diff_str} vs 전일)
🎯 주식 권장: {weight}%  |  현금: {100-weight}%
🚦 국면: {stage_label}
📋 행동: {stage_action}

━━━━━━━━━━━━━━━━━━━━━━━━━
📈 주요 지표

S&P 500  : {format_index(*spy_raw)}
NASDAQ   : {format_index(*qqq_raw)}
KOSPI    : {format_index(*kospi_raw)}
RSI(S&P) : {get_rsi_label(rsi)}  ← 와일더 EMA 방식

💵 환율 (USD/KRW)  [{fx_src}]
{fx_c:,.0f}원  {fx_status}
 ├ 1년 평균: {fx_1y:,.0f}원  ({gap(fx_c, fx_1y):+.1f}%)
 └ 2년 평균: {fx_2y:,.0f}원  ({fx_2y_gap:+.1f}%)

😨 공포탐욕  : {f"{fg_score}  {fg_label}" if fg_score else "지연"}
📊 VIX      : {vix:.2f}  {"🚨" if vix > VIX["danger"] else ("⚠️" if vix > VIX["warn"] else "✅")}
💲 달러인덱스: {dxy:.1f}  {dxy_status}{dxy_mom_str}
🏦 미 10Y금리: {f"{us10y[0]:.2f}%" if us10y and us10y[0] else "지연"}  {f"({us10y[0]-us10y[1]:+.2f}%p)" if us10y and us10y[0] and us10y[1] else ""}
📉 HY스프레드: {hy_str}
🥇 금       : {f"{gold[0]:,.0f}  {get_gold_signal(gold)}" if gold else "지연"}

━━━━━━━━━━━━━━━━━━━━━━━━━
💡 {get_macro_comment(gold, dxy, pct(spy_raw[0], spy_raw[1]) if spy_raw[0] else 0, us10y, hy_spread, dxy_mom)}

🛠 시스템: {"🚨 패닉 감지" if is_panic else "✅ 정상"}
"""

    # ── 텔레그램 전송 ────────────────────────────
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{ENV['TELEGRAM_TOKEN']}/sendMessage",
            data={"chat_id": ENV["CHAT_ID"], "text": msg},
            timeout=15,
        )
        resp.raise_for_status()
        log("✅ 텔레그램 전송 완료")
    except Exception as e:
        log(f"❌ 텔레그램 전송 실패: {e}")

    save_state(total_score, stage_label)
    log(f"✅ 완료 | 점수={total_score:.1f} | 국면={stage_label}")


if __name__ == "__main__":
    main()
