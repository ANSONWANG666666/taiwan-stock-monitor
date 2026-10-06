#!/usr/bin/env python3
"""
型態選股（模組一）：族群強弱 → 穿山二龍／黑飛舞／三角收斂 → 領頭羊排序 → candidates.json

每天收盤後執行（接在全市場掃描之後）。只推播，不下單。
偵測函式也給 backtest.py 與 exit_manager.py 共用，三者用的是同一套判斷。

價格：型態判斷用「除權息還原價」（均線、漲幅才能前後比較）；
      漲跌停判斷與推播顯示用「原始價格」。
"""

from __future__ import annotations

import html
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

import history
import journal
from config import CFG, Config
from notifier import send_telegram
from tw_market_utils import is_limit_up, is_locked_limit_up, limit_up

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent
CANDIDATES_FILE = Path(os.environ.get("CANDIDATES_FILE", "candidates.json"))
LEADER_FILE = Path(os.environ.get("LEADER_FILE", "leaders.json"))
SENT_FILE = Path(os.environ.get("SCREENER_SENT_FILE", "screener_sent.json"))   # 記錄哪一天已推播過
CONCEPT_FILE = ROOT / "concept_groups.yaml"
HOLDINGS_FILE = ROOT / "holdings.yaml"
MIN_AMOUNT = float(os.environ.get("SCREEN_MIN_AMOUNT", "20000000"))   # 20 日均成交值門檻（元）
INDEX_CODE = history.INDEX_CODE


