"""離線測試：模擬 TWSE、Google News、Jev、Telegram，不連任何外部服務。

執行：pip install pytest && python -m pytest -q tests
"""

import json
import sys
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import news_check          # noqa: E402
import stock_check_once    # noqa: E402
import stock_screener      # noqa: E402

TZ = news_check.TZ


# ── 假資料 ───────────────────────────────────────────────────────────
def mis_item(sym, name, price, prev, tv, v, bid=300):
    # tlong 故意放時間戳記，確認程式不會再把它當成交額
    return {"c": sym, "n": name, "z": f"{price:.4f}", "y": f"{prev:.4f}", "tv": str(tv), "v": str(v),
            "g": f"{bid}_100_", "b": f"{price - 1:.4f}_{price - 2:.4f}_", "f": "50_60_",
            "tlong": "1790670689000"}


def rss(items):
    body = "".join(
        f"<item><title>{t} - {src}</title><link>https://example.com/{i}</link>"
        f"<pubDate>{format_datetime(datetime.now(TZ) - timedelta(hours=h))}</pubDate>"
        f"<source url='x'>{src}</source></item>"
        for i, (t, src, h) in enumerate(items))
    return f"<?xml version='1.0' encoding='UTF-8'?><rss><channel>{body}</channel></rss>".encode()


NEWS = {
    "台積電": [("台積電9月營收年增32%創同期新高", "經濟日報", 3),
            ("台股收盤大漲 電子權值股領軍", "Yahoo股市", 5)],
    "聯發科": [("傳聯發科拿下大單 法人預估明年放量", "工商時報", 2)],
    "鴻海": [],
}


def fake_jev_answers(title):
    pos = any(k in title for k in ("年增", "新高", "大單", "放量"))
    market = "台股收盤" in title
    rumor = title.startswith("傳")
    d = {"positive": 0.85, "negative": 0.03, "mixed": 0.04, "neutral": 0.08} if pos else \
        {"positive": 0.1, "negative": 0.1, "mixed": 0.1, "neutral": 0.7}
    return {
        "about_company": {"type": "noul", "noul": 0.15 if market else 0.95},
        "direction": {"type": "choice", "choice": max(d, key=d.get), "probabilities": d, "confidence": 0.8},
        "impact": {"type": "score", "score": 3.1, "confidence": 0.6, "legend": {str(i): "" for i in range(5)},
                   "probabilities": {"0": 0, "1": 0.05, "2": 0.15, "3": 0.45, "4": 0.35}},
        "persistence": {"type": "choice", "choice": "short_term", "confidence": 0.5,
                        "probabilities": {"one_off": 0.2, "short_term": 0.5, "structural": 0.3}},
        "event_type": {"type": "choice", "choice": "monthly_revenue", "confidence": 0.8,
                       "probabilities": {"monthly_revenue": 0.8, "other": 0.2}},
        "unconfirmed": {"type": "noul", "noul": 0.9 if rumor else 0.05},
    }


class FakeJevTransport(httpx2.AsyncBaseTransport):
    def __init__(self):
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        body = json.loads(await request.aread())
        assert body["model"] == "jev-latest"
        assert set(body["questions"]) == {"about_company", "direction", "impact",
                                          "persistence", "event_type", "unconfirmed"}
        title = body["state"]["news"]["title"]
        return httpx2.Response(200, json={"model": "jev-mock", "answers": fake_jev_answers(title),
                                          "usage": {"input_tokens": 900, "output_tokens": 0}})


