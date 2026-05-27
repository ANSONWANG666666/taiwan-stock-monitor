#!/usr/bin/env python3
"""
台股大單監控 v2 - 專業級版本
基於成交金額 + 流動性比例 + 外盤判斷（而非固定張數）
符合職業盤手標準

偵測邏輯：
  層級1（過濾垃圾）: 成交量 > 3000張, 成交值 > 2億
  層級2（抓主力）: 單筆成交 > 均量 3倍, 成交額 > 100~1000萬（依股價等級）
  層級3（確認發動）: 股價上升 + 大單 = 主動買進（外盤）
"""

import os, json, time, logging, sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

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

# ── 環境變數設定 ─────────────────────────────────────────────────────
TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
COOLDOWN  = 300

STATE_FILE = Path("stock_state.json")

# ── 監控清單 ─────────────────────────────────────────────────────────
WATCHLIST_TSE = [
    "2330", "2454", "2317", "2308", "2412",
    "2882", "1301", "2002", "2303", "2891",
    "2886", "3711", "2379", "3034", "2357",
    "0050", "0056"
]
WATCHLIST_OTC = []


# ── 狀態存取 ───────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── TWSE API ─────────────────────────────────────────────────────────
def fetch_stocks(tse: List[str], otc: List[str]) -> list:
    parts = [f"tse_{s}.tw" for s in tse] + [f"otc_{s}.tw" for s in otc]
    if not parts:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
        "Referer":    "https://mis.twse.com.tw/stock/index.jsp",
        "Accept":     "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    # 重試 3 次
    for attempt in range(3):
        try:
            sess = requests.Session()
            sess.headers.update(headers)

            # Warm up session
            sess.get("https://mis.twse.com.tw/stock/index.jsp", timeout=10)
            time.sleep(0.5)

            r = sess.get(
                "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                params={"ex_ch": "|".join(parts), "json": "1", "delay": "0",
                        "_": int(time.time() * 1000)},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json().get("msgArray", [])
            if data:
                logger.info("TWSE API 成功（嘗試 %d）", attempt + 1)
                return data
        except Exception as e:
            logger.warning("TWSE API 失敗（嘗試 %d/3）: %s", attempt + 1, str(e)[:100])
            if attempt < 2:
                time.sleep(2)  # 重試前等待 2 秒

    logger.error("TWSE API 連接失敗，已重試 3 次")
    return []


def parse_item(item: dict) -> Optional[dict]:
    def f(v, d=0.0):
        try:   return float(v) if v and v not in ("-", "--") else d
        except: return d
    def i(v, d=0):
        try:   return int(float(v)) if v and v not in ("-", "--") else d
        except: return d

    price = f(item.get("z"))
    if price <= 0:
        return None

    b_vols   = [i(x) for x in item.get("g", "0").split("_") if x]
    b_prices = [f(x) for x in item.get("b", "0").split("_") if x]
    a_vols   = [i(x) for x in item.get("f", "0").split("_") if x]

    trade_vol = i(item.get("tv"))

    # 優先使用 tlong 字段（TWSE API 直接提供的成交額，單位：千元），降級到計算值
    tlong = i(item.get("tlong", 0))
    if tlong > 0:
        trade_amt = tlong * 1000  # 成交額（由千元轉換為元）
        logger.debug(f"使用 tlong 字段: {item.get('c')} 成交額={trade_amt/1_000_000:.1f}M")
    else:
        trade_amt = price * trade_vol * 1000  # 降級：計算成交金額（元）

    return {
        "symbol":    item.get("c", ""),
        "name":      item.get("n", ""),
        "price":     price,
        "prev":      f(item.get("y")),
        "total_vol": i(item.get("v")),
        "trade_vol": trade_vol,
        "trade_amt": trade_amt,
        "bid_vol1":  b_vols[0] if b_vols else 0,
        "bid_px1":   b_prices[0] if b_prices else 0.0,
        "ask_vol1":  a_vols[0] if a_vols else 0,
    }


