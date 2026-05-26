#!/usr/bin/env python3
"""
台股主升浪前夜選股程式 v3
三層推播架構：09:30 早盤觀察 → 13:00 盤中確認 → 14:00 收盤確認
TWSE 實時數據 + 本地歷史快取
"""

import os, json, time, logging, sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
CACHE_FILE = Path("screener_kline_cache.json")
STATUS_FILE = Path("screener_status.json")  # 記錄推播狀態

# ── TWSE API ────────────────────────────────────────────────────
def fetch_stocks_from_twse(symbols: List[str]) -> Dict:
    """從 TWSE API 取得股票數據"""
    parts = [f"tse_{s}.tw" for s in symbols]
    if not parts:
        return {}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
        "Referer": "https://mis.twse.com.tw/stock/index.jsp",
        "Accept": "application/json",
    }

    for attempt in range(3):
        try:
            sess = requests.Session()
            sess.headers.update(headers)
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
            logger.error("TWSE API 失敗: %s", str(e)[:50])
            return {}

# ── 狀態管理 ────────────────────────────────────────────────────
def load_cache() -> Dict:
    """讀取本地 K 線快取"""
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except:
            pass
    return {}

def save_cache(cache: Dict):
    """存儲本地 K 線快取"""
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def load_status() -> Dict:
    """讀取推播狀態（去重用）"""
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except:
            pass
    return {"today": {}}

def save_status(status: Dict):
    """存儲推播狀態"""
    STATUS_FILE.write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def reset_daily_status():
    """每天重置推播狀態（用在 09:30）"""
    today = datetime.now().strftime("%Y-%m-%d")
    status = load_status()
    if status.get("date") != today:
        # 保留昨日的正式入選結果供參考，今日重新開始
        yesterday_formal = status.get("formal_candidates", {})
        status = {
            "date": today,
            "pushed_0930": {},
            "pushed_1300": {},
            "pushed_1400": {},
            "formal_candidates": yesterday_formal,  # 保留前日結果
        }
        save_status(status)
    return status

def validate_twse_data_quality(item: Dict, symbol: str) -> bool:
    """驗證 TWSE 數據品質
    檢查：成交量是否為 0（數據延遲信號）
         價格跳躍是否異常（數據錯誤信號）
         成交額是否過低（流動性不足）
    """
    try:
        vol = int(float(item.get("tv", 0)))
        price = float(item.get("z", 0))
        amount = int(float(item.get("tlong", 0)))  # 成交額（單位：千元）

        # 檢查 1: 成交量為 0（通常表示數據延遲或無交易）
        if vol == 0:
            logger.debug(f"{symbol}: 成交量為0，數據可能延遲")
            return False

        # 檢查 2: 成交額過低（流動性不足）
        if amount < 50000:  # 50M 以下可能流動性不足
            logger.debug(f"{symbol}: 成交額 {amount/1000:.1f}M 過低，流動性不足")
            return False

        # 檢查 3: 價格有效性（基本檢查）
        if price <= 0:
            logger.debug(f"{symbol}: 價格無效")
            return False

        return True
    except Exception as e:
        logger.debug(f"{symbol}: 數據驗證異常 {e}")
        return False

def update_cache_with_twse(cache: Dict, twse_data: Dict):
    """用 TWSE 實時數據更新快取"""
    today = datetime.now().strftime("%Y-%m-%d")

    for symbol, item in twse_data.items():
        if symbol not in cache:
            cache[symbol] = {"klines": []}

        # 驗證數據品質
        if not validate_twse_data_quality(item, symbol):
            logger.debug(f"{symbol}: 數據未通過品質檢查，跳過此筆")
            continue

        price = float(item.get("z", 0))
        vol = int(float(item.get("tv", 0)))

        if price > 0:
            cache[symbol]["klines"].append({
                "date": today,
                "close": price,
                "volume": vol,
            })
            cache[symbol]["klines"] = cache[symbol]["klines"][-30:]

    return cache

