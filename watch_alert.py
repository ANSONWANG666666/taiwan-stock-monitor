#!/usr/bin/env python3
"""
觀察名單：價格異常與回檔提醒＋市場溫度（加碼閘門）

和型態選股分開：這裡盯的是你自己研究過、寫下「核心投資理由」與「推翻條件」的股票
（watchlist.yaml），觸發時只提醒「去檢視」，不是進出場訊號。

    python watch_alert.py post   收盤後（掛在「台股全市場掃描」15:10）
                                 觀察名單提醒＋Jev 新聞查證＋寫訊號紀錄簿；計算市場溫度存到 data 分支
    python watch_alert.py pre    開盤前（掛在「台股盤中監控」08:50）
                                 用昨天收盤的台股數字＋今早最新的 VIX／美債，推一張市場溫度卡

資料：
  台股日K   data 分支（history.load，含上市櫃全市場，價格用除權息還原價計算）
  櫃買指數、VIX、美 10 年債   yfinance（失敗時該項不計分）
  美國高收益債利差           FRED（免金鑰）
  外資連續賣超（推翻條件）    FinMind（FINMIND_TOKEN 選填）

提醒規則（數字在 watchlist.yaml 的 settings 調整）：
  第一級  自 20 日高點回檔 5%      日常關注
  第二級  自 20 日高點回檔 10%     深度檢視
  第三級  自一年高點回檔 20%，或收盤跌破 200 日均線   重大回檔警報
  門檻 × 波動倍數（個股 ÷ 參考股 2330 的日報酬標準差，限制 0.7～2.5 倍）
  另有：正乖離 200MA 過熱、爆量（> 20 日均量 2 倍）、布林寬度一年最低 5% 收斂、推翻條件觸發
  同一檔同一類 72 小時內不重發；等級升高時立即再通知
"""

from __future__ import annotations

import html
import io
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yaml

import history
import journal
from notifier import send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
WATCHLIST_FILE = Path(os.environ.get("WATCHLIST_FILE", "watchlist.yaml"))
STATE_DIR = Path(os.environ.get("WATCH_STATE_DIR", "data-store/state"))
COOLDOWN_FILE = STATE_DIR / "watch_cooldown.json"
TEMP_FILE = STATE_DIR / "market_temp.json"
CARD_STATE_FILE = Path("watch_card_state.json")      # 盤前溫度卡：同一天只推一次（actions/cache）
INDEX_CODE = history.INDEX_CODE
YEAR = 252

LEVEL_NAME = {1: "第一級｜日常關注", 2: "第二級｜深度檢視", 3: "第三級｜重大回檔警報"}
KIND_NAME = {"ma200_hot": "過熱提醒", "volume_spike": "異常爆量", "squeeze": "波動收斂",
             "invalidation": "推翻條件觸發"}
# 訊號紀錄簿的 slot（週報依此分組統計）
SLOT = {("drawdown", 1): "L1", ("drawdown", 2): "L2", ("drawdown", 3): "L3",
        ("ma200_hot", 0): "過熱", ("volume_spike", 0): "爆量", ("squeeze", 0): "收斂",
        ("invalidation", 0): "推翻"}
# jev_news_for 設定用的名稱
JEV_KEY = {("drawdown", 1): "drawdown1", ("drawdown", 2): "drawdown2", ("drawdown", 3): "drawdown3",
           ("ma200_hot", 0): "ma200_hot", ("volume_spike", 0): "volume_spike",
           ("squeeze", 0): "squeeze", ("invalidation", 0): "invalidation"}

DEFAULT_SETTINGS = {
    "cooldown_hours": 72,
    "ladder": {"l1_dd20": 5, "l2_dd20": 10, "l3_dd252": 20, "l3_break_ma200": True},
    "vol_scale": {"enabled": True, "reference": "2330", "min_mult": 0.7, "max_mult": 2.5},
    "other_alerts": {"ma200_hot_dev": 30, "volume_spike_mult": 2.0, "squeeze_pct": 5},
    "jev_news_for": ["drawdown2", "drawdown3", "invalidation", "volume_spike"],
    "hy_spread_polarity": "contrarian",
}


