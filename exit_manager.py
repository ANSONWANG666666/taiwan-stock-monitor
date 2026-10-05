#!/usr/bin/env python3
"""
出場管理（模組三）：依 holdings.yaml 的持股，提醒何時賣出。只推播，不下單。

A. 黑飛舞短線（Day 1 = 進場日，Day 2 = 下一個交易日）
   Day 2 最大漲幅 = Day 2 最高 / 進場價 − 1
   1. Day 2 漲停             → Day 3 開盤賣 100%
   2. 最大漲幅 ≥ 5%（未漲停） → Day 2 收盤賣 50%、Day 3 開盤賣 50%
   3. 最大漲幅 < 5%           → Day 2 收盤賣 100%
   盤中 13:20 由 intraday_loop 先用即時最高價判斷一次（來得及在收盤前賣），收盤後再確認。

B. 穿二／三角收斂波段
   目標價 = Base × (1 + A × 0.5)；A 依 A_MODE（rally 第 1 階段漲幅／amplitude 型態振幅），兩種都顯示
   1. 碰到目標價 → 賣 50%
   2. 剩下 50%：收盤跌破 20MA 或跌破近期波段低點 → 出清
"""

from __future__ import annotations

import html
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np

import history
import screener
from config import CFG, Config
from notifier import send_telegram
from tw_market_utils import is_limit_up

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
STATE_FILE = Path(os.environ.get("EXIT_STATE_FILE", "exits_state.json"))
SWING = ("穿山二龍", "三角收斂")


# ── A. 黑飛舞 ────────────────────────────────────────────────────
def heifeiwu_decision(entry_price: float, day2_high: float, day2_limit_up: bool,
                      cfg: Config = CFG) -> dict:
    """回傳 {rule, max_gain, sell_close, sell_next_open, text}；比例為 0～1"""
    g = day2_high / entry_price - 1
    if day2_limit_up:
        return {"rule": 1, "max_gain": g, "sell_close": 0.0, "sell_next_open": 1.0,
                "text": f"Day 2 漲停（最大漲幅 {g:+.1%}）→ 明天（Day 3）開盤賣出 100%"}
    if g >= cfg.HFW_EXIT_GAIN:
        return {"rule": 2, "max_gain": g, "sell_close": 0.5, "sell_next_open": 0.5,
                "text": f"Day 2 最大漲幅 {g:+.1%} ≥ {cfg.HFW_EXIT_GAIN:.0%} → 今天收盤賣 50%、明天開盤賣 50%"}
    return {"rule": 3, "max_gain": g, "sell_close": 1.0, "sell_next_open": 0.0,
            "text": f"Day 2 最大漲幅 {g:+.1%} < {cfg.HFW_EXIT_GAIN:.0%} → 今天收盤賣出 100%"}


# ── B. 波段 ──────────────────────────────────────────────────────
def swing_params(S, entry_idx: int, strategy: str, cfg: Config = CFG) -> Optional[dict]:
    """用進場日（或前一天，漲停隔日開盤進場的情況）的型態算出 base / a_rally / a_amp"""
    det = screener.detect_chuaner if strategy == "穿山二龍" else screener.detect_triangle
    for k in (entry_idx, entry_idx - 1):
        if 0 <= k < S.n:
            sig = det(S, k, cfg)
            if sig:
                return {"base": sig["base"], "a_rally": sig["a_rally"], "a_amp": sig["a_amp"], "signal_idx": k}
    return None


def swing_targets(params: dict, cfg: Config = CFG) -> Dict[str, float]:
    return {"rally": params["base"] * (1 + params["a_rally"] * cfg.TARGET_FACTOR),
            "amplitude": params["base"] * (1 + params["a_amp"] * cfg.TARGET_FACTOR)}


def swing_decision(S, i: int, params: dict, target_hit: bool, cfg: Config = CFG) -> dict:
    """今天（i）的波段出場判斷；價格皆為還原價"""
    t = swing_targets(params, cfg)
    target = t[cfg.A_MODE]
    lo = max(0, i - cfg.SWING_LOW_DAYS)
    swing_low = float(np.min(S.l[lo:i])) if i > lo else float(S.l[i])
    below20 = bool(screener._ok(S.ma20[i]) and S.c[i] < S.ma20[i])
    out = {"targets": t, "target": target, "swing_low": swing_low, "actions": []}
    if not target_hit and S.h[i] >= target:
        out["actions"].append(("target", f"碰到目標價（{cfg.A_MODE}）→ 賣出 {cfg.TARGET_SELL:.0%}"))
        target_hit = True
    if target_hit:
        if below20:
            out["actions"].append(("exit", "剩餘部位：收盤跌破 20MA → 出清"))
        elif S.c[i] < swing_low:
            out["actions"].append(("exit", f"剩餘部位：收盤跌破近 {cfg.SWING_LOW_DAYS} 日波段低點 → 出清"))
    elif below20:
        out["actions"].append(("warn", "尚未達標，但收盤跌破 20MA（請自行評估）"))
    out["target_hit"] = target_hit
    return out


# ── 持股評估 ─────────────────────────────────────────────────────
def _idx(S, d: str) -> Optional[int]:
    try:
        return S.date.index(d)
    except ValueError:
        return next((k for k, x in enumerate(S.date) if x >= d), None)


