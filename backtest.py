#!/usr/bin/env python3
"""
回測：穿山二龍／三角收斂／黑飛舞，過去 2 年（data 分支的日K）

進場（與 screener 相同的偵測函式，只用當天以前的資料）
  · 穿山二龍：收盤進場；第 3 階段當天漲停 → 隔日開盤進場，隔日一字漲停則「買不到」不計
  · 三角收斂：突破日收盤進場
  · 黑飛舞：Day 1 收盤進場
出場（與 exit_manager 相同規則）
  · 黑飛舞 Day 2：漲停 → Day 3 開盤全賣；最大漲幅 ≥5% → 收盤賣半＋隔日開盤賣半；<5% → 收盤全賣
  · 波段：碰到目標價（Base×(1+A×0.5)）賣半（跳空開高以開盤價成交）；
          剩餘部位收盤跌破 20MA 或近 10 日低點出清
          達標前收盤跌破 20MA → 全數出場（BT_STOP_BEFORE_TARGET=ma20；設 none 則只靠最長持有）
          最長持有 60 個交易日，到期收盤出場
  · 一字跌停賣不掉 → 順延到下一個交易日開盤
成本：買進 手續費＋滑價；賣出 手續費＋證交稅 0.3%＋滑價（config.py）
報酬一律用還原權息價（除權息不算虧損）

輸出：報告（Markdown）、每筆交易 CSV、Telegram 摘要、GitHub Actions 執行摘要
未納入：領頭羊／落後股剔除、盤中訊號（只用日K）
"""

from __future__ import annotations

import html
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import history
import screener
from config import CFG, Config
from notifier import send_telegram
from tw_market_utils import is_locked_limit_down

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

OUT_DIR = Path(os.environ.get("BACKTEST_DIR", "backtest-out"))
PATTERNS = ("穿山二龍", "三角收斂", "黑飛舞")
MODES = ("rally", "amplitude")
WARMUP_DAYS = 160          # 日曆天：讓 60 日高點、20MA 等指標有足夠資料


# ── 準備 ─────────────────────────────────────────────────────────
def with_locked_down(S: SimpleNamespace) -> SimpleNamespace:
    S.locked_down = np.array([bool(r > 0 and is_locked_limit_down(o, h, l, c, r)) if not np.isnan(r) else False
                              for o, h, l, c, r in zip(S.raw_o, S.raw_h, S.raw_l, S.raw_c, S.prev_ref)])
    return S


def _fill(S, k: int, kind: str):
    """在第 k 天以開盤或收盤賣出；一字跌停賣不掉就順延到下一天開盤。回傳 (天, 價) 或 None（資料不夠）"""
    while k < S.n and S.locked_down[k]:
        k, kind = k + 1, "open"
    if k >= S.n:
        return None
    return k, float(S.o[k] if kind == "open" else S.c[k])


def _ret(entry: float, legs, cfg: Config) -> float:
    """legs: [(權重, 賣價)]；扣除買賣成本"""
    proceeds = sum(w * px for w, px in legs) * (1 - cfg.sell_cost)
    return proceeds / (entry * (1 + cfg.buy_cost)) - 1


# ── 單筆模擬 ─────────────────────────────────────────────────────
def simulate_heifeiwu(S, i: int, cfg: Config = CFG) -> Optional[dict]:
    entry, d2 = float(S.c[i]), i + 1
    if d2 >= S.n:
        return None
    g = S.h[d2] / entry - 1
    if S.limit_up[d2]:
        plan, rule = [(1.0, d2 + 1, "open")], 1
    elif g >= cfg.HFW_EXIT_GAIN:
        plan, rule = [(0.5, d2, "close"), (0.5, d2 + 1, "open")], 2
    else:
        plan, rule = [(1.0, d2, "close")], 3
    legs, last = [], i
    for w, k, kind in plan:
        f = _fill(S, k, kind)
        if f is None:
            return None
        legs.append((w, f[1]))
        last = max(last, f[0])
    return {"entry_idx": i, "exit_idx": last, "entry": entry, "ret": _ret(entry, legs, cfg),
            "rule": rule, "day2_gain": float(g)}