# ── 設定 ─────────────────────────────────────────────────────────
def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_watchlist(path: Optional[Path] = None) -> dict:
    path = path or WATCHLIST_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    data = data or {}
    items = []
    for w in data.get("watchlist") or []:
        if not w or not w.get("code"):
            continue
        w = dict(w)
        w["code"] = str(w["code"]).strip()
        missing = [k for k in ("thesis", "invalidation") if not w.get(k)]
        if missing:
            logger.warning("觀察名單 %s 缺少 %s，提醒照發但卡片會標示「未填」", w["code"], "、".join(missing))
        items.append(w)
    return {"settings": _merge(DEFAULT_SETTINGS, data.get("settings") or {}),
            "benchmark": str(data.get("benchmark") or "0050"), "watchlist": items}


# ── 資料 ─────────────────────────────────────────────────────────
def load_market(end: date, days: int = 420) -> pd.DataFrame:
    """data 分支近 N 天全市場日K（已還原）"""
    df = history.load(start=(end - timedelta(days=days)).isoformat(), end=end.isoformat())
    if df.empty:
        return df
    return history.adjust(df)


def series_of(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """單一股票的時間序列：Close／High／Low 用還原價，Raw／Ref 是原始收盤與參考價"""
    g = df[df["code"] == code].sort_values("date")
    if g.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "Close": g["adj_close"].astype(float).values if "adj_close" in g else g["close"].astype(float).values,
        "High": g["adj_high"].astype(float).values if "adj_high" in g else g["high"].astype(float).values,
        "Low": g["adj_low"].astype(float).values if "adj_low" in g else g["low"].astype(float).values,
        "Volume": g["volume"].astype(float).values,
        "Raw": g["close"].astype(float).values,
        "Ref": g["ref"].astype(float).values,
        "name": g["name"].astype(str).values,
        "market": g["market"].astype(str).values,
    }, index=pd.to_datetime(g["date"]).values)
    return out


def add_indicators(s: pd.DataFrame) -> pd.DataFrame:
    out = s.copy()
    c = out["Close"]
    out["ret"] = c.pct_change()
    out["hi20"] = c.rolling(20, min_periods=20).max()
    out["hi252"] = c.rolling(YEAR, min_periods=200).max()
    out["dd20"] = (c / out["hi20"] - 1) * 100
    out["dd252"] = (c / out["hi252"] - 1) * 100
    out["ma200"] = c.rolling(200, min_periods=200).mean()
    out["dev200"] = (c / out["ma200"] - 1) * 100
    out["vol20"] = out["Volume"].rolling(20, min_periods=20).mean().shift(1)
    out["vol_mult"] = out["Volume"] / out["vol20"].replace(0, np.nan)
    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    out["bbw"] = 4 * sd / mid * 100
    out["bbw_pct"] = out["bbw"].rolling(YEAR, min_periods=120).rank(pct=True) * 100
    return out


def breadth(df: pd.DataFrame) -> float:
    """全市場普通股站上 200 日均線的比例（%），只算有 200 根以上K棒的股票"""
    stocks = df[df["code"].map(history.is_common_stock)]
    col = "adj_close" if "adj_close" in stocks else "close"
    wide = stocks.pivot_table(index="date", columns="code", values=col).sort_index()
    if len(wide) < 200:
        return float("nan")
    ma = wide.rolling(200, min_periods=200).mean().iloc[-1]
    last = wide.iloc[-1]
    ok = ma.notna() & last.notna()
    return float((last[ok] > ma[ok]).mean() * 100) if ok.any() else float("nan")


def yf_close(ticker: str, period: str = "2y") -> pd.Series:
    try:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)   # 失敗時只留下面這一行警告
        d = yf.download(ticker, period=period, interval="1d", auto_adjust=True,
                        progress=False, threads=False)
        if d is None or d.empty:
            logger.warning("yfinance %s 沒有資料（該項不計分）", ticker)
            return pd.Series(dtype=float)
        c = d["Close"]
        if isinstance(c, pd.DataFrame):
            c = c.iloc[:, 0]
        c.index = pd.to_datetime(c.index).tz_localize(None)
        return c.dropna()
    except Exception as e:
        logger.warning("yfinance %s 失敗：%s", ticker, str(e)[:80])
        return pd.Series(dtype=float)


def fred(series_id: str) -> pd.Series:
    try:
        r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", timeout=30)
        r.raise_for_status()
        d = pd.read_csv(io.StringIO(r.text))
        d.columns = ["date", "v"]
        d["v"] = pd.to_numeric(d["v"], errors="coerce")
        return d.set_index(pd.to_datetime(d["date"]))["v"].dropna()
    except Exception as e:
        logger.warning("FRED %s 失敗：%s", series_id, str(e)[:80])
        return pd.Series(dtype=float)