class Resp:
    def __init__(self, content=b"", js=None, status=200):
        self.content, self._js, self.status_code = content, js, status
        self.ok = status < 400
        self.text = content.decode("utf-8", "ignore") if content else json.dumps(js or {})

    def json(self):
        return self._js

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """切到暫存資料夾、攔截所有網路呼叫，回傳紀錄用的物件"""
    monkeypatch.chdir(tmp_path)
    for mod in (news_check, stock_check_once, stock_screener):
        for attr in ("CACHE_FILE", "STATE_FILE", "STATUS_FILE"):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, tmp_path / getattr(mod, attr).name)
    rec = SimpleNamespace(sent=[], mis=[], jev=FakeJevTransport(), news_queries=[])

    def fake_get(url, params=None, headers=None, timeout=None):
        if "news.google.com" in url:
            q = url.split("q=")[1]
            rec.news_queries.append(q)
            for name, items in NEWS.items():
                if __import__("urllib.parse").parse.quote_plus(name) in q:
                    return Resp(rss(items))
            return Resp(rss([]))
        if "STOCK_DAY" in url:
            return Resp(js=stock_day(params["stockNo"], params["date"]))
        raise AssertionError(f"unexpected GET {url}")

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def get(self, url, params=None, timeout=None):
            if "getStockInfo" in url:
                return Resp(js={"msgArray": rec.mis})
            return Resp()

    def fake_post(url, json=None, timeout=None):
        rec.sent.append(json["text"])
        return Resp(js={"ok": True})

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.Session", FakeSession)
    monkeypatch.setattr("time.sleep", lambda s: None)
    for mod in (stock_check_once, stock_screener):
        monkeypatch.setattr(mod, "TOKEN", "t")
        monkeypatch.setattr(mod, "CHAT_ID", "c")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    real_check = news_check.check_news
    wrapped = lambda s, n, transport=None: real_check(s, n, transport=rec.jev)
    monkeypatch.setattr(stock_check_once, "check_news", wrapped)
    monkeypatch.setattr(stock_screener, "check_news", wrapped)
    return rec


