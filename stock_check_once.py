#!/usr/bin/env python3
"""
台股大單監控 v3
基於成交金額 + 流動性比例 + 外盤判斷（而非固定張數），並用 Jev 查證新聞

偵測邏輯：
  層級1（過濾垃圾）: 當日累計成交量 > 3000張, 當日成交值 > 2億
  層級2（抓主力）: 單筆成交額符合股價等級 / 區間量 > 均量 3倍 / 委買一檔 > 1000張
  層級3（確認發動）: 股價上升 + 大單 = 主動買進（外盤）
  新聞查證（選用）: 有警報的股票再用 Jev 判斷近期新聞，附在推播後面

v3 修正：
  - 不再使用 tlong（TWSE 的 tlong 是資料時間戳記，不是成交額）
    單筆成交額 = 成交價 × 當盤成交量(張) × 1000
    當日成交值 ≈ 成交價 × 累計成交量(張) × 1000
  - 「萬元」單位換算修正
  - 時間一律以台灣時間顯示（GitHub Actions 機器是 UTC）
  - 紅漲綠跌（台股慣例）
  - 同一檔股票的多個警報合併成一則訊息
"""

import os, json, time, logging, sys, html
from datetime import datetime
from pathlib import Path
from typing import List, Optional
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

# ── 環境變數設定 ─────────────────────────────────────────────────────
TZ        = ZoneInfo("Asia/Taipei")
TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
COOLDOWN  = int(os.environ.get("ALERT_COOLDOWN", "300"))
LARGE_BID_THRESHOLD = int(os.environ.get("LARGE_BID_THRESHOLD", "1000"))   # 張
VOLUME_SPIKE_RATIO  = float(os.environ.get("VOLUME_SPIKE_RATIO", "3.0"))
VOLUME_SPIKE_MIN    = int(os.environ.get("VOLUME_SPIKE_MIN", "200"))        # 區間量至少幾張
MIN_DAY_VOL         = 3000                                                  # 張
MIN_DAY_AMT         = 200_000_000                                           # 2 億元

STATE_FILE = Path("stock_state.json")

# ── 監控清單 ─────────────────────────────────────────────────────────
WATCHLIST_TSE = [
    "2330", "2454", "2317", "2308", "2412",
    "2882", "1301", "2002", "2303", "2891",
    "2886", "3711", "2379", "3034", "2357",
    "0050", "0056"
]
WATCHLIST_OTC = []
NO_NEWS_CHECK = {"0050", "0056"}   # ETF 不做個股新聞查證


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

    trade_vol = i(item.get("tv"))   # 當盤成交量（張）
    total_vol = i(item.get("v"))    # 當日累計成交量（張）

    return {
        "symbol":    item.get("c", ""),
        "name":      item.get("n", ""),
        "price":     price,
        "prev":      f(item.get("y")),
        "total_vol": total_vol,
        "trade_vol": trade_vol,
        "trade_amt": price * trade_vol * 1000,   # 單筆成交額（元）
        "day_amt":   price * total_vol * 1000,   # 當日成交值估算（元）
        "bid_vol1":  b_vols[0] if b_vols else 0,
        "bid_px1":   b_prices[0] if b_prices else 0.0,
        "ask_vol1":  a_vols[0] if a_vols else 0,
    }


# ── 動態門檻（依股價等級）─────────────────────────────────────────────
def get_thresholds(price: float) -> dict:
    """根據股價等級返回單筆成交額門檻"""
    if price < 50:        # 小型股
        return {"min_trade_amt": 500_000}      # 50 萬
    elif price < 300:     # 中型股
        return {"min_trade_amt": 1_000_000}    # 100 萬
    else:                 # 千金股
        return {"min_trade_amt": 3_000_000}    # 300 萬


# ── 偵測邏輯 ───────────────────────────────────────────────────────────
def detect_alerts(snap: dict, vol_hist: List[int], interval_vol: int) -> List[dict]:
    """
    三層過濾邏輯：
    1. 垃圾過濾: 當日成交量 > 3000張, 當日成交值 > 2億
    2. 主力抓取: 單筆成交額 / 區間量爆發 / 大買盤掛單
    3. 發動確認: 股價上升 + 大單 = 外盤主動買
    interval_vol: 本次與上次執行之間新增的成交量（張）；首次執行為 0
    """
    alerts = []
    price = snap["price"]
    pct = (price - snap["prev"]) / snap["prev"] * 100 if snap["prev"] > 0 else 0

    # ── 層級 1: 垃圾過濾 ──────────────────────────────────────────
    if snap["total_vol"] < MIN_DAY_VOL or snap["day_amt"] < MIN_DAY_AMT:
        return []

    # ── 層級 2: 主力大單偵測 ──────────────────────────────────────
    alerts_l2 = []
    thresholds = get_thresholds(price)

    # 2.1 單筆成交金額大單
    if snap["trade_amt"] >= thresholds["min_trade_amt"]:
        alerts_l2.append({
            "type": "TRADE_AMT",
            "emoji": "💰",
            "label": "大金額成交",
            "detail": f"單筆成交 {snap['trade_amt']/10_000:,.0f} 萬元（{snap['trade_vol']:,} 張）",
            "score": 1,
        })

    # 2.2 量能異常（本次區間量 vs 過去區間量平均）
    if vol_hist and interval_vol >= VOLUME_SPIKE_MIN:
        avg_vol = sum(vol_hist) / len(vol_hist)
        if avg_vol > 0 and interval_vol >= avg_vol * VOLUME_SPIKE_RATIO:
            alerts_l2.append({
                "type": "VOLUME_SPIKE",
                "emoji": "🚀",
                "label": "量能爆發",
                "detail": f"區間量 {interval_vol:,} 張（均量 {avg_vol:,.0f} 張的 {interval_vol/avg_vol:.1f} 倍）",
                "score": 1,
            })

    # 2.3 委買掛單（大買盤信號）
    if snap["bid_vol1"] >= LARGE_BID_THRESHOLD:
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

    if pct >= 0.1:  # 價格至少上升 0.1% → 視為外盤主動買
        for al in alerts_l2:
            al["is_outbound"] = True
            alerts.append(al)
    else:
        # 價格未明顯上升：只保留成交金額大單
        for al in alerts_l2:
            al["is_outbound"] = False
            if al["type"] == "TRADE_AMT":
                alerts.append(al)

    return alerts