def finmind_foreign_net(code: str, days: int = 30) -> pd.Series:
    start = (datetime.now(TZ) - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    headers = {"Authorization": f"Bearer {os.environ['FINMIND_TOKEN']}"} if os.environ.get("FINMIND_TOKEN") else {}
    try:
        r = requests.get("https://api.finmindtrade.com/api/v4/data", headers=headers, timeout=30,
                         params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell",
                                 "data_id": code, "start_date": start})
        rows = r.json().get("data", [])
    except Exception as e:
        logger.warning("FinMind %s 失敗：%s", code, str(e)[:80])
        return pd.Series(dtype=float)
    d = pd.DataFrame(rows)
    if d.empty:
        return pd.Series(dtype=float)
    d = d[d["name"].isin(["Foreign_Investor", "Foreign_Dealer_Self"])]
    return (d["buy"] - d["sell"]).groupby(d["date"]).sum().sort_index()


# ── 市場溫度與加碼閘門（固定公式；100 分 = 有利加碼）────────────────
def pct_rank(s: pd.Series, x: float) -> float:
    s = s.dropna().iloc[-YEAR:]
    if s.empty or x is None or np.isnan(x):
        return float("nan")
    return float((s <= x).mean() * 100)


def dev_score(dev: float) -> float:
    """正乖離 25% → 0、貼著 200MA → 50、負乖離 25% → 100"""
    return float(np.clip(50 - 2 * dev, 0, 100)) if dev is not None and not np.isnan(dev) else float("nan")


def gate_label(score: float) -> str:
    if np.isnan(score):
        return "資料不足"
    if score >= 70:
        return "按原計畫加碼"
    if score >= 40:
        return "減量加碼"
    return "暫停加碼、保留現金"


def heat_label(tw_dev: float, vix_pct: float) -> str:
    if (tw_dev or 0) >= 20 or (not np.isnan(vix_pct) and vix_pct <= 20):
        return "偏熱"
    if (tw_dev or 0) <= -10 or (not np.isnan(vix_pct) and vix_pct >= 80):
        return "偏冷"
    return "中性"


def tw_temperature(df: pd.DataFrame) -> dict:
    """只用台股資料的部分（收盤後計算，存到 data 分支給隔天盤前用）"""
    idx = add_indicators(series_of(df, INDEX_CODE).assign(Volume=0.0))
    otc = yf_close("^TWOII")
    otc_dev = otc_dd = otc_px = float("nan")
    if len(otc) >= 200:
        otc_px = float(otc.iloc[-1])
        otc_dev = float((otc.iloc[-1] / otc.rolling(200).mean().iloc[-1] - 1) * 100)
        otc_dd = float((otc.iloc[-1] / otc.iloc[-YEAR:].max() - 1) * 100)
    return {"asof": str(idx.index[-1].date()),
            "tw_px": float(idx["Close"].iloc[-1]), "tw_dev": float(idx["dev200"].iloc[-1]),
            "tw_dd": float(idx["dd252"].iloc[-1]), "breadth": breadth(df),
            "otc_px": otc_px, "otc_dev": otc_dev, "otc_dd": otc_dd}


def external_temperature() -> dict:
    vix, tnx, hy = yf_close("^VIX"), yf_close("^TNX"), fred("BAMLH0A0HYM2")
    last = lambda s: float(s.iloc[-1]) if len(s) else float("nan")
    return {"vix": last(vix), "vix_pct": pct_rank(vix, last(vix)),
            "tnx": last(tnx), "tnx_pct": pct_rank(tnx, last(tnx)),
            "hy": last(hy), "hy_pct": pct_rank(hy, last(hy)),
            "ext_asof": datetime.now(TZ).strftime("%Y-%m-%d %H:%M")}