# ── 動態門檻（依股價等級）─────────────────────────────────────────────
def get_thresholds(price: float) -> dict:
    """根據股價等級返回動態門檻（Skills 第三段）"""
    if price < 50:  # 小型股
        return {
            "min_trade_amt": 500_000,     # 50 萬
            "min_daily_vol": 3000,         # 日均量門檻
            "daily_vol_ratio": 0.3,        # 日均量比例 0.3%
        }
    elif price < 300:  # 中型股
        return {
            "min_trade_amt": 1_000_000,    # 100 萬
            "min_daily_vol": 3000,
            "daily_vol_ratio": 0.15,       # 日均量比例 0.15%
        }
    else:  # 千金股
        return {
            "min_trade_amt": 3_000_000,    # 300 萬
            "min_daily_vol": 3000,
            "daily_vol_ratio": 0.1,
        }


# ── 偵測邏輯 ───────────────────────────────────────────────────────────
def detect_alerts(snap: dict, vol_hist: List[int], prev_snap: dict) -> List[dict]:
    """
    三層過濾邏輯：
    1. 垃圾過濾: 成交量 > 3000張, 成交值 > 2億
    2. 主力抓取: 單筆成交 > 均量 3倍, 成交額符合等級
    3. 發動確認: 股價上升 + 大單 = 外盤主動買
    """
    alerts = []
    sym = snap["symbol"]
    name = snap["name"]
    price = snap["price"]
    pct = (price - snap["prev"]) / snap["prev"] * 100 if snap["prev"] > 0 else 0

    thresholds = get_thresholds(price)

    # ── 層級 1: 垃圾過濾 ──────────────────────────────────────────
    daily_vol_ratio = snap["total_vol"] / 50000  # 簡化: 假設日均量 5 萬張

    # 成交金額門檻：大於等級對應的最小金額，或成交額 > 1000 萬
    min_amt = thresholds["min_trade_amt"]
    if snap["total_vol"] < 3000 or (snap["trade_amt"] < min_amt and snap["trade_amt"] < 10_000_000):
        return []  # 不符合基本條件，跳過

    # ── 層級 2: 主力大單偵測 ──────────────────────────────────────
    alerts_l2 = []

    # 2.1 成交金額大單
    if snap["trade_amt"] >= thresholds["min_trade_amt"]:
        alerts_l2.append({
            "type": "TRADE_AMT",
            "emoji": "💰",
            "label": "大金額成交",
            "detail": f"單筆成交 {snap['trade_amt']/1_000_000:.1f} 萬元 ({snap['trade_vol']} 張)",
            "score": 1,
        })

    # 2.2 量能異常（相對均量）
    if vol_hist:
        avg_vol = sum(vol_hist) / len(vol_hist)
        vol_ratio = snap["total_vol"] - (prev_snap.get("total_vol", 0) if prev_snap else snap["total_vol"] - snap["trade_vol"])

        if avg_vol > 0 and vol_ratio >= avg_vol * 3:
            alerts_l2.append({
                "type": "VOLUME_SPIKE",
                "emoji": "🚀",
                "label": "量能爆發",
                "detail": f"區間量 {vol_ratio:.0f} 張（均量 {avg_vol:.0f} 張的 {vol_ratio/avg_vol:.1f} 倍）",
                "score": 1,
            })

    # 2.3 委買掛單（大買盤信號）
    if snap["bid_vol1"] >= 1000:
        alerts_l2.append({
            "type": "BID_QUEUE",
            "emoji": "📍",
            "label": "大買盤掛單",
            "detail": f"委買一檔 {snap['bid_px1']:.2f} × {snap['bid_vol1']:,} 張",
            "score": 0.8,
        })

    # ── 層級 3: 發動確認（外盤判斷）─────────────────────────────────
    if not alerts_l2:
        return []

    # 價格上升的大單 = 主動買進（外盤）
    if pct >= 0.1:  # 價格至少上升 0.1%
        for al in alerts_l2:
            al["emoji"] = "🔴" if al["type"] == "TRADE_AMT" else al["emoji"]
            al["is_outbound"] = True
            alerts.append(al)
    else:
        # 價格未明顯上升的大單記錄但優先級低
        for al in alerts_l2:
            al["is_outbound"] = False
            if al["type"] == "TRADE_AMT":  # 成交金額最優先
                alerts.append(al)

    return alerts


