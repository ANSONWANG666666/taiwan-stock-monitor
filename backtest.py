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
  · 資料結束時還沒出場 → 以最後一天收盤價計算（標記「未出場」），避免只剩停損單而低估近期績效
成本：買進 手續費＋滑價；賣出 手續費＋證交稅 0.3%＋滑價（config.py）
報酬一律用還原權息價（除權息不算虧損）

濾網比較（每種型態分開）
  · 基準：強勢族群（≥4 檔且 ≥30%「站上 20MA 或 5 日跑贏大盤」）
  · 大盤濾網：訊號日加權指數收盤在 20MA 之上
  · 族群從嚴：成員要「站上 20MA 而且 5 日跑贏大盤」才算強勢
  · 每日前 N 檔：每天每種型態只取分數最高的 N 檔
  · 全部：以上三個同時使用

資金帳戶模擬：起始資金 1，同時最多持有 BT_MAX_POSITIONS 檔，每檔投入當時資產的 1/N，
  同一天訊號太多時依分數高低優先；每天以收盤價計算資產，最大回撤 = 資產從高點最多跌掉幾 %

輸出：報告（Markdown）、每筆交易 CSV、Telegram 摘要、GitHub Actions 執行摘要
未納入：領頭羊／落後股剔除、盤中訊號（只用日K）
"""

from __future__ import annotations

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
VARIANTS = ("基準", "大盤濾網", "族群從嚴", "每日前N檔", "全部")


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
    """legs: [(天, 權重, 賣價)]；扣除買賣成本"""
    proceeds = sum(w * px for _, w, px in legs) * (1 - cfg.sell_cost)
    return proceeds / (entry * (1 + cfg.buy_cost)) - 1


def _mark_open(S, legs, remaining: float):
    """資料結束還沒賣完：剩餘部位以最後一天收盤價計算"""
    legs.append((S.n - 1, remaining, float(S.c[S.n - 1])))


# ── 單筆模擬 ─────────────────────────────────────────────────────
def simulate_heifeiwu(S, i: int, cfg: Config = CFG) -> Optional[dict]:
    entry, d2 = float(S.c[i]), i + 1
    if d2 >= S.n:
        return None                                   # 進場日就是最後一天，沒有資訊
    g = S.h[d2] / entry - 1
    if S.limit_up[d2]:
        plan, rule = [(1.0, d2 + 1, "open")], 1
    elif g >= cfg.HFW_EXIT_GAIN:
        plan, rule = [(0.5, d2, "close"), (0.5, d2 + 1, "open")], 2
    else:
        plan, rule = [(1.0, d2, "close")], 3
    legs, is_open = [], False
    for w, k, kind in plan:
        f = _fill(S, k, kind)
        if f is None:
            _mark_open(S, legs, w)
            is_open = True
        else:
            legs.append((f[0], w, f[1]))
    return {"entry_idx": i, "exit_idx": max(k for k, _, _ in legs), "entry": entry, "legs": legs,
            "ret": _ret(entry, legs, cfg), "rule": rule, "day2_gain": float(g), "open": is_open}


def simulate_swing(S, i: int, sig: dict, mode: str, cfg: Config = CFG) -> Optional[dict]:
    """i 為訊號日；None = 進場後沒有資料；nofill = 隔日一字漲停買不到"""
    if sig["entry_type"] == "次日開盤進場":
        e = i + 1
        if e >= S.n:
            return None
        if S.locked[e]:
            return {"nofill": True, "entry_idx": e, "exit_idx": e}
        entry = float(S.o[e])
    else:
        e, entry = i, float(S.c[i])
    if e + 1 >= S.n:
        return None
    a = sig["a_rally"] if mode == "rally" else sig["a_amp"]
    target = sig["base"] * (1 + a * cfg.TARGET_FACTOR)
    legs, hit_day, below20 = [], None, False
    remaining = 1.0
    end = e + cfg.MAX_HOLD_DAYS

    def done(is_open, max_hold=False):
        return {"entry_idx": e, "exit_idx": max(k for k, _, _ in legs), "entry": entry, "legs": legs,
                "ret": _ret(entry, legs, cfg), "target": target, "hit": hit_day is not None,
                "days_to_target": hit_day, "below20_before_target": below20,
                "max_hold": max_hold, "open": is_open}

    for k in range(e + 1, min(S.n, end + 1)):
        if hit_day is None and S.h[k] >= target:
            legs.append((k, cfg.TARGET_SELL, max(target, float(S.o[k]))))
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
                _mark_open(S, legs, remaining)
                return done(True)
            legs.append((f[0], remaining, f[1]))
            return done(False, max_hold=k == end and not exit_now)
    _mark_open(S, legs, remaining)                    # 資料結束仍持有
    return done(True)


# ── 族群強弱、大盤（逐日） ───────────────────────────────────────
def strong_by_date(series: Dict[str, SimpleNamespace], taiex: Optional[SimpleNamespace],
                   groups: Dict[str, List[str]], cfg: Config = CFG, strict: bool = False) -> pd.DataFrame:
    """回傳 DataFrame（index=日期, columns=族群, 值=是否強勢）
    strict=False：成員「站上 20MA 或 5 日跑贏大盤」；strict=True：兩者都要"""
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
        rows[code] = pd.Series((above & rs) if strict else (above | rs), index=S.date)
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


def market_up(taiex: Optional[SimpleNamespace]) -> Dict[str, bool]:
    """加權指數收盤在 20MA 之上的日子"""
    if taiex is None:
        return {}
    return {d: bool(screener._ok(m) and c > m) for d, c, m in zip(taiex.date, taiex.c, taiex.ma20)}


def _in_groups(strong: pd.DataFrame, d: str, groups: List[str]) -> bool:
    return bool(groups) and d in strong.index and any(
        g in strong.columns and bool(strong.at[d, g]) for g in groups)


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
               cfg: Config = CFG, strict: Optional[pd.DataFrame] = None,
               mkt: Optional[Dict[str, bool]] = None) -> pd.DataFrame:
    rows = []
    for s in sigs:
        S = series[s["code"]]
        groups = stock_groups.get(s["code"], [])
        base = {"code": s["code"], "name": S.name, "pattern": s["pattern"], "signal_date": s["date"],
                "score": float(s.get("score", 0)),
                "in_strong": _in_groups(strong, s["date"], groups),
                "in_strict": _in_groups(strict, s["date"], groups) if strict is not None else False,
                "market_up": bool(mkt.get(s["date"], False)) if mkt else False,
                "group": "／".join(groups[:2])}
        if s["pattern"] == "黑飛舞":
            sims = [("—", simulate_heifeiwu(S, s["i"], cfg))]
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


def _top_n(df: pd.DataFrame, n: int) -> pd.DataFrame:
    if df.empty:
        return df
    rank = df.groupby(["signal_date", "pattern", "mode"])["score"].rank(method="first", ascending=False)
    return df[rank <= n]


def apply_variant(trades: pd.DataFrame, variant: str, cfg: Config = CFG) -> pd.DataFrame:
    t = trades
    if variant in ("基準", "大盤濾網", "每日前N檔"):
        t = t[t["in_strong"]]
    if variant in ("族群從嚴", "全部"):
        t = t[t["in_strict"]]
    if variant in ("大盤濾網", "全部"):
        t = t[t["market_up"]]
    if variant in ("每日前N檔", "全部"):
        t = _top_n(t, cfg.BT_TOP_N)
    return drop_overlap(t)


def _valid(t: pd.DataFrame) -> pd.DataFrame:
    if "nofill" not in t:
        return t
    return t[~t["nofill"].fillna(False).astype(bool)]


def metrics(t: pd.DataFrame) -> dict:
    t = _valid(t)
    if t.empty:
        return {"n": 0}
    r = t.sort_values("exit_date")["ret"].to_numpy()
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    n_open = int(t["open"].fillna(False).astype(bool).sum()) if "open" in t else 0
    return {"n": len(r), "win": float(np.mean(r > 0)), "avg": float(np.mean(r)), "median": float(np.median(r)),
            "hold": float(t["hold"].mean()), "pf": float(gains / losses) if losses > 0 else float("inf"),
            "open": n_open}


def mode_compare(t: pd.DataFrame) -> dict:
    t = _valid(t)
    if t.empty:
        return {"n": 0}
    hit = t["hit"].astype(bool)
    return {"n": len(t), "hit_rate": float(hit.mean()),
            "days_to_target": float(t.loc[hit, "days_to_target"].mean()) if hit.any() else float("nan"),
            "below20": float(t["below20_before_target"].astype(bool).mean()),
            "avg": float(t["ret"].mean()), "win": float((t["ret"] > 0).mean())}


# ── 資金帳戶模擬 ─────────────────────────────────────────────────
def portfolio(trades: pd.DataFrame, series: Dict[str, SimpleNamespace], calendar: List[str],
              cfg: Config = CFG, max_pos: Optional[int] = None) -> dict:
    """起始資金 1；同時最多 max_pos 檔，每檔投入當時資產的 1/max_pos；回傳最終資產、最大回撤、每日資產"""
    max_pos = max_pos or cfg.BT_MAX_POSITIONS
    t = _valid(trades)
    if t.empty:
        return {"final": 1.0, "ret": 0.0, "mdd": 0.0, "taken": 0, "skipped": 0, "equity": pd.Series(dtype=float)}
    idx_of = {}
    for code in t["code"].unique():
        idx_of[code] = {d: k for k, d in enumerate(series[code].date)}
    entries: Dict[str, list] = {}
    for _, r in t.iterrows():
        entries.setdefault(r["entry_date"], []).append(r)
    cash, equity_prev = 1.0, 1.0
    held: List[dict] = []
    curve, taken, skipped = [], 0, 0
    start = t["entry_date"].min()
    for d in calendar:
        if d < start:
            continue
        # 賣出：今天到期的每一段
        for p in held:
            for k, w, px in p["legs"]:
                if p["dates"][k] == d:
                    cash += p["shares"] * w * px * (1 - cfg.sell_cost)
                    p["left"] -= w
        held = [p for p in held if p["left"] > 1e-9]
        # 買進：依分數排序
        for r in sorted(entries.get(d, []), key=lambda x: -x["score"]):
            if len(held) >= max_pos or any(p["code"] == r["code"] for p in held):
                skipped += 1
                continue
            amt = min(equity_prev / max_pos, cash)
            if amt <= 1e-9:
                skipped += 1
                continue
            S = series[r["code"]]
            cash -= amt
            held.append({"code": r["code"], "S": S, "shares": amt / (r["entry"] * (1 + cfg.buy_cost)),
                         "legs": r["legs"], "dates": S.date, "left": 1.0, "last": r["entry"]})
            taken += 1
        # 收盤計價（停牌沿用最近價格）
        value = cash
        for p in held:
            k = idx_of[p["code"]].get(d)
            if k is not None:
                p["last"] = float(p["S"].c[k])
            value += p["shares"] * p["left"] * p["last"]
        curve.append((d, value))
        equity_prev = value
    eq = pd.Series(dict(curve))
    peak = eq.cummax()
    return {"final": float(eq.iloc[-1]), "ret": float(eq.iloc[-1] - 1), "mdd": float(((peak - eq) / peak).max()),
            "taken": taken, "skipped": skipped, "equity": eq}


def index_stats(taiex: Optional[SimpleNamespace], start: str, end: str) -> dict:
    if taiex is None:
        return {}
    s = pd.Series(taiex.c, index=taiex.date)
    s = s[(s.index >= start) & (s.index <= end)]
    if s.empty:
        return {}
    peak = s.cummax()
    return {"ret": float(s.iloc[-1] / s.iloc[0] - 1), "mdd": float(((peak - s) / peak).max())}


# ── 報告 ─────────────────────────────────────────────────────────
def _pct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:+.{d}f}%"


def evaluate(trades: pd.DataFrame, series, calendar: List[str], period: dict, cfg: Config = CFG) -> List[dict]:
    rows = []
    for p in PATTERNS:
        for v in VARIANTS:
            t = apply_variant(trades[(trades["pattern"] == p) & (trades["mode"].isin(["—", cfg.A_MODE]))], v, cfg)
            m = metrics(t)
            mi = metrics(t[t["signal_date"] <= period["split"]]) if len(t) else {"n": 0}
            mo = metrics(t[t["signal_date"] > period["split"]]) if len(t) else {"n": 0}
            pf = portfolio(t, series, calendar, cfg)
            rows.append({"pattern": p, "variant": v, "m": m, "in": mi, "out": mo, "pf": pf})
    return rows


def build_report(trades: pd.DataFrame, evals: List[dict], nofill: int, period: dict, cfg: Config = CFG) -> str:
    ix = period.get("index", {})
    L = ["# 型態回測報告", "",
         f"- 期間：{period['start']} ～ {period['end']}（樣本內 ～{period['split']}，之後為樣本外）",
         f"- 同期加權指數：{_pct(ix.get('ret'))}，最大回撤 {_pct(-ix['mdd']) if ix else '—'}",
         f"- 股票數：{period['n_stocks']}；訊號 {period['n_signals']} 筆；穿二隔日一字漲停買不到 {nofill} 筆",
         f"- 成本：買 {cfg.buy_cost:.4%}、賣 {cfg.sell_cost:.4%}（手續費 {cfg.FEE_DISCOUNT:g} 折扣）",
         f"- 波段：A_MODE = {cfg.A_MODE}；達標前停損 = {cfg.BT_STOP_BEFORE_TARGET}；最長持有 {cfg.MAX_HOLD_DAYS} 天",
         f"- 資金帳戶：起始 100%，同時最多 {cfg.BT_MAX_POSITIONS} 檔、每檔投入資產 1/{cfg.BT_MAX_POSITIONS}；"
         f"「每日前N檔」N = {cfg.BT_TOP_N}",
         "- 未出場的部位以最後一天收盤價計算（表中「未出場」欄）", "",
         "## 濾網比較", "",
         "| 型態 | 濾網 | 筆數 | 勝率 | 平均報酬 | 樣本內平均 | 樣本外平均 | 帳戶報酬 | 帳戶最大回撤 | 實際進場 | 未出場 |",
         "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for e in evals:
        m, mi, mo, pf = e["m"], e["in"], e["out"], e["pf"]
        if not m["n"]:
            L.append(f"| {e['pattern']} | {e['variant']} | 0 | | | | | | | | |")
            continue
        L.append(f"| {e['pattern']} | {e['variant']} | {m['n']} | {m['win']:.0%} | {_pct(m['avg'], 2)} "
                 f"| {_pct(mi.get('avg'), 2) if mi.get('n') else '—'} | {_pct(mo.get('avg'), 2) if mo.get('n') else '—'} "
                 f"| {_pct(pf['ret'], 1)} | {_pct(-pf['mdd'], 1)} | {pf['taken']} | {m['open']} |")
    L += ["", "> 帳戶報酬／最大回撤：用同一筆資金依序操作該型態的訊號；回撤是資產從高點最多跌掉幾 %。",
          "> 「實際進場」比「筆數」少，是因為持股滿檔時新訊號會被略過。", "",
          "## A_MODE 比較（基準濾網）", "",
          "| 型態 | 模式 | 筆數 | 達標率 | 平均幾天達標 | 達標前跌破 20MA | 平均報酬 | 勝率 |",
          "|---|---|---:|---:|---:|---:|---:|---:|"]
    for p in ("穿山二龍", "三角收斂"):
        for mode in MODES:
            m = mode_compare(apply_variant(trades[(trades["pattern"] == p) & (trades["mode"] == mode)], "基準", cfg))
            if not m["n"]:
                continue
            dtt = "—" if np.isnan(m["days_to_target"]) else f"{m['days_to_target']:.1f}"
            L.append(f"| {p} | {mode} | {m['n']} | {m['hit_rate']:.0%} | {dtt} "
                     f"| {m['below20']:.0%} | {_pct(m['avg'], 2)} | {m['win']:.0%} |")
    hfw = apply_variant(trades[trades["pattern"] == "黑飛舞"], "基準", cfg)
    if not hfw.empty and "rule" in hfw:
        L += ["", "## 黑飛舞 Day 2 出場規則分布（基準濾網）", "", "| 規則 | 筆數 | 平均報酬 |", "|---|---:|---:|"]
        names = {1: "Day 2 漲停 → Day 3 開盤全賣", 2: "≥5% → 收盤半＋隔日開盤半", 3: "<5% → 收盤全賣"}
        for r in (1, 2, 3):
            x = hfw[hfw["rule"] == r]
            if len(x):
                L.append(f"| {names[r]} | {len(x)} | {_pct(x['ret'].mean(), 2)} |")
    L += ["", "> 回測只用日K，未納入領頭羊／落後股剔除與盤中訊號；過去績效不代表未來。只推播不下單。"]
    return "\n".join(L)


def telegram_summary(evals: List[dict], period: dict, cfg: Config = CFG) -> str:
    ix = period.get("index", {})
    L = [f"📊 <b>型態回測｜{period['start']} ～ {period['end']}</b>",
         f"同期大盤 {_pct(ix.get('ret'))}，最大回撤 {_pct(-ix['mdd']) if ix else '—'}",
         f"帳戶報酬／最大回撤（最多 {cfg.BT_MAX_POSITIONS} 檔，A_MODE={cfg.A_MODE}）"]
    for p in PATTERNS:
        L.append(f"\n<b>{p}</b>")
        for e in [x for x in evals if x["pattern"] == p]:
            m, pf, mo = e["m"], e["pf"], e["out"]
            if not m["n"]:
                L.append(f"• {e['variant']}：沒有交易")
                continue
            L.append(f"• {e['variant']}：{_pct(pf['ret'], 0)}／{_pct(-pf['mdd'], 0)}｜{m['n']} 筆 勝率 {m['win']:.0%}"
                     + (f"｜樣本外平均 {_pct(mo['avg'], 1)}" if mo.get("n") else ""))
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
    calendar = sorted(df["date"].unique())
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
    strict = strong_by_date(series, taiex, groups, cfg, strict=True)
    mkt = market_up(taiex)

    sigs = find_signals(series, start, cfg)
    logger.info("訊號 %d 筆", len(sigs))
    trades = run_trades(series, sigs, stock_groups, strong, cfg, strict=strict, mkt=mkt)
    if trades.empty:
        logger.warning("沒有任何交易")
        return
    nofill = int(trades["nofill"].fillna(False).astype(bool).sum()) // len(MODES) if "nofill" in trades else 0
    period = {"start": start, "end": last, "split": split, "n_stocks": len(series), "n_signals": len(sigs),
              "index": index_stats(taiex, start, last)}
    evals = evaluate(trades, series, calendar, period, cfg)
    report = build_report(trades, evals, nofill, period, cfg)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "report.md").write_text(report, encoding="utf-8")
    out = trades.drop(columns=[c for c in ("entry_idx", "exit_idx", "legs") if c in trades])
    out.to_csv(OUT_DIR / "trades.csv", index=False, encoding="utf-8-sig")
    eq = pd.DataFrame({f"{e['pattern']}_{e['variant']}": e["pf"]["equity"] for e in evals})
    eq.to_csv(OUT_DIR / "equity.csv", encoding="utf-8-sig")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report + "\n")
    print(report)
    send_telegram(telegram_summary(evals, period, cfg))
    history.checkpoint(f"回測 {datetime.now().strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    main()
