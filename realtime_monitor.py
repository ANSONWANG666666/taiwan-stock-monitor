#!/usr/bin/env python3
"""
盤中即時監控（模組二）—— 不需要券商帳戶

資料來源：證交所「基本市況報導」即時快照（mis.twse.com.tw，上市＋上櫃，免金鑰）
  · 約每 5 秒更新一次，提供：最近一筆成交價／量、累計量、最高最低、最佳一檔買賣價
  · 不是逐筆明細：每次輪詢只看得到「最近一筆」，兩次輪詢之間的成交只能看累計量差
    → 大單是「抽樣」偵測，抓得到明顯的大單，但不保證每一筆都抓到

監控名單：前一天收盤後 screener.py 產生的 watch（持股＋候選＋等待中的型態），
存在 candidates.json 的 "watch" 欄位，含盤中計算均線所需的數字。

訊號（同一檔同一訊號 30 分鐘內不重複；觸及漲停一天只報一次）：
  1. 大單敲進：單筆 ≥ 500 張或 ≥ 金額門檻（依 20 日均成交值分級），成交價在賣一（外盤）
     持股另外提醒「大單賣出」（內盤）。附近 5 分鐘大單淨買金額
  2. 突破：觸及漲停；或站上 20MA／三角收斂上緣，且近 5 分鐘量 > 之前平均 5 分鐘量 3 倍
  3. 量縮拉回加碼：持股在 20MA 之上，拉回到 5MA 或 10MA 附近，預估全日量 < 20 日均量
  4. 黑飛舞觀察中：Day 0 之後，價格靠近 5MA、預估全日量 < Day 0 的 60%（收盤後才確認）

只推播，不下單。
"""

from __future__ import annotations

import html
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from config import CFG, Config
from notifier import format_alert, send_telegram
from tw_market_utils import limit_up

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
WATCH_FILE = Path(os.environ.get("CANDIDATES_FILE", "candidates.json"))
SESSION_MIN = 270                     # 09:00～13:30
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
CHUNK = 40


def _num(v) -> float:
    try:
        x = float(str(v).split("_")[0])
        return x if x > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def parse_quote(item: dict) -> Optional[dict]:
    """MIS 欄位：c 代號、n 名稱、z 成交價、tv 當盤量、v 累計量（張）、y 昨收、h/l 高低、a/b 五檔賣買價"""
    code = str(item.get("c") or "")
    if not code:
        return None
    return {"code": code, "name": item.get("n", ""), "z": _num(item.get("z")), "tv": int(_num(item.get("tv"))),
            "v": int(_num(item.get("v"))), "y": _num(item.get("y")), "h": _num(item.get("h")),
            "l": _num(item.get("l")), "a1": _num(item.get("a")), "b1": _num(item.get("b"))}


def minutes_since_open(t: datetime) -> float:
    return (t - t.replace(hour=9, minute=0, second=0, microsecond=0)).total_seconds() / 60


class StockState:
    def __init__(self):
        self.last_v: Optional[int] = None
        self.last_z = 0.0
        self.samples: deque = deque()      # (時間, 累計量)
        self.trades: deque = deque()       # (時間, ±金額) 只記大單
        self.alerted: Dict[str, datetime] = {}

    def vol_at(self, t: datetime) -> Optional[int]:
        """t 當下（或之前最近一次）的累計量"""
        best = None
        for ts, v in self.samples:
            if ts <= t:
                best = v
            else:
                break
        return best


