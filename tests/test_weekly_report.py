"""每週成效摘要的離線測試"""

import csv
import random
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import journal         # noqa: E402
import weekly_report   # noqa: E402

TZ = weekly_report.TZ
FRI = datetime(2026, 10, 30, 17, 5, tzinfo=TZ)


def _write(jd, source, rows):
    path = jd / f"signals_{source}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=journal.FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _row(source, date, code, verdict, score, ex5=None, ex20=None):
    return {"source": source, "date": date, "code": code, "name": f"股{code}",
            "news_verdict": verdict, "score": score,
            "excess_5d": "" if ex5 is None else f"{ex5:.2f}",
            "excess_20d": "" if ex20 is None else f"{ex20:.2f}"}


def test_no_results_yet(tmp_path):
    _write(tmp_path, "market", [_row("market", "2026-10-29", "3481", "rumor_driven", 2)])
    msg = weekly_report.build_report(weekly_report.load_signals(tmp_path), FRI)
    assert "本週新增：全市場掃描 1" in msg
    assert "還沒有訊號滿 5 個交易日" in msg


def test_full_report(tmp_path):
    rnd = random.Random(1)
    market = []
    for i in range(40):
        market.append(_row("market", f"2026-10-{1 + i % 20:02d}", f"{2000 + i}", "confirmed_positive", 3,
                           rnd.gauss(2, 3), rnd.gauss(4, 6)))
    for i in range(20):
        market.append(_row("market", f"2026-10-{1 + i:02d}", f"{4000 + i}", "unchecked", 2, rnd.gauss(0, 3)))
    market.append(_row("market", "2026-10-29", "9999", "none", 3))            # 本週、尚無結果
    _write(tmp_path, "market", market)
    # 大單監控同一天同一檔推了 3 次 → 只算 1 筆
    _write(tmp_path, "monitor", [_row("monitor", "2026-10-05", "2330", "neutral", "", 1.0)] * 3)
    tri = [_row("pattern_tri", f"2026-10-{6 + i:02d}", f"{5000 + i}", "", "", rnd.gauss(1, 3)) | {"slot": "進場"}
           for i in range(5)]
    _write(tmp_path, "pattern_tri", tri)
    _write(tmp_path, "pattern_hfw", [_row("pattern_hfw", "2026-10-06", "6000", "", "", -2.0) | {"slot": "觀察"}])

    rows = weekly_report.load_signals(tmp_path)
    assert len(rows) == 68
    msg = weekly_report.build_report(rows, FRI)
    assert "本週新增：全市場掃描 1" in msg
    # 目前推播的型態放最前面
    assert msg.index("型態選股（目前推播）") < msg.index("已停推、仍在追蹤")
    tri_line = next(l for l in msg.split("\n") if "三角收斂・列進場" in l)
    assert "5 筆（樣本不足）" in tri_line
    assert "黑飛舞・觀察" in msg
    # 停推的訊號一行一組
    s3 = next(l for l in msg.split("\n") if "量價齊揚 3/3 分" in l)
    assert "40 筆" in s3 and "20日" in s3 and "樣本不足" not in s3.split("｜")[0]
    assert "量價齊揚 2/3 分" in msg and "權值股大單監控" in msg and "Jev 查過新聞的（已停用）" in msg
    # 最佳／最差優先列三角收斂進場
    assert "本月最佳（5 日，三角收斂進場）" in msg and "本月最差" in msg
    assert "不構成投資建議" in msg
    assert len(msg) < 4096


def test_pattern_waiting_message(tmp_path):
    _write(tmp_path, "market", [_row("market", "2026-10-01", "2000", "unchecked", 3, 1.0)])
    _write(tmp_path, "pattern_tri", [_row("pattern_tri", "2026-10-06", "5000", "", "") | {"slot": "進場"}])
    msg = weekly_report.build_report(weekly_report.load_signals(tmp_path), FRI)
    assert "還沒有滿 5 個交易日的結果（10-06 起記錄）" in msg


def test_main_sends_once_per_week_even_after_midnight(tmp_path, monkeypatch):
    jd = tmp_path / "journal-data"
    jd.mkdir()
    _write(jd, "market", [_row("market", "2026-10-01", "3481", "rumor_driven", 2, 1.5)])
    monkeypatch.setattr(weekly_report, "STATE_FILE", tmp_path / "weekly_state.json")
    monkeypatch.setenv("JOURNAL_DIR", str(jd))
    monkeypatch.delenv("FORCE_REPORT", raising=False)
    sent = []
    monkeypatch.setattr(weekly_report, "send_telegram", lambda t: sent.append(t) or True)
    clock = [datetime(2026, 10, 9, 17, 10, tzinfo=TZ)]

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(weekly_report, "datetime", FakeDT)
    weekly_report.main()
    clock[0] = datetime(2026, 10, 10, 0, 10, tzinfo=TZ)     # 備援排程延遲到週六凌晨
    weekly_report.main()
    assert len(sent) == 1
    clock[0] = datetime(2026, 10, 16, 17, 10, tzinfo=TZ)    # 下週五照常
    weekly_report.main()
    assert len(sent) == 2


def test_main_sends_once_per_day(tmp_path, monkeypatch):
    jd = tmp_path / "journal-data"
    jd.mkdir()
    _write(jd, "market", [_row("market", "2026-10-01", "3481", "rumor_driven", 2, 1.5)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(weekly_report, "STATE_FILE", tmp_path / "weekly_state.json")
    monkeypatch.setenv("JOURNAL_DIR", str(jd))
    sent = []
    monkeypatch.setattr(weekly_report, "send_telegram", lambda t: sent.append(t) or True)
    weekly_report.main()
    assert len(sent) == 1 and sent[0].startswith("📈")
    weekly_report.main()                      # 備援排程再觸發：略過
    assert len(sent) == 1
    monkeypatch.setenv("FORCE_REPORT", "1")   # 手動強制
    weekly_report.main()
    assert len(sent) == 2
