#!/usr/bin/env python3
"""
每月策略檢討：用最新的歷史日K，比較「現行規則」和幾個事先固定的候選修改，
判斷有沒有足夠證據值得調整。只產生報告與建議，不會自動修改任何設定。

為什麼候選清單是固定的？
  如果每個月都自由嘗試各種參數、挑回測最好的，最後一定會找到「剛好適合過去」的設定
  （過度擬合），上線後反而變差。所以候選只有少數幾個、寫死在程式裡；要新增候選必須另外開 PR。

評估方式（每個候選都一樣）
  · 資金帳戶模擬（同時最多 BT_MAX_POSITIONS 檔），和 backtest.py 相同的進出場規則與成本
  · 全期、最近 6 個月、逐月三種角度；並和同期加權指數比較
  · 逐月勝率：帳戶月報酬 > 現行規則月報酬 的月份比例

建議修改的門檻（全部要成立，事先寫死）
  1. 全期帳戶報酬 比現行多 ≥ 5 個百分點
  2. 最近 6 個月帳戶報酬 也比現行好
  3. 逐月勝過現行的月份 ≥ 60%
  4. 最大回撤 不比現行差超過 5 個百分點
  5. 交易筆數 ≥ 30
另外列出實際追蹤（訊號紀錄簿）的三角收斂進場成效，和回測預期對照。

輸出：data 分支 review/YYYY-MM.md、Telegram 摘要、GitHub Actions 執行摘要
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

import backtest as bt
import history
import screener
from config import CFG, Config
from notifier import send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

OUT_DIR = Path(os.environ.get("REVIEW_DIR", "review-out"))
RECENT_MONTHS = 6
# 建議修改的門檻
MIN_GAIN = 0.05           # 全期帳戶報酬至少多 5 個百分點
MIN_MONTH_WIN = 0.60      # 逐月勝過現行的比例
MAX_MDD_WORSE = 0.05      # 最大回撤最多差 5 個百分點
MIN_TRADES = 30


@dataclass
class Candidate:
    key: str
    label: str
    pattern: str = "三角收斂"
    mode: Optional[str] = None            # None = 用 cfg.A_MODE
    strict: Optional[bool] = None         # None = 用 cfg.SCREEN_STRICT_GROUP
    market: Optional[bool] = None         # None = 用 cfg.SCREEN_MARKET_FILTER
    top_n: Optional[int] = None           # None = 用 cfg.SCREEN_TOP_N
    stop: Optional[str] = None            # None = 用 cfg.BT_STOP_BEFORE_TARGET
    change: Dict[str, object] = field(default_factory=dict)   # 若採用，config.py 要改的設定

    def resolve(self, cfg: Config) -> dict:
        return {"pattern": self.pattern, "mode": self.mode or cfg.A_MODE,
                "strict": cfg.SCREEN_STRICT_GROUP if self.strict is None else self.strict,
                "market": cfg.SCREEN_MARKET_FILTER if self.market is None else self.market,
                "top_n": self.top_n or cfg.SCREEN_TOP_N,
                "stop": self.stop or cfg.BT_STOP_BEFORE_TARGET}


def candidates(cfg: Config) -> List[Candidate]:
    """事先固定的候選；第一個一定是現行規則"""
    other_mode = "amplitude" if cfg.A_MODE == "rally" else "rally"
    return [
        Candidate("current", "現行規則"),
        Candidate("top3", "每日只列 3 檔", top_n=3, change={"SCREEN_TOP_N": 3, "BT_TOP_N": 3}),
        Candidate("top8", "每日列 8 檔", top_n=8, change={"SCREEN_TOP_N": 8, "BT_TOP_N": 8}),
        Candidate("mode", f"目標價改用{'振幅法' if other_mode == 'amplitude' else '漲幅法'}",
                  mode=other_mode, change={"A_MODE": other_mode}),
        Candidate("nostop", "達標前不停損（只靠 60 天到期）", stop="none",
                  change={"BT_STOP_BEFORE_TARGET": "none"}),
        Candidate("loose", "族群用寬鬆條件", strict=False, change={"SCREEN_STRICT_GROUP": False}),
        Candidate("chuaner", "穿山二龍改列進場（同樣三個濾網）", pattern="穿山二龍",
                  change={"ACTIVE_PATTERNS": "三角收斂,穿山二龍"}),
    ]


def select(trades: pd.DataFrame, spec: dict) -> pd.DataFrame:
    t = trades[(trades["pattern"] == spec["pattern"]) & (trades["mode"] == spec["mode"])]
    t = t[t["in_strict"]] if spec["strict"] else t[t["in_strong"]]
    if spec["market"]:
        t = t[t["market_up"]]
    t = bt._top_n(t, spec["top_n"])
    return bt.drop_overlap(t)


def monthly(eq: pd.Series) -> pd.Series:
    """每日資產 → 月報酬"""
    if eq.empty:
        return pd.Series(dtype=float)
    s = eq.copy()
    s.index = pd.to_datetime(s.index)
    last = s.groupby(s.index.to_period("M")).last()
    prev = last.shift(1)
    prev.iloc[0] = 1.0
    return last / prev - 1


def account_stats(eq: pd.Series, since: Optional[str] = None) -> dict:
    if eq.empty:
        return {"ret": 0.0, "mdd": 0.0}
    s = eq if since is None else eq[eq.index >= since]
    if s.empty:
        return {"ret": 0.0, "mdd": 0.0}
    base = eq[eq.index < since].iloc[-1] if since and (eq.index < since).any() else 1.0
    peak = s.cummax()
    return {"ret": float(s.iloc[-1] / base - 1), "mdd": float(((peak - s) / peak).max())}


def judge(cur: dict, cand: dict) -> List[str]:
    """回傳沒通過的門檻（空 = 全部通過）"""
    fails = []
    if cand["n"] < MIN_TRADES:
        fails.append(f"交易筆數 {cand['n']} < {MIN_TRADES}")
    if cand["full"]["ret"] - cur["full"]["ret"] < MIN_GAIN:
        fails.append("全期報酬沒有多 5 個百分點")
    if cand["recent"]["ret"] <= cur["recent"]["ret"]:
        fails.append(f"最近 {RECENT_MONTHS} 個月沒有比較好")
    if cand["month_win"] is None or cand["month_win"] < MIN_MONTH_WIN:
        fails.append("逐月勝出不到 60%")
    if cand["full"]["mdd"] - cur["full"]["mdd"] > MAX_MDD_WORSE:
        fails.append("最大回撤變差超過 5 個百分點")
    return fails


def live_tracking(journal_dir: Path) -> dict:
    """訊號紀錄簿裡三角收斂「列進場」的實際 5／20 日超額報酬"""
    p = journal_dir / "signals_pattern_tri.csv"
    if not p.exists():
        return {}
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("slot") != "觀察"]
    out = {"n": len(rows)}
    for h in (5, 20):
        vals = [float(r[f"excess_{h}d"]) for r in rows if r.get(f"excess_{h}d")]
        if vals:
            out[h] = {"n": len(vals), "avg": mean(vals), "win": sum(v > 0 for v in vals) / len(vals)}
    return out


def _p(x, d=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:+.{d}f}%"


def build_report(rows: List[dict], period: dict, live: dict, cfg: Config) -> str:
    ix = period.get("index", {})
    cur = rows[0]
    rec = [r for r in rows[1:] if not r["fails"]]
    rec.sort(key=lambda r: r["full"]["ret"], reverse=True)
    L = [f"# 每月策略檢討｜{period['today']}", "",
         f"- 回測期間：{period['start']} ～ {period['end']}；最近 {RECENT_MONTHS} 個月 = {period['recent']} 起",
         f"- 同期加權指數：全期 {_p(ix.get('ret'))}（最大回撤 {_p(-ix['mdd']) if ix else '—'}）；"
         f"最近 {RECENT_MONTHS} 個月 {_p(period.get('index_recent'))}",
         f"- 現行設定：{'、'.join(cfg.active_patterns)}；A_MODE={cfg.A_MODE}；每日 {cfg.SCREEN_TOP_N} 檔；"
         f"族群從嚴={cfg.SCREEN_STRICT_GROUP}；大盤濾網={cfg.SCREEN_MARKET_FILTER}；達標前停損={cfg.BT_STOP_BEFORE_TARGET}",
         f"- 帳戶模擬：同時最多 {cfg.BT_MAX_POSITIONS} 檔、每檔資產 1/{cfg.BT_MAX_POSITIONS}，含手續費、證交稅、滑價",
         "", "## 結論", ""]
    if rec:
        best = rec[0]
        L.append(f"**建議考慮：{best['label']}**（全部門檻都通過）")
        L.append(f"- 需要修改的設定：`{json.dumps(best['change'], ensure_ascii=False)}`")
        L.append("- 這只是建議，請確認後再改；改完會在下個月的檢討裡繼續被檢驗。")
    else:
        L.append("**維持現行規則**：沒有候選修改通過全部門檻。")
    beat = cur["full"]["ret"] - ix.get("ret", 0) if ix else None
    if beat is not None:
        L.append(f"- 現行規則全期 {_p(cur['full']['ret'])}，比加權指數 {'多' if beat >= 0 else '少'} "
                 f"{abs(beat) * 100:.1f} 個百分點。")
    L += ["", "## 候選比較", "",
          f"| 候選 | 筆數 | 勝率 | 全期帳戶 | 最大回撤 | 最近{RECENT_MONTHS}個月 | 逐月勝過現行 | 判定 |",
          "|---|---:|---:|---:|---:|---:|---:|---|"]
    for r in rows:
        verdict = "（基準）" if r is cur else ("✅ 通過" if not r["fails"] else "❌ " + "；".join(r["fails"]))
        mw = "—" if r["month_win"] is None else f"{r['month_win']:.0%}"
        L.append(f"| {r['label']} | {r['n']} | {r['win']:.0%} | {_p(r['full']['ret'])} | {_p(-r['full']['mdd'])} "
                 f"| {_p(r['recent']['ret'])} | {mw} | {verdict} |")
    L += ["", "## 現行規則逐月表現（帳戶月報酬 vs 加權指數）", "", "| 月份 | 帳戶 | 加權指數 | 差 |", "|---|---:|---:|---:|"]
    im = period.get("index_monthly", pd.Series(dtype=float))
    for m, v in cur["monthly"].tail(12).items():
        iv = im.get(m)
        L.append(f"| {m} | {_p(v)} | {_p(iv)} | {_p(v - iv) if iv is not None else '—'} |")
    L += ["", "## 實際追蹤（訊號紀錄簿，三角收斂列進場）", ""]
    if live.get(5):
        for h in (5, 20):
            if live.get(h):
                x = live[h]
                L.append(f"- {h} 日超額報酬：平均 {x['avg']:+.1f}%／勝率 {x['win']:.0%}／{x['n']} 筆"
                         + ("（樣本不足 30 筆）" if x["n"] < 30 else ""))
    else:
        L.append("- 還沒有足夠的實際結果。")
    L += ["", "## 門檻（事先固定，不因結果調整）", "",
          f"全期帳戶報酬多 ≥ {MIN_GAIN:.0%}、最近 {RECENT_MONTHS} 個月也較好、逐月勝出 ≥ {MIN_MONTH_WIN:.0%}、"
          f"回撤不差超過 {MAX_MDD_WORSE:.0%}、交易 ≥ {MIN_TRADES} 筆。候選清單寫死在 strategy_review.py，"
          "新增候選需另開 PR。", "",
          "> 回測只用日K、未含股利（加權指數為價格指數）；過去績效不代表未來。這是統計資訊，不構成投資建議。"]
    return "\n".join(L)


def telegram_summary(rows: List[dict], period: dict, cfg: Config) -> str:
    cur = rows[0]
    rec = sorted([r for r in rows[1:] if not r["fails"]], key=lambda r: r["full"]["ret"], reverse=True)
    ix = period.get("index", {})
    L = [f"🧭 <b>每月策略檢討｜{period['today']}</b>",
         f"現行：全期 {_p(cur['full']['ret'])}（回撤 {_p(-cur['full']['mdd'])}）｜最近 {RECENT_MONTHS} 個月 "
         f"{_p(cur['recent']['ret'])}",
         f"大盤：全期 {_p(ix.get('ret'))}｜最近 {RECENT_MONTHS} 個月 {_p(period.get('index_recent'))}"]
    if rec:
        b = rec[0]
        L.append(f"\n✅ <b>建議考慮：{b['label']}</b>（全期 {_p(b['full']['ret'])}、最近 {_p(b['recent']['ret'])}、"
                 f"逐月勝出 {b['month_win']:.0%}）")
        L.append("Claude 會在下一次排程開 PR 說明，請你確認後再合併。")
    else:
        L.append("\n➖ <b>維持現行規則</b>：沒有候選修改通過全部門檻。")
    L.append("\n完整報告：data 分支 review/ 與 Actions 執行摘要")
    return "\n".join(L)


def evaluate(trades: pd.DataFrame, series, calendar: List[str], period: dict, cfg: Config,
             cands: List[Candidate], trades_by_stop: Dict[str, pd.DataFrame]) -> List[dict]:
    rows = []
    for c in cands:
        spec = c.resolve(cfg)
        t = select(trades_by_stop[spec["stop"]], spec)
        m = bt.metrics(t)
        pf = bt.portfolio(t, series, calendar, cfg)
        eq = pf["equity"]
        rows.append({"key": c.key, "label": c.label, "change": c.change, "n": m.get("n", 0),
                     "win": m.get("win", 0.0), "full": account_stats(eq),
                     "recent": account_stats(eq, period["recent"]), "monthly": monthly(eq)})
    cur = rows[0]
    for r in rows:
        if r is cur:
            r["month_win"], r["fails"] = None, []
            continue
        a, b = r["monthly"].align(cur["monthly"], join="inner")
        r["month_win"] = float((a > b).mean()) if len(a) else None
        r["fails"] = judge(cur, r)
    return rows


def main():
    cfg = CFG
    month = datetime.now(screener.TZ).strftime("%Y-%m")
    if (OUT_DIR / f"{month}.md").exists() and os.environ.get("FORCE_REVIEW") != "1":
        logger.info("%s 已經檢討過，略過（重跑請設定 FORCE_REVIEW=1）", month)
        return
    df = history.load()
    if df.empty:
        logger.error("沒有歷史資料")
        return
    last = df["date"].max()
    start = (date.fromisoformat(last) - timedelta(days=int(cfg.BT_YEARS * 365))).isoformat()
    start = max(start, (date.fromisoformat(df["date"].min()) + timedelta(days=bt.WARMUP_DAYS)).isoformat())
    recent = (date.fromisoformat(last) - timedelta(days=int(RECENT_MONTHS * 30.44))).isoformat()

    df = history.adjust(df)
    calendar = sorted(df["date"].unique())
    taiex = screener.prepare(df[df["code"] == history.INDEX_CODE])
    series = {code: bt.with_locked_down(screener.prepare(g))
              for code, g in df[df["code"] != history.INDEX_CODE].groupby("code")
              if history.is_common_stock(code) and len(g) >= 80}
    groups = screener.load_groups(history.refresh_industry())
    stock_groups: Dict[str, List[str]] = {}
    for g, codes in groups.items():
        for c in codes:
            stock_groups.setdefault(c, []).append(g)
    strong = bt.strong_by_date(series, taiex, groups, cfg)
    strict = bt.strong_by_date(series, taiex, groups, cfg, strict=True)
    mkt = bt.market_up(taiex)

    cands = candidates(cfg)
    pats = {c.pattern for c in cands}
    sigs = [s for s in bt.find_signals(series, start, cfg) if s["pattern"] in pats]
    logger.info("訊號 %d 筆", len(sigs))
    trades_by_stop = {}
    for stop in {c.resolve(cfg)["stop"] for c in cands}:
        c2 = copy.deepcopy(cfg)
        c2.BT_STOP_BEFORE_TARGET = stop
        trades_by_stop[stop] = bt.run_trades(series, sigs, stock_groups, strong, c2, strict=strict, mkt=mkt)

    ix_eq = pd.Series(taiex.c, index=taiex.date)
    ix_eq = ix_eq[ix_eq.index >= start] / ix_eq[ix_eq.index >= start].iloc[0]
    period = {"today": datetime.now(screener.TZ).strftime("%Y-%m-%d"), "start": start, "end": last,
              "recent": recent, "index": bt.index_stats(taiex, start, last),
              "index_recent": account_stats(ix_eq, recent)["ret"], "index_monthly": monthly(ix_eq)}
    rows = evaluate(trades_by_stop[cfg.BT_STOP_BEFORE_TARGET], series, calendar, period, cfg, cands, trades_by_stop)
    live = live_tracking(Path(os.environ.get("JOURNAL_DIR", "journal-data")))
    report = build_report(rows, period, live, cfg)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{month}.md").write_text(report, encoding="utf-8")
    summary = {"date": period["today"], "recommend": [
        {"key": r["key"], "label": r["label"], "change": r["change"], "full": r["full"]["ret"],
         "recent": r["recent"]["ret"], "month_win": r["month_win"]}
        for r in rows[1:] if not r["fails"]]}
    (OUT_DIR / "latest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report + "\n")
    print(report)
    send_telegram(telegram_summary(rows, period, cfg))
    history.checkpoint(f"每月策略檢討 {month}")


if __name__ == "__main__":
    main()
