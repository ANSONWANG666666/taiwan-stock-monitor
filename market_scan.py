#!/usr/bin/env python3
"""
全市場掃描：每天收盤後掃描所有上市普通股，找出「量價齊揚剛起漲」的股票

流程：
  1. 從 TWSE「每日收盤行情」(MI_INDEX) 一次取得全部上市股票當日收盤
     第一次執行時往前回補約 25 個交易日，之後每天只加一天
  2. 用 stock_screener.evaluate_signal（與盤中選股同一套規則）評分
  3. 符合條件的股票依分數與量比排序，前 MAX_DETAIL 檔逐檔推播並用 Jev 查新聞，
     其餘列在一則摘要訊息（不呼叫 Jev，控制費用）

環境變數：
  SCAN_DATE   指定掃描日期 YYYYMMDD（手動補跑用）；留空為今天
  FORCE_SCAN  設為 1 時，同一天已推播過也重新推播
  MAX_DETAIL  逐檔推播並查新聞的最大檔數（預設 10）
"""

import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from news_check import check_news, format_news_block
from stock_screener import evaluate_signal, format_signal, send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
CACHE_FILE = Path("market_cache.json")
STATE_FILE = Path("market_scan_state.json")

KEEP_BARS = 30              # 每檔保留的日K根數（評分需要 21 根）
BOOTSTRAP_DAYS = 25         # 首次執行回補的交易日數
MIN_DAY_AMT = int(os.environ.get("MIN_DAY_AMT", "50000000"))   # 當日成交值 < 5000 萬不看
MAX_DETAIL = int(os.environ.get("MAX_DETAIL", "10"))
SUMMARY_MAX = 40            # 摘要最多列幾檔（Telegram 單則上限 4096 字）
REQUEST_GAP = 3.0           # TWSE 有頻率限制，每次請求間隔（秒）

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept": "application/json",
}


