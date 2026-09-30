#!/usr/bin/env python3
"""
台股主升浪前夜選股程式 v4
三層推播架構：09:30 早盤觀察 → 13:00 盤中確認 → 14:00 收盤確認
TWSE 日K歷史（STOCK_DAY）+ 盤中即時資料 + Jev 新聞查證

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


# ── 訊號檢測 ───────────────────────────────────────────────────
def detect_consecutive_gain(klines: List[dict]) -> int:
    """訊號: 連續上漲天數（>= 4 才算）"""
    consecutive = 0
    for i in range(len(klines) - 1, 0, -1):
        if klines[i]["close"] > klines[i - 1]["close"]:
            consecutive += 1
        else:
            break
    return consecutive if consecutive >= 4 else 0


def detect_volume_expansion(klines: List[dict]) -> float:
    """訊號: 近 3 日均量 / 前 20 日均量（>= 2 才算）"""
    if len(klines) < 8:
        return 0.0
    recent = klines[-3:]
    base = klines[-23:-3]
    avg_recent = sum(k["volume"] for k in recent) / len(recent)
    avg_base = sum(k["volume"] for k in base) / len(base)
    if avg_base <= 0:
        return 0.0
    ratio = avg_recent / avg_base
    return ratio if ratio >= 2.0 else 0.0


def detect_upward_trend(klines: List[dict]) -> bool:
    """訊號: 近 3 日上升趨勢"""
    if len(klines) < 3:
        return False
    return klines[-1]["close"] > klines[-3]["close"]


def evaluate_signal(klines: List[dict]) -> Dict:
    consecutive = detect_consecutive_gain(klines)
    volume_ratio = detect_volume_expansion(klines)
    uptrend = detect_upward_trend(klines)
    score = sum([consecutive >= 4, volume_ratio >= 2.0, uptrend])
    return {
        "score": int(score),
        "consecutive_gain": consecutive,
        "volume_ratio": round(volume_ratio, 2),
        "uptrend": uptrend,
        "date": now_tw().strftime("%Y-%m-%d %H:%M"),
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
}


def format_signal(slot: str, symbol: str, name: str, s: Dict, pct: float, news_block: str) -> str:
    emoji, title, note = SLOT_HEADER[slot]
    sign = "+" if pct >= 0 else ""
    color = "🔴" if pct >= 0 else "🟢"      # 台股：紅漲綠跌
    return (
        f"{emoji} <b>【{title}】{html.escape(name)}（{symbol}）</b>\n"
        f"{note}\n"
        f"💵 今日 {color} {sign}{pct:.2f}%\n"
        f"📊 評分: {s['score']}/3\n"
        f"  連陽: {s['consecutive_gain']} 天\n"
        f"  量倍: {s['volume_ratio']}x（近3日 / 前20日）\n"
        f"  趨勢: {'↑ 上升' if s['uptrend'] else '→ 持平'}\n"
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


# ── 主程式 ──────────────────────────────────────────────────────
def main():
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
        logger.info("%s: 評分 %d 連陽 %d 量倍 %.2f 趨勢 %s", symbol, ev["score"],
                    ev["consecutive_gain"], ev["volume_ratio"], ev["uptrend"])
        if ev["score"] >= 2:
            candidates[symbol] = {"score": ev["score"], "eval": ev, "pct": pct,
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
        news_block = format_news_block(check_news(symbol, c["name"]), c["pct"])
        msg = format_signal(slot, symbol, c["name"], c["eval"], c["pct"], news_block)
        if send_telegram(msg):
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
