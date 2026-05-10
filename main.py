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

UNRATE_THRESHOLD = 4.2          # 실업률 위험 기준선
VIX              = {"warn": 25, "danger": 35, "panic": 45}
FX_GAP           = {"caution": 4, "danger": 8}
DXY              = {"warn": 122, "danger": 126}
SPY_DAILY_DROP   = -2.5
SPY_TREND_GAP    = 3.0
SCORE_MAX        = 15.0
HY_SPREAD_WARN   = 4.5
HY_SPREAD_DANGER = 6.5
DRAWDOWN_WARN    = -10.0
DRAWDOWN_DANGER  = -20.0
AI_WEIGHT        = 0.5

RETRY_COUNT = 4
RETRY_DELAY = 45

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
MACRO_CRITICAL = ["fed", "fomc", "powell", "cpi", "pce", "rate cut", "rate hike", "연준", "파월", "금리", "인플레이션", "물가"]
NEWS_FEEDS = [
    ("Yahoo Finance",  "https://finance.yahoo.com/news/rssindex"),
    ("CNBC 경제",      "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("MarketWatch",    "https://feeds.marketwatch.com/marketwatch/topstories/"),
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
    except Exception as e: 
        log(f"상태 로드 실패 (초기값 반환): {e}")
        return {"score": 0.0, "ism_pmi": 50.0, "ism_date": "2024-01-01", "last_update_id": 0}

def save_state(state_data):
    try:
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

# ✅ 버그 수정: min_count 파라미터를 추가하여 1달(1mo) 검색 시 50개 미만 에러가 나지 않도록 수정
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
    return closes[-1], closes[-2], sum(closes[-count:]) / count, max(closes[-min(len(closes), 253):]) if len(closes) > 1 else closes[0]

def get_fx_data():
    closes = get_yahoo_closes("KRW=X", "2y")
    return closes[-1], closes[-2], sum(closes[-min(len(closes), 252):]) / min(len(closes), 252), sum(closes[-min(len(closes), 504):]) / min(len(closes), 504), None

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

def format_index(c, p, sma, _=None):
    if c == 0: return "데이터 지연"
    return f"{c:,.0f}  {arrow(pct(c,p))}{abs(pct(c,p)):.1f}%\n └ 200일선 대비: {gap(c,sma):+.1f}%"

# ==========================================
# 🧠 AI 분석
# ==========================================
def extract_news_keywords(entries, max_items=8):
    critical, normal = [], []
    for e in entries:
        title = getattr(e, "title", "").strip()
        if not title: continue
        summary = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html.unescape(getattr(e, "summary", "")))).strip()
        context = summary[:80]
        if any(kw in title.lower() for kw in MACRO_CRITICAL): critical.append(f"🚨[매크로] {title} [{context}]")
        else: normal.append(f"• {title}")
    return "\n".join((critical + normal)[:max_items])

def get_ai_analysis(news: str, market_summary: dict) -> dict:
    prompt = f"""당신은 월스트리트 퀀트 매크로 전략가입니다.
시장 데이터와 뉴스를 분석하여 리스크 점수와 전략을 반환하세요.
데이터: {json.dumps(market_summary, ensure_ascii=False)}
뉴스: {news}
출력(JSON): {{"macro_score": <실수(-1.5~1.5)>, "market_phase": "<국면>", "top_risks": ["<1>","<2>","<3>"], "strategy": "<조언>", "macro_correlation": "<진단>"}}"""
    res = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.25, response_format={"type": "json_object"}, timeout=30)
    data = json.loads(res.choices[0].message.content.strip())
    data["score"] = max(-1.5, min(1.5, safe_float(data.get("macro_score", 0.0))))
    risks = data.get("top_risks", [])
    data["top_risks"] = ((risks if isinstance(risks, list) else [str(risks)]) + ["-", "-", "-"])[:3]
    return data

# ==========================================
# 🎯 위험 점수 산출
# ==========================================
def calc_risk_score(spy, qqq, fx_data, vix, dxy, ai_score, us10y, hy_spread, spy_dd, regime_adj):
    s = 0.0
    if spy[0] > 0 and gap(spy[0], spy[2]) < -SPY_TREND_GAP: s += 2.0
    elif spy[0] > 0 and gap(spy[0], spy[2]) < 0: s += 1.0
    if pct(spy[0], spy[1]) <= -4.0: s += 3.0

    if spy_dd is not None:
        if spy_dd <= DRAWDOWN_DANGER: s += 2.5
        elif spy_dd <= DRAWDOWN_WARN: s += 1.0

    if gap(fx_data[0], fx_data[3]) > FX_GAP["danger"]: s += 2.0
    elif gap(fx_data[0], fx_data[3]) > FX_GAP["caution"]: s += 1.0

    if vix >= VIX["panic"]: s += 4.0
    elif vix >= 30: s += 1.5
    elif vix >= VIX["warn"]: s += 1.0

    if dxy > DXY["danger"]: s += 1.5
    if hy_spread and hy_spread[0]:
        if hy_spread[0] > HY_SPREAD_DANGER: s += 3.0
        elif hy_spread[0] > HY_SPREAD_WARN: s += 1.5

    s += (ai_score * AI_WEIGHT)
    s += regime_adj 
    return max(0.0, min(SCORE_MAX, s))