def gate(t: dict, polarity: str = "contrarian") -> dict:
    nan = float("nan")
    hy = t.get("hy_pct", nan)
    scores = {
        "上市櫃站上200MA比例(反向)": 100 - t.get("breadth", nan),
        "加權指數200MA乖離": dev_score(t.get("tw_dev", nan)),
        "櫃買指數200MA乖離": dev_score(t.get("otc_dev", nan)),
        "VIX 一年分位": t.get("vix_pct", nan),
        "美國高收益債利差分位": hy if polarity == "contrarian" else 100 - hy,
        "美10Y殖利率分位(反向)": 100 - t.get("tnx_pct", nan),
    }
    valid = [v for v in scores.values() if v is not None and not np.isnan(v)]
    score = float(np.mean(valid)) if valid else nan
    return {**t, "scores": scores, "gate_score": score, "gate": gate_label(score),
            "n_valid": len(valid), "n_total": len(scores),
            "heat": heat_label(t.get("tw_dev", 0), t.get("vix_pct", nan))}


def _fmt(v, f="{:+.1f}%"):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else f.format(v)


def temp_line(t: dict) -> str:
    n, total = t.get("n_valid", 6), t.get("n_total", 6)
    partial = f"，只有 {n}/{total} 項有資料" if n < total else ""
    return "\n".join([
        f"🌡 <b>市場溫度：{t['heat']}</b>｜加碼閘門 {_fmt(t['gate_score'], '{:.0f}')}/100（{t['gate']}{partial}）",
        f"加權 {t['tw_px']:,.0f}（距200MA {_fmt(t['tw_dev'])}、距一年高 {_fmt(t['tw_dd'])}）"
        f"｜站上200MA {_fmt(t.get('breadth'), '{:.0f}%')}",
        f"櫃買 {_fmt(t.get('otc_px'), '{:,.2f}')}（距200MA {_fmt(t.get('otc_dev'))}）"
        f"｜外部：VIX {_fmt(t.get('vix'), '{:.1f}')}（分位 {_fmt(t.get('vix_pct'), '{:.0f}%')}）"
        f"、美10Y {_fmt(t.get('tnx'), '{:.2f}%')}",
    ])


def temp_card(t: dict) -> str:
    lines = [temp_line(t), "", "<b>閘門分項</b>（100 = 有利加碼）"]
    lines += [f"• {k}：{_fmt(v, '{:.0f}')}" for k, v in t["scores"].items()]
    lines.append("\n規則：≥70 按計畫加碼｜40～70 減量｜<40 暫停加碼")
    lines.append(f"<i>台股資料 {t['asof']}；分數由固定公式計算，只是紀律檢查，不構成投資建議。</i>")
    return "\n".join(lines)


# ── 提醒規則 ─────────────────────────────────────────────────────
@dataclass
class Alert:
    code: str
    name: str
    kind: str
    level: int
    reason: str
    price: float
    pct: float
    asof: str
    extra: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return LEVEL_NAME[self.level] if self.kind == "drawdown" else KIND_NAME[self.kind]

    @property
    def slot(self) -> str:
        return SLOT[(self.kind, self.level)]

    @property
    def jev_key(self) -> str:
        return JEV_KEY[(self.kind, self.level)]


def vol_multiplier(s: pd.DataFrame, ref: Optional[pd.DataFrame], settings: dict) -> float:
    vs = settings["vol_scale"]
    if not vs.get("enabled") or ref is None or ref.empty:
        return 1.0
    v = s["ret"].iloc[-YEAR:].std()
    r = ref["ret"].iloc[-YEAR:].std()
    if not r or np.isnan(v) or np.isnan(r):
        return 1.0
    return float(np.clip(v / r, vs["min_mult"], vs["max_mult"]))


def crossed_below_ma(s: pd.DataFrame, lookback: int = 3) -> bool:
    """今天收在 200MA 之下，而且最近 N 天內曾在 200MA 之上（剛跌破，不是一直在下面）"""
    c, m = s["Close"], s["ma200"]
    if np.isnan(m.iloc[-1]) or c.iloc[-1] >= m.iloc[-1]:
        return False
    return bool((c.iloc[-1 - lookback:-1] >= m.iloc[-1 - lookback:-1]).any())


def _base(code: str, s: pd.DataFrame) -> dict:
    row = s.iloc[-1]
    ref = row["Ref"] if not np.isnan(row["Ref"]) else (s["Raw"].iloc[-2] if len(s) > 1 else row["Raw"])
    return {"code": code, "name": str(row["name"]), "price": float(row["Raw"]),
            "pct": float((row["Raw"] / ref - 1) * 100) if ref else 0.0, "asof": str(s.index[-1].date())}


