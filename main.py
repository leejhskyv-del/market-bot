import yfinance as yf
import requests
import os
import json
import feedparser
from openai import OpenAI

# ==========================================
# 1. 설정 및 환경 변수
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")

client = OpenAI(api_key=OPENAI_API_KEY)

# ==========================================
# 2. 다중 뉴스 소스 수집
# ==========================================
def fetch_global_news():
    urls = [
        "https://finance.yahoo.com/news/rssindex",
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?id=10000664",
        "https://www.investing.com/rss/news_285.rss"
    ]
    headlines = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
            headlines.extend([entry.title for entry in feed.entries[:4]])
        except: continue
    return "\n".join(headlines)

# ==========================================
# 3. GPT-4o 매크로 분석
# ==========================================
def get_ai_macro_score(news_text):
    if not news_text: return 0, "뉴스 수집 실패", "None"
    
    prompt = f"""
    Analyze macro risk based on 5 categories: 
    1.War/Geopolitics 2.Pandemic 3.Financial System 4.Rates/CPI 5.Liquidity.
    Scoring: +3(Confirmed Black Swan), +2(Major Negative), +1(Minor Negative), 0(Neutral), -1(Minor Positive), -2(Strong Positive).
    News: {news_text}
    Output STRICTLY in JSON: {{"score": int, "reason": "1-sentence", "category": "name"}}
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        res = json.loads(response.choices[0].message.content)
        return res.get('score', 0), res.get('reason', '분석 불가')
    except: return 0, "AI 에러"

# ==========================================
# 4. 시장 지표 수집 (5일 수익률 추가)
# ==========================================
def get_market_status():
    vix_data = yf.Ticker("^VIX").history(period="5d")
    vix = round(vix_data['Close'].iloc[-1], 2)
    
    spy_hist = yf.Ticker("SPY").history(period="300d")
    spy = round(spy_hist['Close'].iloc[-1], 2)
    ma200 = round(spy_hist['Close'].rolling(200).mean().iloc[-1], 2)
    
    # 최근 5일 수익률 (메타 필터용)
    spy_5d_ago = spy_hist['Close'].iloc[-6] if len(spy_hist) >= 6 else spy_hist['Close'].iloc[0]
    spy_return_5d = round((spy - spy_5d_ago) / spy_5d_ago * 100, 2)
    
    delta = spy_hist['Close'].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = round(100 - (100 / (1 + (gain / loss))).iloc[-1], 1)
    
    recent_peak = spy_hist['Close'][-252:].max()
    dd = round((spy - recent_peak) / recent_peak * 100, 1)

    tnx_data = yf.Ticker("^TNX").history(period="1y")
    tnx = round(tnx_data['Close'].iloc[-1], 2)
    tnx_avg = round(tnx_data['Close'].mean(), 2)

    return vix, spy, ma200, spy_return_5d, rsi, dd, tnx, tnx_avg

# ==========================================
# 5. 베이스 점수 계산 (퀀트 + AI)
# ==========================================
def calculate_base_scores(vix, spy, ma200, rsi, dd, tnx, tnx_avg, ai_score):
    quant_score = 0
    if vix >= 30: quant_score += 2
    elif vix >= 20: quant_score += 1
    
    if spy < ma200: quant_score += 2
    if dd <= -5: quant_score += 1   # 느린 하락 방어
    if dd <= -10: quant_score += 1  # 총 -10%시 2점
    
    if rsi < 30: quant_score += 1
    elif rsi > 75 and spy < (ma200 * 1.05): quant_score -= 2
    
    if tnx > (tnx_avg * 1.1): quant_score += 2

    # AI 1차 필터링
    if abs(ai_score) == 2: ai_score = int(round(ai_score * 0.7))
    
    # 퀀트가 이미 패닉(8 이상)이면 AI 노이즈 억제 (과열 방지 밸브)
    if quant_score >= 8: ai_score = min(ai_score, 1)

    return quant_score, ai_score

# ==========================================
# 6. ⭐ 메타 필터 (시스템 감시자) ⭐
# ==========================================
def apply_meta_filter(quant_score, ai_score, vix, spy_return_5d):
    meta_alerts = []
    
    # 룰 1: AI 환각 감지 (뉴스는 난리인데 시장은 평온)
    if ai_score >= 2 and vix < 20:
        ai_score = 0
        meta_alerts.append("🚫 [메타 제어] VIX 평온. AI 과잉 경고(환각) 차단.")
        
    # 룰 2: 과도한 공포 억제 (점수는 높은데 시장은 오르는 중)
    if quant_score >= 6 and spy_return_5d > 0:
        quant_score -= 1
        meta_alerts.append("📉 [메타 제어] SPY 단기 상승 중. 과도한 공포 점수 1점 하향.")
        
    # 룰 3: 시스템 맹점 방어 (점수는 낮고 조용한데 시장은 계속 녹아내림)
    if (quant_score + ai_score) <= 2 and spy_return_5d <= -3.0:
        quant_score += 2
        meta_alerts.append("⚠️ [메타 제어] 지표 맹점 감지! 숨은 하락 추세로 인해 2점 강제 상향.")

    return quant_score, ai_score, meta_alerts

# ==========================================
# 7. 행동 지침 & 메인 실행
# ==========================================
def get_action(score):
    if score <= -2: return "💎 과열", "추격 매수 금지 / 상승장 즐기기"
    elif score >= 9: return "🚨 패닉", "현금 실탄 투입 (분할 매수)"
    elif score >= 6: return "🔴 위험", "주식 추가 익절 (현금 확보 확대)"
    elif score >= 4: return "🟠 경고", "주식 1차 익절 (리스크 관리)"
    elif score >= 2: return "🟡 주의", "신규 매수 보류 / 관망"
    else: return "🟢 정상", "유지 / 기존 자동매수 진행"

def send(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.get(url, params={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def main():
    try:
        # 상태 로드
        state = {"last_score": 0}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f: state = json.load(f)

        # 1. 데이터 수집
        vix, spy, ma200, spy_return_5d, rsi, dd, tnx, tnx_avg = get_market_status()
        news_text = fetch_global_news()
        raw_ai_score, ai_reason = get_ai_macro_score(news_text)
        
        # 2. 1차 점수 계산
        base_quant, base_ai = calculate_base_scores(vix, spy, ma200, rsi, dd, tnx, tnx_avg, raw_ai_score)
        
        # 3. 메타 필터 가동 (오류 교정)
        final_quant, final_ai, meta_alerts = apply_meta_filter(base_quant, base_ai, vix, spy_return_5d)
        
        # 4. 최종 합산 및 상한선(Max 10) 적용
        total_score = min(final_quant + final_ai, 10)
        last_score = state.get("last_score", 0)
        status, strategy = get_action(total_score)
        
        # 노이즈 필터 (낮은 점수 구간의 1점 차이 무시)
        if abs(total_score - last_score) < 2 and total_score < 4:
            strategy = "어제와 상황 동일 (노이즈 필터 유지)"
            
        # 상태 저장
        state["last_score"] = total_score
        with open(STATE_FILE, "w") as f: json.dump(state, f)

        # 5. 리포트 작성
        meta_msg = "\n".join(meta_alerts) if meta_alerts else "✅ 시스템 정상 작동 중 (특이사항 없음)"
        
        msg = f"""
🤖 **하이브리드 퀀트 V4.0 (Meta-Engine)**

🔥 **현재 상태: {status}**
👉 **최종 점수: {total_score}점** (어제 {last_score}점)
👉 **행동 지침: {strategy}**

━━━━━━━━━━
🛡️ **메타 감시자 (System Check)**
{meta_msg}

━━━━━━━━━━
📊 **핵심 시장 지표**
• VIX: {vix} | 5일 수익률: {spy_return_5d}%
• SPY: {spy} (200일선: {ma200})
• 금리(TNX): {tnx}% (평균: {tnx_avg}%)
• RSI: {rsi} | 낙폭: {dd}%

━━━━━━━━━━
🧠 **AI 뉴스 코멘트**
• {ai_reason}
"""
        send(msg)
        print("V4.0 리포트 발송 완료!")

    except Exception as e:
        print("에러 발생:", e)
        send(f"🚨 시스템 에러: {e}")

if __name__ == "__main__":
    main()
