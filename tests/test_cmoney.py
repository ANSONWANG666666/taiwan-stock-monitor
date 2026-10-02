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


def _cache(spec):
    """spec = {代號: (前 20 天收盤, 前 20 天張數, 今日收盤, 今日張數)}"""
    days = [f"2026-09-{d:02d}" for d in range(1, 21)] + ["2026-10-02"]
    return {"days": days, "stocks": {c: {"bars": [[d, p0, v0] for d in days[:-1]] + [[days[-1], p1, v1]]}
                                     for c, (p0, v0, p1, v1) in spec.items()}}


def test_group_strength_and_summary():
    # 機殼族群 4 檔：3 檔起漲、全部上漲、成交值放大；Apple 概念 10 檔只有 2 檔起漲、量能平平
    spec = {"3013": (100, 1000, 107, 4000), "3032": (50, 1000, 54, 3500), "8210": (800, 100, 850, 400),
            "6117": (60, 1000, 61, 1100)}
    for i in range(8):
        spec[f"{2300 + i}"] = (100, 1000, 99, 1000)
    cache = _cache(spec)
    tags = {"3013": {"sub_industry": "電子中游-機殼", "concepts": ["Apple"]},
            "3032": {"sub_industry": "電子中游-機殼", "concepts": []},
            "8210": {"sub_industry": "電子中游-機殼", "concepts": ["雲伺服器"]},
            "6117": {"sub_industry": "電子中游-機殼", "concepts": []}}
    for i in range(8):
        tags[f"{2300 + i}"] = {"sub_industry": "電子下游-組裝", "concepts": ["Apple"]}
    tags["3032"]["concepts"] = ["Apple"]
    liquid = list(spec)
    hits = {"3013", "3032", "8210"}
    big, sub, con = market_scan.build_groups(liquid, {}, tags)
    g_sub = market_scan.group_strength(sub, hits, cache, "2026-10-02")
    g_con = market_scan.group_strength(con, hits, cache, "2026-10-02")
    case = next(g for g in g_sub if g["name"] == "電子中游-機殼")
    assert (case["k"], case["n"], case["up"]) == (3, 4, 4) and case["flow"] > 2.5
    assert market_scan.is_flowing(case)
    apple = next(g for g in g_con if g["name"] == "Apple")
    assert (apple["k"], apple["n"]) == (2, 10) and apple["flow"] < 1.5
    assert not market_scan.is_flowing(apple)

    ev = {"score": 3, "pct": 7.0, "vol_ratio": 4.0}
    hit_rows = [{"code": c, "name": c, "ev": ev} for c in ("3013", "3032", "8210")]
    msg = market_scan.format_summary("2026-10-02", 1086, 419, hit_rows, 0,
                                     {"big": [], "sub": g_sub, "con": g_con}, {}, tags, coverage=0.95)
    assert "• 電子中游-機殼　起漲 3／4｜量比" in msg and "上漲 4／4 🔥" in msg
    assert "• Apple　起漲 2／10｜量比" in msg
    assert "👉 資金流入<b>電子中游-機殼</b>" in msg
    assert "3013　電子中游-機殼｜Apple　3/3" in msg

    # 母數還沒建完：只顯示起漲檔數，不下資金流入結論
    msg2 = market_scan.format_summary("2026-10-02", 1086, 419, hit_rows, 0,
                                      {"big": [], "sub": g_sub, "con": g_con}, {}, tags, coverage=0.4)
    assert "• 電子中游-機殼　起漲 3\n" in msg2 + "\n" or "• 電子中游-機殼　起漲 3" in msg2
    assert "量比" not in msg2.split("其他符合條件")[0].split("🔎")[1]
    assert "母數建立中（已完成 40%）" in msg2
    assert "👉" not in msg2


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
