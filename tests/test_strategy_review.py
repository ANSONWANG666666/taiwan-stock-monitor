"""每月策略檢討：門檻判斷、月報酬、候選篩選"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config            # noqa: E402
import strategy_review as sr   # noqa: E402


def row(full, mdd, recent, month_win, n=100):
    return {"n": n, "full": {"ret": full, "mdd": mdd}, "recent": {"ret": recent}, "month_win": month_win}


CUR = row(0.60, 0.18, 0.10, None)


def test_judge_passes_only_when_all_gates_pass():
    assert sr.judge(CUR, row(0.70, 0.18, 0.12, 0.65)) == []
    assert "全期報酬沒有多 5 個百分點" in sr.judge(CUR, row(0.64, 0.18, 0.12, 0.65))
    assert any("最近" in f for f in sr.judge(CUR, row(0.70, 0.18, 0.09, 0.65)))
    assert "逐月勝出不到 60%" in sr.judge(CUR, row(0.70, 0.18, 0.12, 0.57))
    assert "最大回撤變差超過 5 個百分點" in sr.judge(CUR, row(0.70, 0.24, 0.12, 0.65))
    assert any("筆數" in f for f in sr.judge(CUR, row(0.70, 0.18, 0.12, 0.65, n=10)))


def test_monthly_and_account_stats():
    eq = pd.Series([1.0, 1.1, 1.21, 0.968, 1.0648],
                   index=["2026-01-05", "2026-01-30", "2026-02-27", "2026-03-31", "2026-04-30"])
    m = sr.monthly(eq)
    assert list(m.round(4)) == [0.1, 0.1, -0.2, 0.1]
    full = sr.account_stats(eq)
    assert full["ret"] == pytest.approx(0.0648) and full["mdd"] == pytest.approx(0.2)
    rec = sr.account_stats(eq, "2026-03-01")              # 從 2 月底 1.21 起算
    assert rec["ret"] == pytest.approx(1.0648 / 1.21 - 1)


def test_candidates_fixed_and_current_first():
    c = sr.candidates(config.Config())
    assert c[0].key == "current" and c[0].change == {}
    assert all(x.change for x in c[1:])                   # 每個候選都寫明要改哪個設定
    spec = c[0].resolve(config.Config())
    assert spec == {"pattern": "三角收斂", "mode": "rally", "strict": True, "market": True, "top_n": 5, "stop": "ma20"}


def test_select_applies_filters():
    df = pd.DataFrame({
        "code": list("ABCDEF"), "pattern": ["三角收斂"] * 5 + ["穿山二龍"], "mode": ["rally"] * 6,
        "signal_date": ["2026-01-02"] * 6, "score": [90, 80, 70, 60, 50, 99],
        "in_strong": [True] * 6, "in_strict": [True, False, True, True, True, True],
        "market_up": [True, True, True, False, True, True],
        "entry_idx": [1] * 6, "exit_idx": [3] * 6})
    spec = {"pattern": "三角收斂", "mode": "rally", "strict": True, "market": True, "top_n": 2, "stop": "ma20"}
    assert sorted(sr.select(df, spec)["code"]) == ["A", "C"]
    spec |= {"strict": False, "top_n": 5}
    assert sorted(sr.select(df, spec)["code"]) == ["A", "B", "C", "E"]


def test_report_recommends_only_passing(monkeypatch):
    cur = row(0.60, 0.18, 0.10, None) | {"key": "current", "label": "現行規則", "change": {}, "win": 0.32,
                                          "fails": [], "monthly": pd.Series(dtype=float)}
    good = row(0.75, 0.15, 0.15, 0.7) | {"key": "x", "label": "好候選", "change": {"A_MODE": "amplitude"},
                                          "win": 0.35, "fails": [], "monthly": pd.Series(dtype=float)}
    bad = row(0.50, 0.30, 0.0, 0.3) | {"key": "y", "label": "差候選", "change": {"X": 1}, "win": 0.2,
                                        "fails": ["全期報酬沒有多 5 個百分點"], "monthly": pd.Series(dtype=float)}
    period = {"today": "2026-11-01", "start": "2025-01-01", "end": "2026-10-31", "recent": "2026-05-01",
              "index": {"ret": 0.9, "mdd": 0.25}, "index_recent": 0.1, "index_monthly": pd.Series(dtype=float)}
    md = sr.build_report([cur, good, bad], period, {}, config.Config())
    assert "建議考慮：好候選" in md and '"A_MODE": "amplitude"' in md and "❌ 全期報酬" in md
    tg = sr.telegram_summary([cur, bad], period, config.Config())
    assert "維持現行規則" in tg