def simulate_swing(S, i: int, sig: dict, mode: str, cfg: Config = CFG) -> Optional[dict]:
    """i 為訊號日；回傳 None = 資料不夠（尚未結束）；nofill = 隔日一字漲停買不到"""
    if sig["entry_type"] == "次日開盤進場":
        e = i + 1
        if e >= S.n:
            return None
        if S.locked[e]:
            return {"nofill": True, "entry_idx": e, "exit_idx": e}
        entry = float(S.o[e])
    else:
        e, entry = i, float(S.c[i])
    a = sig["a_rally"] if mode == "rally" else sig["a_amp"]
    target = sig["base"] * (1 + a * cfg.TARGET_FACTOR)
    legs, hit_day, below20 = [], None, False
    remaining = 1.0
    end = e + cfg.MAX_HOLD_DAYS
    for k in range(e + 1, min(S.n, end + 1)):
        if hit_day is None and S.h[k] >= target:
            legs.append((cfg.TARGET_SELL, max(target, float(S.o[k]))))
            remaining -= cfg.TARGET_SELL
            hit_day = k - e
        lo = max(0, k - cfg.SWING_LOW_DAYS)
        swing_low = float(np.min(S.l[lo:k]))
        under20 = screener._ok(S.ma20[k]) and S.c[k] < S.ma20[k]
        exit_now = False
        if hit_day is not None:
            exit_now = under20 or S.c[k] < swing_low
        elif under20:
            below20 = True
            exit_now = cfg.BT_STOP_BEFORE_TARGET == "ma20"
        if exit_now or k == end:
            f = _fill(S, k, "close")
            if f is None:
                return None
            legs.append((remaining, f[1]))
            return {"entry_idx": e, "exit_idx": f[0], "entry": entry, "ret": _ret(entry, legs, cfg),
                    "target": target, "hit": hit_day is not None, "days_to_target": hit_day,
                    "below20_before_target": below20, "max_hold": k == end and not exit_now}
    return None


# ── 族群強弱（逐日） ─────────────────────────────────────────────
def strong_by_date(series: Dict[str, SimpleNamespace], taiex: Optional[SimpleNamespace],
                   groups: Dict[str, List[str]], cfg: Config = CFG) -> pd.DataFrame:
    """回傳 DataFrame（index=日期, columns=族群, 值=是否強勢）"""
    tx = dict(zip(taiex.date, taiex.c)) if taiex is not None else {}
    d = cfg.RS_DAYS
    rows = {}
    for code, S in series.items():
        above = (S.c > S.ma20) & ~np.isnan(S.ma20)
        rs = np.zeros(S.n, bool)
        for i in range(d, S.n):
            t1, t0 = tx.get(S.date[i]), tx.get(S.date[i - d])
            if t1 and t0:
                rs[i] = (S.c[i] / S.c[i - d] - 1) > (t1 / t0 - 1)
        rows[code] = pd.Series(above | rs, index=S.date)
    flags = pd.DataFrame(rows)
    out = {}
    for g, codes in groups.items():
        cols = [c for c in codes if c in flags.columns]
        if len(cols) < cfg.GROUP_MIN_MEMBERS:
            continue
        sub = flags[cols]
        n = sub.notna().sum(axis=1)
        k = sub.fillna(False).astype(bool).sum(axis=1)
        out[g] = (n >= cfg.GROUP_MIN_MEMBERS) & (k >= cfg.GROUP_MIN_STRONG) & (k / n.replace(0, np.nan) >= cfg.GROUP_MIN_RATIO)
    return pd.DataFrame(out).fillna(False) if out else pd.DataFrame(index=flags.index)


