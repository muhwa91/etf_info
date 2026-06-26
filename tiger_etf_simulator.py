import requests
import time
import os
import json
import datetime
import sys

# Windows UTF-8 encoding setup
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

APP_KEY = os.environ.get("KIS_APP_KEY")
APP_SECRET = os.environ.get("KIS_APP_SECRET")

# 로컬 테스트용 config 파일에서 조회
kis_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kis_config.json")
if (not APP_KEY or not APP_SECRET) and os.path.exists(kis_config_path):
    try:
        with open(kis_config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            APP_KEY = APP_KEY or config.get("app_key")
            APP_SECRET = APP_SECRET or config.get("app_secret")
    except Exception:
        pass

BASE_URL   = "https://openapi.koreainvestment.com:9443"

HOLDINGS = {
    "SPCX": 0.252,
    "LUNR": 0.150,
    "RDW":  0.150,
    "RKLB": 0.150,
    "ASTS": 0.070,
    "SATS": 0.070,
    "PL":   0.070,
    "FLY":  0.030,
    "KRMN": 0.030,
    "VOYG": 0.030,
}

KOREAN_NAMES = {
    "SPCX": "스페이스X",
    "LUNR": "인튜이티브",
    "RDW":  "레드와이어",
    "RKLB": "로켓랩",
    "ASTS": "AST스페이스",
    "SATS": "에코스타",
    "PL":   "플래닛랩스",
    "FLY":  "파이어플라이",
    "KRMN": "카만",
    "VOYG": "보이저",
    "GSAT": "글로벌스타",
}

# SpaceX was added at Tuesday June 16 close.
# For dates before June 17, SPCX weight was 0% and others normalized to 100%.
HOLDINGS_NO_SPCX = {}
total_weight_no_spcx = sum(HOLDINGS[t] for t in HOLDINGS if t != "SPCX")
for t in HOLDINGS:
    if t != "SPCX":
        HOLDINGS_NO_SPCX[t] = HOLDINGS[t] / total_weight_no_spcx

EXCD_MAP = {
    "SPCX": "NAS",
    "LUNR": "NAS",
    "RDW":  "NYS",
    "RKLB": "NAS",
    "ASTS": "NAS",
    "SATS": "NAS",
    "PL":   "NYS",
    "FLY":  "NAS",
    "KRMN": "NYS",
    "VOYG": "NYS",
}

ETF_CODE = "0183J0"
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_cache.json")
FX_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fx_cache.json")
DPRT_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dprt_cache.json")
# 동시호가 1회-전송 마커. GitHub 예약 실행이 지연·드롭되어 08:30~09:00 창을 한 번에 못 맞추므로
# 아침 창 동안 여러 번(매 10분) 예약 실행하고, 유효한 antc_cnpr 메시지를 '하루 1회만' 보내기 위한 중복 방지 기록.
# (GitHub 러너는 매 실행이 새 환경이라 actions/cache 로 이 파일을 그날 실행들 사이에 전달한다.)
SENT_MARKER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_marker.json")


def _read_marker():
    """전송 마커(sent_marker.json) 전체를 dict 로 읽는다. 없으면 빈 dict."""
    try:
        if os.path.exists(SENT_MARKER_FILE):
            with open(SENT_MARKER_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def read_auction_sent_date():
    """오늘 이미 동시호가 예상 시가 메시지를 보냈는지 확인용 — 마지막 전송 KST 날짜(YYYY-MM-DD) 반환. 없으면 None."""
    return _read_marker().get("auction_date")


def read_last_us_date():
    """직전 전송 때 반영했던 '미국 최근 거래일(d1)' 반환. 없으면 None.
    오늘 d1 이 이 값과 같으면 직전 전송 이후 미국 새 세션이 없었다는 뜻(미국 휴장 등) → ETF 변동 없음."""
    return _read_marker().get("last_us_date")


def write_auction_sent_today(date_str, us_date=None):
    """전송 성공 기록 — 같은 날 중복 차단(auction_date)과, 미국 변동 없는 날 차단용(last_us_date) 저장."""
    try:
        data = _read_marker()
        data["auction_date"] = date_str
        if us_date:
            data["last_us_date"] = us_date
        with open(SENT_MARKER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠ 전송 마커 기록 실패(중복 방지 약화 가능): {e}")


def wait_until_send_time(target_hm, max_wait_min=40):
    """GitHub 예약은 정시 발화를 보장하지 않으므로, 워크플로를 08:30 '이전'에 깨워 두고
    이 함수가 목표 전송시각(기본 08:30 KST)까지 정확히 대기했다가 보내게 한다 → 평소엔 08:30 정각 도착.
    - 평일 + 목표시각 이전 + (목표까지 ≤max_wait_min)일 때만 대기한다.
    - 목표시각을 이미 지났으면 즉시 반환(지연 발화 시 곧장 진행 → 폴백/창내 전송 로직이 처리).
    - 너무 이르게(>max_wait_min) 깨어난 비정상 상황에선 대기하지 않는다(안전장치)."""
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(kst)
    if now.weekday() >= 5:
        return
    target = now.replace(hour=target_hm // 100, minute=target_hm % 100, second=0, microsecond=0)
    wait_s = (target - now).total_seconds()
    if 0 < wait_s <= max_wait_min * 60:
        print(f"  ⏲ 목표 전송시각 {target_hm // 100:02d}:{target_hm % 100:02d} KST 까지 약 {int(wait_s)}초 대기 후 전송...")
        time.sleep(wait_s)


def market_status_line(kr_open, us_new_session, compact=False):
    """🇰🇷 국내장 / 🇺🇸 미국장 상태 텍스트(스타일 A: 깃발+신호등).
    compact=True 면 한 줄(예측 메시지 헤더용), False 면 두 줄(휴장 안내용)."""
    kr = "정상 🟢" if kr_open else "휴장 🔴"
    us = "정상 🟢" if us_new_session else "휴장 🔴"
    if compact:
        return f"🇰🇷 국내장 {kr}\n🇺🇸 미국장 {us}"
    return f"🇰🇷 국내장 : 금일 {kr}\n🇺🇸 미국장 : 전일 {us}"


def build_market_info_message(date_header, kr_open, us_new_session):
    """휴장/변동없음 안내 메시지(스타일 A). 국내휴장·미국휴장·둘다 상황별 문구."""
    status = market_status_line(kr_open, us_new_session, compact=False)
    if not kr_open and not us_new_session:
        head = "🛑 <b>오늘은 휴장일입니다</b>"
        tail = "시가 예측 - 익일 아침 전송예정"
    elif not kr_open:
        head = "🛑 <b>오늘은 국내장 휴장일입니다</b>"
        tail = "시가 예측 - 익일 아침 전송예정"
    else:  # 국내 개장 + 미국 전일 휴장(반영할 변동 없음)
        head = "😴 <b>오늘은 예측을 쉽니다</b>"
        tail = ("미국장 휴장 ETF에 반영 가격 변동X\n"
                "미국장 개장 익일 아침 전송예정")
    return f"<b>[{date_header}]</b>\n\n{head}\n\n{status}\n\n{tail}"

# 개장 할인율 모델 상수.
#   OPEN_DPRT_RATIO : '개장 괴리율 / 종가 괴리율' 비율.
#     KIS 실측(2026-06-19): 개장 -3.06% / 종가 -4.64% = 0.66.
#     ETF 의 NAV 대비 할인은 장중 확대되어 시가 할인이 종가 할인보다 완만한 구조적 특성.
#   OPEN_DPRT_MIN_SAMPLES : 캐시의 측정 개장할인 표본이 이 수 이상이면 캐시 평균을 우선 사용.
#   OPEN_DPRT_RECENT_DAYS : 캐시 평균에 쓸 최근 영업일 수.
#   OPEN_DPRT_CAP : 전일 종가괴리 이상치 상한(%) — 급변기 과대할인 방지, 3일 실측 백테스트 기반
#     (과도기 안전장치, 표본 누적 후 재튜닝). cold-start 경로에만 적용.
#   OPEN_BAND_* : 폴백(antc 없음) 개장 시가 정밀 범위 밴드.
#     OPEN_BAND_VOL_DPRT : 변동성 임계 — |전일종가괴리(kis_dprt)| 가 이보다 크면 큰 이상치 국면.
#     OPEN_BAND_VOL_RATIO: 큰 이상치 밴드(예측NAV 비례, ~±100원). OPEN_BAND_FB_RATIO: 일반 폴백(~±60원).
#     (antc 유효 경로는 ±25원 고정 유지. 3일 실측 백테스트 기반 — 폴백 범위 적중 0/3→3/3.)
OPEN_DPRT_RATIO = 0.66
OPEN_DPRT_CAP = 3.0
OPEN_DPRT_MIN_SAMPLES = 3
OPEN_DPRT_RECENT_DAYS = 5
OPEN_BAND_VOL_DPRT = 5.0
OPEN_BAND_VOL_RATIO = 0.010
OPEN_BAND_FB_RATIO = 0.006

def safe_float(val, default=0.0):
    try:
        return float(val) if val not in (None, "", " ") else default
    except (ValueError, TypeError):
        return default

def send_telegram_message(message):
    import requests
    import os
    import json
    
    print("💬 텔레그램 메시지 전송 중...")
    
    # 1. GitHub Actions 또는 시스템 환경 변수에서 조회
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    # 2. 로컬 테스트용 config 파일에서 조회
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_config.json")
    if (not bot_token or not chat_id) and os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                bot_token = bot_token or config.get("bot_token")
                chat_id = chat_id or config.get("chat_id")
        except Exception:
            pass
            
    if not bot_token or not chat_id:
        print("  ⚠️ 경고: 텔레그램 설정(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)을 찾을 수 없습니다.")
        print("  로컬 테스트 시 'telegram_config.json' 파일에 아래 형식으로 작성해 두시면 됩니다:")
        print('  {"bot_token": "YOUR_BOT_TOKEN", "chat_id": "YOUR_CHAT_ID"}')
        return False
        
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    
    # 일시적 네트워크/텔레그램 장애에 대비해 최대 3회 재시도(2초·4초 백오프).
    for attempt in range(1, 4):  # 1, 2, 3
        try:
            res = requests.post(url, json=payload, timeout=15)
            res_data = res.json()
            if res.status_code == 200 and res_data.get("ok"):
                print("  ✅ 텔레그램 메시지 전송 성공!")
                return True
            else:
                print(f"  ❌ 텔레그램 메시지 전송 실패(시도 {attempt}/3): {res.text}")
        except Exception as e:
            print(f"  ❌ 텔레그램 메시지 전송 중 오류 발생(시도 {attempt}/3): {e}")
        if attempt < 3:
            wait = 2 * attempt  # 2초, 4초 백오프
            print(f"  ⏳ {wait}초 후 재전송 시도")
            time.sleep(wait)
    return False


def get_token():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                cache = json.load(f)
            mtime = os.path.getmtime(CACHE_FILE)
            if time.time() - mtime < 20 * 3600:
                print("✅ 토큰 발급 완료 (캐시 로드)")
                return cache["access_token"]
        except Exception:
            pass

    # KIS 토큰 발급 — 해외(GitHub 러너) IP 에서 가끔 연결 타임아웃이 나므로 타임아웃+백오프 재시도.
    last_err = None
    for attempt in range(1, 5):  # 최대 4회
        try:
            res = requests.post(f"{BASE_URL}/oauth2/tokenP", json={
                "grant_type": "client_credentials",
                "appkey": APP_KEY,
                "appsecret": APP_SECRET
            }, timeout=15)
            res.raise_for_status()
            data = res.json()
            try:
                with open(CACHE_FILE, "w") as f:
                    json.dump(data, f)
            except Exception:
                pass
            print("✅ 토큰 발급 완료 (신규 발급)")
            return data["access_token"]
        except Exception as e:
            last_err = e
            wait = 3 * attempt  # 3, 6, 9s 백오프
            print(f"  ⚠ KIS 토큰 발급 시도 {attempt}/4 실패: {type(e).__name__} → {wait}s 후 재시도")
            if attempt < 4:
                time.sleep(wait)
    raise RuntimeError(f"KIS 토큰 발급 4회 실패(네트워크/일시장애 추정): {last_err}")

def get_us_price(token, ticker, retry=3):
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHDFS00000300",
    }

    excd_list = [EXCD_MAP[ticker]]
    for fallback in ["NAS", "NYS", "AMS"]:
        if fallback not in excd_list:
            excd_list.append(fallback)

    for excd in excd_list:
        for attempt in range(retry):
            res = requests.get(
                f"{BASE_URL}/uapi/overseas-price/v1/quotations/price",
                headers=headers,
                params={"AUTH": "", "EXCD": excd, "SYMB": ticker},
                timeout=15,
            )
            data = res.json()

            if data.get("rt_cd") == "0":
                o = data["output"]
                current = safe_float(o.get("last"))
                prev    = safe_float(o.get("base"))
                rate    = safe_float(o.get("rate"))
                if current > 0 and prev > 0:
                    return {"current": current, "prev": prev, "rate": rate, "excd": excd}
                else:
                    break

            msg = data.get("msg1", "")
            if "초과" in msg:
                wait = 0.5 * (attempt + 1)
                print(f"  ⏳ {ticker}({excd}) 속도제한 → {wait}초 후 재시도")
                time.sleep(wait)
            else:
                break

        time.sleep(0.2)

    return None

def get_usdkrw(token, retry=3):
    """KIS 해외주식 현재가상세(tr_id HHDFS76200200)에서 USD/KRW 환율(t_rate)을 조회.

    응답 output 의 `t_rate` 가 적용 환율(예 "1529.00")이며 `last × t_rate = t_xprc`(원화환산)로 검증됨.
    야간엔 실시간 환율 `p_rate` 가 빈 값일 수 있으므로 `p_rate` 가 유효하면 우선 사용하고,
    비어 있으면 `t_rate` 로 폴백한다. 보유종목 중 첫 성공 응답을 사용하고, 모두 실패하면 None.
    (get_us_price/get_us_daily 의 거래소 fallback·레이트리밋 백오프 패턴을 따른다.)
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHDFS76200200",
    }

    for ticker in HOLDINGS:
        excd_list = [EXCD_MAP.get(ticker, "NAS")]
        for fallback in ["NAS", "NYS", "AMS"]:
            if fallback not in excd_list:
                excd_list.append(fallback)

        for excd in excd_list:
            for attempt in range(retry):
                res = requests.get(
                    f"{BASE_URL}/uapi/overseas-price/v1/quotations/price-detail",
                    headers=headers,
                    params={"AUTH": "", "EXCD": excd, "SYMB": ticker},
                    timeout=15,
                )
                data = res.json()

                if data.get("rt_cd") == "0":
                    o = data.get("output", {})
                    # 야간엔 p_rate(실시간)가 빈 값일 수 있음 → 비어 있으면 t_rate 사용
                    p_rate = safe_float(o.get("p_rate"))
                    t_rate = safe_float(o.get("t_rate"))
                    fx = p_rate if p_rate > 0 else t_rate
                    if fx > 0:
                        return fx
                    break

                msg = data.get("msg1", "")
                if "초과" in msg:
                    wait = 0.5 * (attempt + 1)
                    print(f"  ⏳ 환율({ticker}/{excd}) 속도제한 → {wait}초 후 재시도")
                    time.sleep(wait)
                else:
                    break

            time.sleep(0.2)

    return None

def load_fx_cache():
    """일별 환율 캐시(fx_cache.json) 로드: { "YYYY-MM-DD": t_rate } 형태."""
    if os.path.exists(FX_CACHE_FILE):
        try:
            with open(FX_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_fx_today(fx_rate):
    """오늘(KST 날짜) 환율을 일별 캐시에 저장. 실행할 때마다 당일 값으로 갱신."""
    if fx_rate is None or fx_rate <= 0:
        return
    kst_tz = datetime.timezone(datetime.timedelta(hours=9))
    today = datetime.datetime.now(kst_tz).strftime("%Y-%m-%d")
    cache = load_fx_cache()
    cache[today] = fx_rate
    try:
        with open(FX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_dprt_cache():
    """일별 괴리율 캐시(dprt_cache.json) 로드.

    형태: { "YYYY-MM-DD": {"open": 개장괴리율%, "close": 종가괴리율%} }
    실행 때마다 KIS 실측 괴리율을 누적해, 개장 할인율 추정의 근거 시계열을 만든다.
    (공개 시세 기반 데이터지만 fx_cache 와 동일하게 로컬 캐시로 .gitignore 처리)
    """
    if os.path.exists(DPRT_CACHE_FILE):
        try:
            with open(DPRT_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_dprt_today(open_dprt=None, close_dprt=None):
    """오늘(KST 날짜)의 측정 괴리율을 일별 캐시에 누적 저장.

    open_dprt/close_dprt 는 % 단위. 유효한 값만 갱신(없으면 기존 값 보존).
    """
    if (open_dprt is None or open_dprt == 0.0) and (close_dprt is None or close_dprt == 0.0):
        # 둘 다 없으면 저장 생략 (단, 0.0 자체가 의미있는 경우는 드물어 가드)
        if open_dprt is None and close_dprt is None:
            return
    kst_tz = datetime.timezone(datetime.timedelta(hours=9))
    today = datetime.datetime.now(kst_tz).strftime("%Y-%m-%d")
    cache = load_dprt_cache()
    entry = cache.get(today, {})
    if open_dprt is not None:
        entry["open"] = round(open_dprt, 3)
    if close_dprt is not None:
        entry["close"] = round(close_dprt, 3)
    if entry:
        cache[today] = entry
        try:
            with open(DPRT_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def estimate_open_discount(kis_dprt, today_open_dprt=None):
    """개장 할인율(%) 추정 — 시가_pred = 예측NAV × (1 + 추정개장할인/100) 에 사용.

    우선순위:
      1) 캐시에 측정된 '개장 괴리율(open)' 표본이 OPEN_DPRT_MIN_SAMPLES 이상이면
         최근 OPEN_DPRT_RECENT_DAYS 영업일 평균 사용 (KIS 실측 누적치 = 가장 견고).
      2) 표본이 부족하면 cold-start: 가장 최근에 알 수 있는 종가 괴리율 × OPEN_DPRT_RATIO.
         - 장중(live): 실시간 kis_dprt 사용.
         - 장후(after): 캐시의 최근 종가 괴리율(없으면 kis_dprt) 사용.

    today_open_dprt 가 주어지면(=장중 실측 개장괴리) 그 자체를 최우선으로 반환한다.
    반환: (추정개장할인%, 근거설명문자열, 변동성신호)
      변동성신호 = cold-start 에서 쓴 '전일 종가괴리'(클리핑 전 원본). 폴백 범위밴드의
      변동성 판단(클리핑과 동일 기준)에 쓴다. 당일 실측·캐시 평균 경로는 None(견고한 경로라 좁은 밴드면 충분).
    """
    # 장중에 오늘 개장 괴리율을 실측했다면 그것이 정답에 가장 가깝다.
    if today_open_dprt is not None and today_open_dprt != 0.0:
        return today_open_dprt, f"당일 실측 개장 괴리율({today_open_dprt:+.2f}%)", None

    # ★ 룩어헤드 방지: 8시반 개장 전 예측이므로 '오늘'(및 미래) 캐시는 절대 쓰지 않는다.
    #   (오늘의 개장/종가 괴리율은 개장 후에야 알 수 있는 값)
    today_kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d")

    cache = load_dprt_cache()
    open_samples = []
    for d in sorted(cache.keys(), reverse=True):
        if d >= today_kst:
            continue  # 오늘/미래 제외
        v = cache[d].get("open")
        if v is not None:
            open_samples.append(v)
        if len(open_samples) >= OPEN_DPRT_RECENT_DAYS:
            break

    if len(open_samples) >= OPEN_DPRT_MIN_SAMPLES:
        avg = sum(open_samples) / len(open_samples)
        return avg, f"전일까지 {len(open_samples)}영업일 측정 개장괴리 평균({avg:+.2f}%)", None

    # cold-start: 전일까지의 최근 종가 괴리율 × 비율 (오늘 제외)
    base_close_dprt = None
    src = None
    for d in sorted(cache.keys(), reverse=True):
        if d >= today_kst:
            continue  # 오늘/미래 제외
        v = cache[d].get("close")
        if v is not None:
            base_close_dprt = v
            src = f"전일 종가괴리({d})"
            break
    if base_close_dprt is None:
        # 전일까지 캐시가 전혀 없는 첫 실행 한정 폴백(개장 전 KIS dprt는 보통 전일 종가괴리값)
        base_close_dprt = kis_dprt
        src = "KIS dprt(전일 데이터 없음·첫 실행 폴백)"

    clipped = max(-OPEN_DPRT_CAP, min(OPEN_DPRT_CAP, base_close_dprt))
    est = clipped * OPEN_DPRT_RATIO
    clip_note = f" → 이상치 ±{OPEN_DPRT_CAP:.0f}% 클립 {clipped:+.2f}%" if clipped != base_close_dprt else ""
    return est, f"{src} {base_close_dprt:+.2f}%{clip_note} × {OPEN_DPRT_RATIO}(개장/종가 비율) = {est:+.2f}%", base_close_dprt


def get_etf_open_nav(token, retry=3):
    """KIS NAV 추이(tr_id FHPST02440000)에서 당일 개장/고가/저가/전일종가 NAV·가격 조회.

    output1: 가격(stck_oprc 시가·stck_hgpr·stck_lwpr·stck_prpr 현재가)
    output2: NAV(oprc_nav 개장NAV·hprc_nav·lprc_nav·nav 현재NAV·prdy_clpr_nav 전일종가NAV)
    개장 괴리율 = (시가 - 개장NAV)/개장NAV 실측에 사용. 장 시작 전이면 시가/개장NAV가 0일 수 있음.
    레이트리밋 초과 시 백오프 재시도(다른 KIS 호출과 동일 패턴).
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHPST02440000",
    }
    for attempt in range(retry):
        res = requests.get(
            f"{BASE_URL}/uapi/etfetn/v1/quotations/nav-comparison-trend",
            headers=headers,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ETF_CODE},
            timeout=15,
        )
        data = res.json()
        if data.get("rt_cd") == "0":
            o1 = data.get("output1", {})
            o2 = data.get("output2", {})
            return {
                "open_price":    safe_float(o1.get("stck_oprc")),
                "cur_price":     safe_float(o1.get("stck_prpr")),
                "oprc_nav":      safe_float(o2.get("oprc_nav")),
                "cur_nav":       safe_float(o2.get("nav")),
                "prdy_clpr_nav": safe_float(o2.get("prdy_clpr_nav")),
            }
        if "초과" in data.get("msg1", ""):
            wait = 0.5 * (attempt + 1)
            print(f"  ⏳ NAV추이 속도제한 → {wait}초 후 재시도")
            time.sleep(wait)
        else:
            break
    return None