# ── TWSE 每日收盤行情 ────────────────────────────────────────────
def _num(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _find_stock_table(js: dict):
    """回傳 (fields, rows)；支援新版 tables 格式與舊版 fields9/data9 格式"""
    for t in js.get("tables", []) or []:
        f = t.get("fields") or []
        if "證券代號" in f and "收盤價" in f:
            return f, t.get("data") or []
    for k, v in js.items():
        if k.startswith("fields") and isinstance(v, list) and "證券代號" in v and "收盤價" in v:
            return v, js.get("data" + k[len("fields"):], [])
    return None, []


def is_common_stock(code: str) -> bool:
    """只看 4 碼普通股；排除 00 開頭的 ETF / 受益憑證"""
    return len(code) == 4 and code.isdigit() and not code.startswith("00")


def fetch_market_day(date_str: str) -> Optional[Dict[str, dict]]:
    """取得某一天全部上市股票收盤；非交易日或尚未公布回傳 None
    回傳 {code: {name, close, volume(張), amount(元)}}"""
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    params = {"date": date_str, "type": "ALLBUT0999", "response": "json"}
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            js = r.json()
            break
        except Exception as e:
            logger.warning("MI_INDEX %s 失敗（%d/3）: %s", date_str, attempt + 1, str(e)[:80])
            time.sleep(REQUEST_GAP * 2)
    else:
        return None
    if js.get("stat") != "OK":
        return None
    fields, rows = _find_stock_table(js)
    if not fields:
        logger.warning("MI_INDEX %s 找不到個股收盤表格", date_str)
        return None
    ix = {name: fields.index(name) for name in ("證券代號", "證券名稱", "成交股數", "成交金額", "收盤價")}
    out = {}
    for row in rows:
        code = str(row[ix["證券代號"]]).strip()
        if not is_common_stock(code):
            continue
        close, shares, amount = (_num(row[ix[k]]) for k in ("收盤價", "成交股數", "成交金額"))
        if not close or shares is None:
            continue   # 當天無成交（收盤價 "--"）
        out[code] = {"name": str(row[ix["證券名稱"]]).strip(), "close": close,
                     "volume": int(shares // 1000), "amount": amount or 0.0}
    return out or None


# ── 快取 ─────────────────────────────────────────────────────────
def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def add_day(cache: dict, date_iso: str, day: Dict[str, dict]):
    """把一天的全市場資料併入快取：cache = {"days": [...], "stocks": {code: {"name", "bars": [[date, close, vol]]}}}"""
    stocks = cache.setdefault("stocks", {})
    for code, d in day.items():
        entry = stocks.setdefault(code, {"name": d["name"], "bars": []})
        entry["name"] = d["name"]
        bars = [b for b in entry["bars"] if b[0] != date_iso]
        bars.append([date_iso, d["close"], d["volume"]])
        bars.sort(key=lambda b: b[0])
        entry["bars"] = bars[-KEEP_BARS:]
    days = set(cache.get("days", [])) | {date_iso}
    cache["days"] = sorted(days)[-KEEP_BARS:]
    # 清掉已下市或長期停牌（最後一根超過快取期間）的股票
    oldest = cache["days"][0]
    for code in [c for c, e in stocks.items() if e["bars"][-1][0] < oldest]:
        del stocks[code]


def bootstrap(cache: dict, scan_date: datetime):
    """快取交易日不足時，從掃描日往前回補"""
    have = set(cache.get("days", []))
    need = BOOTSTRAP_DAYS - len([d for d in have if d < scan_date.strftime("%Y-%m-%d")])
    if need <= 0:
        return
    logger.info("快取只有 %d 個交易日，往前回補約 %d 天", len(have), need)
    d, tries = scan_date - timedelta(days=1), 0
    while need > 0 and tries < BOOTSTRAP_DAYS * 2:
        tries += 1
        iso = d.strftime("%Y-%m-%d")
        if d.weekday() < 5 and iso not in have:
            day = fetch_market_day(d.strftime("%Y%m%d"))
            time.sleep(REQUEST_GAP)
            if day:
                add_day(cache, iso, day)
                need -= 1
                logger.info("  回補 %s：%d 檔", iso, len(day))
        d -= timedelta(days=1)


# ── 掃描 ─────────────────────────────────────────────────────────
def scan(cache: dict, date_iso: str, today: Dict[str, dict]) -> List[dict]:
    at_close = datetime.fromisoformat(date_iso).replace(hour=14, minute=30, tzinfo=TZ)
    hits = []
    for code, d in today.items():
        if d["amount"] < MIN_DAY_AMT:
            continue
        bars = cache["stocks"].get(code, {}).get("bars", [])
        klines = [{"date": b[0], "close": b[1], "volume": b[2]} for b in bars if b[0] <= date_iso]
        if not klines or klines[-1]["date"] != date_iso:
            continue
        ev = evaluate_signal(klines, at_close)
        if ev["qualified"]:
            hits.append({"code": code, "name": d["name"], "ev": ev, "amount": d["amount"]})
    hits.sort(key=lambda h: (h["ev"]["score"], h["ev"]["vol_ratio"]), reverse=True)
    return hits


def format_summary(date_iso: str, n_all: int, n_liquid: int, hits: List[dict], detail_n: int) -> str:
    lines = [f"📡 <b>全市場掃描｜{date_iso} 收盤</b>",
             f"上市普通股 {n_all:,} 檔 → 成交值 ≥ {MIN_DAY_AMT/1e8:g} 億 {n_liquid:,} 檔 → "
             f"<b>符合剛起漲 {len(hits)} 檔</b>"]
    if not hits:
        lines.append("今天沒有股票符合條件。")
        return "\n".join(lines)
    if detail_n:
        lines.append(f"前 {detail_n} 檔逐檔推播並附 Jev 新聞查證。")
    rest = hits[detail_n:]
    if rest:
        lines.append("\n<b>其他符合條件（未查新聞）</b>")
        for h in rest[:SUMMARY_MAX]:
            e = h["ev"]
            lines.append(f"• {html.escape(h['name'])} {h['code']}　{e['score']}/3　"
                         f"🔴 +{e['pct']:.1f}%　量比 {e['vol_ratio']:.1f}x")
        if len(rest) > SUMMARY_MAX:
            lines.append(f"…另有 {len(rest) - SUMMARY_MAX} 檔（詳見 Actions log）")
    return "\n".join(lines)


def main():
    now = datetime.now(TZ)
    scan_date = datetime.strptime(os.environ["SCAN_DATE"], "%Y%m%d").replace(tzinfo=TZ) \
        if os.environ.get("SCAN_DATE") else now
    date_iso = scan_date.strftime("%Y-%m-%d")
    force = os.environ.get("FORCE_SCAN") == "1"
    logger.info("=== 全市場掃描 %s ===", date_iso)

    state = load_json(STATE_FILE, {})
    if state.get("last_pushed") == date_iso and not force:
        logger.info("%s 已經推播過，略過（手動重跑請設定 FORCE_SCAN=1）", date_iso)
        return

    today = fetch_market_day(scan_date.strftime("%Y%m%d"))
    if not today:
        logger.warning("%s 收盤資料尚未公布或非交易日，稍後排程會再試", date_iso)
        return
    logger.info("%s 上市普通股 %d 檔", date_iso, len(today))

    cache = load_json(CACHE_FILE, {})
    add_day(cache, date_iso, today)
    bootstrap(cache, scan_date)
    save_json(CACHE_FILE, cache)

    n_liquid = sum(1 for d in today.values() if d["amount"] >= MIN_DAY_AMT)
    hits = scan(cache, date_iso, today)
    logger.info("成交值合格 %d 檔，符合條件 %d 檔", n_liquid, len(hits))
    for h in hits:
        e = h["ev"]
        logger.info("  %s %-6s %d/3 漲 %.2f%% 量比 %.2f 突破 %s 強量 %s 低檔 %s（+%.1f%%）",
                    h["code"], h["name"], e["score"], e["pct"], e["vol_ratio"],
                    e["breakout"], e["strong_vol"], e["near_low"], e["rise_from_low"])

    detail = hits[:MAX_DETAIL]
    ok = send_telegram(format_summary(date_iso, len(today), n_liquid, hits, len(detail)))
    for h in detail:
        result = check_news(h["code"], h["name"])
        if result:
            logger.info("  [NEWS] %s %s（Jev 呼叫 %d 次）", h["code"], result["verdict"],
                        result.get("jev_calls", 0))
        msg = format_signal("scan", h["code"], h["name"], h["ev"], h["ev"]["pct"],
                            format_news_block(result, h["ev"]["pct"]))
        ok = send_telegram(msg) and ok

    if ok:
        state["last_pushed"] = date_iso
        save_json(STATE_FILE, state)
    logger.info("完成：摘要 1 則、逐檔 %d 則", len(detail))


if __name__ == "__main__":
    main()