# ── 主流程 ───────────────────────────────────────────────────────
def find_signals(series: Dict[str, SimpleNamespace], start: str, cfg: Config = CFG) -> List[dict]:
    """逐檔逐日跑偵測函式；只用該日以前的資料"""
    sigs = []
    for n_done, (code, S) in enumerate(series.items(), 1):
        amt20 = pd.Series(S.amount).shift(1).rolling(20).mean().to_numpy()
        for i in range(S.n):
            if S.date[i] < start or not (amt20[i] >= screener.MIN_AMOUNT):
                continue
            for det in screener.DETECTORS:
                sig = det(S, i, cfg)
                if sig:
                    sigs.append({"code": code, "i": i, "date": S.date[i], **sig})
        if n_done % 300 == 0:
            logger.info("偵測進度 %d／%d 檔，訊號 %d 筆", n_done, len(series), len(sigs))
    return sigs


def run_trades(series, sigs: List[dict], stock_groups: Dict[str, List[str]], strong: pd.DataFrame,
               cfg: Config = CFG) -> pd.DataFrame:
    rows = []
    for s in sigs:
        S = series[s["code"]]
        groups = stock_groups.get(s["code"], [])
        in_strong = bool(groups) and s["date"] in strong.index and any(
            g in strong.columns and bool(strong.at[s["date"], g]) for g in groups)
        base = {"code": s["code"], "name": S.name, "pattern": s["pattern"], "signal_date": s["date"],
                "in_strong": in_strong, "group": "／".join(groups[:2])}
        if s["pattern"] == "黑飛舞":
            t = simulate_heifeiwu(S, s["i"], cfg)
            sims = [("—", t)]
        else:
            sims = [(m, simulate_swing(S, s["i"], s, m, cfg)) for m in MODES]
        for mode, t in sims:
            if t is None:
                continue
            row = base | {"mode": mode, **{k: v for k, v in t.items() if k not in ("entry_idx", "exit_idx")}}
            row["entry_date"] = S.date[t["entry_idx"]]
            row["exit_date"] = S.date[t["exit_idx"]]
            row["hold"] = t["exit_idx"] - t["entry_idx"]
            row["entry_idx"], row["exit_idx"] = t["entry_idx"], t["exit_idx"]
            rows.append(row)
    return pd.DataFrame(rows)


def drop_overlap(df: pd.DataFrame) -> pd.DataFrame:
    """同一檔、同一型態、同一模式：前一筆還沒出場時的新訊號不重複進場"""
    if df.empty:
        return df
    keep = []
    for _, g in df.sort_values("entry_idx").groupby(["code", "pattern", "mode"], sort=False):
        last_exit = -1
        for idx, r in g.iterrows():
            if r["entry_idx"] > last_exit:
                keep.append(idx)
                last_exit = r["exit_idx"]
    return df.loc[sorted(keep)]


def metrics(t: pd.DataFrame) -> dict:
    t = t[~t.get("nofill", pd.Series(False, index=t.index)).fillna(False).astype(bool)]
    if t.empty:
        return {"n": 0}
    r = t.sort_values("exit_date")["ret"].to_numpy()
    cum = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    return {"n": len(r), "win": float(np.mean(r > 0)), "avg": float(np.mean(r)), "median": float(np.median(r)),
            "hold": float(t["hold"].mean()), "mdd": float(np.max(peak - cum)) if len(r) else 0.0,
            "total": float(cum[-1]), "pf": float(gains / losses) if losses > 0 else float("inf")}


def mode_compare(t: pd.DataFrame) -> dict:
    t = t[~t.get("nofill", pd.Series(False, index=t.index)).fillna(False).astype(bool)]
    if t.empty:
        return {"n": 0}
    hit = t["hit"].astype(bool)
    return {"n": len(t), "hit_rate": float(hit.mean()),
            "days_to_target": float(t.loc[hit, "days_to_target"].mean()) if hit.any() else float("nan"),
            "below20": float(t["below20_before_target"].astype(bool).mean()),
            "avg": float(t["ret"].mean()), "win": float((t["ret"] > 0).mean())}


def _pct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:+.{d}f}%"