def evaluate(code: str, s: pd.DataFrame, settings: dict, mult: float) -> List[Alert]:
    lad, oth = settings["ladder"], settings["other_alerts"]
    row = s.iloc[-1]
    b = _base(code, s)
    out: List[Alert] = []
    l1, l2, l3 = lad["l1_dd20"] * mult, lad["l2_dd20"] * mult, lad["l3_dd252"] * mult
    note = f"，已依波動度 ×{mult:.2f} 校正" if abs(mult - 1) > 1e-9 else ""
    dd20, dd252 = row["dd20"], row["dd252"]
    if not np.isnan(dd252) and dd252 <= -l3:
        out.append(Alert(kind="drawdown", level=3, **b,
                         reason=f"自一年高點回檔 {-dd252:.1f}%（門檻 {l3:.1f}%{note}），進入重大回檔區"))
    elif lad.get("l3_break_ma200") and crossed_below_ma(s):
        out.append(Alert(kind="drawdown", level=3, **b,
                         reason=f"收盤跌破 200 日均線（乖離 {row['dev200']:+.1f}%）"))
    elif not np.isnan(dd20) and dd20 <= -l2:
        out.append(Alert(kind="drawdown", level=2, **b,
                         reason=f"自 20 日高點回檔 {-dd20:.1f}%（門檻 {l2:.1f}%{note}），進入技術修正區間"))
    elif not np.isnan(dd20) and dd20 <= -l1:
        out.append(Alert(kind="drawdown", level=1, **b,
                         reason=f"自 20 日高點回檔 {-dd20:.1f}%（門檻 {l1:.1f}%{note}）"))
    hot = oth["ma200_hot_dev"] * mult
    if not np.isnan(row["dev200"]) and row["dev200"] >= hot:
        out.append(Alert(kind="ma200_hot", level=0, **b,
                         reason=f"正乖離 200 日均線 {row['dev200']:.1f}%（門檻 {hot:.1f}%），短線過熱"))
    if not np.isnan(row["vol_mult"]) and row["vol_mult"] >= oth["volume_spike_mult"]:
        side = "上漲" if row["ret"] > 0 else "下跌"
        out.append(Alert(kind="volume_spike", level=0, **b,
                         reason=f"成交量 {row['Volume']:,.0f} 張，為 20 日均量 {row['vol_mult']:.1f} 倍（{side}日）"))
    if not np.isnan(row["bbw_pct"]) and row["bbw_pct"] <= oth["squeeze_pct"]:
        out.append(Alert(kind="squeeze", level=0, **b,
                         reason=f"布林通道寬度 {row['bbw']:.1f}%，落在一年內最低 {row['bbw_pct']:.0f}% 分位，波動極度收斂"))
    return out


def check_invalidation(item: dict, s: pd.DataFrame, foreign=finmind_foreign_net) -> List[str]:
    """可由程式判斷的推翻條件；文字條件（指引下修等）留給週覆盤逐條確認"""
    hits = []
    row = s.iloc[-1]
    for r in (item.get("invalidation") or {}).get("rules") or []:
        t = r.get("type")
        if t == "close_below" and r.get("value") is not None and row["Raw"] < float(r["value"]):
            hits.append(f"收盤 {row['Raw']:,.2f} 跌破關鍵支撐 {float(r['value']):,.2f}")
        elif t == "break_ma":
            n = int(r.get("days", 200))
            ma = s["Close"].rolling(n).mean().iloc[-1]
            if not np.isnan(ma) and row["Close"] < ma:
                hits.append(f"收盤位於 {n} 日均線之下")
        elif t == "drawdown_from_high" and r.get("pct") is not None and row["dd252"] <= -float(r["pct"]):
            hits.append(f"自一年高點回檔 {-row['dd252']:.1f}%（≥ {r['pct']}%）")
        elif t == "foreign_net_sell_streak":
            n = int(r.get("days", 10))
            net = foreign(item["code"], n + 10)
            if len(net) >= n and (net.iloc[-n:] < 0).all():
                hits.append(f"外資連續 {n} 個交易日賣超")
    return hits


def rule_text(r: dict) -> Optional[str]:
    t = r.get("type")
    if t == "close_below":
        return f"收盤跌破 {float(r['value']):,.2f}" if r.get("value") is not None else None
    if t == "break_ma":
        return f"跌破 {r.get('days', 200)} 日均線"
    if t == "foreign_net_sell_streak":
        return f"外資連續 {r.get('days', 10)} 日賣超"
    if t == "drawdown_from_high":
        return f"自一年高點回檔 ≥ {r.get('pct')}%"
    return str(r)


