"""型態測試用的合成 K 線"""

import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history    # noqa: E402
import screener   # noqa: E402

DAYS = pd.bdate_range("2025-01-02", periods=400).strftime("%Y-%m-%d").tolist()


def frame(bars, code="9999", name="測試"):
    """bars: [(open, high, low, close, volume), ...]；ref 用前一日收盤"""
    rows, prev = [], None
    for k, (o, h, l, c, v) in enumerate(bars):
        rows.append({"date": DAYS[k], "code": code, "name": name, "market": "TSE",
                     "open": o, "high": h, "low": l, "close": c, "volume": v,
                     "amount": c * v * 1000, "ref": prev if prev is not None else c})
        prev = c
    return pd.DataFrame(rows)


def series(bars, **kw):
    return screener.prepare(history.adjust(frame(bars, **kw)))


def flat(n, p=100.0, v=1000):
    return [(p, p * 1.01, p * 0.99, p, v) for _ in range(n)]


def ramp(n, p0, p1, v=1000):
    out = []
    for k in range(n):
        p = p0 + (p1 - p0) * (k + 1) / n
        out.append((p * 0.995, p * 1.01, p * 0.985, p, v))
    return out


def chuaner_bars(rally_to=140.0, final_close=None, limit=False):
    """平盤 → 漲到 rally_to → 快速回落跌破 20MA → 盤整 → 帶量紅K站回"""
    bars = flat(40) + ramp(30, 100, rally_to) + ramp(5, rally_to, 118) + flat(10, 118)
    prev = bars[-1][3]
    if limit:
        c = 129.5                                    # 118 的漲停價
        bars.append((119, c, 118.5, c, 4000))
    else:
        c = final_close or 126.0                     # +6.8%
        bars.append((118.5, c * 1.005, 118, c, 4000))
    return bars


def heifeiwu_bars(day1_vol_ratio=0.5, break_ma5=False):
    """上漲創 60 日新高 → Day0 爆量黑K → Day1 量縮回測 5MA"""
    bars = flat(60) + ramp(20, 100, 130)
    # Day0：開高走低的黑K，量 5000（約 5 倍均量），收盤仍高於 5MA
    bars.append((134, 136, 129.5, 130.5, 5000))
    if break_ma5:
        bars.append((130, 130.5, 124, 125, 1500))    # 收盤跌破 5MA → 失效
    # Day1：最低碰到 5MA 附近、收在 5MA 上，量縮
    bars.append((130.5, 131.5, 128.2, 131.0, int(5000 * day1_vol_ratio)))
    return bars


def triangle_bars(kind="sym", breakout_vol=3000, n=40):
    """前段上漲 → 收斂整理 40 天 → 帶量突破"""
    bars = flat(40) + ramp(20, 100, 120)
    for k in range(n):
        if kind == "sym":
            amp = 8 * (1 - k / n) + 0.8
            mid = 116
        else:                                         # 上升三角：上緣 122 持平、下緣墊高
            amp = (122 - (106 + 14 * k / n)) / 2 + 0.3
            mid = 122 - amp
        p = mid + amp * math.sin(k * 2 * math.pi / 10)
        bars.append((p, p + 0.6, p - 0.6, p, 800))
    last = bars[-1][3]
    c = (124.5 if kind == "sym" else 125.5)
    bars.append((last, c + 0.3, last - 0.2, c, breakout_vol))
    return bars
