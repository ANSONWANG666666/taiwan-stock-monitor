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
    assert "本週新增訊號：全市場掃描 1" in msg
    assert "還沒有訊號滿 5 個交易日" in msg


def test_full_report(tmp_path):
    rnd = random.Random(1)
    market = []
    for i in range(40):   # 證實利多平均較好、題材帶動較差（假資料）
        market.append(_row("market", f"2026-10-{1 + i % 20:02d}", f"{2000 + i}", "confirmed_positive", 3,
                           rnd.gauss(2, 3), rnd.gauss(4, 6)))
    for i in range(12):
        market.append(_row("market", f"2026-10-{1 + i:02d}", f"{3000 + i}", "rumor_driven", 2, rnd.gauss(-1, 3)))
    for i in range(20):
        market.append(_row("market", f"2026-10-{1 + i:02d}", f"{4000 + i}", "unchecked", 2, rnd.gauss(0, 3)))
    market.append(_row("market", "2026-10-29", "9999", "none", 3))            # 本週、尚無結果
    _write(tmp_path, "market", market)
    # 大單監控同一天同一檔推了 3 次 → 只算 1 筆
    _write(tmp_path, "monitor", [_row("monitor", "2026-10-05", "2330", "neutral", "", 1.0)] * 3)

    rows = weekly_report.load_signals(tmp_path)
    assert len(rows) == 74
    msg = weekly_report.build_report(rows, FRI)
    assert "本週新增訊號：全市場掃描 1" in msg
    assert "✅ 證實利多" in msg and "⚠️ 題材帶動" in msg and "▫️ 未查證" in msg
    conf_line = msg.split("✅ 證實利多</b>\n")[1].split("\n")[0]
    assert "40 筆" in conf_line and "樣本不足" not in conf_line
    rumor_line = msg.split("⚠️ 題材帶動</b>\n")[1].split("\n")[0]
    assert "12 筆（樣本不足）" in rumor_line
    assert "有查新聞（前 10 名）" in msg and "未查新聞" in msg
    assert "3/3 分" in msg and "2/3 分" in msg
    assert "大單監控" in msg and "1 筆" in msg
    assert "本月 5 日超額報酬最佳" in msg and "最差" in msg
    assert "不構成投資建議" in msg
    assert len(msg) < 4096


def test_main_sends(tmp_path, monkeypatch):
    _write(tmp_path, "market", [_row("market", "2026-10-01", "3481", "rumor_driven", 2, 1.5)])
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path))
    sent = []
    monkeypatch.setattr(weekly_report, "send_telegram", lambda t: sent.append(t) or True)
    weekly_report.main()
    assert len(sent) == 1 and sent[0].startswith("📈")