def get_kr_market_open(token, yyyymmdd):
    """KIS 국내휴장일조회(CTCA0903R)로 해당일(YYYYMMDD) 개장여부 반환.
    True=개장 / False=휴장(주말·공휴일) / None=조회실패(이때는 발송을 막지 않는다)."""
    try:
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": "CTCA0903R",
            "custtype": "P",
        }
        res = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/chk-holiday",
            headers=headers,
            params={"BASS_DT": yyyymmdd, "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
            timeout=10,
        )
        data = res.json()
        if data.get("rt_cd") != "0":
            return None
        for r in data.get("output", []):
            if r.get("bass_dt") == yyyymmdd:
                return r.get("opnd_yn") == "Y"
    except Exception:
        pass
    return None


def get_etf_nav(token):
    """KIS ETF 실시간 iNAV·괴리율·전일확정NAV 조회 (tr_id FHPST02400000).

    output 주요 필드:
      nav            : 실시간 추정 iNAV
      prdy_last_nav  : 전일 확정 NAV (= base_nav 로 사용, 1일 지연 제거)
      nav_prdy_vrss  : NAV 전일대비
      nav_prdy_ctrt  : NAV 전일대비율(%)
      dprt           : 괴리율(%)
      trc_errt       : 추적오차(%)
      etf_ntas_ttam  : 순자산총액(억 단위)
      stck_prpr      : ETF 현재가
      stck_sdpr      : ETF 전일 종가(기준가)
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHPST02400000",
    }
    res = requests.get(
        f"{BASE_URL}/uapi/etfetn/v1/quotations/inquire-price",
        headers=headers,
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ETF_CODE},
        timeout=15,
    )
    data = res.json()
    if data.get("rt_cd") != "0":
        print(f"  ⚠ ETF iNAV 조회 실패: {data.get('msg1','')}")
        return None
    o = data["output"]
    return {
        "nav":           safe_float(o.get("nav")),                # 실시간 추정 iNAV
        "prdy_last_nav": safe_float(o.get("prdy_last_nav")),      # 전일 확정 NAV
        "nav_prdy_vrss": safe_float(o.get("nav_prdy_vrss")),
        "nav_prdy_ctrt": safe_float(o.get("nav_prdy_ctrt")),
        "dprt":          safe_float(o.get("dprt")),               # 괴리율(%)
        "trc_errt":      safe_float(o.get("trc_errt")),           # 추적오차(%)
        "etf_ntas_ttam": safe_float(o.get("etf_ntas_ttam")),      # 순자산총액
        "current":       safe_float(o.get("stck_prpr")),          # ETF 현재가
        "prev":          safe_float(o.get("stck_sdpr")),          # ETF 전일 종가
    }

def get_etf_expected_open(token, retry=3):
    """KIS 예상체결가 조회 (tr_id FHKST01010200) — 장전 동시호가(8:30~09:00) 예상 시가.

    엔드포인트: /uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn
    output2 주요 필드:
      antc_cnpr           : 예상체결가(= 시장이 만든 예상 시가)
      antc_cntg_prdy_ctrt : 예상 전일대비율(%)
      antc_cntg_vrss      : 예상 전일대비(원)
      antc_vol            : 예상 거래량
      antc_mkop_cls_code  : 장운영 구분코드
      stck_prpr           : 현재가(동시호가 시간이 아니면 antc_cnpr 가 이 값으로 나옴)

    반환: 위 필드를 담은 dict(antc_cnpr/stck_prpr/prev 는 float). 실패/빈값이면 None.
    (get_etf_nav 의 헤더·레이트리밋 백오프 패턴을 따른다.)
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010200",
    }
    for attempt in range(retry):
        res = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            headers=headers,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ETF_CODE},
            timeout=15,
        )
        data = res.json()
        if data.get("rt_cd") == "0":
            o2 = data.get("output2", {}) or {}
            antc_cnpr = safe_float(o2.get("antc_cnpr"))
            if antc_cnpr <= 0:
                return None
            return {
                "antc_cnpr":          antc_cnpr,                              # 예상체결가
                "antc_cntg_prdy_ctrt": safe_float(o2.get("antc_cntg_prdy_ctrt")),  # 예상 전일대비%
                "antc_cntg_vrss":     safe_float(o2.get("antc_cntg_vrss")),   # 예상 전일대비(원)
                "antc_vol":           safe_float(o2.get("antc_vol")),         # 예상 거래량
                "antc_mkop_cls_code": (o2.get("antc_mkop_cls_code") or "").strip(),  # 장운영 구분코드
                "cur_price":          safe_float(o2.get("stck_prpr")),        # 현재가
                "prev":               safe_float(o2.get("stck_sdpr")),        # 전일 종가(기준가)
            }
        if "초과" in data.get("msg1", ""):
            wait = 0.5 * (attempt + 1)
            print(f"  ⏳ 예상체결가 속도제한 → {wait}초 후 재시도")
            time.sleep(wait)
        else:
            break
    return None