# ── 冷卻 ─────────────────────────────────────────────────────────
def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _write_json(p: Path, d: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=float), encoding="utf-8")


def should_send(cd: dict, a: Alert, hours: float, now: datetime) -> bool:
    rec = cd.get(f"{a.code}|{a.kind}")
    if not rec:
        return True
    if now - datetime.fromisoformat(rec["ts"]) >= timedelta(hours=hours):
        return True
    return a.level > rec.get("level", 0)          # 等級升高立即再通知


def mark_sent(cd: dict, a: Alert, now: datetime):
    cd[f"{a.code}|{a.kind}"] = {"ts": now.isoformat(timespec="seconds"), "level": a.level, "asof": a.asof}


# ── 推播卡片 ─────────────────────────────────────────────────────
ICON = {1: "🟡", 2: "🟠", 3: "🔴"}


def alert_card(a: Alert, item: dict, temp: Optional[dict], news_block: str = "") -> str:
    icon = "⛔" if a.kind == "invalidation" else (ICON.get(a.level, "🔔") if a.kind == "drawdown" else "🔔")
    e = html.escape
    lines = [f"{icon} <b>【觀察名單｜{a.title}】{e(item.get('name') or a.name)}（{a.code}）</b>",
             f"💵 {a.price:,.2f}（{a.pct:+.2f}%）｜資料日 {a.asof}"]
    if temp:
        lines += ["", temp_line(temp)]
    lines += ["", f"⚠️ <b>觸發原因</b>：{e(a.reason)}",
              "", "📌 <b>核心投資理由</b>", e(" ".join(str(item.get("thesis") or "（未填，請在 watchlist.yaml 補上）").split())),
              "", "🚫 <b>推翻條件</b>"]
    inv = item.get("invalidation") or {}
    conds = list(inv.get("text") or []) + [x for x in (rule_text(r) for r in inv.get("rules") or []) if x]
    lines += [f"• {e(str(c))}" for c in conds] or ["（未填，沒有推翻條件就無法覆盤）"]
    if a.extra.get("hits"):
        lines += ["<b>目前已觸發：</b>"] + [f"• {e(h)}" for h in a.extra["hits"]]
    if news_block:
        lines.append(news_block)
    lines += ["", f"<i>加入觀察 {item.get('added', '—')}｜來源 {e(str(item.get('source', '—')))}</i>",
              "<i>此提醒只代表「請深入檢視現況」，不是買進或賣出訊號。</i>",
              f"🔗 <a href='https://tw.stock.yahoo.com/quote/{a.code}'>查看行情</a>"]
    return "\n".join(lines)


def _news(a: Alert, settings: dict):
    """Jev 新聞查證；沒設 TYPESAFE_API_KEY 或這類提醒不查時回傳 (None, '')"""
    if a.jev_key not in (settings.get("jev_news_for") or []):
        return None, ""
    try:
        from news_check import check_news, format_news_block
        result = check_news(a.code, a.name)
        return result, format_news_block(result, a.pct)
    except Exception as e:
        logger.warning("新聞查證失敗 %s：%s", a.code, str(e)[:80])
        return None, ""


