"""盤中長時間執行的離線測試：用假時鐘快轉，不真的等待"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import intraday_loop   # noqa: E402

TZ = intraday_loop.TZ


class Clock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


@pytest.fixture
def run(monkeypatch):
    def _run(start, trading=True, monitor_fail=False):
        clock = Clock(start)
        calls = {"monitor": [], "screener": []}

        def monitor():
            calls["monitor"].append(clock.t)
            clock.t += timedelta(seconds=15)          # 每次大單監控約 15 秒
            if monitor_fail and len(calls["monitor"]) == 2:
                raise RuntimeError("TWSE 暫時連不上")

        def screener():
            import os
            calls["screener"].append((os.environ["SCREENER_SLOT"], clock.t))
            clock.t += timedelta(seconds=40)

        monkeypatch.setattr(intraday_loop, "now", clock.now)
        monkeypatch.setattr(intraday_loop, "sleep", clock.sleep)
        monkeypatch.setattr(intraday_loop.stock_check_once, "main", monitor)
        monkeypatch.setattr(intraday_loop.stock_screener, "main", screener)
        monkeypatch.setattr(intraday_loop, "is_trading_day", lambda t: trading)
        intraday_loop.main()
        return calls, clock.t
    return _run


THU = datetime(2026, 10, 1, tzinfo=TZ)


def test_full_day_from_0850(run):
    calls, end = run(THU.replace(hour=8, minute=50))
    mon = calls["monitor"]
    assert mon[0].strftime("%H:%M") == "09:00"
    assert mon[-1].strftime("%H:%M") == "13:35"
    assert len(mon) == 56                                    # 09:00–13:35 每 5 分鐘
    gaps = {(b - a) for a, b in zip(mon, mon[1:])}
    assert max(gaps) <= timedelta(minutes=5, seconds=30)
    assert [s for s, _ in calls["screener"]] == ["0930", "1300", "1400"]
    assert [t.strftime("%H:%M") for _, t in calls["screener"]][0] == "09:30"
    assert end.strftime("%H:%M") < "14:00"                   # 做完 13:50 就結束


def test_late_start_skips_expired_slot(run):
    calls, _ = run(THU.replace(hour=12, minute=30))
    assert calls["monitor"][0].strftime("%H:%M") == "12:30"   # 從啟動當下開始監控
    assert [s for s, _ in calls["screener"]] == ["1300", "1400"]   # 09:30 窗口已過，不補


def test_start_after_close_exits(run):
    calls, _ = run(THU.replace(hour=14, minute=20))
    assert calls == {"monitor": [], "screener": []}


def test_holiday_exits(run):
    calls, end = run(THU.replace(hour=8, minute=50), trading=False)
    assert len(calls["monitor"]) <= 2 and calls["screener"] == []
    assert end < THU.replace(hour=9, minute=10)


def test_weekend_exits(run):
    calls, _ = run(datetime(2026, 10, 3, 8, 50, tzinfo=TZ))
    assert calls == {"monitor": [], "screener": []}


def test_one_failure_does_not_stop_loop(run):
    calls, _ = run(THU.replace(hour=8, minute=50), monitor_fail=True)
    assert len(calls["monitor"]) == 56 and len(calls["screener"]) == 3