def get_us_daily(token, ticker, retry=3):
    """KIS 미국 일별 OHLC 조회 (tr_id HHDFS76240000, 최근 100일).

    output2 리스트는 최신일이 [0]. 각 행: xymd(YYYYMMDD)·clos·open·high·low·rate·tvol.
    반환: 날짜(YYYY-MM-DD) → 종가 dict. 야후 get_yfinance_history_by_date 대체.
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHDFS76240000",
    }

    excd_list = [EXCD_MAP.get(ticker, "NAS")]
    for fallback in ["NAS", "NYS", "AMS"]:
        if fallback not in excd_list:
            excd_list.append(fallback)

    for excd in excd_list:
        for attempt in range(retry):
            res = requests.get(
                f"{BASE_URL}/uapi/overseas-price/v1/quotations/dailyprice",
                headers=headers,
                params={"AUTH": "", "EXCD": excd, "SYMB": ticker,
                        "GUBN": "0", "BYMD": "", "MODP": "1"},
                timeout=15,
            )
            data = res.json()

            if data.get("rt_cd") == "0":
                rows = data.get("output2") or []
                date_dict = {}
                for row in rows:
                    xymd = (row.get("xymd") or "").strip()
                    clos = safe_float(row.get("clos"))
                    if len(xymd) == 8 and clos > 0:
                        dt_str = f"{xymd[:4]}-{xymd[4:6]}-{xymd[6:8]}"
                        date_dict[dt_str] = clos
                if date_dict:
                    return date_dict
                break

            msg = data.get("msg1", "")
            if "초과" in msg:
                wait = 0.5 * (attempt + 1)
                print(f"  ⏳ {ticker}({excd}) 일봉 속도제한 → {wait}초 후 재시도")
                time.sleep(wait)
            else:
                break

        time.sleep(0.2)

    return {}

def get_recent_us_dates(token, ticker="RKLB"):
    """KIS 미국 일별 데이터에서 최근 2개 영업일(d0, d1)을 추출. (야후 get_us_trading_dates 대체)"""
    hist = get_us_daily(token, ticker)
    dates = sorted(hist.keys())
    if len(dates) >= 2:
        return dates[-2], dates[-1]
    today = datetime.date.today()
    d1 = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    d0 = (today - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    return d0, d1

def is_korea_market_open(now_kst=None):
    """한국 정규장(평일 09:00~15:30 KST) 여부 → iNAV 신선도 판정 근거."""
    if now_kst is None:
        kst_tz = datetime.timezone(datetime.timedelta(hours=9))
        now_kst = datetime.datetime.now(kst_tz)
    if now_kst.weekday() >= 5:  # 토(5)·일(6)
        return False
    minutes = now_kst.hour * 60 + now_kst.minute
    return 9 * 60 <= minutes <= 15 * 60 + 30

def should_poll_auction(
    auction_only: bool,
    no_telegram: bool,
    in_preopen_auction: bool,
    antc,
) -> bool:
    """동시호가 antc 폴링 진입 여부(순수 함수). 부수효과 없음.

    조건: auction_only and not no_telegram and in_preopen_auction
          and (antc is None or antc.get('antc_cnpr', 0) <= 0)

    Args:
        auction_only: --auction-only 플래그.
        no_telegram: --no-telegram 플래그.
        in_preopen_auction: 현재 시각이 평일 08:30~09:00 구간인지 여부.
        antc: get_etf_expected_open() 반환값(None 또는 dict).
    """
    return (
        auction_only
        and not no_telegram
        and in_preopen_auction
        and (antc is None or antc.get("antc_cnpr", 0) <= 0)
    )


def decide_auction_send(
    expected_open_valid: bool,
    auction_primary_attempted: bool,
    after_auction_window: bool,
) -> str:
    """--auction-only 전송 게이트 판정(순수 함수). 부수효과 없음.

    반환: 'send_real' | 'send_fallback_primary' | 'send_fallback_late' | 'skip'
    우선순위:
        ① 유효 antc → 'send_real'
        ② 정시 주 실행(폴링 수행)인데 미확보 → 'send_fallback_primary'
        ③ 창 종료(09:00 이후) 뒤늦은 실행 → 'send_fallback_late'
        ④ 그 외(08:30 전 조기 실행) → 'skip'

    Args:
        expected_open_valid: antc_cnpr 가 유효한지 여부(expected_open is not None).
        auction_primary_attempted: 08:30~09:00 창 안에서 폴링까지 수행한 정시 주 실행 여부.
        after_auction_window: 평일 09:00 이후 여부.
    """
    if expected_open_valid:
        return "send_real"
    if auction_primary_attempted:
        return "send_fallback_primary"
    if after_auction_window:
        return "send_fallback_late"
    return "skip"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="TIGER 미국우주테크 ETF 시뮬레이터 (KIS API 단독)")
    parser.add_argument("--mode", choices=["auto", "live", "after"], default="auto",
                        help="예측 모드 (auto: 한국 장중/장후 자동판정, live: KIS iNAV 직접 사용, after: 미국가×고정비중 익영업일 예측)")
    parser.add_argument("--force", action="store_true", help="미국 휴장 시 강제 실행")
    parser.add_argument("--no-telegram", action="store_true", help="텔레그램 메시지 전송 생략")
    parser.add_argument("--auction-only", action="store_true",
                        help="장전 동시호가 예상체결가(antc_cnpr)가 유효할 때만 하루 1회 전송(GitHub 다회 예약용). "
                             "창 밖이거나 이미 보냈으면 전송하지 않고 조용히 종료.")
    parser.add_argument("--send-at", default="0830",
                        help="auction-only 시 목표 전송시각(HHMM, KST, 기본 0830). 이 시각 이전에 깨어난 평일 실행은 "
                             "목표시각까지 대기했다가 전송 → 평소 08:30 정각 도착.")
    parser.add_argument("--no-kakao", action="store_true", help=argparse.SUPPRESS) # 하위 호환용 숨김 옵션
    parser.add_argument("--d0", help="비교 시작일 (YYYY-MM-DD)")
    parser.add_argument("--d1", help="비교 종료일 (YYYY-MM-DD)")
    parser.add_argument("--fx-to", type=float,
                        help="현재 USD/KRW 환율 수동 지정. 미지정 시 KIS price-detail t_rate 자동 사용")
    parser.add_argument("--fx-from", type=float,
                        help="기준일 USD/KRW 환율 수동 지정. 미지정 시 일별 캐시(fx_cache.json)의 기준일 값 사용")

    # support running via run_simulator.bat which doesn't pass args, handle unknown args
    args, unknown = parser.parse_known_args()

    force_execution = args.force or "--force" in sys.argv
    no_telegram = args.no_telegram or args.no_kakao or "--no-telegram" in sys.argv or "--no-kakao" in sys.argv
    auction_only = args.auction_only or "--auction-only" in sys.argv

    # 모드 자동 판정: 한국 정규장(09:00~15:30 KST)이면 live(iNAV 직접), 그 외엔 after(익영업일 예측)
    kst_tz = datetime.timezone(datetime.timedelta(hours=9))
    now_kst = datetime.datetime.now(kst_tz)
    today_kst_str = now_kst.strftime("%Y-%m-%d")
    # (a) 지금이 한국 정규장(09:00~15:30) 중인가 — mode(live/after) 분기용.
    kr_regular_open = is_korea_market_open(now_kst)

    # 동시호가 1회-전송 모드: 오늘 이미 보냈으면 불필요한 KIS API 호출 없이 즉시 종료.
    if auction_only and read_auction_sent_date() == today_kst_str:
        print(f"  ℹ 오늘({today_kst_str}) 동시호가 예상 시가 메시지는 이미 전송됨 → 이번 예약 실행 스킵.")
        return

    # 08:30 정각 전송: GitHub 가 08:30 이전에 깨워 줬으면 08:30:00 까지 대기했다가 진행(antc 도 그때 신선하게 수집).
    if auction_only:
        try:
            target_hm = int(args.send_at)
        except (TypeError, ValueError):
            target_hm = 830
        wait_until_send_time(target_hm)
        # 대기 후 현재시각 갱신(이후 동시호가 창 판정이 대기 후 시각을 쓰도록).
        now_kst = datetime.datetime.now(kst_tz)
        today_kst_str = now_kst.strftime("%Y-%m-%d")
        # 대기 동안 정규장 진입 여부가 바뀔 수 있으므로 mode 판정 직전 시각 기준으로 재계산.
        kr_regular_open = is_korea_market_open(now_kst)
    if args.mode == "auto":
        mode = "live" if kr_regular_open else "after"
    else:
        mode = args.mode

    mode_label = "장중(KIS iNAV 직접)" if mode == "live" else "장후/새벽(미국가×고정비중 익영업일 예측)"
    print(f"\n🚀 TIGER 미국우주테크 ETF 시뮬레이터 — KIS API 단독 (모드: {mode_label})\n")

    token = get_token()

    # 미국 기준 영업일(d0, d1) 확정 — 휴장/상태 판정과 예측에 모두 사용(이른 단계에서 1회만).
    if args.d0 and args.d1:
        d0, d1 = args.d0, args.d1
    else:
        d0, d1 = get_recent_us_dates(token)
    print(f"\n📡 미국 기초자산 수집 기준 ({d0} 종가 → {d1} 종가)...")

    # 시장 상태 판정(auction-only): 국내장 금일 개장여부(KIS 휴장조회) + 미국장 전일 새 세션 여부.
    #   휴장(국내) 또는 변동없음(미국 새 세션 X)이면 → 상황별 '안내 메시지'를 하루 1회 보내고 종료.
    #   ETF 는 미국 자산 추종이라, 직전 전송 미국거래일(last_us)과 오늘 d1 이 같으면 미국 새 세션 없음.
    # (b) 오늘이 한국 증시 개장일인가 — 휴장 안내용(정규장 진행 여부와 무관).
    kr_trading_day = us_new_session = True
    if auction_only:
        kr_trading_day = get_kr_market_open(token, today_kst_str.replace("-", "")) is not False  # None(조회실패)=개장 간주
        last_us = read_last_us_date()
        us_new_session = (last_us is None) or (d1 != last_us)
        if (not kr_trading_day) or (not us_new_session):
            date_header = f"{now_kst.year}년 {now_kst.month}월 {now_kst.day}일"
            info_msg = build_market_info_message(date_header, kr_trading_day, us_new_session)
            label = "국내장 휴장" if not kr_trading_day else "미국장 전일 휴장(반영 변동 없음)"
            print(f"  🛑 {label} 감지 → 안내 메시지 발송 후 종료.")
            print(f"\n[텔레그램 전송 메시지 내용]\n{info_msg}\n")
            if no_telegram:
                print("  ℹ --no-telegram → 안내 전송 생략.")
            elif send_telegram_message(info_msg):
                write_auction_sent_today(today_kst_str)  # 하루 1회(us_date 는 갱신 안 함)
            return

    # 1. ETF iNAV/괴리율/전일확정NAV 수집 (KIS FHPST02400000)
    print("\n📈 ETF iNAV·괴리율 수집 중 (KIS)...")
    etf = get_etf_nav(token)
    if etf is None:
        print("  ❌ KIS ETF iNAV 데이터를 불러올 수 없습니다. 종료합니다.")
        return

    inav = etf["nav"]                  # 실시간 추정 iNAV
    prdy_last_nav = etf["prdy_last_nav"]  # 전일 확정 NAV (base_nav)
    etf_current = etf["current"]       # ETF 현재가
    etf_prev = etf["prev"]             # ETF 전일 종가
    kis_dprt = etf["dprt"]             # KIS 괴리율(%)

    print(f"  KIS 실시간 iNAV   : {inav:>9,.2f}원 / 현재가: {etf_current:>6,.0f}원 / 전일종가: {etf_prev:>6,.0f}원")
    print(f"  KIS 전일확정 NAV  : {prdy_last_nav:>9,.2f}원 (base_nav)")
    print(f"  KIS 괴리율/추적오차: {kis_dprt:>+.2f}% / {etf['trc_errt']:.2f}% | 순자산총액: {etf['etf_ntas_ttam']:,.0f}억")

    # 1-b. 개장/현재 괴리율 실측 + 일별 캐시 누적 (개장 할인율 추정 근거)
    print("\n📉 ETF 개장/현재 괴리율 수집 중 (KIS NAV 추이)...")
    time.sleep(0.2)
    measured_open_dprt = None   # 당일 실측 개장 괴리율(%)
    onav = get_etf_open_nav(token)
    if onav is not None:
        op, onv = onav["open_price"], onav["oprc_nav"]
        cp, cnv = onav["cur_price"], onav["cur_nav"]
        if op > 0 and onv > 0:
            measured_open_dprt = (op - onv) / onv * 100
            print(f"  당일 시가/개장NAV  : {op:>6,.0f}원 / {onv:>9,.2f}원 → 개장 괴리율 {measured_open_dprt:>+.2f}%")
        else:
            print("  당일 개장가/개장NAV 미생성(장 시작 전) → 개장 괴리율 미측정")
        measured_close_dprt = (cp - cnv) / cnv * 100 if (cp > 0 and cnv > 0) else None
        if measured_close_dprt is not None:
            print(f"  당일 현재가/현재NAV: {cp:>6,.0f}원 / {cnv:>9,.2f}원 → 현재 괴리율 {measured_close_dprt:>+.2f}%")
        # 측정값 일별 캐시 누적(개장 할인율 추정 시계열 축적)
        save_dprt_today(open_dprt=measured_open_dprt, close_dprt=measured_close_dprt)
    else:
        print("  ⚠ KIS NAV 추이 조회 실패 → 개장/현재 괴리율 미측정")
        # 실시간 dprt 라도 종가괴리로 캐시에 누적
        if kis_dprt != 0.0:
            save_dprt_today(close_dprt=kis_dprt)

    # 1-c. KIS 예상체결가(antc_cnpr) 수집 — 장전 동시호가(8:30~09:00)의 '시장 예상 시가'.
    #   antc_cnpr 는 '동시호가 시간에만' 예상 시가 의미를 가진다(그 외엔 현재가로 나옴).
    #   → KST 평일 08:30~09:00(장전 동시호가)일 때만 유효로 본다. 무효면 폴백.
    #   [폴링 보강] auction_only + 동시호가 창 안 + antc가 아직 0/빈값이면 최대 08:38 KST 까지
    #   15초 간격으로 재조회한다. GAS 단일 디스패치 1회 실행이 08:30:00 정각에 창에 진입했을 때
    #   KIS 가 아직 예상체결가를 0으로 내려줄 수 있으므로 창 안에서 재시도해 유효값을 확보한다.
    print("\n🕗 KIS 예상체결가(antc_cnpr·장전 동시호가) 수집 중...")
    time.sleep(0.2)
    expected_open = None        # 유효한 예상 시가(원). 무효/조회실패면 None
    auction_primary_attempted = False  # 동시호가 창 안에서 폴링까지 수행한 '정시 주 실행' 플래그
    kst_tz_antc = datetime.timezone(datetime.timedelta(hours=9))
    now_kst = datetime.datetime.now(kst_tz_antc)
    kst_hm = now_kst.hour * 100 + now_kst.minute
    in_preopen_auction = (now_kst.weekday() < 5) and (830 <= kst_hm < 900)  # 평일 08:30~09:00
    antc = get_etf_expected_open(token)

    # 폴링 진입 조건: auction_only + --no-telegram 없음 + 창 안 + antc 미확보 상태
    _need_poll = should_poll_auction(auction_only, no_telegram, in_preopen_auction, antc)
    if _need_poll:
        auction_primary_attempted = True
        # 08:38:00 KST 를 폴링 데드라인으로 삼는다 (08:40 마감 요건 + 마지막 조회 여유 2분).
        _poll_deadline = now_kst.replace(hour=8, minute=38, second=0, microsecond=0)
        _poll_try = 1
        print(f"  ⏳ antc_cnpr 아직 0/미생성 — 동시호가 폴링 시작 (데드라인 08:38 KST, 15초 간격)")
        while True:
            _now = datetime.datetime.now(kst_tz_antc)
            if _now >= _poll_deadline:
                print(f"  ⏳ 폴링 데드라인(08:38) 도달 — antc_cnpr 끝내 미확보, 폴백으로 진행.")
                break
            time.sleep(15)
            _poll_try += 1
            _now = datetime.datetime.now(kst_tz_antc)
            _hms = _now.strftime("%H:%M:%S")
            print(f"  ⏳ 예상체결가 대기 폴링... ({_hms}, 시도 {_poll_try})")
            antc = get_etf_expected_open(token)
            if antc is not None and antc.get("antc_cnpr", 0) > 0:
                print(f"  ✅ antc_cnpr 확보 (시도 {_poll_try}, {_hms})")
                break
        # 폴링 종료 후 현재 시각·창 판정 갱신 (흐른 시간 반영)
        now_kst = datetime.datetime.now(kst_tz_antc)
        kst_hm = now_kst.hour * 100 + now_kst.minute
        in_preopen_auction = (now_kst.weekday() < 5) and (830 <= kst_hm < 900)

    if antc is not None:
        antc_cnpr = antc["antc_cnpr"]
        antc_vol = antc["antc_vol"]
        if in_preopen_auction and antc_cnpr > 0:
            expected_open = antc_cnpr
            print(f"  예상체결가(antc_cnpr): {antc_cnpr:>8,.0f}원  "
                  f"(전일대비 {antc['antc_cntg_prdy_ctrt']:+.2f}% · 예상거래량 {antc_vol:,.0f}주 · "
                  f"장운영코드 {antc['antc_mkop_cls_code']})")
        else:
            reason = "동시호가 시간(평일 08:30~09:00) 아님" if not in_preopen_auction else "antc_cnpr 빈값"
            print(f"  예상체결가 무효({reason}) → 폴백 사용")
    else:
        print("  ⚠ KIS 예상체결가 조회 실패/빈값 → 폴백 사용")

    # (d0, d1 는 위 토큰 직후에서 이미 확정됨 — 휴장/상태 판정과 공유)

    # 미국 장 휴일 체크 (KST 어제 날짜와 최근 영업일 d1을 비교) — 장후 모드에서만 의미 있음
    yesterday_kst = (now_kst - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if mode == "after" and not force_execution and d1 != yesterday_kst:
        print(f"  🛑 미국 장 휴장 감지 (한국기준 어제 날짜: {yesterday_kst} / 미국 최근 거래일: {d1})")
        print("  텔레그램 메시지 전송을 생략하고 종료합니다. (강제 실행을 원하시면 --force 인자를 붙여 실행하세요.)")
        return

    # 3. 고정 비중 선택 (SPCX 편입 시차 보정: 6/17 이전엔 SPCX 0% 재정규화)
    global HOLDINGS, EXCD_MAP
    if d1 < "2026-06-17":
        active_holdings = dict(HOLDINGS_NO_SPCX)
        print("  💡 SpaceX 편입 이전 기간 감지: SPCX 비중 0%로 조정 및 타 종목 비중 재정규화")
    else:
        active_holdings = dict(HOLDINGS)
        print("  💡 SpaceX 편입 이후 기간 감지: 표준 고정 비중(SPCX 25.2%) 사용")
    active_excd_map = dict(EXCD_MAP)

    print(f"  ✅ 고정 비중 적용 ({sum(1 for w in active_holdings.values() if w > 0)}개 종목):")
    for t, w in sorted(active_holdings.items(), key=lambda x: x[1], reverse=True):
        if w > 0:
            print(f"    - {t:<6}: {w*100:>5.2f}% (고정 비중)")

    HOLDINGS = active_holdings
    EXCD_MAP = active_excd_map

    # 4. 미국 기초자산 시세 수집 (모드별)
    confirmed = []
    stock_returns = {}
    latest_prices = {}

    # d0(전전일=수익률 기준) 종가는 KIS 미국 일별 데이터로 일괄 확보
    print("  KIS 미국 일별 데이터에서 기준일(d0) 종가 조회 중...")
    us_daily_cache = {}
    for ticker in active_holdings:
        if active_holdings[ticker] <= 0:
            continue
        us_daily_cache[ticker] = get_us_daily(token, ticker)
        time.sleep(0.2)

    if mode == "live":
        # 장중: 미국 일봉 d0 → d1(최근 확정) 종가 등락률 (참고용 표시), 예측치는 iNAV 직접 사용
        print("  KIS 미국 일별 데이터(d0→d1 확정 종가)로 종목 등락 분석 중...")
        for ticker in active_holdings:
            if active_holdings[ticker] <= 0:
                continue
            hist = us_daily_cache.get(ticker, {})
            p_from = hist.get(d0)
            p_to = hist.get(d1)
            excd = active_excd_map.get(ticker, "US")
            if p_from is not None and p_to is not None:
                ret = (p_to - p_from) / p_from * 100
                print(f"  {ticker:<6}  [{excd}]  {d0} {p_from:>9.2f}  →  {d1} {p_to:>9.2f}  ({ret:>+6.2f}%)")
                stock_returns[ticker] = ret
                latest_prices[ticker] = p_to
                confirmed.append(ticker)
            else:
                print(f"  ❌ {ticker} 일별 데이터 누락 (p_from: {p_from}, p_to: {p_to})")
    else:
        # 장후/새벽: KIS 미국 실시간가(현재) × 고정비중 → d0 대비 종목 합산 수익률
        print("  KIS API에서 미국 실시간 시세 조회 중...")
        for ticker in active_holdings:
            if active_holdings[ticker] <= 0:
                continue
            kis_data = get_us_price(token, ticker)
            hist = us_daily_cache.get(ticker, {})
            p_from = hist.get(d0)

            p_to = None
            excd = active_excd_map.get(ticker, "US")
            if kis_data:
                p_to = kis_data["current"]
                excd = kis_data["excd"]
            else:
                p_to = hist.get(d1)  # 실시간 실패 시 최근 확정 종가로 fallback

            if p_from is not None and p_to is not None:
                ret = (p_to - p_from) / p_from * 100
                print(f"  {ticker:<6}  [{excd}]  {d0} {p_from:>9.2f}  →  현재 {p_to:>9.2f}  ({ret:>+6.2f}%)")
                stock_returns[ticker] = ret
                latest_prices[ticker] = p_to
                confirmed.append(ticker)
            else:
                if not kis_data:
                    print(f"  ❌ {ticker} KIS 실시간 시세 수집 실패")
                else:
                    print(f"  ❌ {ticker} 기준일({d0}) 종가 누락")
            time.sleep(0.3)

    # 5. 환율 — KIS price-detail(t_rate) 자동 반영.
    #    현재 환율(fx_to): --fx-to 지정 시 그 값, 미지정 시 KIS t_rate 자동 사용.
    #    기준 환율(fx_from): --fx-from 지정 시 그 값 > 일별 캐시(기준일) > 0%(폴백) 순.
    print("\n💱 환율 처리 (KIS price-detail t_rate 자동 반영)...")

    # 현재 환율 확보 (수동값 우선, 없으면 KIS 자동)
    if args.fx_to is not None:
        fx_to = args.fx_to
        print(f"  현재 USD/KRW(수동 지정): {fx_to:.2f}")
    else:
        fx_to = get_usdkrw(token)
        if fx_to is not None:
            print(f"  현재 USD/KRW(KIS t_rate): {fx_to:.2f}")
        else:
            print("  ⚠ KIS 환율 조회 실패 → 환율 변동 0% 처리")

    # 현재 환율을 일별 캐시에 저장(다음 실행부터 기준 환율로 누적 활용)
    if args.fx_to is None and fx_to is not None:
        save_fx_today(fx_to)

    # 기준 환율 확보 및 환율 변동률 계산
    fx_change = 0.0
    if fx_to is None:
        # 현재 환율 자체가 없으면 변동 0% 처리
        pass
    elif args.fx_from is not None:
        # 수동 지정값이 최우선 (기존 동작 유지)
        fx_from = args.fx_from
        fx_change = (fx_to - fx_from) / fx_from * 100
        print(f"  USD/KRW(수동) {fx_from:.2f} → {fx_to:.2f}  ({fx_change:+.2f}%)")
    else:
        # 일별 캐시에서 비교 기준일(d1, 없으면 d0)의 환율을 base FX 로 사용
        fx_cache = load_fx_cache()
        base_fx = fx_cache.get(d1) or fx_cache.get(d0)
        base_fx = safe_float(base_fx)
        if base_fx > 0:
            fx_change = (fx_to / base_fx - 1) * 100
            print(f"  USD/KRW {base_fx:.2f}(기준일 캐시) → {fx_to:.2f}(현재)  ({fx_change:+.2f}%)")
        else:
            print(f"  현재 USD/KRW {fx_to:.2f} · 기준 환율 없음 → 변동 0% 처리 (다음 실행부터 캐시 누적)")

    # 6. 예측 계산
    weighted_stock_return = sum(stock_returns[t] * active_holdings[t] for t in confirmed)
    DAILY_FEE_RATE = 0.0049 / 365  # 신탁보수 연 0.49% 일할 차감

    # base_nav: KIS 전일 확정 NAV / base_etf: ETF 전일 종가
    base_nav = prdy_last_nav
    base_etf = etf_prev

    if mode == "live":
        # 장중: 예측 NAV = KIS 실시간 iNAV 직접 사용 (환율·비중 재계산 불필요 — iNAV에 이미 반영)
        target_date_str = "당일 장중 (KIS iNAV)"
        predicted_nav = inav
        total_return = (inav - base_nav) / base_nav if base_nav else 0.0
        total_return_pct = total_return * 100
        # 예측 ETF 가격: iNAV에 KIS 괴리율 반영 (현재가 ≈ iNAV*(1+dprt/100))
        predicted_etf = inav * (1 + kis_dprt / 100)
        # 실제값(장중 비교용)
        actual_nav = inav
        actual_etf = etf_current
    else:
        # 장후/새벽: base_nav 에 종목 합산수익률+환율+보수 적용해 익영업일 예측
        target_date_str = "익영업일 예상"
        total_return = (1 + weighted_stock_return / 100) * (1 + fx_change / 100) - 1
        total_return_pct = total_return * 100
        predicted_nav = base_nav * (1 + total_return) * (1 - DAILY_FEE_RATE)
        predicted_etf = base_etf * (1 + total_return) * (1 - DAILY_FEE_RATE)
        actual_nav = None
        actual_etf = None

    # Result Outputs
    print("\n" + "=" * 50)
    print(f"  ✅ 수집 완료 : {len(confirmed)}개  {confirmed}")
    print(f"\n  종목 가중 수익률   : {weighted_stock_return:>+.2f}%")
    print(f"  환율 수익률        : {fx_change:>+.2f}%")
    print(f"  합산 예측 수익률   : {total_return_pct:>+.2f}%")
    
    print("-" * 50)
    print(f"  [📊 {target_date_str} 시뮬레이션 결과]")
    print(f"  기준 ETF NAV       : {base_nav:>8,.0f}원")
    print(f"  예측 ETF NAV       : {predicted_nav:>8,.0f}원")
    if actual_nav is not None:
        print(f"  실제 ETF NAV       : {actual_nav:>8,.0f}원")
        nav_err = actual_nav - predicted_nav
        nav_err_pct = (nav_err / predicted_nav * 100) if predicted_nav else 0.0
        print(f"  오차               : {nav_err:>+8,.0f}원  ({nav_err_pct:+.2f}%)")
    else:
        print(f"  실제 ETF NAV       : 미공시 (장후 예측 또는 업데이트 지연)")
        print(f"  오차               : N/A")

    print("-" * 50)
    print(f"  [📈 {target_date_str} 주가 시뮬레이션 결과]")
    print(f"  기준 ETF 종가      : {base_etf:>8,.0f}원")
    print(f"  예측 ETF 가격      : {predicted_etf:>8,.0f}원")
    if actual_etf is not None:
        print(f"  실제 ETF 현재가    : {actual_etf:>8,.0f}원")
        etf_err = actual_etf - predicted_etf
        etf_err_pct = (etf_err / predicted_etf * 100) if predicted_etf else 0.0
        print(f"  오차               : {etf_err:>+8,.0f}원  ({etf_err_pct:+.2f}%)")
    else:
        print(f"  실제 ETF 현재가    : 미공시 (장후 예측 또는 업데이트 지연)")
        print(f"  오차               : N/A")
        
    # 개장 시가 예측.
    #   ★ 메인: KIS 예상체결가(antc_cnpr) — 장전 동시호가(8:30~09:00) 동안 시장이 만든 예상 시가.
    #     공정가치(예측 NAV)는 펀더멘털 모델 그대로 두고, 예상 괴리율 = (예상시가-예측NAV)/예측NAV.
    #   ▷ 폴백: antc_cnpr 가 무효(동시호가 시간 아님·빈값)면 기존 괴리율 기반 개장 할인 모델로 추정.
    #     시가_pred = 예측NAV × (1 + 추정개장할인/100). 추정개장할인 = ① 캐시 측정 개장괴리 평균(견고)
    #     → ② cold-start: 최근 종가괴리 × 비율. 장중(live)엔 당일 실측 개장괴리를 최우선 사용.
    #   (KIS 해외선물 권한 없음 → NQ 야간선물 시나리오는 종전대로 제거.)
    expected_open_valid = expected_open is not None

    if expected_open_valid:
        # 메인: 시장 동시호가 예상체결가를 '오늘의 예상 시가'로 최우선 채택.
        open_nav_track = expected_open
        predicted_open = int(round(expected_open / 5) * 5)
        # 예상 괴리율 = (예상시가 − 예측NAV)/예측NAV × 100 (저평가=할인, 고평가=프리미엄)
        open_discount = (expected_open - predicted_nav) / predicted_nav * 100 if predicted_nav else 0.0
        discount_basis = "예상시가 vs 예측NAV (시장 동시호가 antc_cnpr)"
        scenario_name = "KIS 예상체결가(antc_cnpr·시장 동시호가)"
        antc_ctrt = antc["antc_cntg_prdy_ctrt"]
    else:
        # 폴백: 괴리율 기반 개장 할인 모델 (동시호가 전·외 — 참고 추정).
        # 장중(live)엔 '오늘'의 실측 개장괴리가 곧 정답(같은 날 시가 예측). 장후(after)엔
        # 측정된 개장괴리는 '이미 지난 오늘'의 값이므로 익영업일 예측에 직접 쓰지 않고 캐시·cold-start로만 반영.
        live_open_dprt = measured_open_dprt if mode == "live" else None
        open_discount, discount_basis, open_vol_signal = estimate_open_discount(kis_dprt, today_open_dprt=live_open_dprt)
        open_nav_track = predicted_nav * (1 + open_discount / 100)
        predicted_open = int(round(open_nav_track / 5) * 5)
        scenario_name = "동시호가 전·외 — 시장 예상체결가 없음(참고 추정: 예측NAV × 괴리율모델)"
        antc_ctrt = None

    # 정밀 범위 밴드.
    #   antc 유효(시장 동시호가) 경로 → ±25원 고정 유지(시장이 만든 예상시가라 좁아도 됨).
    #   폴백(antc 없음)이면 괴리율 모델 추정이라 불확실성이 크다 → 변동성에 따라 밴드 확대:
    #     큰 이상치(|전일종가괴리|>5%) → 예측NAV×OPEN_BAND_VOL_RATIO(~±100원)
    #     그 외 폴백               → 예측NAV×OPEN_BAND_FB_RATIO (~±60원)
    #   ★ 변동성 신호 = 클리핑과 동일 기준(estimate_open_discount cold-start 가 쓴 '전일 종가괴리').
    #     중심 예측의 클리핑·범위 확대를 같은 신호로 일관화해야 6/23처럼 종가괴리만 큰 날도 넓은 밴드로 잡는다.
    #     신호가 None(캐시 평균·당일 실측 등 견고한 경로)이면 좁은 FB 밴드로 충분(룩어헤드 아님).
    #   (3일 실측 백테스트: 폴백 범위 적중 0/3 → 3/3.)
    if expected_open_valid:
        open_band = 25.0
    elif open_vol_signal is not None and abs(open_vol_signal) > OPEN_BAND_VOL_DPRT:
        open_band = max(25.0, open_nav_track * OPEN_BAND_VOL_RATIO)
    else:
        open_band = max(25.0, open_nav_track * OPEN_BAND_FB_RATIO)
    open_lower = int(round((open_nav_track - open_band) / 5) * 5)
    open_upper = int(round((open_nav_track + open_band) / 5) * 5)

    # 한줄 의견 — 경로별로 의견 신호를 다르게 한다.
    #   A) antc 유효: open_discount = (시장예상가−NAV)/NAV 라 '진짜 시장 신호'.
    #      NAV 대비 저평가/고평가 4구간을 그대로 분류하고 '(시장 동시호가 기준)' 표기.
    #   B) 폴백: open_discount 는 우리가 가정한 할인율(=clip(전일종가괴리)×비율)이라 자기참조적.
    #      대신 공정가치(예측NAV)의 전일대비 변화 nav_change 로 의견을 만든다(간밤 기초자산·환율
    #      반영한 펀더멘털 결과 → 자기참조 아님). 방향+강도 위주로 쓰고 '(시장 예상가 미확보·모델 추정)' 표기.
    if expected_open_valid:
        if open_discount <= -3.0:
            decision_msg = "시장 예상가가 공정가치(예측 NAV) 대비 큰 폭 저평가 — 큰 폭 할인 출발 (시장 동시호가 기준)."
        elif open_discount <= -1.0:
            decision_msg = "시장 예상가가 공정가치(예측 NAV) 대비 저평가 — 다소 낮게(할인) 출발 (시장 동시호가 기준)."
        elif open_discount >= 1.0:
            decision_msg = "시장 예상가가 공정가치(예측 NAV) 대비 고평가 — 다소 높게(프리미엄) 출발 (시장 동시호가 기준)."
        else:
            decision_msg = "시장 예상가가 공정가치(예측 NAV) 부근(괴리 작음)에서 출발 전망 (시장 동시호가 기준)."
    else:
        nav_change = (predicted_nav - base_nav) / base_nav * 100 if base_nav else 0.0
        if nav_change <= -3.0:
            decision_msg = "간밤 기초자산·환율 약세로 공정가치가 전일보다 크게 낮아짐 → 큰 폭 하락 출발 전망 (시장 예상가 미확보·모델 추정)."
        elif nav_change <= -1.0:
            decision_msg = "간밤 기초자산·환율 영향으로 공정가치가 다소 낮아짐 → 약세 출발 전망 (시장 예상가 미확보·모델 추정)."
        elif nav_change >= 3.0:
            decision_msg = "간밤 기초자산·환율 강세로 공정가치가 전일보다 크게 높아짐 → 큰 폭 상승 출발 전망 (시장 예상가 미확보·모델 추정)."
        elif nav_change >= 1.0:
            decision_msg = "간밤 기초자산·환율 영향으로 공정가치가 다소 높아짐 → 강세 출발 전망 (시장 예상가 미확보·모델 추정)."
        else:
            decision_msg = "간밤 기초자산·환율 변동이 작아 공정가치가 전일과 비슷함 → 보합 출발 전망 (시장 예상가 미확보·모델 추정)."

    print("-" * 50)
    if expected_open_valid:
        print(f"  [📈 {target_date_str} 개장 시가 — KIS 예상체결가(장전 동시호가) 기반]")
        print(f"  * 예상체결가는 09:00 확정 전까지 변동 가능(08:50경 더 정확).")
        print(f"  - 예상 시가(antc_cnpr) : {expected_open:>8,.0f}원  (전일대비 {antc_ctrt:+.2f}%)")
        print(f"  - 공정가치(예측 NAV)   : {predicted_nav:>8,.0f}원")
        print(f"  - 예상 괴리율          : {open_discount:>+7.2f}%  ({discount_basis})")
        print(f"  - 정밀 범위            : {open_lower:>8,.0f}원 ~ {open_upper:>8,.0f}원 (±25원)")
    else:
        print(f"  [📈 {target_date_str} 개장 시가 — 동시호가 전·외(참고 추정)]")
        print(f"  * 시장 예상체결가(antc_cnpr) 없음 → 괴리율 기반 개장 할인 모델로 참고 추정.")
        print(f"  * NQ 야간선물 시나리오는 KIS 해외선물 권한 미보유로 제거.")
        print(f"  - 공정가치(예측 NAV)   : {predicted_nav:>8,.0f}원")
        print(f"  - 추정 개장할인율      : {open_discount:>+7.2f}%  (근거: {discount_basis})")
        print(f"  - 예상 기준가          : {open_nav_track:>8,.0f}원")
        print(f"  - 정밀 범위            : {open_lower:>8,.0f}원 ~ {open_upper:>8,.0f}원 (±{open_band:,.0f}원)")

    print("-" * 50)
    print(f"  [🎯 최종 시가 예측 요약]")
    print(f"  - 오늘의 예측 시가   : {predicted_open:>8,.0f}원")
    print(f"  - 초정밀 범위 (±25원) : {open_lower:>8,.0f}원 ~ {open_upper:>8,.0f}원")
    print(f"  - 채택된 방식         : {scenario_name}")
    print(f"  - 분석 의견           : {decision_msg}")

    # 괴리율 정보 (KIS dprt 또는 (현재가-iNAV)/iNAV)
    print("-" * 50)
    if inav > 0 and etf_current > 0:
        calc_dprt = (etf_current - inav) / inav * 100
        print(f"  KIS 공시 괴리율    : {kis_dprt:>+.2f}%")
        print(f"  계산 괴리율(현재가-iNAV): {calc_dprt:>+.2f}%")
    print("=" * 50)

    # KakaoTalk notification (Strictly < 200 chars for PlayMCP limit)
    try:
        sorted_holdings = sorted(active_holdings.items(), key=lambda x: x[1], reverse=True)
        top_holdings = sorted_holdings[:3]
        holdings_parts = []
        for ticker, weight in top_holdings:
            ret = stock_returns.get(ticker, 0.0)
            p_to = latest_prices.get(ticker, 0.0)
            name = KOREAN_NAMES.get(ticker, ticker)
            
            # 한국 투자자 직관에 맞춰 상승은 빨간 삼각(🔺), 하락은 파란 삼각(🔻)으로 표시
            if ret > 0:
                emoji = "🔺"
            elif ret < 0:
                emoji = "🔻"
            else:
                emoji = "▫️"
            holdings_parts.append(f"{emoji} <b>{name}</b>: ${p_to:,.2f} ({ret:+.2f}%)")
        holdings_str = "\n".join(holdings_parts)
        
        # 오늘 날짜 및 시간 계산 (KST 기준)
        kst_tz = datetime.timezone(datetime.timedelta(hours=9))
        now_kst = datetime.datetime.now(kst_tz)
        date_header = f"{now_kst.year}년 {now_kst.month}월 {now_kst.day}일"

        # 추가 지표 및 이모지 연산
        price_diff = predicted_open - base_etf
        price_diff_pct = (price_diff / base_etf * 100) if base_etf else 0.0
        price_diff_dir = "🔺" if price_diff > 0 else "🔻" if price_diff < 0 else "▫️"
        price_diff_sign = "+" if price_diff > 0 else ""

        # 아침용 텔레그램 — 핵심만 깔끔하게: (상태헤더) / 전일대비 / 예상 시가 / 범위 / 의견 / 미국 종목.
        #   (공정가치·괴리율 '수치 줄'은 제거하고 신호는 '의견' 한 줄로. 예상 시가는 꼬리표 없이.)
        #   공정가치·괴리율 상세는 콘솔 출력에는 그대로 남는다.
        # auction-only(정상 예측)면 상단에 국내장·미국장 상태 한 줄을 붙인다(여기 왔다는 건 둘 다 정상).
        status_header = (market_status_line(True, True, compact=True) + "\n") if auction_only else ""
        telegram_msg = (
            f"<b>[{date_header}]</b>\n"
            f"{status_header}\n"
            f"📢 <b>[TIGER 미국우주테크]</b>\n"
            f"<b>ETF 시가 예측</b>\n\n"
            f"✨ 전일 종가({base_etf:,.0f}원) 대비\n"
            f"{price_diff_dir} {price_diff_sign}{price_diff:,.0f}원 ({price_diff_pct:+.2f}%)\n"
            f"🎯 <b>예상 시가 : <u>{predicted_open:,.0f}원</u></b>\n"
            f"🔍 <b>범위 : <code>{open_lower:,.0f}원 ~ {open_upper:,.0f}원</code> (±{open_band:,.0f}원)</b>\n\n"
            f"의견: {decision_msg}\n\n\n"
            f"🇺🇸 <b>주요 종목 종가 (등락률)</b>\n"
            f"{holdings_str}"
        )
        
        print(f"\n[텔레그램 전송 메시지 내용]\n{telegram_msg}\n")
        # --auction-only 전송 게이트. 하루 1회, 가능한 한 '진짜 예상체결가'로.
        #   ① 동시호가 창(08:30~09:00) + 유효 antc_cnpr → 시장 예상 시가로 전송(최우선).
        #   ② 창 안에서 폴링까지 수행한 '정시 주 실행'(auction_primary_attempted=True)이지만
        #      08:38 데드라인까지 antc_cnpr 를 못 받은 경우 → 폴백 추정으로 그 자리에서 전송.
        #      (GAS 단일 디스패치 1회 실행이므로 "다음 예약"을 기다리면 메시지가 오지 않는다.)
        #   ③ 창 종료(09:00 이후)인데 아직 미발송 → 뒤늦은 cron 백업 실행이 폴백으로 최후 1회 전송.
        #   ④ 창 이전(08:30 전) — wait_until_send_time() 이 처리하므로 사실상 도달 불가(안전장치만).
        send_hm = now_kst.hour * 100 + now_kst.minute
        after_auction_window = now_kst.weekday() < 5 and send_hm >= 900  # 평일 09:00 이후
        if no_telegram:
            print("  ℹ --no-telegram 옵션 지정으로 인해 텔레그램 전송이 생략되었습니다.")
        elif auction_only and read_auction_sent_date() == today_kst_str:
            # 전송 직전 마커 재확인(중복 방지 이중화). main() 초기 확인 이후 KIS 호출·대기 동안
            # 다른 실행이 먼저 보내 마커를 갱신했을 수 있으므로, 실제 전송 직전 한 번 더 막는다.
            print(f"  ℹ 전송 직전 재확인 — 오늘({today_kst_str}) 동시호가 메시지 이미 전송됨 → 전송 생략.")
        elif auction_only:
            _gate = decide_auction_send(expected_open_valid, auction_primary_attempted, after_auction_window)
            if _gate == "send_real":
                # ① 유효 antc_cnpr — 시장 예상 시가로 전송(최우선)
                if send_telegram_message(telegram_msg):
                    write_auction_sent_today(today_kst_str, us_date=d1)
            elif _gate == "send_fallback_primary":
                # ② 정시 주 실행이 폴링 끝까지 antc_cnpr 못 잡은 경우 → 폴백으로 그 자리에서 전송
                print("  ⏳ 폴링 후에도 예상체결가 미확보 → 폴백 추정으로 정시 전송(메시지 누락 방지).")
                if send_telegram_message(telegram_msg):
                    write_auction_sent_today(today_kst_str, us_date=d1)
            elif _gate == "send_fallback_late":
                # ③ 뒤늦은 cron 백업 실행 — 창 닫힌 후 도착한 실행이 최후 1회 전송
                print("  ⏳ 동시호가 창(08:30~09:00) 종료·예상체결가 못 받음 → 폴백 추정으로 최후 1회 전송(메시지 누락 방지).")
                if send_telegram_message(telegram_msg):
                    write_auction_sent_today(today_kst_str, us_date=d1)
            else:  # 'skip'
                # ④ 창 이전(08:30 전) 비정상 조기 실행 — wait 가 처리하므로 사실상 도달 불가
                print("  ⏳ 동시호가 창(08:30~09:00) 이전 — 대기 후 재진행 예정(조기 실행 안전장치).")
        else:
            send_telegram_message(telegram_msg)
    except Exception as e:
        print(f"  ❌ 텔레그램 전송 준비 중 오류 발생: {e}")


if __name__ == "__main__":
    main()