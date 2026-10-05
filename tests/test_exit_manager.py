"""階段 3：出場管理（黑飛舞三種情境、波段目標價、A_MODE 切換、盤中 13:20）"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from pattern_data import DAYS, chuaner_bars, frame, heifeiwu_bars, ramp, series  # noqa: E402
import config        # noqa: E402
import exit_manager  # noqa: E402
from exit_manager import TZ  # noqa: E402

HFW_ENTRY = 131.0          # heifeiwu_bars 的 Day 1 收盤
HFW_LIMIT = 144.0          # 131 的漲停價


def cfg(**kw):
    c = config.Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ── 黑飛舞：決策函式 ─────────────────────────────────────────────
def test_hfw_rule1_limit_up_sell_all_next_open():
    d = exit_manager.heifeiwu_decision(HFW_ENTRY, HFW_LIMIT, True)
    assert d["rule"] == 1 and d["sell_close"] == 0 and d["sell_next_open"] == 1.0


def test_hfw_rule2_gain_over_5pct_split():
    d = exit_manager.heifeiwu_decision(HFW_ENTRY, 138.0, False)       # +5.3%
    assert d["rule"] == 2 and d["sell_close"] == 0.5 and d["sell_next_open"] == 0.5


def test_hfw_rule3_gain_under_5pct_sell_all_close():
    d = exit_manager.heifeiwu_decision(HFW_ENTRY, 135.0, False)       # +3.1%
    assert d["rule"] == 3 and d["sell_close"] == 1.0 and d["sell_next_open"] == 0


def test_hfw_exit_gain_configurable():
    d = exit_manager.heifeiwu_decision(HFW_ENTRY, 135.0, False, cfg(HFW_EXIT_GAIN=0.03))
    assert d["rule"] == 2


# ── 黑飛舞：用日K評估持股 ────────────────────────────────────────
def hfw_df(day2):
    bars = heifeiwu_bars() + [day2]
    return frame(bars), DAYS[len(bars) - 2]          # 進場日 = Day 1


@pytest.mark.parametrize("day2, rule_text", [
    ((132, HFW_LIMIT, 131, HFW_LIMIT, 3000), "Day 2 漲停"),
    ((132, 138, 131, 135, 3000), "今天收盤賣 50%"),
    ((132, 135, 130, 133, 3000), "今天收盤賣出 100%"),
])
def test_hfw_evaluate_day2(day2, rule_text):
    df, entry = hfw_df(day2)
    h = {"code": "9999", "strategy": "黑飛舞", "entry_date": entry, "entry_price": HFW_ENTRY}
    state = {}
    alerts = exit_manager.run([h], df, state)
    assert len(alerts) == 1 and rule_text in alerts[0]
    assert exit_manager.run([h], df, state) == []        # 同一天不重複推播


def test_hfw_not_day2_yet():
    bars = heifeiwu_bars()
    h = {"code": "9999", "strategy": "黑飛舞", "entry_date": DAYS[len(bars) - 1], "entry_price": HFW_ENTRY}
    assert exit_manager.run([h], frame(bars), {}) == []


def test_hfw_after_day2_reminds_once():
    bars = heifeiwu_bars() + [(132, 135, 130, 133, 3000), (133, 134, 131, 132, 2000)]
    h = {"code": "9999", "strategy": "黑飛舞", "entry_date": DAYS[len(bars) - 3], "entry_price": HFW_ENTRY}
    state = {}
    alerts = exit_manager.run([h], frame(bars), state)
    assert len(alerts) == 1 and "已過 Day 2" in alerts[0]
    assert exit_manager.run([h], frame(bars), state) == []


# ── 波段：目標價、A_MODE、移動出場 ──────────────────────────────
ENTRY_IDX = 85                                   # chuaner_bars 的訊號日


def swing_bars(after):
    return chuaner_bars() + after


def holding(**kw):
    return {"code": "9999", "strategy": "穿山二龍", "entry_date": DAYS[ENTRY_IDX], "entry_price": 126.0, **kw}


def test_swing_params_recomputed_from_entry_date():
    S = series(swing_bars(ramp(3, 126, 130)))
    p = exit_manager.swing_params(S, ENTRY_IDX, "穿山二龍")
    assert p["base"] == pytest.approx(126.0) and p["signal_idx"] == ENTRY_IDX
    t = exit_manager.swing_targets(p)
    assert t["rally"] == pytest.approx(126 * (1 + p["a_rally"] * 0.5))
    assert t["amplitude"] == pytest.approx(126 * (1 + p["a_amp"] * 0.5))
    assert t["rally"] > t["amplitude"]


def test_swing_params_next_open_entry_uses_previous_day():
    bars = chuaner_bars(limit=True) + [(130, 132, 129, 131, 3000)]
    S = series(bars)
    p = exit_manager.swing_params(S, ENTRY_IDX + 1, "穿山二龍")      # 漲停隔日開盤進場
    assert p and p["signal_idx"] == ENTRY_IDX


@pytest.mark.parametrize("mode, hit", [("amplitude", True), ("rally", False)])
def test_swing_a_mode_switch(mode, hit):
    df = frame(swing_bars(ramp(5, 128, 141)))          # 最高 ≈142.4：過振幅法目標 139.6、未到漲幅法 153
    alerts = exit_manager.run([holding()], df, {}, cfg(A_MODE=mode))
    assert any("碰到目標價" in a for a in alerts) is hit
    if alerts:
        assert "漲幅法" in alerts[0] and "振幅法" in alerts[0]   # 兩種目標價都顯示


def test_swing_exit_after_target_when_below_ma20():
    c = cfg(A_MODE="amplitude")
    up = ramp(5, 128, 141)
    state = {}
    exit_manager.run([holding()], frame(swing_bars(up)), state, c)
    assert state["9999"]["target_hit"]
    crash = up + [(124, 124.5, 118, 118.5, 3000)]          # 跌破 20MA
    alerts = exit_manager.run([holding()], frame(swing_bars(crash)), state, c)
    assert any("出清" in a and "20MA" in a for a in alerts)


def test_swing_exit_on_swing_low_break():
    S = series(swing_bars(ramp(5, 128, 141) + [(140, 141, 139, 140, 2000)]))
    i = S.n - 1
    params = exit_manager.swing_params(S, ENTRY_IDX, "穿山二龍")
    # 人為把今天收盤壓到近 10 日低點以下、但仍在 20MA 之上
    S.c, S.ma20 = S.c.copy(), S.ma20.copy()
    S.c[i] = float(S.l[i - 10:i].min()) - 0.1
    S.ma20[i] = S.c[i] - 1
    d = exit_manager.swing_decision(S, i, params, True)
    assert [a[0] for a in d["actions"]] == ["exit"] and "波段低點" in d["actions"][0][1]


def test_swing_warn_below_ma20_before_target():
    crash = [(124, 124.5, 115, 116, 3000)]
    alerts = exit_manager.run([holding()], frame(swing_bars(crash)), {})
    assert len(alerts) == 1 and alerts[0].startswith("⚠️") and "尚未達標" in alerts[0]


def test_swing_sold_half_skips_target_alert():
    df = frame(swing_bars(ramp(5, 128, 141)))
    alerts = exit_manager.run([holding(sold_half=True)], df, {}, cfg(A_MODE="amplitude"))
    assert not any("碰到目標價" in a for a in alerts)


def test_swing_manual_params_override():
    df = frame(swing_bars(ramp(5, 128, 141)))
    h = holding(base=126.0, a_rally=0.10, a_amp=0.10)       # 目標 132.3，已過
    alerts = exit_manager.run([h], df, {}, cfg(A_MODE="rally"))
    assert any("碰到目標價" in a for a in alerts)


def test_swing_missing_pattern_warns():
    df = frame(swing_bars(ramp(3, 126, 130)))
    h = holding(entry_date=DAYS[50])                          # 那天沒有型態
    alerts = exit_manager.run([h], df, {})
    assert len(alerts) == 1 and "找不到進場日的型態" in alerts[0]


def test_unknown_code_reported():
    alerts = exit_manager.run([{"code": "1234", "strategy": "黑飛舞", "entry_date": DAYS[0]}],
                              frame(heifeiwu_bars()), {})
    assert "查無歷史資料" in alerts[0]


# ── 盤中 13:20 ───────────────────────────────────────────────────
def test_intraday_check_day2():
    sent = []
    hs = [{"code": "9999", "name": "測試", "strategy": "黑飛舞", "entry_date": "2026-10-02", "entry_price": 100},
          {"code": "8888", "strategy": "黑飛舞", "entry_date": "2026-09-30", "entry_price": 100},  # 不是 Day 2
          {"code": "7777", "strategy": "穿山二龍", "entry_date": "2026-10-02", "entry_price": 100}]
    live = [{"c": "9999", "n": "測試", "h": "106", "y": "101", "z": "104"}]
    now = datetime(2026, 10, 5, 13, 20, tzinfo=TZ)            # 週五進場 → 週一是 Day 2
    out = exit_manager.intraday_check(now, fetch=lambda: live, holdings=hs, send=sent.append)
    assert len(out) == 1 and "9999" in out[0] and "收盤賣 50%" in out[0]
    assert len(sent) == 1


def test_intraday_check_limit_up_and_dash_values():
    hs = [{"code": "9999", "strategy": "黑飛舞", "entry_date": "2026-10-02", "entry_price": 100},
          {"code": "8888", "strategy": "黑飛舞", "entry_date": "2026-10-02", "entry_price": 50}]
    live = [{"c": "9999", "h": "110.5", "y": "100.5", "z": "110.5"},
            {"c": "8888", "h": "-", "y": "50", "z": "-"}]             # 尚未成交
    now = datetime(2026, 10, 5, 13, 20, tzinfo=TZ)
    out = exit_manager.intraday_check(now, fetch=lambda: live, holdings=hs, send=lambda t: None)
    assert len(out) == 1 and "Day 2 漲停" in out[0]


def test_intraday_check_nothing_to_do():
    called = []
    now = datetime(2026, 10, 5, 13, 20, tzinfo=TZ)
    out = exit_manager.intraday_check(now, fetch=lambda: called.append(1) or [], holdings=[], send=print)
    assert out == [] and not called
