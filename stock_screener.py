#!/usr/bin/env python3
"""
台股主升浪前夜選股程式 v2
使用 TWSE 實時數據 + 本地歷史快取
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

def update_cache_with_twse(cache: Dict, twse_data: Dict):
    """用 TWSE 實時數據更新快取"""
    today = datetime.now().strftime("%Y-%m-%d")

    for symbol, item in twse_data.items():
        if symbol not in cache:
            cache[symbol] = {"klines": []}

        price = float(item.get("z", 0))
        vol = int(float(item.get("tv", 0)))

        if price > 0:
            cache[symbol]["klines"].append({
                "date": today,
                "close": price,
                "volume": vol,
            })

            # 保留最近 30 日
            cache[symbol]["klines"] = cache[symbol]["klines"][-30:]

    return cache

# ── 四大訊號檢測 ───────────────────────────────────────────────
def detect_consecutive_gain(klines: List[dict]) -> int:
    """訊號2: 連續漲幅（相比前一日）"""
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
    """訊號4: 成交量放倍"""
    if not klines or len(klines) < 4:
        return 0.0

    recent_3d = sum(k["volume"] for k in klines[-3:]) / 3 if len(klines[-3:]) else 0
    avg_20d = sum(k["volume"] for k in klines[-20:]) / min(20, len(klines)) if klines else 0

    if avg_20d > 0:
        ratio = recent_3d / avg_20d
        return ratio if ratio >= 2.0 else 0.0

    return 0.0

def detect_upward_trend(klines: List[dict]) -> bool:
    """簡化訊號：近 3 日上漲趨勢"""
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
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        logger.error("Telegram 失敗: %s", e)

def format_signal(symbol: str, eval_result: Dict) -> str:
    s = eval_result
    emoji = "🚀" if s["score"] >= 3 else "📈"

    return (
        f"{emoji} <b>【強勢訊號】{symbol}</b>\n"
        f"📊 訊號評分: {s['score']}/3\n"
        f"  ✅ 連陽: {s['consecutive_gain']} 根\n"
        f"  ✅ 量倍: {s['volume_ratio']}x\n"
        f"  ✅ 上升: {'是' if s['uptrend'] else '否'}\n"
        f"🕐 {s['date']}\n"
        f"🔗 <a href='https://tw.stock.yahoo.com/quote/{symbol}'>查看行情</a>"
    )

# ── 主程式 ──────────────────────────────────────────────────────
def main():
    logger.info("=== 台股強勢訊號選股 ===")

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
    candidates = []
    for symbol in stocks:
        if symbol not in cache:
            continue

        klines = cache[symbol]["klines"]
        if len(klines) < 3:
            continue

        eval_result = evaluate_signal(klines)

        if eval_result["score"] >= 2:
            candidates.append({
                "symbol": symbol,
                "score": eval_result["score"],
                "eval": eval_result,
            })
            logger.info("  🚀 %s - 訊號評分: %d/3", symbol, eval_result["score"])

    # 推播
    if candidates:
        candidates.sort(key=lambda x: x["score"], reverse=True)
        for cand in candidates[:5]:
            msg = format_signal(cand["symbol"], cand["eval"])
            send_telegram(msg)
            logger.info("已推播: %s", cand["symbol"])
    else:
        logger.info("未發現符合訊號條件的標的")


if __name__ == "__main__":
    main()
