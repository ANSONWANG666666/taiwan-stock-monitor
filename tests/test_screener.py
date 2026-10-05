"""階段 2：穿山二龍、黑飛舞、三角收斂、族群強弱、領頭羊"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from pattern_data import (chuaner_bars, flat, frame, heifeiwu_bars, ramp, series,  # noqa: E402
                          triangle_bars)
import config     # noqa: E402
import screener   # noqa: E402


def signals(S, det):
    return [(i, s) for i in range(S.n) if (s := det(S, i))]


# ── 穿山二龍三階段 ───────────────────────────────────────────────
def test_chuaner_three_stages():
    S = series(chuaner_bars())
    hits = signals(S, screener.detect_chuaner)
    assert len(hits) == 1 and hits[0][0] == S.n - 1
    s = hits[0][1]
    assert s["entry_type"] == "收盤進場"
    assert s["a_rally"] == pytest.approx(140 * 1.01 / (100 * 0.99) - 1, rel=1e-3)   # 第1階段 低→高
    assert s["swing_high"] > s["base"] > s["pattern_low"]


def test_chuaner_limit_up_enters_next_open():
    s = screener.detect_chuaner(series(chuaner_bars(limit=True)), 85)
    assert s and s["entry_type"] == "次日開盤進場"


def test_chuaner_needs_30pct_rally():
    assert not signals(series(chuaner_bars(rally_to=125)), screener.detect_chuaner)


def test_chuaner_needs_strong_candle():
    assert not signals(series(chuaner_bars(final_close=120.5)), screener.detect_chuaner)


def test_chuaner_stage3_pct_configurable():
    cfg = config.Config()
    cfg.CHUANER_STAGE3_PCT = 0.08                       # 6.8% 不夠
    assert screener.detect_chuaner(series(chuaner_bars()), 85, cfg) is None


# ── 黑飛舞 ───────────────────────────────────────────────────────
def test_heifeiwu_day0_to_day1():
    S = series(heifeiwu_bars())
    hits = signals(S, screener.detect_heifeiwu)
    assert len(hits) == 1 and hits[0][0] == S.n - 1
    assert hits[0][1]["day0"] == S.date[S.n - 2] and hits[0][1]["entry_type"] == "收盤進場"


def test_heifeiwu_no_volume_shrink_no_signal():
    assert not signals(series(heifeiwu_bars(day1_vol_ratio=0.8)), screener.detect_heifeiwu)


def test_heifeiwu_break_ma5_invalidates():
    assert not signals(series(heifeiwu_bars(break_ma5=True)), screener.detect_heifeiwu)


def test_heifeiwu_needs_burst_volume():
    bars = heifeiwu_bars()
    o, h, l, c, v = bars[-2]
    bars[-2] = (o, h, l, c, 1500)                       # Day0 只有 1.5 倍均量
    assert not signals(series(bars), screener.detect_heifeiwu)


# ── 三角收斂 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("kind,label", [("sym", "對稱三角"), ("asc", "上升三角")])
def test_triangle_breakout(kind, label):
    S = series(triangle_bars(kind))
    s = screener.detect_triangle(S, S.n - 1)
    assert s and s["tri_kind"] == label and s["bars"] >= 15
    assert s["a_amp"] > 0 and s["base"] == S.c[-1]


def test_triangle_needs_volume():
    S = series(triangle_bars("sym", breakout_vol=1000))
    assert screener.detect_triangle(S, S.n - 1) is None


def test_triangle_not_in_trend():
    S = series(flat(40) + ramp(60, 100, 160) + [(160, 166, 159, 165, 4000)])
    assert screener.find_triangle(S, S.n - 1) is None


def test_longer_triangle_scores_higher():
    S1, S2 = series(triangle_bars("sym", n=50)), series(triangle_bars("sym", n=30))
    long_, short = screener.detect_triangle(S1, S1.n - 1), screener.detect_triangle(S2, S2.n - 1)
    assert long_ and short and long_["bars"] > short["bars"] and long_["score"] > short["score"]


# ── 目標價（兩種 A）────────────────────────────────────────────────
def test_targets_both_modes():
    s = {"base": 100.0, "a_rally": 0.4, "a_amp": 0.2}
    assert screener.targets(s) == {"rally": pytest.approx(120.0), "amp": pytest.approx(110.0)}


# ── 族群強弱、領頭羊 ─────────────────────────────────────────────
def test_group_strength_threshold():
    groups = {"大族群": [f"A{i}" for i in range(20)], "小族群": ["B1", "B2", "B3", "B4", "B5"],
              "太小": ["C1", "C2"]}
    snap = {f"A{i}": {"above20": i < 5, "rs5": -1} for i in range(20)}        # 5/20 = 25%
    snap |= {f"B{i}": {"above20": i <= 4, "rs5": -1} for i in range(1, 6)}   # 4/5
    snap |= {"C1": {"above20": True, "rs5": 1}, "C2": {"above20": True, "rs5": 1}}
    st = screener.group_strength(groups, snap)
    assert not st["大族群"]["strong"]           # ≥4 檔但不到 30%
    assert st["小族群"]["strong"]
    assert "太小" not in st


def _stock(code, bars):
    return screener.prepare(__import__("history").adjust(frame(bars, code=code, name=code)))


def test_leader_ranking_and_rotation():
    # A 最早創新高且漲最多 → 領頭羊；之後 A 停滯、B 創新高 → 換手
    a = _stock("A", flat(60) + ramp(20, 100, 150) + flat(10, 149))
    b = _stock("B", flat(60) + ramp(28, 100, 120) + ramp(2, 121, 160))
    c = _stock("C", flat(60) + ramp(30, 100, 110))
    tx = _stock("IX", flat(90, 20000))
    series = {"A": a, "B": b, "C": c}
    ranked = screener.rank_leaders(["A", "B", "C"], series, tx)
    assert ranked[-1] == "C"
    state = {"G": {"code": "A", "date": "x"}}
    msg = screener.check_rotation("G", ranked[0], series, state, ["A", "B", "C"])
    assert state["G"]["code"] == "B" and msg and "換成 B" in msg


def test_screen_end_to_end(tmp_path, monkeypatch):
    # 4 檔同族群都站上 20MA（強勢），其中 1 檔出現黑飛舞
    frames = [frame(heifeiwu_bars(), code="1111", name="飛舞")]
    for k, code in enumerate(("2222", "3333", "4444")):
        frames.append(frame(flat(60) + ramp(22, 100, 115 + k), code=code, name=code))
    frames.append(frame(flat(82, 20000), code="IX0001", name="加權指數"))
    df = pd.concat(frames)
    df["amount"] = df["amount"] * 100                    # 通過流動性門檻
    industry = {c: {"name": c, "market": "TSE", "industry": "測試族群"} for c in ("1111", "2222", "3333", "4444")}
    monkeypatch.setattr(screener, "CONCEPT_FILE", tmp_path / "none.yaml")
    res = screener.screen(df, industry, {}, held={"1111"})
    assert res["strong_groups"]["測試族群"]["strong_n"] == 4
    c = res["candidates"]
    assert len(c) == 1 and c[0]["pattern"] == "黑飛舞" and c[0]["action"] == "加碼"
    msg = screener.format_report(res)
    assert "型態選股" in msg and "【加碼】" in msg and "測試族群" in msg