def evaluate(holding: dict, S, state: dict, cfg: Config = CFG) -> List[str]:
    """回傳要推播的文字（每一行一個提醒）；state 會被更新以免重複"""
    code, strat = str(holding["code"]), holding.get("strategy", "")
    entry_date = str(holding["entry_date"])
    e = _idx(S, entry_date)
    if e is None:
        return []
    i = S.n - 1
    st = state.setdefault(code, {})
    today = S.date[i]
    ratio = S.raw_c[i] / S.c[i] if S.c[i] else 1.0          # 還原價 → 今日原始價
    name = holding.get("name") or S.name
    head = f"{html.escape(name)}（{code}）{strat}"
    out = []
    if strat == "黑飛舞":
        if i == e + 1 and st.get("hfw_day2") != today:          # 今天是 Day 2
            entry = float(holding.get("entry_price") or S.raw_c[e])
            lu = is_limit_up(S.raw_c[i], S.prev_ref[i])
            d = heifeiwu_decision(entry, S.raw_h[i], lu, cfg)
            out.append(f"⏱ {head}：{d['text']}")
            st["hfw_day2"] = today
        elif i >= e + 2 and not st.get("hfw_done"):
            out.append(f"ℹ️ {head}：已過 Day 2，若尚未依規則出場請確認")
            st["hfw_done"] = today
        return out
    if strat in SWING:
        params = {k: holding[k] for k in ("base", "a_rally", "a_amp") if holding.get(k) is not None}
        if len(params) < 3:
            auto = swing_params(S, e, strat, cfg)
            if not auto:
                return [f"⚠️ {head}：找不到進場日的型態，請在 holdings.yaml 填 base／a_rally／a_amp"]
            if "base" in params:                         # 使用者填的是原始價
                params["base"] = params["base"] / ratio
            params = {**auto, **params}
        elif "base" in params:
            params["base"] = params["base"] / ratio
        hit = bool(holding.get("sold_half") or st.get("target_hit"))
        d = swing_decision(S, i, params, hit, cfg)
        t = d["targets"]
        tgt_txt = f"目標價 漲幅法 {t['rally'] * ratio:.2f}／振幅法 {t['amplitude'] * ratio:.2f}（採用 {cfg.A_MODE}）"
        for kind, text in d["actions"]:
            key = f"{kind}_{today}"
            if st.get(key):
                continue
            st[key] = True
            icon = {"target": "🎯", "exit": "🚪", "warn": "⚠️"}[kind]
            out.append(f"{icon} {head}：{text}　收盤 {S.raw_c[i]:.2f}　{tgt_txt}")
        if d["target_hit"] and not st.get("target_hit"):
            st["target_hit"] = today
    return out


def run(holdings: List[dict], df, state: dict, cfg: Config = CFG) -> List[str]:
    df = history.adjust(df)
    alerts = []
    for h in holdings:
        g = df[df["code"] == str(h["code"])]
        if g.empty:
            alerts.append(f"⚠️ {h['code']}：查無歷史資料")
            continue
        alerts += evaluate(h, screener.prepare(g), state, cfg)
    return alerts


# ── 盤中 13:20：黑飛舞 Day 2 提前判斷 ─────────────────────────────
def next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _num(v) -> float:
    """MIS 欄位可能是 "-" 或空字串（尚未成交）"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def intraday_check(now: Optional[datetime] = None, fetch=None, cfg: Config = CFG,
                   holdings: Optional[List[dict]] = None, send=send_telegram) -> List[str]:
    """持有黑飛舞且今天是 Day 2 的股票，用即時最高價先判斷，讓你來得及在收盤前賣出
    （Day 2 以「進場日的下一個平日」計算；遇到國定假日請以收盤後的確認訊息為準）"""
    now = now or datetime.now(TZ)
    holdings = [h for h in (holdings if holdings is not None else screener.load_holdings())
                if h.get("strategy") == "黑飛舞"]
    todo = [h for h in holdings if next_weekday(date.fromisoformat(str(h["entry_date"]))) == now.date()]
    if not todo:
        return []
    if fetch is None:
        import stock_check_once
        codes = [str(h["code"]) for h in todo]
        fetch = lambda: stock_check_once.fetch_stocks(codes, codes)   # 上市、上櫃都查，無效的會被忽略
    live = {str(x.get("c")): x for x in fetch()}
    out = []
    for h in todo:
        x = live.get(str(h["code"]))
        if not x:
            continue
        high, prev, last = _num(x.get("h")), _num(x.get("y")), _num(x.get("z"))
        if not high or not prev:
            continue
        entry = _num(h.get("entry_price"))
        if not entry:
            logger.warning("%s 沒有填 entry_price，略過盤中判斷", h["code"])
            continue
        lu = is_limit_up(last or high, prev)
        d = heifeiwu_decision(entry, high, lu, cfg)
        name = h.get("name") or x.get("n", "")
        out.append(f"⏱ <b>13:20 盤中</b>｜{html.escape(name)}（{h['code']}）黑飛舞 Day 2：{d['text']}"
                   f"（即時最高 {high:.2f}，收盤後會再確認）")
    if out:
        send("📤 <b>出場提醒</b>\n" + "\n".join(out))
    return out


def main():
    holdings = screener.load_holdings()
    if not holdings:
        logger.info("holdings.yaml 沒有持股，略過")
        return
    end = datetime.now(TZ).date()
    if os.environ.get("SCAN_DATE"):
        end = datetime.strptime(os.environ["SCAN_DATE"], "%Y%m%d").date()
    df = history.load(start=(end - timedelta(days=200)).isoformat(), end=end.isoformat())
    if df.empty:
        logger.error("沒有歷史資料")
        return
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    alerts = run(holdings, df, state)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    if alerts:
        send_telegram(f"📤 <b>出場提醒｜{df['date'].max()}</b>\n" + "\n".join(alerts)
                      + "\n<i>只提醒不下單；達標賣出一半後請在 holdings.yaml 設 sold_half: true</i>")
    logger.info("持股 %d 檔，提醒 %d 則", len(holdings), len(alerts))
    history.checkpoint(f"出場檢查 {df['date'].max()}")


if __name__ == "__main__":
    main()
