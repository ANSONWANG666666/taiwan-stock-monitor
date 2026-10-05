"""回測：進出場模擬、成本、一字漲跌停、指標計算"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from pattern_data import chuaner_bars, frame, heifeiwu_bars, ramp, series  # noqa: E402
import backtest as bt  # noqa: E402
import config          # noqa: E402
import screener        # noqa: E402

HFW_ENTRY = 131.0


def S_of(bars):
    return bt.with_locked_down(series(bars))


def no_cost():
    c = config.Config()
    c.FEE_RATE = c.TAX_RATE = c.SLIPPAGE = 0.0
    return c


# ── 黑飛舞三種出場 ───────────────────────────────────────────────
def test_hfw_rule1_limit_up_sell_day3_open():
    S = S_of(heifeiwu_bars() + [(132, 144, 131, 144, 3000), (146, 150, 140, 141, 3000)])
    t = bt.simulate_heifeiwu(S, S.n - 3, no_cost())
    assert t["rule"] == 1 and t["ret"] == pytest.approx(146 / HFW_ENTRY - 1)
    assert t["exit_idx"] == S.n - 1


def test_hfw_rule2_half_close_half_next_open():
    S = S_of(heifeiwu_bars() + [(132, 138, 131, 135, 3000), (136, 137, 134, 135, 2000)])
    t = bt.simulate_heifeiwu(S, S.n - 3, no_cost())
    assert t["rule"] == 2 and t["ret"] == pytest.approx((0.5 * 135 + 0.5 * 136) / HFW_ENTRY - 1)


def test_hfw_rule3_sell_all_day2_close():
    S = S_of(heifeiwu_bars() + [(132, 135, 130, 133, 3000)])
    t = bt.simulate_heifeiwu(S, S.n - 2, no_cost())
    assert t["rule"] == 3 and t["ret"] == pytest.approx(133 / HFW_ENTRY - 1)


def test_hfw_costs_applied():
    S = S_of(heifeiwu_bars() + [(132, 135, 130, 133, 3000)])
    c = config.Config()
    t = bt.simulate_heifeiwu(S, S.n - 2, c)
    assert t["ret"] == pytest.approx(133 * (1 - c.sell_cost) / (HFW_ENTRY * (1 + c.buy_cost)) - 1)
    assert t["ret"] < 133 / HFW_ENTRY - 1


def test_hfw_unfinished_returns_none():
    S = S_of(heifeiwu_bars())
    assert bt.simulate_heifeiwu(S, S.n - 1) is None


def test_locked_limit_down_delays_sale():
    # Day 2 一字跌停（131 → 118，tick 0.5 → 跌停 118.0）賣不掉，Day 3 開盤 115 才賣
    S = S_of(heifeiwu_bars() + [(118, 118, 118, 118, 100), (115, 117, 112, 113, 3000)])
    assert S.locked_down[S.n - 2]
    t = bt.simulate_heifeiwu(S, S.n - 3, no_cost())
    assert t["rule"] == 3 and t["ret"] == pytest.approx(115 / HFW_ENTRY - 1) and t["exit_idx"] == S.n - 1


# ── 波段 ─────────────────────────────────────────────────────────
def chuaner_signal(S, i=85):
    s = screener.detect_chuaner(S, i)
    assert s
    return s


def test_swing_amplitude_target_then_ma20_exit():
    up = ramp(5, 128, 141)                                # 最高 ≈142.4：過振幅法目標 139.6
    crash = [(124, 124.5, 118, 118.5, 3000)]
    S = S_of(chuaner_bars() + up + crash)
    sig = chuaner_signal(S)
    t = bt.simulate_swing(S, 85, sig, "amplitude", no_cost())
    tgt = sig["base"] * (1 + sig["a_amp"] * 0.5)
    assert t["hit"] and t["days_to_target"] >= 1 and not t["below20_before_target"]
    assert t["ret"] == pytest.approx((0.5 * tgt + 0.5 * 118.5) / 126 - 1, rel=1e-6)


def test_swing_rally_mode_stops_at_ma20_before_target():
    S = S_of(chuaner_bars() + ramp(5, 128, 141) + [(124, 124.5, 118, 118.5, 3000)])
    t = bt.simulate_swing(S, 85, chuaner_signal(S), "rally", no_cost())
    assert not t["hit"] and t["below20_before_target"] and t["days_to_target"] is None
    assert t["ret"] == pytest.approx(118.5 / 126 - 1)


def test_swing_no_stop_runs_to_max_hold():
    c = no_cost()
    c.BT_STOP_BEFORE_TARGET = "none"
    c.MAX_HOLD_DAYS = 5
    S = S_of(chuaner_bars() + [(124, 124.5, 118, 118.5, 3000)] * 6)
    t = bt.simulate_swing(S, 85, chuaner_signal(S), "rally", c)
    assert t["max_hold"] and t["exit_idx"] == 90 and t["below20_before_target"]


def test_swing_gap_above_target_fills_at_open():
    S = S_of(chuaner_bars() + [(150, 152, 149, 151, 3000), (150, 151, 120, 121, 3000)])
    sig = chuaner_signal(S)
    t = bt.simulate_swing(S, 85, sig, "amplitude", no_cost())   # 目標 139.6，開盤 150 跳空
    assert t["hit"] and t["ret"] == pytest.approx((0.5 * 150 + 0.5 * 121) / 126 - 1)


def test_chuaner_limit_up_enters_next_open_or_nofill():
    bars = chuaner_bars(limit=True)
    S = S_of(bars + [(131, 133, 130, 132, 3000)] + flat_after(3))
    sig = chuaner_signal(S)
    t = bt.simulate_swing(S, 85, sig, "rally", no_cost())
    assert t is not None and t.get("entry") == 131
    lu = 142.0                                                    # 129.5 的漲停價
    S2 = S_of(bars + [(lu, lu, lu, lu, 100)] + flat_after(3))
    t2 = bt.simulate_swing(S2, 85, screener.detect_chuaner(S2, 85), "rally", no_cost())
    assert t2 == {"nofill": True, "entry_idx": 86, "exit_idx": 86}


def flat_after(n, p=120.0):
    return [(p, p + 1, p - 1, p, 1000)] * n


# ── 指標 ─────────────────────────────────────────────────────────
def test_metrics_drawdown_and_winrate():
    t = pd.DataFrame({"ret": [0.10, -0.05, -0.08, 0.20], "hold": [1, 2, 3, 4],
                      "exit_date": ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04"]})
    m = bt.metrics(t)
    assert m["n"] == 4 and m["win"] == 0.5 and m["avg"] == pytest.approx(0.0425)
    assert m["mdd"] == pytest.approx(0.13) and m["total"] == pytest.approx(0.17)
    assert m["pf"] == pytest.approx(0.30 / 0.13)


def test_metrics_excludes_nofill():
    t = pd.DataFrame({"ret": [0.1, np.nan], "hold": [1, 0], "exit_date": ["2025-01-01", "2025-01-02"],
                      "nofill": [np.nan, True]})
    assert bt.metrics(t)["n"] == 1


def test_mode_compare():
    t = pd.DataFrame({"ret": [0.1, -0.05, 0.02], "hit": [True, False, True], "days_to_target": [3, None, 5],
                      "below20_before_target": [False, True, False]})
    m = bt.mode_compare(t)
    assert m["hit_rate"] == pytest.approx(2 / 3) and m["days_to_target"] == 4 and m["below20"] == pytest.approx(1 / 3)


def test_drop_overlap():
    df = pd.DataFrame({"code": ["1", "1", "1", "2"], "pattern": ["黑飛舞"] * 4, "mode": ["—"] * 4,
                       "entry_idx": [10, 12, 20, 11], "exit_idx": [15, 18, 22, 13]})
    assert sorted(bt.drop_overlap(df)["entry_idx"]) == [10, 11, 20]


def test_strong_by_date():
    bars_up = ramp(30, 100, 130)
    bars_dn = ramp(30, 130, 100)
    ser = {c: series(bars_up, code=c) for c in "ABCD"} | {c: series(bars_dn, code=c) for c in "EFG"}
    groups = {"強": list("ABCD"), "弱": ["E", "F", "G", "A"], "太少": ["A", "B"]}
    s = bt.strong_by_date(ser, None, groups)
    last = s.index[-1]
    assert bool(s.at[last, "強"]) and not bool(s.at[last, "弱"]) and "太少" not in s.columns


# ── 整體流程（假資料）─────────────────────────────────────────────
def test_main_end_to_end(tmp_path, monkeypatch):
    up = ramp(5, 128, 141) + [(124, 124.5, 118, 118.5, 3000)]
    frames = [frame(chuaner_bars() + up, code="1111", name="穿二股"),
              frame(heifeiwu_bars() + [(132, 138, 131, 135, 3000), (136, 137, 134, 135, 2000)] + flat_after(4, 133),
                    code="2222", name="黑飛舞股")]
    df = pd.concat(frames, ignore_index=True)
    monkeypatch.setattr(bt.history, "load", lambda *a, **k: df.copy())
    monkeypatch.setattr(bt.history, "refresh_industry", lambda *a, **k: {
        c: {"name": "", "market": "TSE", "industry": "測試業"} for c in ("1111", "2222")})
    monkeypatch.setattr(bt, "WARMUP_DAYS", 0)
    monkeypatch.setattr(bt, "OUT_DIR", tmp_path)
    sent = []
    monkeypatch.setattr(bt, "send_telegram", sent.append)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    bt.main()
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    trades = pd.read_csv(tmp_path / "trades.csv", dtype={"code": str})
    assert set(trades["pattern"]) == {"穿山二龍", "黑飛舞"}
    assert set(trades.loc[trades["pattern"] == "穿山二龍", "mode"]) == {"rally", "amplitude"}
    assert "A_MODE 比較" in report and "黑飛舞 Day 2 出場規則分布" in report
    assert sent and "型態回測" in sent[0]