def build_report(trades: pd.DataFrame, nofill: int, period: dict, cfg: Config = CFG) -> str:
    L = [f"# 型態回測報告", "",
         f"- 期間：{period['start']} ～ {period['end']}（樣本內 ～{period['split']}，之後為樣本外）",
         f"- 股票數：{period['n_stocks']}；訊號 {period['n_signals']} 筆；穿二隔日一字漲停買不到 {nofill} 筆",
         f"- 成本：買 {cfg.buy_cost:.4%}、賣 {cfg.sell_cost:.4%}（手續費折扣 {cfg.FEE_DISCOUNT}）",
         f"- 波段：目前 A_MODE = {cfg.A_MODE}；達標前停損 = {cfg.BT_STOP_BEFORE_TARGET}；最長持有 {cfg.MAX_HOLD_DAYS} 天",
         "- 最大回撤 = 每筆投入固定 1 單位，依出場日累加報酬的最大跌幅（單位：一筆部位的 %）", ""]
    for label, sub in (("強勢族群內（實際會推播的）", trades[trades["in_strong"]]), ("全部訊號（不看族群）", trades)):
        L += [f"## {label}", "",
              "| 型態 | 期間 | 筆數 | 勝率 | 平均報酬 | 中位數 | 平均持有 | 最大回撤 | 累計 | 獲利因子 |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for p in PATTERNS:
            tp = sub[(sub["pattern"] == p) & (sub["mode"].isin(["—", cfg.A_MODE]))]
            for plabel, tt in (("全期", tp), ("樣本內", tp[tp["signal_date"] <= period["split"]]),
                               ("樣本外", tp[tp["signal_date"] > period["split"]])):
                m = metrics(tt)
                if not m["n"]:
                    L.append(f"| {p} | {plabel} | 0 | | | | | | | |")
                    continue
                L.append(f"| {p} | {plabel} | {m['n']} | {m['win']:.0%} | {_pct(m['avg'], 2)} | {_pct(m['median'], 2)} "
                         f"| {m['hold']:.1f} 天 | {_pct(-m['mdd'], 0)} | {_pct(m['total'], 0)} | {m['pf']:.2f} |")
        L.append("")
    L += ["## A_MODE 比較（穿山二龍＋三角收斂）", "",
          "| 範圍 | 型態 | 模式 | 筆數 | 達標率 | 平均幾天達標 | 達標前跌破 20MA | 平均報酬 | 勝率 |",
          "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for label, sub in (("強勢族群", trades[trades["in_strong"]]), ("全部", trades)):
        for p in ("穿山二龍", "三角收斂"):
            for mode in MODES:
                m = mode_compare(sub[(sub["pattern"] == p) & (sub["mode"] == mode)])
                if not m["n"]:
                    continue
                dtt = "—" if np.isnan(m["days_to_target"]) else f"{m['days_to_target']:.1f}"
                L.append(f"| {label} | {p} | {mode} | {m['n']} | {m['hit_rate']:.0%} | {dtt} "
                         f"| {m['below20']:.0%} | {_pct(m['avg'], 2)} | {m['win']:.0%} |")
    hfw = trades[trades["pattern"] == "黑飛舞"]
    if not hfw.empty and "rule" in hfw:
        L += ["", "## 黑飛舞 Day 2 出場規則分布", "", "| 規則 | 筆數 | 平均報酬 |", "|---|---:|---:|"]
        names = {1: "Day 2 漲停 → Day 3 開盤全賣", 2: "≥5% → 收盤半＋隔日開盤半", 3: "<5% → 收盤全賣"}
        for r in (1, 2, 3):
            x = hfw[hfw["rule"] == r]
            if len(x):
                L.append(f"| {names[r]} | {len(x)} | {_pct(x['ret'].mean(), 2)} |")
    L += ["", "> 回測只用日K，未納入領頭羊／落後股剔除與盤中訊號；過去績效不代表未來。只推播不下單。"]
    return "\n".join(L)


def telegram_summary(trades: pd.DataFrame, period: dict, cfg: Config = CFG) -> str:
    L = [f"📊 <b>型態回測｜{period['start']} ～ {period['end']}</b>", "<b>強勢族群內，全期</b>（A_MODE={cfg.A_MODE}）"]
    sub = trades[trades["in_strong"]]
    for p in PATTERNS:
        m = metrics(sub[(sub["pattern"] == p) & (sub["mode"].isin(["—", cfg.A_MODE]))])
        if not m["n"]:
            L.append(f"• {p}：沒有交易")
            continue
        mo = metrics(sub[(sub["pattern"] == p) & (sub["mode"].isin(["—", cfg.A_MODE]))
                         & (sub["signal_date"] > period["split"])])
        L.append(f"• <b>{p}</b> {m['n']} 筆｜勝率 {m['win']:.0%}｜平均 {_pct(m['avg'], 2)}｜回撤 {_pct(-m['mdd'], 0)}"
                 + (f"｜樣本外平均 {_pct(mo['avg'], 2)}（{mo['n']} 筆）" if mo.get("n") else ""))
    L.append("\n<b>A_MODE 比較</b>（達標率／平均天數／達標前破 20MA）")
    for p in ("穿山二龍", "三角收斂"):
        parts = []
        for mode in MODES:
            m = mode_compare(sub[(sub["pattern"] == p) & (sub["mode"] == mode)])
            if m["n"]:
                d = "—" if np.isnan(m["days_to_target"]) else f"{m['days_to_target']:.0f}天"
                parts.append(f"{mode} {m['hit_rate']:.0%}／{d}／{m['below20']:.0%}")
        if parts:
            L.append(f"• {p}：" + "；".join(parts))
    L.append("\n完整報告在 Actions 執行摘要與 data 分支 backtest/report.md")
    return "\n".join(L)


def main():
    cfg = CFG
    df = history.load()
    if df.empty:
        logger.error("沒有歷史資料，請先執行 history.py backfill")
        return
    last = df["date"].max()
    first_data = df["date"].min()
    start = (date.fromisoformat(last) - timedelta(days=int(cfg.BT_YEARS * 365))).isoformat()
    start = max(start, (date.fromisoformat(first_data) + timedelta(days=WARMUP_DAYS)).isoformat())
    split = (date.fromisoformat(start) + timedelta(days=int(cfg.BT_IN_SAMPLE_MONTHS * 30.44))).isoformat()
    logger.info("資料 %s ～ %s；回測訊號期間 %s ～ %s", first_data, last, start, last)

    df = history.adjust(df)
    taiex = screener.prepare(df[df["code"] == history.INDEX_CODE]) if (df["code"] == history.INDEX_CODE).any() else None
    series = {}
    for code, g in df[df["code"] != history.INDEX_CODE].groupby("code"):
        if history.is_common_stock(code) and len(g) >= 80:
            series[code] = with_locked_down(screener.prepare(g))
    logger.info("股票 %d 檔", len(series))

    industry = history.refresh_industry()
    groups = screener.load_groups(industry)
    stock_groups: Dict[str, List[str]] = {}
    for g, codes in groups.items():
        for c in codes:
            stock_groups.setdefault(c, []).append(g)
    strong = strong_by_date(series, taiex, groups, cfg)

    sigs = find_signals(series, start, cfg)
    logger.info("訊號 %d 筆", len(sigs))
    trades = run_trades(series, sigs, stock_groups, strong, cfg)
    if trades.empty:
        logger.warning("沒有任何交易")
        return
    nofill = int(trades.get("nofill", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()) // len(MODES)
    trades = drop_overlap(trades)
    period = {"start": start, "end": last, "split": split, "n_stocks": len(series), "n_signals": len(sigs)}
    report = build_report(trades, nofill, period, cfg)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "report.md").write_text(report, encoding="utf-8")
    trades.drop(columns=["entry_idx", "exit_idx"]).to_csv(OUT_DIR / "trades.csv", index=False, encoding="utf-8-sig")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report + "\n")
    print(report)
    send_telegram(telegram_summary(trades, period, cfg))
    history.checkpoint(f"回測 {datetime.now().strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    main()