# ── 指標 ─────────────────────────────────────────────────────────
def prepare(df: pd.DataFrame) -> SimpleNamespace:
    """單一股票（依日期排序、已 adjust）→ numpy 陣列與指標"""
    df = df.sort_values("date").reset_index(drop=True)
    c = df["adj_close"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    s = pd.Series(c)
    vs = pd.Series(v)
    raw_close = df["close"].to_numpy(float)
    prev_ref = df["prev_ref"].to_numpy(float)
    lu = np.array([bool(r > 0 and is_limit_up(x, r)) if not np.isnan(r) else False
                   for x, r in zip(raw_close, prev_ref)])
    locked = np.array([bool(r > 0 and is_locked_limit_up(o, h, l, x, r)) if not np.isnan(r) else False
                       for o, h, l, x, r in zip(df["open"], df["high"], df["low"], raw_close, prev_ref)])
    h = df["adj_high"].to_numpy(float)
    return SimpleNamespace(
        n=len(df), date=df["date"].tolist(), code=str(df["code"].iloc[0]) if len(df) else "",
        name=str(df["name"].iloc[-1]) if len(df) else "",
        market=str(df["market"].iloc[-1]) if len(df) and "market" in df else "",
        o=df["adj_open"].to_numpy(float), h=h, l=df["adj_low"].to_numpy(float), c=c, v=v,
        raw_o=df["open"].to_numpy(float), raw_h=df["high"].to_numpy(float),
        raw_l=df["low"].to_numpy(float), raw_c=raw_close, prev_ref=prev_ref,
        amount=df["amount"].to_numpy(float),
        ma5=s.rolling(5).mean().to_numpy(), ma10=s.rolling(10).mean().to_numpy(),
        ma20=s.rolling(20).mean().to_numpy(),
        vol20=vs.shift(1).rolling(20).mean().to_numpy(),            # 前 20 日均量（不含當天）
        hh60=pd.Series(h).rolling(60, min_periods=20).max().to_numpy(),  # 含當天的 60 日最高
        pct=s.pct_change().to_numpy(),
        limit_up=lu, locked=locked,
    )


def _ok(*vals) -> bool:
    return all(x is not None and not (isinstance(x, float) and np.isnan(x)) for x in vals)


# ── 穿山二龍 ─────────────────────────────────────────────────────
def _chuaner_stage12(S, i: int, cfg: Config = CFG) -> Optional[dict]:
    """檢查 i 之前（不含 i）已完成第 1、2 階段：i 可以是今天（盤後）或明天（盤中監控用 i = n）"""
    if i - 1 < 0 or not _ok(S.ma20[i - 1]) or S.c[i - 1] > S.ma20[i - 1]:
        return None
    # 第 2 階段：找出這段「收在 20MA 之下」的起點，必須在 N 天內
    b = i - 1
    while b - 1 >= 0 and _ok(S.ma20[b - 1]) and S.c[b - 1] <= S.ma20[b - 1]:
        b -= 1
    if not any(S.c[k] < S.ma20[k] for k in range(b, i)):
        return None
    if i - b > cfg.CHUANER_BREAK_WINDOW:
        return None
    # 第 1 階段：跌破前 60 日內，波段低點 → 之後的高點漲 ≥30%，高點當天均線多頭排列
    start = max(0, b - cfg.CHUANER_LOOKBACK)
    if b - start < 10:
        return None
    hi_idx = start + int(np.argmax(S.h[start:b]))
    if hi_idx <= start:
        return None
    lo_idx = start + int(np.argmin(S.l[start:hi_idx]))
    rally = S.h[hi_idx] / S.l[lo_idx] - 1
    if rally < cfg.CHUANER_RALLY:
        return None
    if not (_ok(S.ma5[hi_idx], S.ma10[hi_idx], S.ma20[hi_idx])
            and S.ma5[hi_idx] > S.ma10[hi_idx] > S.ma20[hi_idx]):
        return None
    return {"b": b, "hi_idx": hi_idx, "lo_idx": lo_idx, "rally": float(rally)}


def detect_chuaner(S, i: int, cfg: Config = CFG) -> Optional[dict]:
    """i 為「第 3 階段：帶量紅K站回 20MA」那一天"""
    if i < cfg.CHUANER_LOOKBACK + 25 or not _ok(S.ma20[i], S.ma20[i - 1]):
        return None
    # 第 3 階段：今天站回 20MA，且是 ≥3% 實體紅K 或漲停
    if not (S.c[i] > S.ma20[i] and S.c[i - 1] <= S.ma20[i - 1]):
        return None
    strong = (S.c[i] > S.o[i] and S.pct[i] >= cfg.CHUANER_STAGE3_PCT) or S.limit_up[i]
    if not strong:
        return None
    st = _chuaner_stage12(S, i, cfg)
    if not st:
        return None
    b, hi_idx, lo_idx, rally = st["b"], st["hi_idx"], st["lo_idx"], st["rally"]
    pull_low = float(np.min(S.l[hi_idx:i + 1]))
    return {
        "pattern": "穿山二龍",
        "entry_type": "次日開盤進場" if S.limit_up[i] else "收盤進場",
        "base": float(S.c[i]), "price": float(S.raw_c[i]),
        "swing_high": float(S.h[hi_idx]), "swing_low": float(S.l[lo_idx]),
        "pattern_high": float(S.h[hi_idx]), "pattern_low": pull_low,
        "a_rally": float(rally), "a_amp": float(S.h[hi_idx] / pull_low - 1),
        "rally_date": S.date[hi_idx], "break_date": S.date[b],
        "score": 50 + min(rally, 1.0) * 30 + (10 if S.limit_up[i] else 0),
        "reason": f"第1階段漲 {rally:.0%}（{S.date[lo_idx][5:]}→{S.date[hi_idx][5:]}），"
                  f"{S.date[b][5:]} 跌破 20MA，今日{'漲停' if S.limit_up[i] else f'紅K +{S.pct[i]:.1%}'}站回",
    }


# ── 黑飛舞／黑飛龍 ───────────────────────────────────────────────
def _hfw_day0(S, d0: int, cfg: Config) -> bool:
    if d0 < 60 or not _ok(S.vol20[d0]) or S.vol20[d0] <= 0:
        return False
    black = S.c[d0] < S.o[d0]
    burst = S.v[d0] >= cfg.HFW_VOL_MULT * S.vol20[d0]
    recent = range(max(0, d0 - cfg.HFW_RECENT_DAYS + 1), d0 + 1)
    hot = any((_ok(S.hh60[k]) and S.h[k] >= S.hh60[k]) or S.limit_up[k] for k in recent)
    return bool(black and burst and hot)


def _hfw_day1_ok(S, k: int, d0: int, cfg: Config) -> bool:
    if not _ok(S.ma5[k], S.ma5[k - 1]):
        return False
    return bool(S.l[k] <= S.ma5[k] * (1 + cfg.HFW_TOUCH_PCT) and S.c[k] > S.ma5[k]
                and S.ma5[k] > S.ma5[k - 1] and S.v[k] <= cfg.HFW_SHRINK * S.v[d0])


def detect_heifeiwu(S, i: int, cfg: Config = CFG) -> Optional[dict]:
    """i 為 Day 1（進場日）。Day 0 = 爆量黑K，Day 1 在其後 1～N 天"""
    for d0 in range(i - 1, max(0, i - cfg.HFW_WINDOW) - 1, -1):
        if not _hfw_day0(S, d0, cfg):
            continue
        # 窗口內收盤跌破 5MA → 型態失效
        if any(_ok(S.ma5[k]) and S.c[k] < S.ma5[k] for k in range(d0 + 1, i + 1)):
            return None
        # 同一個 Day 0 只發一次訊號（前面已經有符合的 Day 1 就不重複）
        if any(_hfw_day1_ok(S, k, d0, cfg) for k in range(d0 + 1, i)):
            return None
        if _hfw_day1_ok(S, i, d0, cfg):
            return {
                "pattern": "黑飛舞", "entry_type": "收盤進場",
                "base": float(S.c[i]), "price": float(S.raw_c[i]),
                "day0": S.date[d0], "day0_volume": float(S.v[d0]),
                "score": 60 + min(S.v[d0] / S.vol20[d0], 5) * 4,
                "reason": f"{S.date[d0][5:]} 爆量黑K（{S.v[d0] / S.vol20[d0]:.1f} 倍均量），"
                          f"今日量縮至 {S.v[i] / S.v[d0]:.0%} 回測 5MA 守住",
            }
        return None
    return None


# ── 三角收斂 ─────────────────────────────────────────────────────
def _swings(arr: np.ndarray, lo: int, hi: int, k: int, mode: str) -> List[int]:
    out = []
    for j in range(lo + k, hi - k + 1):
        w = arr[j - k:j + k + 1]
        if (mode == "high" and arr[j] == w.max()) or (mode == "low" and arr[j] == w.min()):
            if not out or j - out[-1] > k:          # 同一段平台只取一點
                out.append(j)
    return out


def find_triangle(S, i: int, cfg: Config = CFG) -> Optional[dict]:
    """找以 i-1 為結尾、最長的有效三角收斂；不檢查突破"""
    best = None
    for L in range(min(cfg.TRI_MAX_BARS, i - 1), cfg.TRI_MIN_BARS - 1, -5):
        lo, hi = i - L, i - 1
        if lo < 0:
            continue
        sh = _swings(S.h, lo, hi, cfg.TRI_SWING_K, "high")
        sl = _swings(S.l, lo, hi, cfg.TRI_SWING_K, "low")
        if len(sh) < 2 or len(sl) < 2:
            continue
        mu, bu = np.polyfit(sh, S.h[sh], 1)
        ml, bl = np.polyfit(sl, S.l[sl], 1)
        px = float(np.mean(S.c[lo:hi + 1]))
        su, sl_ = mu / px, ml / px
        if su < -cfg.TRI_FLAT_SLOPE and sl_ > 0:
            kind = "對稱三角"
        elif abs(su) <= cfg.TRI_FLAT_SLOPE and sl_ > cfg.TRI_FLAT_SLOPE / 2:
            kind = "上升三角"
        else:
            continue
        up = lambda x: mu * x + bu
        dn = lambda x: ml * x + bl
        w0, w1 = up(lo) - dn(lo), up(hi) - dn(hi)
        if not (w1 > 0 and w1 < w0):
            continue
        xs = np.arange(lo, hi + 1)
        outside = np.mean((S.c[lo:hi + 1] > up(xs) * (1 + cfg.TRI_TOLERANCE)) |
                          (S.c[lo:hi + 1] < dn(xs) * (1 - cfg.TRI_TOLERANCE)))
        if outside > 0.1:
            continue
        ph, pl = float(np.max(S.h[lo:hi + 1])), float(np.min(S.l[lo:hi + 1]))
        pre = max(0, lo - 60)
        pre_low = float(np.min(S.l[pre:lo])) if lo > pre else pl
        best = {"kind": kind, "bars": L, "start": lo, "upper_now": float(up(i)), "lower_now": float(dn(i)),
                "pattern_high": ph, "pattern_low": pl,
                "swing_high": ph, "swing_low": pre_low,
                "a_amp": ph / pl - 1, "a_rally": max(ph / pre_low - 1, 0.0)}
        break          # 由長到短，第一個成立的就是最長的
    return best


def detect_triangle(S, i: int, cfg: Config = CFG) -> Optional[dict]:
    """i 為突破日：收盤站上上緣，量 > 1.5 倍 20 日均量"""
    if i < cfg.TRI_MIN_BARS + 25 or not _ok(S.vol20[i]) or S.vol20[i] <= 0:
        return None
    if not (S.v[i] > cfg.TRI_BREAK_VOL * S.vol20[i] and S.c[i] > S.c[i - 1]):
        return None
    t = find_triangle(S, i, cfg)
    if not t or S.c[i] <= t["upper_now"]:
        return None
    return {
        "pattern": "三角收斂", "entry_type": "收盤進場",
        "base": float(S.c[i]), "price": float(S.raw_c[i]),
        "swing_high": t["swing_high"], "swing_low": t["swing_low"],
        "pattern_high": t["pattern_high"], "pattern_low": t["pattern_low"],
        "a_rally": t["a_rally"], "a_amp": t["a_amp"], "tri_kind": t["kind"], "bars": t["bars"],
        "score": 40 + min(t["bars"], 60) / 60 * 40,
        "reason": f"{t['kind']}整理 {t['bars']} 天，今日帶量 {S.v[i] / S.vol20[i]:.1f} 倍突破上緣",
    }


DETECTORS = [detect_chuaner, detect_heifeiwu, detect_triangle]


def targets(sig: dict, cfg: Config = CFG) -> Dict[str, float]:
    """目標價 = Base × (1 + A × 0.5)；兩種 A 都算，推播一起顯示"""
    return {m: sig["base"] * (1 + sig[f"a_{m}"] * cfg.TARGET_FACTOR) for m in ("rally", "amp")}


# ── 族群 ─────────────────────────────────────────────────────────
def load_groups(industry: Dict[str, dict]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for code, m in industry.items():
        if history.is_common_stock(code):
            groups.setdefault(m.get("industry") or "未分類", []).append(code)
    if CONCEPT_FILE.exists():
        concepts = yaml.safe_load(CONCEPT_FILE.read_text(encoding="utf-8")) or {}
        for name, codes in concepts.items():
            if codes:
                groups[f"概念:{name}"] = [str(c) for c in codes]
    return groups


def group_strength(groups: Dict[str, List[str]], snap: Dict[str, dict], cfg: Config = CFG,
                   strict: bool = False) -> Dict[str, dict]:
    """snap[code] = {above20, rs5}；強勢族群：≥4 檔且 ≥30% 成員「站上 20MA 或 5 日跑贏大盤」
    strict=True（族群從嚴）：成員要「站上 20MA 而且 5 日跑贏大盤」"""
    out = {}
    for g, codes in groups.items():
        members = [c for c in codes if c in snap]
        if len(members) < cfg.GROUP_MIN_MEMBERS:
            continue
        if strict:
            k = sum(1 for c in members if snap[c]["above20"] and snap[c]["rs5"] > 0)
        else:
            k = sum(1 for c in members if snap[c]["above20"] or snap[c]["rs5"] > 0)
        out[g] = {"n": len(members), "strong_n": k,
                  "strong": k >= cfg.GROUP_MIN_STRONG and k / len(members) >= cfg.GROUP_MIN_RATIO}
    return out


def rank_leaders(members: List[str], series: Dict[str, SimpleNamespace], taiex: SimpleNamespace,
                 cfg: Config = CFG) -> List[str]:
    """領頭羊排序：最早創 60 日新高、10 日漲停次數、20 日相對強弱；名次加總越小越強"""
    rows = []
    for c in members:
        S = series.get(c)
        if not S or S.n < 61:
            continue
        i = S.n - 1
        first_high = next((k for k in range(max(0, i - 19), i + 1) if _ok(S.hh60[k]) and S.h[k] >= S.hh60[k]), None)
        lu10 = int(np.sum(S.limit_up[max(0, i - cfg.LEADER_LIMITUP_DAYS + 1):i + 1]))
        d = cfg.LEADER_RS_DAYS
        rs = (S.c[i] / S.c[i - d] - 1) - (taiex.c[-1] / taiex.c[-1 - d] - 1) if taiex.n > d else 0.0
        rows.append((c, first_high if first_high is not None else 10 ** 6, -lu10, -rs))
    if not rows:
        return []
    order = {}
    for col in (1, 2, 3):
        for rank, r in enumerate(sorted(rows, key=lambda r: r[col])):
            order[r[0]] = order.get(r[0], 0) + rank
    return sorted(order, key=lambda c: order[c])


def check_rotation(group: str, leader: str, series: Dict[str, SimpleNamespace], state: dict,
                   members: List[str], cfg: Config = CFG) -> Optional[str]:
    """前任領頭羊 3 天沒創新高 → 交棒給最近創新高的成員"""
    prev = state.get(group, {}).get("code")
    new_leader = leader
    if prev and prev in series and prev != leader:
        S = series[prev]
        i = S.n - 1
        stale = not any(_ok(S.hh60[k]) and S.h[k] >= S.hh60[k]
                        for k in range(max(0, i - cfg.LEADER_STALE_DAYS + 1), i + 1))
        if not stale:
            new_leader = prev        # 前任還在創高，不換手
        else:
            latest = max((m for m in members if m in series),
                         key=lambda m: max((k for k in range(series[m].n)
                                            if _ok(series[m].hh60[k]) and series[m].h[k] >= series[m].hh60[k]),
                                           default=-1))
            new_leader = latest
    state[group] = {"code": new_leader, "date": series[new_leader].date[-1] if new_leader in series else ""}
    if prev and new_leader != prev:
        return f"{group}：{series[prev].name if prev in series else prev} 連 {cfg.LEADER_STALE_DAYS} 天未創高，" \
               f"領頭羊換成 {series[new_leader].name}（{new_leader}）"
    return None


# ── 持股 ─────────────────────────────────────────────────────────
def load_holdings() -> List[dict]:
    if not HOLDINGS_FILE.exists():
        return []
    data = yaml.safe_load(HOLDINGS_FILE.read_text(encoding="utf-8")) or {}
    return [h for h in (data.get("holdings") or []) if h and h.get("code")]


# ── 給盤中即時監控（模組二）的觀察名單 ───────────────────────────
def live_context(S) -> dict:
    """明天盤中要用的數字：用今天以前的收盤和即時價就能算出當下的 5／10／20MA"""
    i = S.n - 1
    c, v = S.c, S.v
    return {
        "prev_close": float(S.raw_c[i]),
        "sum4": float(np.sum(c[max(0, i - 3):i + 1])), "sum9": float(np.sum(c[max(0, i - 8):i + 1])),
        "sum19": float(np.sum(c[max(0, i - 18):i + 1])),
        "ma5_prev": float(S.ma5[i]) if _ok(S.ma5[i]) else None,
        "ma20_prev": float(S.ma20[i]) if _ok(S.ma20[i]) else None,
        "above20": bool(_ok(S.ma20[i]) and c[i] > S.ma20[i]),
        "vol20": float(np.mean(v[max(0, i - 19):i + 1])),                 # 張
        "amount20": float(np.mean(S.amount[max(0, i - 19):i + 1])),
    }


def watch_tags(S, cfg: Config = CFG, patterns: Optional[tuple] = None) -> Dict[str, dict]:
    """明天可能在盤中出現訊號的型態（尚未成立，只是「等待中」）；patterns 限定型態（預設全部）"""
    i, nxt = S.n - 1, S.n
    tags: Dict[str, dict] = {}
    # 黑飛舞：Day 0 已出現，明天仍在 Day 1 窗口內，且尚未失效、尚未出過 Day 1
    for d0 in range(i, max(0, nxt - cfg.HFW_WINDOW) - 1, -1):
        if not _hfw_day0(S, d0, cfg):
            continue
        broken = any(_ok(S.ma5[k]) and S.c[k] < S.ma5[k] for k in range(d0 + 1, i + 1))
        done = any(_hfw_day1_ok(S, k, d0, cfg) for k in range(d0 + 1, i + 1))
        if not broken and not done:
            tags["黑飛舞"] = {"day0": S.date[d0], "day0_volume": float(S.v[d0])}
        break
    # 穿山二龍：第 1、2 階段完成，等明天站回 20MA
    if nxt >= cfg.CHUANER_LOOKBACK + 25:
        st = _chuaner_stage12(S, nxt, cfg)
        if st:
            tags["穿山二龍"] = {"rally": st["rally"], "break_date": S.date[st["b"]]}
    # 三角收斂：收斂中、尚未突破；記下明天的上緣價位
    if nxt >= cfg.TRI_MIN_BARS + 25:
        t = find_triangle(S, nxt, cfg)
        if t and S.c[i] <= t["upper_now"]:
            tags["三角收斂"] = {"upper": t["upper_now"], "kind": t["kind"], "bars": t["bars"]}
    if patterns is not None:
        tags = {k: v for k, v in tags.items() if k in patterns}
    return tags


def build_watch(series: Dict[str, SimpleNamespace], stock_groups: Dict[str, List[str]],
                cands: List[dict], held: Dict[str, SimpleNamespace], cfg: Config = CFG) -> Dict[str, dict]:
    """持股 > 今天的候選 > 強勢族群裡等待中的型態；最多 RT_MAX_WATCH 檔"""
    out: Dict[str, dict] = {}

    def add(code, S, prio, groups, tags):
        w = out.get(code)
        if w:
            w["tags"].update(tags)
            w["prio"] = min(w["prio"], prio)
            return
        out[code] = {"name": S.name, "market": S.market, "groups": groups, "prio": prio,
                     "tags": dict(tags), "ctx": live_context(S)}

    active = cfg.active_patterns                      # 盤中只盯列進場的型態；觀察用的不盯
    for code, S in held.items():
        add(code, S, 0, stock_groups.get(code, []), {"持股": {}} | watch_tags(S, cfg, active))
    for s in cands:
        add(s["code"], series[s["code"]], 1, s["groups"], {"候選": {"pattern": s["pattern"], "score": s["score"]}})
    for code, groups in stock_groups.items():
        tags = watch_tags(series[code], cfg, active)
        if tags:
            add(code, series[code], 2, groups, tags)
    ranked = sorted(out.items(), key=lambda kv: (kv[1]["prio"], -kv[1]["ctx"]["amount20"]))
    return dict(ranked[:cfg.RT_MAX_WATCH])


def split_signals(kept: List[dict], strict_groups: set, market_ok: bool, cfg: Config = CFG):
    """依回測結果分流：
    · 列進場：ACTIVE_PATTERNS 的型態，且（族群從嚴）、（大盤在 20MA 之上），每種型態取分數前 N 檔
    · 觀察用：其他型態（回測不佳），只列名稱
    回傳 (進場, 觀察, 被濾網擋下的檔數說明)"""
    active = cfg.active_patterns
    entries, observe = [], []
    held_back = {"market": 0, "group": 0, "top_n": 0}
    count: Dict[str, int] = {}
    for s in kept:                                    # kept 已依分數排序
        if s["pattern"] not in active:
            observe.append(s)
            continue
        if cfg.SCREEN_STRICT_GROUP and not any(g in strict_groups for g in s["groups"]):
            held_back["group"] += 1
            continue
        if cfg.SCREEN_MARKET_FILTER and not market_ok:
            held_back["market"] += 1
            continue
        if count.get(s["pattern"], 0) >= cfg.SCREEN_TOP_N:
            held_back["top_n"] += 1
            continue
        count[s["pattern"]] = count.get(s["pattern"], 0) + 1
        entries.append(s)
    return entries, observe, held_back


# ── 主流程 ───────────────────────────────────────────────────────
def screen(df: pd.DataFrame, industry: Dict[str, dict], leader_state: dict,
           held: set, cfg: Config = CFG) -> dict:
    df = history.adjust(df)
    held_series = {c: prepare(g) for c, g in df[df["code"].isin(held)].groupby("code") if len(g) >= 20}
    last_date = df["date"].max()
    taiex = prepare(df[df["code"] == INDEX_CODE]) if (df["code"] == INDEX_CODE).any() else None
    series: Dict[str, SimpleNamespace] = {}
    for code, g in df[df["code"] != INDEX_CODE].groupby("code"):
        if g["date"].iloc[-1] != last_date or len(g) < 61:
            continue
        if g["amount"].tail(20).mean() < MIN_AMOUNT:
            continue
        series[code] = prepare(g)
    # 族群強弱
    snap = {}
    for code, S in series.items():
        i = S.n - 1
        d = cfg.RS_DAYS
        rs5 = (S.c[i] / S.c[i - d] - 1) - (taiex.c[-1] / taiex.c[-1 - d] - 1) if taiex and taiex.n > d else 0.0
        snap[code] = {"above20": bool(_ok(S.ma20[i]) and S.c[i] > S.ma20[i]), "rs5": rs5}
    groups = load_groups(industry)
    strength = group_strength(groups, snap, cfg)
    strict = group_strength(groups, snap, cfg, strict=True)
    strong_groups = {g: [c for c in groups[g] if c in series] for g, s in strength.items() if s["strong"]}
    strict_groups = {g for g, s in strict.items() if s["strong"]}
    market_ok = bool(taiex is not None and _ok(taiex.ma20[-1]) and taiex.c[-1] > taiex.ma20[-1])
    stock_groups: Dict[str, List[str]] = {}
    for g, codes in strong_groups.items():
        for c in codes:
            stock_groups.setdefault(c, []).append(g)
    # 型態
    cands = []
    for code in stock_groups:
        S = series[code]
        i = S.n - 1
        for det in DETECTORS:
            sig = det(S, i, cfg)
            if sig:
                sig.update({"code": code, "name": S.name, "date": S.date[i],
                            "market": industry.get(code, {}).get("market", ""),
                            "groups": stock_groups[code]})
                if sig["pattern"] == "黑飛舞":
                    sig["action"] = "加碼" if code in held else "進場"
                if sig["pattern"] != "黑飛舞":
                    sig["targets"] = targets(sig, cfg)
                cands.append(sig)
    # 領頭羊、換手、落後股
    leaders, rotations = {}, []
    for g, codes in strong_groups.items():
        ranked = rank_leaders(codes, series, taiex, cfg) if taiex else []
        if not ranked:
            continue
        msg = check_rotation(g, ranked[0], series, leader_state, codes, cfg)
        leaders[g] = leader_state[g]["code"]
        if msg:
            rotations.append(msg)
    kept = []
    for s in cands:
        s["leader_of"] = [g for g in s["groups"] if leaders.get(g) == s["code"]]
        is_leader = bool(s["leader_of"])
        # 落後股：族群領頭羊已經突破（近 5 天創 60 日新高），自己卻還在 20MA 之下 → 剔除
        lagging = False
        for g in s["groups"]:
            ld = leaders.get(g)
            if ld and ld != s["code"] and ld in series:
                L = series[ld]
                j = L.n - 1
                broke = any(_ok(L.hh60[k]) and L.h[k] >= L.hh60[k] for k in range(max(0, j - 4), j + 1))
                S = series[s["code"]]
                if broke and _ok(S.ma20[-1]) and S.c[-1] < S.ma20[-1]:
                    lagging = True
        if lagging:
            continue
        s["score"] = round(s["score"] + (15 if is_leader else 0)
                           + 5 * max(strength[g]["strong_n"] / strength[g]["n"] for g in s["groups"]), 1)
        kept.append(s)
    kept.sort(key=lambda s: s["score"], reverse=True)
    entries, observe, held_back = split_signals(kept, strict_groups, market_ok, cfg)
    watch_groups = {c: gs for c, gs in stock_groups.items() if any(g in strict_groups for g in gs)} \
        if cfg.SCREEN_STRICT_GROUP else stock_groups
    watch = build_watch(series, watch_groups, entries, held_series, cfg) \
        if (market_ok or not cfg.SCREEN_MARKET_FILTER) else build_watch(series, {}, [], held_series, cfg)
    return {"date": last_date, "candidates": entries, "observe": observe, "held_back": held_back,
            "market_ok": market_ok, "rotations": rotations, "watch": watch,
            "strong_groups": {g: strength[g] | {"leader": leaders.get(g)} for g in strong_groups},
            "n_universe": len(series)}


def format_report(res: dict, cfg: Config = CFG) -> str:
    c = res["candidates"]
    obs = res.get("observe") or []
    hb = res.get("held_back") or {}
    active = "、".join(cfg.active_patterns)
    sg = sorted(res["strong_groups"].items(), key=lambda kv: kv[1]["strong_n"] / kv[1]["n"], reverse=True)
    lines = [f"🎯 <b>型態選股｜{res['date']}</b>",
             f"可交易 {res['n_universe']} 檔 → 強勢族群 {len(sg)} 個 → <b>進場候選 {len(c)} 檔</b>（{active}）"]
    if cfg.SCREEN_MARKET_FILTER:
        lines.append("📈 大盤在 20MA 之上" if res.get("market_ok") else
                     "📉 <b>大盤在 20MA 之下：今天不列進場</b>（回測顯示此時進場勝率較差）")
    if sg:
        lines.append("\n🔥 <b>強勢族群</b>（強勢檔數／成員）")
        lines.append("、".join(f"{html.escape(g)} {s['strong_n']}／{s['n']}" for g, s in sg[:8]))
    for r in res["rotations"]:
        lines.append(f"🔄 {html.escape(r)}")
    for pat in cfg.active_patterns:
        rows = [s for s in c if s["pattern"] == pat]
        if not rows:
            continue
        lines.append(f"\n<b>{pat}</b>")
        for s in rows:
            tag = "👑" if s.get("leader_of") else "•"
            act = f"【{s['action']}】" if s.get("action") else ""
            line = (f"{tag} {html.escape(s['name'])} {s['code']}　{act}{s['entry_type']} {s['price']:.2f}"
                    f"　{html.escape('／'.join(g.replace('概念:', '') for g in s['groups'][:2]))}")
            lines.append(line)
            lines.append(f"　  {html.escape(s['reason'])}")
            if s.get("targets"):
                ratio = s["price"] / s["base"] if s["base"] else 1
                t = s["targets"]
                lines.append(f"　  目標價：漲幅法 {t['rally'] * ratio:.2f}／振幅法 {t['amp'] * ratio:.2f}")
    if not c:
        lines.append("\n今天沒有列進場的股票。")
    skipped = []
    if hb.get("group"):
        skipped.append(f"族群不夠強 {hb['group']} 檔")
    if hb.get("market"):
        skipped.append(f"大盤濾網 {hb['market']} 檔")
    if hb.get("top_n"):
        skipped.append(f"超過每日 {cfg.SCREEN_TOP_N} 檔 {hb['top_n']} 檔")
    if skipped:
        lines.append("<i>被濾網擋下：" + "、".join(skipped) + "</i>")
    if obs:
        lines.append("\n👀 <b>觀察用</b>（回測不佳，不列進場建議）")
        for pat in sorted({s["pattern"] for s in obs}):
            names = [f"{html.escape(s['name'])} {s['code']}" for s in obs if s["pattern"] == pat]
            more = f" 等 {len(names)} 檔" if len(names) > 8 else ""
            lines.append(f"{pat}：" + "、".join(names[:8]) + more)
    w = res.get("watch") or {}
    if w:
        cnt = {}
        for x in w.values():
            for tag in x["tags"]:
                cnt[tag] = cnt.get(tag, 0) + 1
        lines.append(f"\n⚡ 明天盤中即時監控 {len(w)} 檔（" + "、".join(f"{k} {v}" for k, v in cnt.items()) + "）")
    lines.append("\n<i>只推播不下單；目標價依 A_MODE 兩種算法列出，請自行判斷。</i>")
    return "\n".join(lines)


def history_cache(df: pd.DataFrame, days: int = 40) -> dict:
    """把歷史日K轉成 journal.backfill 用的格式（含上櫃），用來回填訊號的 5／20 日報酬"""
    ds = sorted(df["date"].unique())[-days:]
    sub = df[df["date"].isin(ds)]
    stocks = {}
    for code, g in sub.groupby("code"):
        key = journal.INDEX_CODE if code == INDEX_CODE else code
        stocks[key] = {"bars": [[d, float(c)] for d, c in zip(g["date"], g["close"])]}
    return {"days": ds, "stocks": stocks}


def record_journal(res: dict):
    """進場與觀察都記進訊號紀錄簿（週報會追蹤之後的表現）"""
    src = {"三角收斂": "pattern_tri", "穿山二龍": "pattern_chuaner", "黑飛舞": "pattern_hfw"}
    for s in src.values():
        journal.remove(s, res["date"])
    for slot, rows in (("進場", res["candidates"]), ("觀察", res.get("observe") or [])):
        for s in rows:
            journal.record(src.get(s["pattern"], "pattern"), s["code"], s["name"], slot=slot,
                           price=s["price"], date=res["date"], alerts=s["reason"],
                           industry="／".join(s["groups"][:2]))


def main():
    end = datetime.now(TZ).date()
    if os.environ.get("SCAN_DATE"):
        end = datetime.strptime(os.environ["SCAN_DATE"], "%Y%m%d").date()
    df = history.load(start=(end - timedelta(days=200)).isoformat(), end=end.isoformat())
    if df.empty:
        logger.error("沒有歷史資料，請先執行 history.py backfill")
        return
    if df["date"].max() != end.isoformat():
        logger.warning("最新資料日期 %s 不是 %s，以最新資料計算", df["date"].max(), end)
    # 同一個資料日只推播一次（主要觸發＋備援排程會跑好幾次）；手動重跑設 FORCE_SCAN=1
    data_date = df["date"].max()
    sent = json.loads(SENT_FILE.read_text(encoding="utf-8")) if SENT_FILE.exists() else {}
    if sent.get("last_date") == data_date and os.environ.get("FORCE_SCAN") != "1":
        logger.info("%s 的型態選股已經推播過，略過（手動重跑請設定 FORCE_SCAN=1）", data_date)
        return
    industry = history.refresh_industry()
    leader_state = json.loads(LEADER_FILE.read_text(encoding="utf-8")) if LEADER_FILE.exists() else {}
    held = {str(h["code"]) for h in load_holdings()}
    res = screen(df, industry, leader_state, held)
    LEADER_FILE.write_text(json.dumps(leader_state, ensure_ascii=False, indent=1), encoding="utf-8")
    CANDIDATES_FILE.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    logger.info("候選 %d 檔、強勢族群 %d 個、明日盤中觀察 %d 檔",
                len(res["candidates"]), len(res["strong_groups"]), len(res["watch"]))
    for s in res["candidates"]:
        logger.info("  %s %s %s %s %.1f", s["code"], s["name"], s["pattern"], s["entry_type"], s["score"])
    try:
        record_journal(res)
        journal.backfill(history_cache(df))
    except Exception as e:
        logger.warning("訊號紀錄失敗：%s", e)
    if send_telegram(format_report(res)) or not os.environ.get("TELEGRAM_BOT_TOKEN"):
        SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        SENT_FILE.write_text(json.dumps({"last_date": res["date"]}), encoding="utf-8")
    history.checkpoint(f"型態選股 {res['date']}")


if __name__ == "__main__":
    main()