# ==========================================
# 🚀 메인 실행부
# ==========================================
def main():
    log("📊 퀀텀 하이브리드 v9.5.2 가동 시작")
    
    state = load_state()
    prev_score = state.get("score", 0.0)
    current_ism = state.get("ism_pmi", 50.0)
    ism_date = state.get("ism_date", "2024-01-01")
    last_update_id = state.get("last_update_id", 0)

    # 텔레그램 명령 스캔
    new_ism = None
    try:
        url = f"https://api.telegram.org/bot{ENV['TELEGRAM_TOKEN']}/getUpdates?offset={last_update_id + 1}"
        res = requests.get(url, timeout=10).json()
        if res.get("ok") and res["result"]:
            for item in res["result"]:
                update_id = item["update_id"]
                if update_id > last_update_id: 
                    last_update_id = update_id
                
                msg_text = item.get("message", {}).get("text", "").upper()
                if msg_text.startswith("ISM "):
                    try:
                        new_ism = float(msg_text.replace("ISM", "").strip())
                    except: pass
    except Exception as e:
        log(f"텔레그램 명령 확인 실패: {e}")

    if new_ism is not None:
        current_ism = new_ism
        ism_date = datetime.now().strftime("%Y-%m-%d")
        log(f"📩 텔레그램 명령 수신: ISM {current_ism}")

    days_since_update = 0
    try:
        days_since_update = (datetime.now() - datetime.strptime(ism_date, "%Y-%m-%d")).days
    except: pass

    # ✅ 시스템 에러 추적기 부활
    api_errors = []

    spy_raw = safe(lambda: get_yahoo_stats("^GSPC"), "SPY")
    if not spy_raw: spy_raw = (0,0,0,0); api_errors.append("SPY")

    qqq_raw = safe(lambda: get_yahoo_stats("^IXIC"), "QQQ")
    if not qqq_raw: qqq_raw = (0,0,0,0); api_errors.append("QQQ")

    fx_data = safe(lambda: get_fx_data(), "FX")
    if not fx_data: fx_data = (1400.0, 1400.0, 1400.0, 1400.0, None); api_errors.append("FX")

    us10y   = safe(lambda: get_us10y(), "10Y")
    if not us10y: us10y = (None, None); api_errors.append("10Y")

    hy_spread = safe(lambda: get_hy_spread(), "HY")
    if not hy_spread: hy_spread = (None, None); api_errors.append("HY스프레드")

    unrate  = safe(lambda: get_unrate(), "실업률")
    if not unrate: unrate = 4.0; api_errors.append("실업률")

    # ✅ 버그 수정: min_count=10 전달
    vix_closes = safe(lambda: get_yahoo_closes("^VIX", "1mo", min_count=10), "VIX")
    vix = vix_closes[-1] if vix_closes else 22.0
    if not vix_closes: api_errors.append("VIX")

    dxy_closes = safe(lambda: get_yahoo_closes("DX-Y.NYB", "1mo", min_count=10), "DXY")
    dxy = dxy_closes[-1] if dxy_closes else 118.0
    if not dxy_closes: api_errors.append("DXY")

    spy_dd = ((spy_raw[0] - spy_raw[3]) / spy_raw[3] * 100) if spy_raw[0] and spy_raw[3] else None
    regime_info = get_macro_regime(current_ism, unrate)

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
        "HY_Spread": hy_spread[0] if hy_spread else None
    }
    
    ai = get_ai_analysis(news_context, market_summary) if news_context != "뉴스 수집 실패" else {"score":0.5, "market_phase":"지연", "top_risks":["-","-","-"], "strategy":"대기", "macro_correlation":"-"}

    total_score = calc_risk_score(spy_raw, qqq_raw, fx_data, vix, dxy, ai["score"], us10y, hy_spread, spy_dd, regime_info["score_adj"])
    is_panic = vix >= VIX["panic"] or (spy_raw[0] > 0 and pct(spy_raw[0], spy_raw[1]) <= -4.0)

    raw_score = total_score
    diff_str = f"{(raw_score - prev_score):+.1f}"

    if is_panic:
        stage_label, weight = "💀 패닉 구간", 0
        sell_idx, sell_div = "100% (전량)", "50% (절반 유지)"
        stage_action = "기존 자산 현금화 대피 (배당 절반 유지)"
        total_score = SCORE_MAX
    elif total_score < 3:  
        stage_label, weight = "🟢 공격적 매수", 100
        sell_idx, sell_div = "0%", "0%"
        stage_action = "주식 비중 100% 유지 및 추가 매수(수량 확보)"
    elif total_score < 7:  
        stage_label, weight = "🔵 적극적 유지", 80
        sell_idx, sell_div = "20%", "10%"
        stage_action = "1차 수익 실현 및 방어 (배당 파이프라인 유지)"
    elif total_score < 11: 
        stage_label, weight = "🟡 부분 방어",   60
        sell_idx, sell_div = "25%", "15%"
        stage_action = "2차 수익 실현 (하락장 대응 실탄 충전)"
    elif total_score < 13:
        stage_label, weight = "🟠 적극적 축소", 30
        sell_idx, sell_div = "50%", "25%"
        stage_action = "보수적 운영 및 자산 비중 대폭 축소"
    else:                  
        stage_label, weight = "🔴 위험 회피",   0
        sell_idx, sell_div = "100% (전량)", "50% (절반 유지)"
        stage_action = "대피 및 폭풍우 관망 (배당으로 멘탈 방어)"

    hy_eval = f"{hy_spread[0]:.2f}%" if hy_spread[0] else "지연"

    msg_header = f"🤖 퀀텀 인사이트 v9.5.2  |  {datetime.now().strftime('%Y.%m.%d %H:%M')}"
    if new_ism is not None:
        msg_header += f"\n\n✅ [업데이트 완료] 텔레그램 명령으로 ISM 지수가 {current_ism}로 갱신되었습니다!"
    elif days_since_update > 35:
        msg_header += f"\n\n🚨🚨 [경고] ISM 지수가 너무 오래되었습니다! (마지막 갱신: {days_since_update}일 전)\n채팅창에 'ISM 50.2' 형식으로 최신 수치를 보내주세요! 🚨🚨"

    # ✅ 시스템 상태 로직 부활
    sys_status_msg = f"⚠️ 데이터 지연 ({', '.join(api_errors)})" if api_errors else "✅ 정상"
    if is_panic: sys_status_msg = f"🚨 패닉 감지 | {sys_status_msg}"

    msg = f"""{msg_header}
━━━━━━━━━━━━━━━━━━
🌍 거시 경제 국면 (매크로 내비게이션)
 ├ ISM 제조업: {current_ism} / 미국 실업률: {unrate}%
 ├ 현재 국면  : {regime_info['emoji']} {regime_info['name']}
 └ 시스템 보정: {regime_info['action']} (위험점수 {regime_info['score_adj']:+.1f}점 조절)
━━━━━━━━━━━━━━━━━━
📌 시장 국면 (AI 진단)
{ai['market_phase']}

⚠️ 핵심 리스크
① {ai['top_risks'][0]}
② {ai['top_risks'][1]}
③ {ai['top_risks'][2]}

🧭 대응 전략
{ai['strategy']}
━━━━━━━━━━━━━━━━━━
📊 최종 위험 점수: {total_score:.1f} / 15.0 ({diff_str})
🎯 자산 배분: 주식 {weight}%  |  현금 {100-weight}%

📢 매도 지침 (현재 수량 기준):
 ├ 📈 지수/성장(TQQQ, QQQ): 【 {sell_idx} 】
 └ 💰 배당/인컴(SCHD, JEPI): 【 {sell_div} 】

🚦 국면: {stage_label}
📋 행동: {stage_action}
━━━━━━━━━━━━━━━━━━
📈 주요 지표 요약

S&P 500  : {format_index(*spy_raw)}
NASDAQ   : {format_index(*qqq_raw)}

💵 환율 (USD/KRW)
{fx_data[0]:,.0f}원 (1년 평균 대비: {gap(fx_data[0], fx_data[2]):+.1f}%)

📊 VIX 지수  : {vix:.2f}
💲 달러인덱스: {dxy:.1f}
🏦 미10Y금리 : {f"{us10y[0]:.2f}%" if us10y[0] else "지연"}
📉 HY스프레드: {hy_eval}
━━━━━━━━━━━━━━━━━━
💡 매크로 지표 심층 분석 (AI)
{ai['macro_correlation']}
━━━━━━━━━━━━━━━━━━
🛠 시스템: {sys_status_msg}
"""

    def split_message(text, max_len=3900):
        return [text[i:i+max_len] for i in range(0, len(text), max_len)]

    for chunk in split_message(msg):
        for _ in range(3):
            try:
                requests.post(
                    f"https://api.telegram.org/bot{ENV['TELEGRAM_TOKEN']}/sendMessage",
                    data={"chat_id": ENV["CHAT_ID"], "text": chunk},
                    timeout=15
                ).raise_for_status()
                break # 성공 시 반복문 즉시 탈출
            except Exception as e:
                time.sleep(2) # 실패 시 2초 대기 후 재시도
        else:
            log("❌ 텔레그램 메시지 전송 최종 실패")

    save_state({
        "score": raw_score,
        "ism_pmi": current_ism,
        "ism_date": ism_date,
        "last_update_id": last_update_id,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    log(f"✅ v9.5.2 완료 | 국면={regime_info['name']} | 산출점수={raw_score:.1f}")

if __name__ == "__main__":
    main()
