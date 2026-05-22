#!/usr/bin/env python3
"""
台股大單監控 - GitHub Actions 單次執行版
由 GitHub Actions 每 5 分鐘呼叫一次，偵測完畢即結束
狀態（歷史量能）透過 stock_state.json 在每次執行間保存
"""

import os, json, time, logging, sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

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

# ── 從環境變數讀取設定（GitHub Secrets）───────────────────────────
TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TRADE_TH  = int(os.environ.get("LARGE_TRADE_THRESHOLD", "500"))
BID_TH    = int(os.environ.get("LARGE_BID_THRESHOLD", "1000"))
SPIKE_R   = float(os.environ.get("VOLUME_SPIKE_RATIO", "3.0"))
SPIKE_MIN = int(os.environ.get("VOLUME_SPIKE_MIN", "200"))
COOLDOWN  = 300  # 同一股票同類型警報冷卻秒數

STATE_FILE = Path("stock_state.json")

# ── 監控清單 ───────────────────────────────────────────────────────
WATCHLIST_TSE = [
    "2330", "2454", "2317", "2308", "2412",
    "2882", "1301", "2002", "2303", "2891",
    "2886", "3711", "2379", "3034", "2357",
    "0050", "0056"
]
WATCHLIST_OTC = []


# ── 狀態存取 ────────────────────────────────────────────────────────
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


# ── TWSE API ────────────────────────────────────────────────────────
def fetch_stocks(tse: List[str], otc: List[str]) -> list:
    parts = [f"tse_{s}.tw" for s in tse] + [f"otc_{s}.tw" for s in otc]
    if not parts:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer":    "https://mis.twse.com.tw/stock/index.jsp",
    }
    sess = requests.Session()
    sess.headers.update(headers)
    try:
        sess.get("https://mis.twse.com.tw/stock/index.jsp", timeout=10)
        r = sess.get(
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
            params={"ex_ch": "|".join(parts), "json": "1", "delay": "0",
                    "_": int(time.time() * 1000)},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("msgArray", [])
    except Exception as e:
        logger.error("TWSE API 失敗: %s", e)
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

    return {
        "symbol":    item.get("c", ""),
        "name":      item.get("n", ""),
        "price":     price,
        "prev":      f(item.get("y")),
        "total_vol": i(item.get("v")),
        "trade_vol": i(item.get("tv")),
        "bid_vol1":  b_vols[0]   if b_vols   else 0,
        "bid_px1":   b_prices[0] if b_prices else 0.0,
    }


# ── 偵測邏輯 ─────────────────────────────────────────────────────────
def detect_alerts(snap: dict, vol_hist: List[int], prev_total_vol: int) -> List[dict]:
    alerts = []
    price  = snap["price"]
    pct    = (price - snap["prev"]) / snap["prev"] * 100 if snap["prev"] > 0 else 0

    base = {"symbol": snap["symbol"], "name": snap["name"], "price": price, "pct": pct}

    # 1. 大單成交
    if snap["trade_vol"] >= TRADE_TH:
        alerts.append({**base,
            "type": "LARGE_TRADE", "emoji": "🔴", "label": "大單成交",
            "detail": f"最新一筆成交 {snap['trade_vol']:,} 張",
        })

    # 2. 量能爆發
    delta = snap["total_vol"] - prev_total_vol
    if delta > 0 and vol_hist:
        avg = sum(vol_hist) / len(vol_hist)
        if avg > 0 and delta >= avg * SPIKE_R and delta >= SPIKE_MIN:
            alerts.append({**base,
                "type": "VOLUME_SPIKE", "emoji": "🚀", "label": "量能爆發",
                "detail": f"區間量 {delta:,} 張（均量 {avg:.0f} 張的 {delta/avg:.1f} 倍）",
            })

    # 3. 大買盤掛單
    if snap["bid_vol1"] >= BID_TH:
        alerts.append({**base,
            "type": "LARGE_BID", "emoji": "💰", "label": "大買盤掛單",
            "detail": f"委買一檔 {snap['bid_px1']:.2f} × {snap['bid_vol1']:,} 張",
        })

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

def format_alert(ev: dict) -> str:
    sign  = "+" if ev["pct"] >= 0 else ""
    color = "🟢" if ev["pct"] >= 0 else "🔴"
    return (
        f"{ev['emoji']} <b>【{ev['label']}】{ev['name']}（{ev['symbol']}）</b>\n"
        f"💵 現價 <b>{ev['price']:.2f}</b>　{color} {sign}{ev['pct']:.2f}%\n"
        f"📦 {ev['detail']}\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"🔗 <a href='https://tw.stock.yahoo.com/quote/{ev['symbol']}'>查看行情</a>"
    )


# ── 主程式 ────────────────────────────────────────────────────────────
def main():
    logger.info("台股大單偵測開始（%s）", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    state     = load_state()
    cooldowns = state.get("_cooldowns", {})
    now_ts    = datetime.now().timestamp()

    # 分批抓取（每批最多 20 支）
    all_snaps = []
    batch_size = 20
    tse_list   = WATCHLIST_TSE
    otc_list   = WATCHLIST_OTC

    for i in range(0, max(len(tse_list), 1), batch_size):
        items = fetch_stocks(tse_list[i:i+batch_size], otc_list[i:i+batch_size])
        for item in items:
            snap = parse_item(item)
            if snap:
                all_snaps.append(snap)
        if i + batch_size < len(tse_list):
            time.sleep(1)

    new_state      = {}
    alert_count    = 0

    for snap in all_snaps:
        sym       = snap["symbol"]
        sym_state = state.get(sym, {})
        vol_hist  = sym_state.get("vol_hist", [])
        prev_vol  = sym_state.get("total_vol", 0)

        # 更新量能歷史
        delta = snap["total_vol"] - prev_vol
        if delta > 0:
            vol_hist = (vol_hist + [delta])[-10:]

        new_state[sym] = {
            "total_vol": snap["total_vol"],
            "vol_hist":  vol_hist,
        }

        logger.info("  %s %-6s 價:%-8.2f 累量:%7d 張  委買1:%5d 張",
                    sym, snap["name"], snap["price"],
                    snap["total_vol"], snap["bid_vol1"])

        alerts = detect_alerts(snap, vol_hist, prev_vol)
        for ev in alerts:
            ckey = f"{sym}_{ev['type']}"
            if now_ts - cooldowns.get(ckey, 0) >= COOLDOWN:
                logger.info("  ↳ [ALERT] %s %s", ev["type"], ev["detail"])
                send_telegram(format_alert(ev))
                cooldowns[ckey] = now_ts
                alert_count += 1

    new_state["_cooldowns"] = cooldowns
    save_state(new_state)
    logger.info("偵測完成，觸發 %d 個警報", alert_count)


if __name__ == "__main__":
    main()