# ── 收盤後 ───────────────────────────────────────────────────────
def run_post(end: date, now: Optional[datetime] = None, df: Optional[pd.DataFrame] = None,
             ext: Optional[dict] = None, force: bool = False, news=_news, foreign=finmind_foreign_net,
             send=send_telegram) -> dict:
    now = now or datetime.now(TZ)
    cfg = load_watchlist()
    settings = cfg["settings"]
    df = load_market(end) if df is None else df
    if df.empty:
        logger.error("沒有歷史資料（data 分支），略過")
        return {"sent": 0, "skipped": "no-data"}
    data_date = str(df["date"].max())
    cd = _read_json(COOLDOWN_FILE)
    if cd.get("_last_date") == data_date and not force:
        logger.info("%s 的觀察名單已檢查過，略過（手動重跑請設 FORCE_SCAN=1）", data_date)
        return {"sent": 0, "skipped": "done"}

    temp = None
    try:
        tw = tw_temperature(df)
        temp = gate({**tw, **(ext if ext is not None else external_temperature())},
                    settings.get("hy_spread_polarity", "contrarian"))
        _write_json(TEMP_FILE, temp)
    except Exception as e:
        logger.warning("市場溫度計算失敗：%s", e)

    items = cfg["watchlist"]
    if not items:
        logger.info("watchlist.yaml 沒有觀察標的，只更新市場溫度")
    ref_code = str(settings["vol_scale"].get("reference") or "2330")
    ref = series_of(df, ref_code)
    ref = add_indicators(ref) if not ref.empty else None
    sent = 0
    for item in items:
        s = series_of(df, item["code"])
        if len(s) < 30:
            logger.warning("%s 資料不足（%d 根），略過", item["code"], len(s))
            continue
        s = add_indicators(s)
        mult = vol_multiplier(s, ref, settings)
        alerts = evaluate(item["code"], s, settings, mult)
        hits = check_invalidation(item, s, foreign)
        if hits:
            b = _base(item["code"], s)
            alerts.append(Alert(kind="invalidation", level=0, reason="；".join(hits), extra={"hits": hits}, **b))
        for a in alerts:
            if not should_send(cd, a, settings["cooldown_hours"], now):
                logger.info("冷卻中略過：%s %s", a.code, a.title)
                continue
            result, block = news(a, settings)
            if send(alert_card(a, item, temp, block)) or not os.environ.get("TELEGRAM_BOT_TOKEN"):
                mark_sent(cd, a, now)
                sent += 1
                journal.record("watch", a.code, item.get("name") or a.name, slot=a.slot, price=a.price,
                               pct=a.pct, alerts=a.reason, news=result, date=data_date)
    cd["_last_date"] = data_date
    _write_json(COOLDOWN_FILE, cd)
    logger.info("觀察名單 %d 檔，推播 %d 則", len(items), sent)
    return {"sent": sent, "temp": temp}


# ── 開盤前 ───────────────────────────────────────────────────────
def is_holiday(d: date) -> Optional[bool]:
    """TWSE 休市日程；查不到時回傳 None（當作有開盤）"""
    try:
        r = requests.get("https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule", timeout=15)
        rows = r.json()
    except Exception:
        return None
    roc = f"{d.year - 1911}{d:%m%d}"
    keys = {roc, d.strftime("%Y%m%d"), d.isoformat()}
    for row in rows or []:
        vals = {str(v).replace("/", "").replace("-", "") for v in row.values()} | {str(v) for v in row.values()}
        if keys & vals:
            return True
    return False


def run_pre(now: Optional[datetime] = None, ext: Optional[dict] = None, holiday=is_holiday,
            send=send_telegram) -> bool:
    now = now or datetime.now(TZ)
    if now.weekday() >= 5 or now.hour * 60 + now.minute >= 9 * 60 + 30:
        logger.info("非開盤前時段（%s），不推市場溫度卡", now.strftime("%a %H:%M"))
        return False
    today = now.date().isoformat()
    if _read_json(CARD_STATE_FILE).get("sent") == today and os.environ.get("FORCE_CARD") != "1":
        logger.info("今天的市場溫度卡已推過")
        return False
    if holiday(now.date()):
        logger.info("今天休市，不推市場溫度卡")
        return False
    stored = _read_json(TEMP_FILE)
    if not stored.get("asof"):
        logger.warning("找不到 %s（收盤後的觀察名單步驟還沒跑過），略過", TEMP_FILE)
        return False
    settings = load_watchlist()["settings"]
    tw_keys = ("asof", "tw_px", "tw_dev", "tw_dd", "breadth", "otc_px", "otc_dev", "otc_dd")
    t = gate({**{k: stored.get(k, float("nan")) for k in tw_keys},
              **(ext if ext is not None else external_temperature())},
             settings.get("hy_spread_polarity", "contrarian"))
    if send("☀️ <b>開盤前市場溫度</b>\n" + temp_card(t)) or not os.environ.get("TELEGRAM_BOT_TOKEN"):
        _write_json(CARD_STATE_FILE, {"sent": today})
        return True
    return False


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "post"
    if mode == "pre":
        run_pre()
        return
    end = datetime.now(TZ).date()
    if os.environ.get("SCAN_DATE"):
        end = datetime.strptime(os.environ["SCAN_DATE"], "%Y%m%d").date()
    res = run_post(end, force=os.environ.get("FORCE_SCAN") == "1")
    if not res.get("skipped"):
        history.checkpoint(f"觀察名單 {end}")


if __name__ == "__main__":
    main()