def stock_day(symbol, yyyymm01):
    """前 20 天平盤小量，最後 5 天連漲且量放大（到昨天為止）"""
    y, m = int(yyyymm01[:4]), int(yyyymm01[4:6])
    today = datetime.now(TZ).date()
    days = [today - timedelta(days=i) for i in range(40, 0, -1)]
    days = [d for d in days if d.weekday() < 5]
    rows = []
    for idx, d in enumerate(days):
        if (d.year, d.month) != (y, m):
            continue
        n_left = len(days) - idx
        up = symbol in ("2330", "2454") and n_left <= 5
        close = 1000 + (6 - n_left) * 10 if up else 1000
        vol = 60_000_000 if up else 20_000_000
        rows.append([f"{d.year - 1911}/{d.month:02d}/{d.day:02d}", f"{vol:,}", "0", "0", "0", "0",
                     f"{close:,.2f}", "0", "0"])
    return {"stat": "OK", "fields": ["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                                      "收盤價", "漲跌價差", "成交筆數"], "data": rows}


# ── 大單監控 ─────────────────────────────────────────────────────────
def test_monitor_trade_amount_not_from_tlong(env):
    # 小單：單筆 1 張 × 1050 = 105 萬 < 千金股門檻 300 萬 → 不應觸發大金額
    env.mis = [mis_item("2330", "台積電", 1050, 1030, tv=1, v=25000)]
    stock_check_once.main()
    assert env.sent == []


def test_monitor_alert_with_news(env):
    env.mis = [mis_item("2330", "台積電", 1050, 1030, tv=5, v=25000, bid=1500)]
    stock_check_once.main()
    assert len(env.sent) == 1
    msg = env.sent[0]
    assert "大金額成交" in msg and "大買盤掛單" in msg      # 同檔多警報合併成一則
    assert "525 萬元" in msg                               # 1050 × 5 張 × 1000 = 525 萬
    assert "🔴 +1.94%" in msg                              # 紅漲
    assert "有證實的利多消息" in msg
    assert "營收年增" in msg
    assert "台股收盤大漲" not in msg                        # 大盤新聞被 about_company 過濾
    assert env.jev.calls == 2

    # 冷卻期內不重推；新聞判斷走快取，不再呼叫 Jev
    stock_check_once.main()
    assert len(env.sent) == 1
    assert env.jev.calls == 2


def test_monitor_volume_spike_uses_interval_volume(env):
    env.mis = [mis_item("2317", "鴻海", 200, 198, tv=1, v=10000)]
    stock_check_once.main()                                 # 首次：建立基準
    for v in (10300, 10600, 10900):                         # 每輪 300 張
        env.mis = [mis_item("2317", "鴻海", 200, 198, tv=1, v=v)]
        stock_check_once.main()
    assert env.sent == []
    env.mis = [mis_item("2317", "鴻海", 201, 198, tv=1, v=12500)]  # 1600 張 → 5.3 倍
    stock_check_once.main()
    assert len(env.sent) == 1
    assert "量能爆發" in env.sent[0]
    assert "查無相關新聞" in env.sent[0]
    assert "上漲缺乏證實消息支撐" in env.sent[0]


def test_monitor_rumor_only(env):
    env.mis = [mis_item("2454", "聯發科", 1300, 1280, tv=5, v=9000)]
    stock_check_once.main()
    assert "只有傳聞" in env.sent[0]


def test_monitor_without_api_key(env, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    env.mis = [mis_item("2330", "台積電", 1050, 1030, tv=5, v=25000)]
    stock_check_once.main()
    assert len(env.sent) == 1 and "新聞查證" not in env.sent[0]
    assert env.jev.calls == 0


# ── 四訊號選股 ───────────────────────────────────────────────────────
def test_slot_uses_taiwan_time():
    mk = lambda h, m: datetime(2026, 9, 30, h, m, tzinfo=TZ)   # 週三
    assert stock_screener.current_slot(mk(9, 30)) == "0930"
    assert stock_screener.current_slot(mk(9, 58)) == "0930"   # 排程延遲
    assert stock_screener.current_slot(mk(13, 20)) == "1300"
    assert stock_screener.current_slot(mk(14, 25)) == "1400"
    assert stock_screener.current_slot(mk(11, 0)) == "0930"
    assert stock_screener.current_slot(mk(17, 0)) is None
    assert stock_screener.current_slot(datetime(2026, 10, 3, 9, 30, tzinfo=TZ)) is None  # 週六


def test_screener_full_flow(env, monkeypatch):
    env.mis = [mis_item(s, n, p, pv, tv=1, v=v) for s, n, p, pv, v in [
        ("2330", "台積電", 1060, 1050, 70000),
        ("2454", "聯發科", 1060, 1050, 70000),
        ("2317", "鴻海", 1000, 1000, 20000),
    ]]
    monkeypatch.setenv("SCREENER_SLOT", "1400")
    stock_screener.main()
    cache = json.loads(Path("screener_kline_cache.json").read_text(encoding="utf-8"))
    bars = cache["2330"]["klines"]
    assert len({b["date"] for b in bars}) == len(bars)       # 一天一根
    assert bars[-1]["date"] == datetime.now(TZ).strftime("%Y-%m-%d")
    assert len(env.sent) == 2
    tsmc = next(m for m in env.sent if "2330" in m)
    assert "正式入選" in tsmc and "台積電" in tsmc and "有證實的利多消息" in tsmc
    mtk = next(m for m in env.sent if "2454" in m)
    assert "只有傳聞" in mtk

    # 同一天再跑一次：不重複推播、今天那根 K 被覆寫而不是新增
    n_bars = len(bars)
    stock_screener.main()
    assert len(env.sent) == 2
    cache = json.loads(Path("screener_kline_cache.json").read_text(encoding="utf-8"))
    assert len(cache["2330"]["klines"]) == n_bars
    status = json.loads(Path("screener_status.json").read_text(encoding="utf-8"))
    assert set(status["formal_candidates"]) == {"2330", "2454"}


def test_news_summarize_rules():
    now = datetime.now(TZ)
    it = lambda title, h: {"title": title, "published": (now - timedelta(hours=h)).isoformat(),
                           "answers": fake_jev_answers(title)}
    assert news_check.summarize([], now)["verdict"] == "none"
    assert news_check.summarize([it("台股收盤大漲", 1)], now)["verdict"] == "none"
    assert news_check.summarize([it("傳拿下大單", 1)], now)["verdict"] == "rumor_only"
    assert news_check.summarize([it("9月營收年增", 1)], now)["verdict"] == "confirmed_positive"
    block = news_check.format_news_block({"verdict": "confirmed_negative", "top": []}, pct=2.0)
    assert "留意是否為出貨" in block
    assert news_check.format_news_block(None) == ""
