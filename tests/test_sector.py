"""族群統計的離線測試"""

import csv
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import market_scan   # noqa: E402
from test_offline import Resp   # noqa: E402
from test_market_scan import env as scan_env   # noqa: E402,F401

# 假資料：群創 光電、41xx 前 7 檔鋼鐵、後 5 檔半導體
COMPANY = [{"公司代號": "3481", "公司簡稱": "群創", "產業別": "26"},
           {"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24"},
           {"公司代號": "2317", "公司簡稱": "鴻海", "產業別": "31"}]
COMPANY += [{"公司代號": f"41{i:02d}", "公司簡稱": f"起漲{i}", "產業別": "10" if i < 7 else "24"}
            for i in range(12)]


def _with_openapi(monkeypatch, calls):
    inner = requests.get

    def get(url, *a, **k):
        if "openapi.twse.com.tw" in url:
            calls.append(url)
            return Resp(js=COMPANY)
        return inner(url, *a, **k)
    monkeypatch.setattr("requests.get", get)


def test_industry_code_mapping(monkeypatch):
    calls = []
    monkeypatch.setattr("requests.get", lambda *a, **k: Resp(js=COMPANY + [
        {"公司代號": "9999", "產業別": "99"}, {"公司代號": "8888", "產業別": "鋼鐵工業"}]))
    m = market_scan.fetch_industry_map()
    assert m["3481"] == "光電" and m["4100"] == "鋼鐵" and m["2330"] == "半導體"
    assert m["9999"] == "其他(99)" and m["8888"] == "鋼鐵工業"


def test_sector_summary(scan_env, monkeypatch, tmp_path):
    sent, _, _ = scan_env
    calls = []
    _with_openapi(monkeypatch, calls)
    jd = tmp_path / "journal-data"
    jd.mkdir()
    monkeypatch.setenv("JOURNAL_DIR", str(jd))
    market_scan.main()
    summary = sent[0]
    assert "🏭 <b>族群</b>" in summary
    assert "鋼鐵 7／7" in summary and "半導體 5／6" in summary      # 台積電也在半導體合格股裡
    assert summary.index("鋼鐵 7／7") < summary.index("半導體 5／6")
    assert "資金集中在<b>鋼鐵</b>：合格股中 100% 同步起漲" in summary
    assert "另有 1 個族群各 1 檔" in summary                     # 光電只有群創
    assert "4100　鋼鐵　3/3" in summary                          # 摘要清單每檔帶族群
    rows = list(csv.DictReader((jd / "signals_market.csv").open(encoding="utf-8-sig")))
    assert {r["industry"] for r in rows} == {"光電", "鋼鐵", "半導體"}

    # 產業別存在快取，7 天內不重抓
    cache = json.loads(Path("market_cache.json").read_text(encoding="utf-8"))
    assert cache["industry"]["3481"] == "光電" and len(calls) == 1
    monkeypatch.setenv("FORCE_SCAN", "1")
    market_scan.main()
    assert len(calls) == 1


def test_summary_without_industry_data():
    ev = {"score": 3, "pct": 5.0, "vol_ratio": 4.0}
    msg = market_scan.format_summary("2026-09-30", 1087, 434, [{"code": "3481", "name": "群創", "ev": ev}], 1)
    assert "族群" not in msg
