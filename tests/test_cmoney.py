"""CMoney 細產業／概念股標籤的離線測試（用模擬的頁面 HTML）"""

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cmoney_tags   # noqa: E402
import journal       # noqa: E402
import market_scan   # noqa: E402

PAGE = """
<nav><a href="/forum/category/C99999">熱門分類</a><a href="/forum/concept/C88888">AI 熱門</a></nav>
<div class="breadcrumb"><a href="/forum">台股大盤行情</a> &gt;
  <a href="https://www.cmoney.tw/forum/category/C23230"><span>電子中游-機殼</span></a> &gt; 晟銘電</div>
<h1>晟銘電 3013</h1>
<section><h2>相關概念股</h2>
  <a href="/forum/concept/C50030" class="x"><div>Apple</div><div>1.36%</div></a>
  <a href="/forum/concept/C50112"><div>雲伺服器</div><div>-0.52%</div></a>
  <a href="/forum/concept/C50030">Apple</a>
</section>
"""


def test_parse():
    t = cmoney_tags.parse(PAGE)
    assert t["sub_industry"] == "電子中游-機殼"            # 不是選單裡的「熱門分類」
    assert t["concepts"] == ["Apple", "雲伺服器"]           # 不含選單的「AI 熱門」、去掉漲跌幅、去重


def test_parse_missing_sections():
    assert cmoney_tags.parse("<html>改版了</html>") == {"sub_industry": "", "concepts": []}


def test_label_and_count():
    tags = {"3013": {"sub_industry": "電子中游-機殼", "concepts": ["Apple", "雲伺服器"]},
            "2059": {"sub_industry": "電子中游-機殼", "concepts": ["Apple"]},
            "2330": {"sub_industry": "電子上游-IC-代工", "concepts": []}}
    assert cmoney_tags.label(tags["3013"]) == "電子中游-機殼｜Apple・雲伺服器"
    assert cmoney_tags.label(tags["2330"]) == "電子上游-IC-代工"
    assert cmoney_tags.label(None) == ""
    hits = [{"code": c} for c in ("3013", "2059", "2330", "9999")]
    sub, con = cmoney_tags.count_tags(hits, tags)
    assert sub[0] == ("電子中游-機殼", 2) and con[0] == ("Apple", 2)


def test_get_tags_cache_and_giveup(monkeypatch):
    calls = []
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(cmoney_tags, "fetch", lambda c: calls.append(c) or {"sub_industry": "X", "concepts": []})
    cache = {}
    got = cmoney_tags.get_tags(["3013", "2330"], cache, "2026-10-02")
    assert set(got) == {"3013", "2330"} and len(calls) == 2
    cmoney_tags.get_tags(["3013", "2330"], cache, "2026-10-20")       # 30 天內走快取
    assert len(calls) == 2
    cmoney_tags.get_tags(["3013"], cache, "2026-11-05")               # 過期重抓
    assert len(calls) == 3

    calls.clear()
    monkeypatch.setattr(cmoney_tags, "fetch", lambda c: calls.append(c) or None)
    cmoney_tags.get_tags([f"{1000 + i}" for i in range(20)], {}, "2026-10-02")
    assert len(calls) == 5                                            # 連續失敗 5 次就放棄


def test_summary_shows_fine_tags():
    ev = {"score": 3, "pct": 6.8, "vol_ratio": 4.0}
    hits = [{"code": c, "name": n, "ev": ev} for c, n in (("3013", "晟銘電"), ("2059", "川湖"), ("2330", "台積電"))]
    tags = {"3013": {"sub_industry": "電子中游-機殼", "concepts": ["Apple"]},
            "2059": {"sub_industry": "電子中游-機殼", "concepts": ["Apple", "雲伺服器"]}}
    msg = market_scan.format_summary("2026-10-02", 1086, 419, hits, 0, None, {"2330": "半導體"}, tags)
    assert "🔎 <b>細產業</b>：電子中游-機殼 2" in msg
    assert "💡 <b>概念股</b>：Apple 2" in msg
    assert "晟銘電 3013　電子中游-機殼｜Apple　3/3" in msg
    assert "台積電 2330　半導體　3/3" in msg                         # 沒有細分類時退回 TWSE 大類


def test_journal_header_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path))
    old_fields = journal.FIELDS[:journal.FIELDS.index("industry")]      # 9/30 建立的舊表頭
    path = tmp_path / "signals_market.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(old_fields)
        w.writerow(["" for _ in old_fields][:4] + ["3481", "群創"] + ["" for _ in old_fields][6:] + ["光電"])
    journal.record("market", "3013", "晟銘電", date="2026-10-02",
                   sub_industry="電子中游-機殼", concepts="Apple")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert list(rows[0].keys()) == journal.FIELDS
    assert rows[0]["code"] == "3481" and rows[0]["industry"] == "光電"   # 舊列多出的值對回 industry
    assert rows[1]["sub_industry"] == "電子中游-機殼" and rows[1]["concepts"] == "Apple"