# ── 四大訊號檢測 ───────────────────────────────────────────────
def detect_consecutive_gain(klines: List[dict]) -> int:
    """訊號: 連續漲幅"""
    if not klines or len(klines) < 2:
        return 0

    consecutive = 0
    for i in range(len(klines) - 1, 0, -1):
        if klines[i]["close"] > klines[i - 1]["close"]:
            consecutive += 1
        else:
            break

    return consecutive if consecutive >= 4 else 0

def detect_volume_expansion(klines: List[dict]) -> float:
    """訊號: 成交量放倍"""
    if not klines or len(klines) < 4:
        return 0.0

    recent_3d = sum(k["volume"] for k in klines[-3:]) / 3 if len(klines[-3:]) else 0
    avg_20d = sum(k["volume"] for k in klines[-20:]) / min(20, len(klines)) if klines else 0

    if avg_20d > 0:
        ratio = recent_3d / avg_20d
        return ratio if ratio >= 2.0 else 0.0

    return 0.0

def detect_upward_trend(klines: List[dict]) -> bool:
    """訊號: 近 3 日上升趨勢"""
    if not klines or len(klines) < 3:
        return False

    recent = klines[-3:]
    return recent[-1]["close"] > recent[0]["close"]

# ── 評估 ────────────────────────────────────────────────────────
def evaluate_signal(klines: List[dict]) -> Dict:
    """評估股票訊號"""
    consecutive = detect_consecutive_gain(klines)
    volume_ratio = detect_volume_expansion(klines)
    uptrend = detect_upward_trend(klines)

    score = sum([
        1 if consecutive >= 4 else 0,
        1 if volume_ratio >= 2.0 else 0,
        1 if uptrend else 0,
    ])

    return {
        "score": score,
        "consecutive_gain": consecutive,
        "volume_ratio": round(volume_ratio, 2),
        "uptrend": uptrend,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# ── Telegram ────────────────────────────────────────────────────
def send_telegram(text: str):
    if not TOKEN or not CHAT_ID:
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        return True
    except Exception as e:
        logger.error("Telegram 失敗: %s", e)
        return False

def format_signal_0930(symbol: str, eval_result: Dict) -> str:
    """09:30 監察清單｜低置信度 格式"""
    s = eval_result
    return (
        f"🔍 <b>【監察清單｜低置信度】{symbol}</b>\n"
        f"⚠️  <i>早盤出現強勢異動，僅供觀察，尚未收盤確認</i>\n"
        f"📊 評分: {s['score']}/3\n"
        f"  連陽: {s['consecutive_gain']} 根\n"
        f"  量倍: {s['volume_ratio']}x\n"
        f"  趨勢: {'↑ 上升' if s['uptrend'] else '→ 持平'}\n"
        f"🕐 {s['date']}\n"
        f"🔗 <a href='https://tw.stock.yahoo.com/quote/{symbol}'>查看行情</a>"
    )

def format_signal_1300(symbol: str, eval_result: Dict) -> str:
    """13:00 中場更新｜中置信度 格式"""
    s = eval_result
    return (
        f"📌 <b>【中場更新｜中置信度】{symbol}</b>\n"
        f"⚠️  <i>仍維持強勢，成交量與價格條件改善，等待收盤確認</i>\n"
        f"📊 評分: {s['score']}/3\n"
        f"  連陽: {s['consecutive_gain']} 根\n"
        f"  量倍: {s['volume_ratio']}x\n"
        f"  趨勢: {'↑ 上升' if s['uptrend'] else '→ 持平'}\n"
        f"🕐 {s['date']}\n"
        f"🔗 <a href='https://tw.stock.yahoo.com/quote/{symbol}'>查看行情</a>"
    )

def format_signal_1400(symbol: str, eval_result: Dict) -> str:
    """14:00 正式入選｜高置信度 格式"""
    s = eval_result
    return (
        f"✅ <b>【正式入選｜高置信度】{symbol}</b>\n"
        f"📊 收盤資料確認符合週線主升浪條件，可列入正式觀察清單\n"
        f"📈 最終評分: {s['score']}/3\n"
        f"  連陽: {s['consecutive_gain']} 根\n"
        f"  量倍: {s['volume_ratio']}x\n"
        f"  趨勢: {'↑ 上升' if s['uptrend'] else '→ 持平'}\n"
        f"🕐 {s['date']}\n"
        f"🔗 <a href='https://tw.stock.yahoo.com/quote/{symbol}'>查看行情</a>"
    )

# ── 主程式 ──────────────────────────────────────────────────────
def main():
    # 判斷當前時段
    now = datetime.now()
    hour = now.hour
    minute = now.minute
    current_time = f"{hour:02d}:{minute:02d}"

    stocks = [
        "2330", "2454", "2317", "2308", "2412",
        "2882", "1301", "2002", "2303", "2891",
    ]

    # 取得 TWSE 實時數據
    twse_data = fetch_stocks_from_twse(stocks)
    if not twse_data:
        logger.error("無法取得 TWSE 數據")
        return

    # 更新本地快取
    cache = load_cache()
    cache = update_cache_with_twse(cache, twse_data)
    save_cache(cache)

    # 評估訊號
    candidates = {}
    for symbol in stocks:
        if symbol not in cache:
            continue

        klines = cache[symbol]["klines"]
        if len(klines) < 3:
            continue

        eval_result = evaluate_signal(klines)

        if eval_result["score"] >= 2:
            candidates[symbol] = {
                "score": eval_result["score"],
                "eval": eval_result,
            }

    # ── 09:30 早盤異動推播 ──────────────────────────────────────
    if hour == 9 and 25 <= minute <= 35:
        logger.info("=== 09:30 早盤異動掃描 ===")
        status = reset_daily_status()  # 重置每日狀態

        for symbol in sorted(candidates.keys()):
            msg = format_signal_0930(symbol, candidates[symbol]["eval"])
            if send_telegram(msg):
                status["pushed_0930"][symbol] = candidates[symbol]["score"]
                logger.info("✓ 推播 09:30: %s", symbol)

        save_status(status)

    # ── 13:00 盤中確認推播 ──────────────────────────────────────
    elif hour == 13 and 0 <= minute <= 10:
        logger.info("=== 13:00 盤中確認掃描 ===")
        status = load_status()

        for symbol in sorted(candidates.keys()):
            # 只在新進榜或強度提高時推播
            prev_score = status.get("pushed_0930", {}).get(symbol, 0)
            curr_score = candidates[symbol]["score"]

            if symbol not in status.get("pushed_0930", {}) or curr_score > prev_score:
                msg = format_signal_1300(symbol, candidates[symbol]["eval"])
                if send_telegram(msg):
                    status["pushed_1300"][symbol] = curr_score
                    logger.info("✓ 推播 13:00: %s (prev:%d curr:%d)", symbol, prev_score, curr_score)

        save_status(status)

    # ── 14:00 後 收盤確認推播 ──────────────────────────────────
    elif hour >= 14 and hour < 15:
        logger.info("=== 14:00 收盤確認掃描 ===")
        status = load_status()

        # 推播所有符合條件的股票（今日首次推播）
        # 同時更新「正式訊號紀錄」（只有此時段會寫入）
        formal_candidates = {}
        for symbol in sorted(candidates.keys()):
            if symbol not in status.get("pushed_1400", {}):
                msg = format_signal_1400(symbol, candidates[symbol]["eval"])
                if send_telegram(msg):
                    status["pushed_1400"][symbol] = candidates[symbol]["score"]
                    # ✓ 只有 14:00 的結果才寫入正式訊號紀錄
                    formal_candidates[symbol] = {
                        "score": candidates[symbol]["score"],
                        "eval": candidates[symbol]["eval"],
                    }
                    logger.info("✓ 推播 14:00: %s (加入正式觀察清單)", symbol)

        # 更新正式訊號紀錄（僅在 14:00）
        if formal_candidates:
            status["formal_candidates"] = formal_candidates
        save_status(status)

    else:
        logger.info(f"[{current_time}] 非推播時段，更新快取完成")


if __name__ == "__main__":
    main()
