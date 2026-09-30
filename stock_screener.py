#!/usr/bin/env python3
"""
台股「量價齊揚剛起漲」選股程式 v5
三層推播架構：09:30 早盤觀察 → 13:00 盤中確認 → 14:00 收盤確認
TWSE 日K歷史（STOCK_DAY）+ 盤中即時資料 + Jev 新聞查證

v5：選股規則改為「量價齊揚剛起漲」（見「訊號檢測」段落）。
    舊版三個條件中，連漲 4 天必然使「近 3 日上升」成立，等於連漲就直接得 2 分，已移除。

v4 修正：
  - 時段判斷改用台灣時間（GitHub Actions 機器是 UTC，原本 hour==9 永遠不成立）
  - 時段改用區間判斷，GitHub 排程延遲 10～30 分鐘也不會漏推
  - 一天只有一根日K（盤中重複執行會覆寫當日那根，不再一天塞 3 筆）
  - 成交量改用當日累計量 v（原本用當盤量 tv）
  - 每天第一次執行時從 TWSE 補近兩個月日K，第一天就有足夠歷史
  - 不再使用 tlong（TWSE 的 tlong 是時間戳記，不是成交額）
  - 快取與推播狀態由 workflow 保存（見 .github/workflows/stock_screener.yml）
"""

import os, json, time, logging, sys, html
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

import journal
from news_check import check_news, format_news_block

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

TZ       = ZoneInfo("Asia/Taipei")
TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
CACHE_FILE  = Path("screener_kline_cache.json")
STATUS_FILE = Path("screener_status.json")   # 記錄推播狀態
KEEP_BARS   = 60
MIN_DAY_AMT = 50_000_000                      # 當日成交值低於 5000 萬視為流動性不足

STOCKS = [
    "2330", "2454", "2317", "2308", "2412",
    "2882", "1301", "2002", "2303", "2891",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept": "application/json",
}


def now_tw() -> datetime:
    return datetime.now(TZ)