class Monitor:
    def __init__(self, watch: Dict[str, dict], cfg: Config = CFG):
        self.watch = watch
        self.cfg = cfg
        self.state: Dict[str, StockState] = {c: StockState() for c in watch}

    # ── 小工具 ───────────────────────────────────────────────
    def large_threshold(self, ctx: dict) -> float:
        amt = ctx.get("amount20", 0)
        cfg = self.cfg
        if amt < 1e8:
            return cfg.RT_LARGE_AMT_SMALL
        if amt < 1e9:
            return cfg.RT_LARGE_AMT_MID
        return cfg.RT_LARGE_AMT_BIG

    def vol_burst(self, st: StockState, v: int, ctx: dict, t: datetime) -> Optional[float]:
        """近 5 分鐘量 ÷ 之前平均每 5 分鐘量；資料不足時用 20 日均量換算"""
        w = self.cfg.RT_BAR_MIN
        el = minutes_since_open(t)
        before = st.vol_at(t - timedelta(minutes=w))
        if before is None:
            return None
        recent = v - before
        prior_min = el - w
        if prior_min >= 3 * w and before > 0:
            avg = before / (prior_min / w)
        else:
            avg = ctx.get("vol20", 0) / (SESSION_MIN / w)
        return recent / avg if avg > 0 else None

    def projected(self, v: int, t: datetime) -> Optional[float]:
        el = minutes_since_open(t)
        if el < self.cfg.RT_MIN_ELAPSED_MIN:
            return None
        return v * SESSION_MIN / min(el, SESSION_MIN)

    def _fire(self, st: StockState, key: str, t: datetime, once_a_day=False) -> bool:
        last = st.alerted.get(key)
        if last and (once_a_day or t - last < timedelta(minutes=self.cfg.RT_COOLDOWN_MIN)):
            return False
        st.alerted[key] = t
        return True

    # ── 主邏輯 ───────────────────────────────────────────────
    def on_quotes(self, items: List[dict], t: datetime) -> List[dict]:
        cfg = self.cfg
        alerts = []
        today = t.strftime("%Y%m%d")
        for raw in items:
            if raw.get("d") and str(raw["d"]) != today:       # 休市日 MIS 會回傳前一個交易日的資料
                continue
            q = parse_quote(raw)
            if not q or q["code"] not in self.watch:
                continue
            w, st = self.watch[q["code"]], self.state[q["code"]]
            ctx, tags = w.get("ctx", {}), w.get("tags", {})
            price = q["z"] or st.last_z
            v = q["v"]
            first = st.last_v is None
            if first and minutes_since_open(t) < cfg.RT_BAR_MIN:
                st.samples.append((t.replace(hour=9, minute=0, second=0, microsecond=0), 0))
            st.samples.append((t, v))
            while st.samples and st.samples[0][0] < t - timedelta(minutes=SESSION_MIN):
                st.samples.popleft()
            name = w.get("name") or q["name"]
            group = "／".join(g.replace("概念:", "") for g in (w.get("groups") or [])[:2])
            held = "持股" in tags

            def add(signal, reason, key=None, once=False, net=None):
                if self._fire(st, key or signal, t, once):
                    alerts.append({"code": q["code"], "name": name, "group": group, "signal": signal,
                                   "reason": reason, "price": price, "net_large": net})

            # 1. 大單
            new_trade = not first and v > (st.last_v or 0) and q["tv"] > 0 and q["z"] > 0
            if new_trade:
                amt = q["z"] * q["tv"] * 1000
                if q["tv"] >= cfg.RT_LARGE_LOTS or amt >= self.large_threshold(ctx):
                    if q["a1"] and q["z"] >= q["a1"]:
                        side = 1
                    elif q["b1"] and q["z"] <= q["b1"]:
                        side = -1
                    else:
                        side = (q["z"] > st.last_z) - (q["z"] < st.last_z) if st.last_z else 0
                    st.trades.append((t, side * amt))
                    while st.trades and st.trades[0][0] < t - timedelta(minutes=cfg.RT_NET_WINDOW_MIN):
                        st.trades.popleft()
                    net = sum(a for _, a in st.trades)
                    desc = f"單筆 {q['tv']:,} 張（{amt / 1e4:,.0f} 萬）"
                    if side > 0:
                        add("大單敲進", f"{desc}外盤成交；近 {cfg.RT_NET_WINDOW_MIN} 分鐘大單淨買 {net / 1e4:+,.0f} 萬",
                            net=net)
                    elif side < 0 and held:
                        add("大單賣出", f"持股出現{desc}內盤成交；近 {cfg.RT_NET_WINDOW_MIN} 分鐘大單淨額 "
                                        f"{net / 1e4:+,.0f} 萬", net=net)

            if price > 0:
                pct = price / q["y"] - 1 if q["y"] else 0.0
                ma5 = (ctx["sum4"] + price) / 5 if ctx.get("sum4") else None
                ma10 = (ctx["sum9"] + price) / 10 if ctx.get("sum9") else None
                ma20 = (ctx["sum19"] + price) / 20 if ctx.get("sum19") else None
                burst = self.vol_burst(st, v, ctx, t)
                burst_ok = burst is not None and burst > cfg.RT_BREAK_VOL
                burst_txt = f"近 {cfg.RT_BAR_MIN} 分鐘量為平均的 {burst:.1f} 倍" if burst else ""
                proj = self.projected(v, t)

                # 2. 突破
                if q["y"] and (q["h"] or price) >= limit_up(q["y"]):
                    add("觸及漲停", f"漲停價 {limit_up(q['y']):.2f}（{pct:+.1%}）", once=True)
                if ma20 and not ctx.get("above20", True) and price > ma20 and burst_ok:
                    sig = "穿二站回 20MA" if "穿山二龍" in tags else "站上 20MA"
                    extra = "（收盤需 ≥ +3% 紅K 才算第 3 階段）" if "穿山二龍" in tags else ""
                    add(sig, f"{pct:+.1%} 站上 20MA {ma20:.2f}，{burst_txt}{extra}")
                tri = tags.get("三角收斂")
                if tri and price > tri["upper"] and burst_ok:
                    add("三角突破", f"{tri['kind']}（整理 {tri['bars']} 天）突破上緣 {tri['upper']:.2f}，{burst_txt}"
                                    "（收盤量 > 1.5 倍均量才確認）")

                # 3. 量縮拉回加碼（持股）
                if held and ma20 and price > ma20 and proj is not None and proj < ctx.get("vol20", 0):
                    for label, ma in (("5MA", ma5), ("10MA", ma10)):
                        if ma and abs(price / ma - 1) <= cfg.RT_TOUCH_PCT and price < q["y"]:
                            add("量縮拉回加碼", f"拉回到 {label} {ma:.2f} 附近，預估全日量 {proj:,.0f} 張 "
                                              f"< 20 日均量 {ctx['vol20']:,.0f} 張", key="拉回")
                            break

                # 4. 黑飛舞觀察中
                hfw = tags.get("黑飛舞")
                if hfw and ma5 and proj is not None:
                    near = ma5 <= price <= ma5 * (1 + cfg.RT_TOUCH_PCT) or (
                        q["l"] and q["l"] <= ma5 * (1 + cfg.RT_TOUCH_PCT) and price > ma5)
                    rising = ctx.get("ma5_prev") is None or ma5 > ctx["ma5_prev"]
                    if near and rising and proj < cfg.HFW_SHRINK * hfw["day0_volume"]:
                        add("黑飛舞觀察中", f"{hfw['day0'][5:]} 爆量黑K後回測 5MA {ma5:.2f}，"
                                          f"預估全日量 {proj:,.0f} 張 = Day 0 的 {proj / hfw['day0_volume']:.0%}"
                                          "（收盤後確認才算進場訊號）", once=True)

            st.last_v = v
            if q["z"]:
                st.last_z = q["z"]
        return alerts