# ── Telegram ─────────────────────────────────────────────────────────
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
        logger.error("Telegram 發送失敗: %s", e)
        return False


def format_alert(snap: dict, events: List[dict], pct: float, news_block: str = "") -> str:
    sign  = "+" if pct >= 0 else ""
    color = "🔴" if pct >= 0 else "🟢"      # 台股：紅漲綠跌
    outbound = "【外盤主動買】" if any(ev.get("is_outbound") for ev in events) else ""
    labels = "・".join(ev["label"] for ev in events)
    name = html.escape(snap["name"])

    lines = [
        f"{events[0]['emoji']} <b>【{labels}】{name}（{snap['symbol']}）{outbound}</b>",
        f"💵 現價 <b>{snap['price']:.2f}</b>　{color} {sign}{pct:.2f}%",
    ]
    lines += [f"{ev['emoji']} {ev['detail']}" for ev in events]
    lines.append(f"🕐 {datetime.now(TZ).strftime('%H:%M:%S')}")
    lines.append(f"🔗 <a href='https://tw.stock.yahoo.com/quote/{snap['symbol']}'>查看行情</a>")
    return "\n".join(lines) + news_block


# ── 主程式 ────────────────────────────────────────────────────────────
def main():
    logger.info("=== 台股大單監控 v3 ===")
    state     = load_state()
    cooldowns = state.get("_cooldowns", {})
    now_ts    = datetime.now(TZ).timestamp()
    today     = datetime.now(TZ).strftime("%Y-%m-%d")

    # 分批抓取
    all_snaps = []
    batch_size = 20
    n = max(len(WATCHLIST_TSE), len(WATCHLIST_OTC), 1)
    for i in range(0, n, batch_size):
        items = fetch_stocks(
            WATCHLIST_TSE[i:i+batch_size],
            WATCHLIST_OTC[i:i+batch_size]
        )
        for item in items:
            snap = parse_item(item)
            if snap:
                all_snaps.append(snap)
        if i + batch_size < n:
            time.sleep(1)

    new_state   = {}
    alert_count = 0

    for snap in all_snaps:
        sym       = snap["symbol"]
        sym_state = state.get(sym, {})
        # 跨日重置：昨天的累計量不能拿來減
        if sym_state.get("date") != today:
            sym_state = {}
        vol_hist  = sym_state.get("vol_hist", [])
        prev_vol  = sym_state.get("total_vol")

        interval_vol = snap["total_vol"] - prev_vol if prev_vol is not None else 0
        interval_vol = max(0, interval_vol)

        pct = (snap["price"] - snap["prev"]) / snap["prev"] * 100 if snap["prev"] > 0 else 0

        logger.info("  %s %-8s 價:%-8.2f 單筆:%6.0f萬 日量:%7d 委買1:%5d 區間量:%6d",
                    sym, snap["name"], snap["price"], snap["trade_amt"]/10_000,
                    snap["total_vol"], snap["bid_vol1"], interval_vol)

        alerts = detect_alerts(snap, vol_hist, interval_vol)

        # 先判斷完才把本次區間量加入歷史，避免自己跟自己比
        if interval_vol > 0:
            vol_hist = (vol_hist + [interval_vol])[-10:]
        new_state[sym] = {"date": today, "total_vol": snap["total_vol"], "vol_hist": vol_hist}

        fresh = []
        for ev in alerts:
            ckey = f"{sym}_{ev['type']}"
            if now_ts - cooldowns.get(ckey, 0) >= COOLDOWN:
                fresh.append(ev)
                cooldowns[ckey] = now_ts
                logger.info("  ↳ [ALERT] %s: %s", ev["type"], ev["detail"])
        if not fresh:
            continue

        news_block = ""
        if sym not in NO_NEWS_CHECK:
            result = check_news(sym, snap["name"])
            news_block = format_news_block(result, pct)
            if result:
                logger.info("  ↳ [NEWS] %s: %s（相關 %d／%d 則）", sym, result["verdict"],
                            result.get("n_relevant", 0), result.get("n_news", 0))

        send_telegram(format_alert(snap, fresh, pct, news_block))
        alert_count += 1

    new_state["_cooldowns"] = {k: v for k, v in cooldowns.items() if now_ts - v < 86400}
    save_state(new_state)
    logger.info("偵測完成，推播 %d 則\n", alert_count)


if __name__ == "__main__":
    main()