# ── Telegram ─────────────────────────────────────────────────────────
def send_telegram(text: str):
    if not TOKEN or not CHAT_ID:
        logger.warning("Telegram 未設定，略過")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        logger.error("Telegram 發送失敗: %s", e)

def format_alert(snap: dict, ev: dict, pct: float) -> str:
    sign  = "+" if pct >= 0 else ""
    color = "🟢" if pct >= 0 else "🔴"
    outbound = "【外盤主動買】" if ev.get("is_outbound") else ""

    return (
        f"{ev['emoji']} <b>【{ev['label']}】{snap['name']}（{snap['symbol']}）{outbound}</b>\n"
        f"💵 現價 <b>{snap['price']:.2f}</b>　{color} {sign}{pct:.2f}%\n"
        f"📦 {ev['detail']}\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"🔗 <a href='https://tw.stock.yahoo.com/quote/{snap['symbol']}'>查看行情</a>"
    )


# ── 主程式 ────────────────────────────────────────────────────────────
def main():
    logger.info("=== 台股大單監控 v2（專業級）===")
    state     = load_state()
    cooldowns = state.get("_cooldowns", {})
    now_ts    = datetime.now().timestamp()

    # 分批抓取
    all_snaps = []
    batch_size = 20

    for i in range(0, max(len(WATCHLIST_TSE), 1), batch_size):
        items = fetch_stocks(
            WATCHLIST_TSE[i:i+batch_size],
            WATCHLIST_OTC[i:i+batch_size]
        )
        for item in items:
            snap = parse_item(item)
            if snap:
                all_snaps.append(snap)
        if i + batch_size < len(WATCHLIST_TSE):
            time.sleep(1)

    new_state   = {}
    alert_count = 0

    for snap in all_snaps:
        sym       = snap["symbol"]
        sym_state = state.get(sym, {})
        vol_hist  = sym_state.get("vol_hist", [])
        prev_snap = sym_state.get("prev_snap")
        prev_vol  = sym_state.get("total_vol", 0)

        # 更新量能歷史
        delta = snap["total_vol"] - prev_vol
        if delta > 0:
            vol_hist = (vol_hist + [delta])[-10:]

        pct = (snap["price"] - snap["prev"]) / snap["prev"] * 100 if snap["prev"] > 0 else 0

        new_state[sym] = {
            "total_vol": snap["total_vol"],
            "vol_hist":  vol_hist,
            "prev_snap": snap,
        }

        logger.info("  %s %-8s 價:%-8.2f 成交金:%-10.0f 委買1:%5d",
                    sym, snap["name"], snap["price"],
                    snap["trade_amt"]/1_000_000, snap["bid_vol1"])

        alerts = detect_alerts(snap, vol_hist, prev_snap)
        for ev in alerts:
            ckey = f"{sym}_{ev['type']}"
            if now_ts - cooldowns.get(ckey, 0) >= COOLDOWN:
                logger.info("  ↳ [ALERT] %s: %s", ev["type"], ev["detail"])
                send_telegram(format_alert(snap, ev, pct))
                cooldowns[ckey] = now_ts
                alert_count += 1

    new_state["_cooldowns"] = cooldowns
    save_state(new_state)
    logger.info("偵測完成，觸發 %d 個警報\n", alert_count)


if __name__ == "__main__":
    main()