def format_message(alerts: List[dict], t: datetime) -> str:
    blocks = [format_alert(a["name"], a["code"], a["group"], a["signal"], a["reason"],
                           a["price"] or None, a["net_large"]) for a in alerts]
    return f"⚡ <b>盤中即時｜{t:%H:%M}</b>\n\n" + "\n\n".join(blocks) + "\n\n<i>即時快照抽樣偵測，只推播不下單</i>"


# ── 抓即時行情（沿用證交所 MIS，一個 session 重複使用）───────────
class MisClient:
    def __init__(self):
        self.sess: Optional[requests.Session] = None

    def _session(self) -> requests.Session:
        if self.sess is None:
            s = requests.Session()
            s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
                              "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
            s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=10)
            self.sess = s
        return self.sess

    def fetch(self, watch: Dict[str, dict]) -> List[dict]:
        parts = [f"{'otc' if w.get('market') == 'OTC' else 'tse'}_{c}.tw" for c, w in watch.items()]
        out = []
        for k in range(0, len(parts), CHUNK):
            try:
                r = self._session().get(MIS_URL, params={"ex_ch": "|".join(parts[k:k + CHUNK]), "json": "1",
                                                         "delay": "0", "_": int(time.time() * 1000)}, timeout=10)
                out += r.json().get("msgArray", [])
            except Exception as e:
                logger.warning("即時行情取得失敗：%s", e)
                self.sess = None
            time.sleep(1)
        return out


def load_watch(path: Optional[Path] = None, today: Optional[datetime] = None) -> Dict[str, dict]:
    path = path or WATCH_FILE
    if not path.exists():
        logger.warning("找不到 %s，盤中即時監控略過（需要先跑收盤後的型態選股）", path)
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    watch = data.get("watch") or {}
    today = today or datetime.now(TZ)
    d = data.get("date", "")
    if d and (today.date() - datetime.fromisoformat(d).date()).days > 5:
        logger.warning("觀察名單是 %s 的，已經過期，略過", d)
        return {}
    return watch


_monitor: Optional[Monitor] = None
_client = MisClient()


def tick(now: Optional[datetime] = None, fetch: Optional[Callable] = None,
         send: Callable[[str], bool] = send_telegram) -> List[dict]:
    """intraday_loop 每 RT_EVERY_SEC 秒呼叫一次"""
    global _monitor
    now = now or datetime.now(TZ)
    if _monitor is None:
        _monitor = Monitor(load_watch(today=now))
        logger.info("盤中即時監控：%d 檔", len(_monitor.watch))
    if not _monitor.watch:
        return []
    items = fetch() if fetch else _client.fetch(_monitor.watch)
    alerts = _monitor.on_quotes(items, now)
    if alerts:
        for a in alerts:
            logger.info("  %s %s %s %s", a["code"], a["name"], a["signal"], a["reason"])
        send(format_message(alerts, now))
    return alerts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    w = load_watch()
    print(f"觀察名單 {len(w)} 檔")
    for c, x in w.items():
        print(c, x["name"], x["market"], "、".join(x["tags"]))
    if w and os.environ.get("RT_ONCE") == "1":
        q = MisClient().fetch(w)
        print(f"取得即時行情 {len(q)} 筆")