# ── TWSE 即時 API ───────────────────────────────────────────────
def fetch_stocks_from_twse(symbols: List[str]) -> Dict:
    """從 TWSE 即時 API 取得股票數據"""
    parts = [f"tse_{s}.tw" for s in symbols]
    if not parts:
        return {}

    for attempt in range(3):
        try:
            sess = requests.Session()
            sess.headers.update({**HEADERS, "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
            sess.get("https://mis.twse.com.tw/stock/index.jsp", timeout=10)
            time.sleep(0.5)

            r = sess.get(
                "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                params={"ex_ch": "|".join(parts), "json": "1", "delay": "0",
                        "_": int(time.time() * 1000)},
                timeout=15,
            )
            r.raise_for_status()
            return {item["c"]: item for item in r.json().get("msgArray", [])}
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
                continue
            logger.error("TWSE API 失敗: %s", str(e)[:80])
            return {}


# ── TWSE 日K歷史（STOCK_DAY）────────────────────────────────────
def _num(s: str) -> Optional[float]:
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def fetch_month_daily(symbol: str, year: int, month: int) -> List[dict]:
    """抓單月日K；回傳 [{date, close, volume(張)}]"""
    params = {"date": f"{year}{month:02d}01", "stockNo": symbol, "response": "json"}
    urls = ["https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
            "https://www.twse.com.tw/exchangeReport/STOCK_DAY"]
    for url in urls:
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            js = r.json()
            if js.get("stat") != "OK":
                continue
            fields = js.get("fields", [])
            i_date = fields.index("日期") if "日期" in fields else 0
            i_vol = fields.index("成交股數") if "成交股數" in fields else 1
            i_close = fields.index("收盤價") if "收盤價" in fields else 6
            bars = []
            for row in js.get("data", []):
                y, m, d = row[i_date].strip().split("/")
                close, vol = _num(row[i_close]), _num(row[i_vol])
                if close is None or vol is None:
                    continue
                bars.append({"date": f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}",
                             "close": close, "volume": int(vol // 1000)})
            return bars
        except Exception as e:
            logger.debug("%s STOCK_DAY %s 失敗: %s", symbol, url, e)
    return []


def refresh_history(cache: Dict, symbols: List[str]):
    """每天第一次執行時補近兩個月日K（覆寫同日期資料）"""
    today = now_tw().date()
    months = [(today.year, today.month)]
    prev = today.replace(day=1) - timedelta(days=1)
    months.insert(0, (prev.year, prev.month))
    for sym in symbols:
        entry = cache.setdefault(sym, {"klines": []})
        if entry.get("hist_date") == str(today):
            continue
        bars = []
        for y, m in months:
            bars += fetch_month_daily(sym, y, m)
            time.sleep(0.6)   # TWSE 有頻率限制
        if bars:
            merged = {k["date"]: k for k in entry["klines"]}
            merged.update({b["date"]: b for b in bars})
            entry["klines"] = sorted(merged.values(), key=lambda k: k["date"])[-KEEP_BARS:]
            entry["hist_date"] = str(today)
            logger.info("%s 補日K %d 根（共 %d 根）", sym, len(bars), len(entry["klines"]))
        else:
            logger.warning("%s 無法取得日K歷史，沿用快取 %d 根", sym, len(entry["klines"]))


# ── 狀態管理 ────────────────────────────────────────────────────
def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_daily_status() -> Dict:
    """讀取推播狀態；換日時重置（保留前一日正式入選結果）"""
    today = now_tw().strftime("%Y-%m-%d")
    status = load_json(STATUS_FILE, {})
    if status.get("date") != today:
        status = {
            "date": today,
            "pushed_0930": {},
            "pushed_1300": {},
            "pushed_1400": {},
            "formal_candidates": status.get("formal_candidates", {}),
        }
    for k in ("pushed_0930", "pushed_1300", "pushed_1400", "formal_candidates"):
        status.setdefault(k, {})
    return status


def parse_live(item: Dict) -> Optional[dict]:
    """解析即時資料；價格無效或流動性不足時回傳 None"""
    try:
        price = float(item.get("z", 0))
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:   # 最近一筆沒有成交價（"-"）時改用開盤價以外的資料不可靠，直接跳過
        return None
    try:
        vol = int(float(item.get("v", 0)))   # 當日累計成交量（張）
    except (TypeError, ValueError):
        return None
    if vol <= 0 or price * vol * 1000 < MIN_DAY_AMT:
        return None
    return {"close": price, "volume": vol,
            "prev": float(item.get("y") or 0) if item.get("y") not in (None, "-") else 0.0}


def update_today_bar(cache: Dict, twse_data: Dict):
    """用即時資料建立或覆寫「今天」這一根日K"""
    today = now_tw().strftime("%Y-%m-%d")
    for symbol, item in twse_data.items():
        live = parse_live(item)
        entry = cache.setdefault(symbol, {"klines": []})
        entry["name"] = item.get("n", entry.get("name", ""))
        if not live:
            logger.debug("%s: 即時資料不完整或流動性不足，今日K棒不更新", symbol)
            continue
        klines = [k for k in entry["klines"] if k["date"] != today]
        klines.append({"date": today, "close": live["close"], "volume": live["volume"]})
        entry["klines"] = klines[-KEEP_BARS:]


# ── 訊號檢測：量價齊揚剛起漲 ─────────────────────────────────────
# 必要條件（全部符合才評分）：
#   量比 >= VOL_RATIO_MIN   今日（預估全日）成交量 / 前 20 日均量
#   漲幅 >= PCT_MIN         今日收盤 vs 昨日收盤
#   連漲 <= MAX_STREAK      已經連漲太多天就不算「剛」起漲，避免追高
# 加分（0–3 分，>= PUSH_SCORE 才推播）：
#   突破      收盤 > 前 20 日最高收盤
#   強量      量比 >= STRONG_VOL
#   低檔起漲  昨收距前 20 日最低收盤 <= NEAR_LOW_PCT（發動前還在低檔整理，不是漲一大段後）
VOL_RATIO_MIN = float(os.environ.get("VOL_RATIO_MIN", "2.0"))
PCT_MIN       = float(os.environ.get("PCT_MIN", "1.0"))
MAX_STREAK    = int(os.environ.get("MAX_STREAK", "5"))
STRONG_VOL    = float(os.environ.get("STRONG_VOL", "3.0"))
NEAR_LOW_PCT  = float(os.environ.get("NEAR_LOW_PCT", "15"))
PUSH_SCORE    = int(os.environ.get("PUSH_SCORE", "2"))
BASE_DAYS     = 20
MIN_BASE_DAYS = 10
TRADING_MIN   = 270          # 09:00–13:30


def volume_fraction(t: datetime) -> float:
    """盤中已經過的交易時間比例，用來把盤中累計量換算成全日預估量。
    早盤成交量通常偏大，線性換算會高估，所以 09:30 的訊號只當「低置信度」。"""
    minutes = (t.hour - 9) * 60 + t.minute
    return min(1.0, max(0.1, minutes / TRADING_MIN))


def up_streak(klines: List[dict]) -> int:
    n = 0
    for i in range(len(klines) - 1, 0, -1):
        if klines[i]["close"] > klines[i - 1]["close"]:
            n += 1
        else:
            break
    return n


def evaluate_signal(klines: List[dict], now: Optional[datetime] = None) -> Dict:
    now = now or now_tw()
    empty = {"qualified": False, "gate": False, "score": 0, "vol_ratio": 0.0, "pct": 0.0,
             "streak": 0, "breakout": False, "strong_vol": False, "near_low": False,
             "rise_from_low": 0.0, "projected": False, "reason": "日K不足",
             "date": now.strftime("%Y-%m-%d %H:%M")}
    if len(klines) < MIN_BASE_DAYS + 1:
        return empty

    today, hist = klines[-1], klines[:-1]
    base = hist[-BASE_DAYS:]
    avg_vol = sum(k["volume"] for k in base) / len(base)
    projected = today["date"] == now.strftime("%Y-%m-%d") and volume_fraction(now) < 1.0
    vol = today["volume"] / volume_fraction(now) if projected else today["volume"]
    vol_ratio = vol / avg_vol if avg_vol > 0 else 0.0

    close, prev_close = today["close"], hist[-1]["close"]
    pct = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    high20 = max(k["close"] for k in base)
    low20 = min(k["close"] for k in base)
    # 起漲位置看「今天發動前」：用昨收算，否則漲停當天自己的漲幅會把低檔起漲判掉
    rise_from_low = (prev_close / low20 - 1) * 100 if low20 else 0.0
    streak = up_streak(klines)

    breakout = close > high20
    strong_vol = vol_ratio >= STRONG_VOL
    near_low = rise_from_low <= NEAR_LOW_PCT

    reasons = []
    if vol_ratio < VOL_RATIO_MIN:
        reasons.append(f"量比 {vol_ratio:.1f} < {VOL_RATIO_MIN:g}")
    if pct < PCT_MIN:
        reasons.append(f"漲幅 {pct:.1f}% < {PCT_MIN:g}%")
    if streak > MAX_STREAK:
        reasons.append(f"已連漲 {streak} 天")
    gate = not reasons
    score = int(breakout) + int(strong_vol) + int(near_low) if gate else 0
    return {
        "qualified": gate and score >= PUSH_SCORE, "gate": gate, "score": score,
        "vol_ratio": round(vol_ratio, 2), "pct": round(pct, 2), "streak": streak,
        "breakout": breakout, "strong_vol": strong_vol, "near_low": near_low,
        "rise_from_low": round(rise_from_low, 1), "projected": projected,
        "reason": "；".join(reasons), "date": now.strftime("%Y-%m-%d %H:%M"),
    }


# ── Telegram ────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    if not TOKEN or not CHAT_ID:
        logger.warning("Telegram 未設定，略過")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        if not r.ok:
            logger.error("Telegram 回應 %s: %s", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        logger.error("Telegram 失敗: %s", e)
        return False


SLOT_HEADER = {
    "0930": ("🔍", "監察清單｜低置信度", "<i>早盤出現強勢異動，僅供觀察，尚未收盤確認</i>"),
    "1300": ("📌", "中場更新｜中置信度", "<i>仍維持強勢，等待收盤確認</i>"),
    "1400": ("✅", "正式入選｜高置信度", "📊 收盤資料確認符合條件，可列入正式觀察清單"),
    "test": ("🧪", "測試推播｜不是真實訊號", "<i>手動測試：不論評分高低都會推播，也不寫入推播紀錄</i>"),
    "scan": ("📡", "全市場掃描｜收盤確認", "📊 上市股收盤資料符合「量價齊揚剛起漲」"),
}


def format_signal(slot: str, symbol: str, name: str, s: Dict, pct: float, news_block: str) -> str:
    emoji, title, note = SLOT_HEADER[slot]
    sign = "+" if pct >= 0 else ""
    color = "🔴" if pct >= 0 else "🟢"      # 台股：紅漲綠跌
    ok = lambda b: "✅" if b else "▫️"
    vol_note = "（盤中預估全日量）" if s.get("projected") else ""
    gate = "" if s.get("gate") else f"\n  ⛔ 未達必要條件：{html.escape(s.get('reason', ''))}"
    return (
        f"{emoji} <b>【{title}】{html.escape(name)}（{symbol}）</b>\n"
        f"{note}\n"
        f"💵 今日 {color} {sign}{pct:.2f}%　量比 {s['vol_ratio']}x{vol_note}\n"
        f"📊 起漲評分: {s['score']}/3{gate}\n"
        f"  {ok(s['breakout'])} 突破前 20 日高點\n"
        f"  {ok(s['strong_vol'])} 強量（量比 ≥ {STRONG_VOL:g}）\n"
        f"  {ok(s['near_low'])} 低檔起漲（發動前距 20 日低點 +{s['rise_from_low']}%）\n"
        f"  連漲 {s['streak']} 天\n"
        f"🕐 {s['date']}\n"
        f"🔗 <a href='https://tw.stock.yahoo.com/quote/{symbol}'>查看行情</a>"
        f"{news_block}"
    )


# ── 時段判斷（台灣時間，容許排程延遲）───────────────────────────
def current_slot(t: datetime) -> Optional[str]:
    if t.weekday() >= 5:
        return None
    minutes = t.hour * 60 + t.minute
    if 9 * 60 + 15 <= minutes < 12 * 60:
        return "0930"
    if 12 * 60 + 45 <= minutes < 13 * 60 + 50:
        return "1300"
    if 13 * 60 + 50 <= minutes < 16 * 60:
        return "1400"
    return None


# ── 測試模式：強制推播一檔股票（不寫入推播狀態）───────────────────
def run_test(symbol: str) -> int:
    """手動執行時指定 TEST_SYMBOL，強制推播該股票並實際呼叫 Jev 查新聞。"""
    logger.info("=== 測試模式：強制推播 %s ===", symbol)
    live_raw = fetch_stocks_from_twse([symbol]).get(symbol, {})
    name = live_raw.get("n", "") or symbol
    live = parse_live(live_raw) or {}
    prev = live.get("prev") or 0
    pct = (live["close"] - prev) / prev * 100 if live and prev else 0.0
    if not live_raw:
        logger.warning("%s：TWSE 即時資料取不到（上櫃股票目前不支援），漲跌幅以 0 顯示", symbol)

    cache = load_json(CACHE_FILE, {})
    klines = cache.get(symbol, {}).get("klines", [])
    if len(klines) < 5:
        today = now_tw().date()
        prev_m = today.replace(day=1) - timedelta(days=1)
        for y, m in ((prev_m.year, prev_m.month), (today.year, today.month)):
            klines += fetch_month_daily(symbol, y, m)
            time.sleep(0.6)
        klines = sorted({k["date"]: k for k in klines}.values(), key=lambda k: k["date"])
        logger.info("%s：臨時抓取日K %d 根（不寫入快取）", symbol, len(klines))
    today_str = now_tw().strftime("%Y-%m-%d")
    if live:   # 把今天的即時資料併進日K，和正式流程一致
        klines = [k for k in klines if k["date"] != today_str] + [
            {"date": today_str, "close": live["close"], "volume": live["volume"]}]
    ev = evaluate_signal(klines)

    result = check_news(symbol, name, use_symbol_cache=False)
    if result is None:
        diag = "⚙️ 未設定 TYPESAFE_API_KEY，沒有做新聞查證"
    else:
        diag = (f"⚙️ 診斷：Jev 呼叫 {result.get('jev_calls', 0)} 次・新聞 {result.get('n_news', 0)} 則"
                f"・相關 {result.get('n_relevant', 0)} 則・結論 {result['verdict']}")
        if result.get("message"):
            diag += f"\n⚙️ 錯誤：{html.escape(result['message'])}"
        if result.get("errors"):
            diag += f"\n⚙️ Jev 錯誤：{html.escape(result['errors'][0])}"
    logger.info(diag.replace("⚙️ ", ""))

    msg = (format_signal("test", symbol, name, ev, pct, format_news_block(result, pct))
           + "\n\n" + diag)
    ok = send_telegram(msg)
    logger.info("Telegram 推播%s", "成功" if ok else "失敗")
    return 0 if ok else 1


# ── 主程式 ──────────────────────────────────────────────────────
def main():
    test_symbol = os.environ.get("TEST_SYMBOL", "").strip()
    if test_symbol:
        sys.exit(run_test(test_symbol))

    t = now_tw()
    slot = os.environ.get("SCREENER_SLOT") or current_slot(t)   # SCREENER_SLOT 可手動指定（測試用）
    logger.info("=== 四訊號選股 %s（台灣時間 %s）===", slot or "非推播時段", t.strftime("%Y-%m-%d %H:%M"))

    cache = load_json(CACHE_FILE, {})
    refresh_history(cache, STOCKS)

    twse_data = fetch_stocks_from_twse(STOCKS)
    if not twse_data:
        logger.error("無法取得 TWSE 即時數據")
        save_json(CACHE_FILE, cache)
        return
    update_today_bar(cache, twse_data)
    save_json(CACHE_FILE, cache)

    candidates = {}
    for symbol in STOCKS:
        klines = cache.get(symbol, {}).get("klines", [])
        if len(klines) < 5:
            logger.info("%s: 日K只有 %d 根，資料不足", symbol, len(klines))
            continue
        ev = evaluate_signal(klines)
        live = parse_live(twse_data.get(symbol, {})) or {}
        prev = live.get("prev") or 0
        pct = (live["close"] - prev) / prev * 100 if live and prev else 0.0
        logger.info("%s: 評分 %d 量比 %.2f 漲幅 %.2f%% 突破 %s 強量 %s 低檔 %s 連漲 %d %s", symbol,
                    ev["score"], ev["vol_ratio"], ev["pct"], ev["breakout"], ev["strong_vol"],
                    ev["near_low"], ev["streak"], ev["reason"])
        if ev["qualified"]:
            candidates[symbol] = {"score": ev["score"], "eval": ev, "pct": pct,
                                  "price": live.get("close", 0.0) if live else 0.0,
                                  "name": cache[symbol].get("name", "")}

    if not slot:
        logger.info("非推播時段，只更新快取")
        return

    status = load_daily_status()
    to_push = []
    for symbol in sorted(candidates):
        c = candidates[symbol]
        if slot == "0930":
            if symbol not in status["pushed_0930"]:
                to_push.append(symbol)
        elif slot == "1300":
            prev_score = max(status["pushed_0930"].get(symbol, 0), status["pushed_1300"].get(symbol, 0))
            if symbol not in status["pushed_1300"] and (
                    symbol not in status["pushed_0930"] or c["score"] > prev_score):
                to_push.append(symbol)
        else:  # 1400
            if symbol not in status["pushed_1400"]:
                to_push.append(symbol)

    formal = {}
    for symbol in to_push:
        c = candidates[symbol]
        news = check_news(symbol, c["name"])
        msg = format_signal(slot, symbol, c["name"], c["eval"], c["pct"], format_news_block(news, c["pct"]))
        if send_telegram(msg):
            journal.record("screener", symbol, c["name"], slot=slot, price=c.get("price", 0.0),
                           pct=c["pct"], ev=c["eval"], news=news)
            status[f"pushed_{slot}"][symbol] = c["score"]
            logger.info("✓ 推播 %s: %s", slot, symbol)
            if slot == "1400":
                formal[symbol] = {"score": c["score"], "eval": c["eval"]}

    if formal:
        status["formal_candidates"] = formal
    save_json(STATUS_FILE, status)
    logger.info("本次推播 %d 檔", len(to_push))


if __name__ == "__main__":
    main()
