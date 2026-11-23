"""Wiki 收件匣：Wiki 身分的提醒預設寫 data 分支 inbox/jarvis/YYYY-MM-DD.jsonl，不推 Telegram（全部離線）"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import notifier           # noqa: E402
import stock_check_once   # noqa: E402
import watch_alert as wa  # noqa: E402
import wiki_inbox         # noqa: E402
from config import Config  # noqa: E402
from test_watch import NO_EXT, WATCH_YAML, market_df, path_series  # noqa: E402

TZ = wiki_inbox.TZ


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("WIKI_INBOX_DIR", raising=False)
    monkeypatch.delenv("WIKI_TELEGRAM", raising=False)
    monkeypatch.setenv("TG_SENDER", "WIKI")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    posted = []

    class R:
        ok = True
        status_code = 200
        text = ""
    monkeypatch.setattr("requests.post", lambda url, json=None, timeout=None: posted.append(json) or R())
    monkeypatch.setattr(stock_check_once, "TOKEN", "t")
    monkeypatch.setattr(stock_check_once, "CHAT_ID", "c")
    return tmp_path / "data" / "inbox" / "jarvis", posted


def rows(d: Path, day: str):
    p = d / f"{day}.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_default_is_off():
    assert Config.WIKI_TELEGRAM is False


def test_wiki_message_goes_to_inbox_not_telegram(box):
    d, posted = box
    # 台北 2026-11-24 00:30 = UTC 11-23 16:30 → 用台北日期
    now = datetime(2026, 11, 23, 16, 30, tzinfo=timezone.utc)
    with wiki_inbox.meta("推翻條件", "3017", "奇鋐"):
        assert wiki_inbox.append("<b>【推翻條件】奇鋐（3017）</b>\n收盤跌破 &lt;200MA&gt;", now=now)
    assert not (d / "2026-11-23.jsonl").exists()
    r = rows(d, "2026-11-24")
    assert r == [{"time": "2026-11-24T00:30:00+08:00", "type": "推翻條件", "code": "3017", "name": "奇鋐",
                  "text": "【推翻條件】奇鋐（3017）\n收盤跌破 <200MA>"}]

    assert notifier.send_telegram("<b>⚡ 盤中即時｜10:00</b>\n內容")
    assert posted == []                                     # 沒有送出 Telegram
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    last = rows(d, today)[-1]
    assert last["type"] == "⚡ 盤中即時｜10:00" and last["code"] == "" and "內容" in last["text"]

    assert stock_check_once.send_telegram("大單") and posted == []


@pytest.mark.parametrize("sender,flag", [("WIKI", "true"), ("SENTRY", ""), ("", ""), ("DEFAULT", "")])
def test_telegram_when_enabled_or_other_role(box, monkeypatch, sender, flag):
    d, posted = box
    monkeypatch.setenv("TG_SENDER", sender)
    if flag:
        monkeypatch.setenv("WIKI_TELEGRAM", flag)
    assert notifier.send_telegram("hi")
    assert posted and posted[0]["text"] == "hi"
    assert not d.exists()


def test_watch_alert_rows_have_type_code_name(box, tmp_path, monkeypatch):
    d, posted = box
    (tmp_path / "watchlist.yaml").write_text(WATCH_YAML, encoding="utf-8")
    (tmp_path / "journal").mkdir()
    monkeypatch.setattr(wa, "WATCHLIST_FILE", tmp_path / "watchlist.yaml")
    monkeypatch.setattr(wa, "COOLDOWN_FILE", tmp_path / "state" / "watch_cooldown.json")
    monkeypatch.setattr(wa, "TEMP_FILE", tmp_path / "state" / "market_temp.json")
    monkeypatch.setattr(wa, "CARD_STATE_FILE", tmp_path / "card.json")
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path / "journal"))
    n = 300
    kiq = np.r_[path_series(n - 1, 300, 0.001, 0.01, seed=5), [0]]
    kiq[-1] = kiq[-21:-1].max() * 0.88                                      # 回檔 12% → 第二級
    df = market_df(n, special={"3017": kiq})
    now = datetime(2026, 10, 16, 15, 20, tzinfo=TZ)
    wa._write_json(wa.COOLDOWN_FILE, {"_known": ["3017", "8888"]})
    res = wa.run_post(datetime(2026, 10, 16).date(), now=now, df=df, ext=NO_EXT,
                      news=lambda a, s: (None, ""), foreign=lambda c, d: pd.Series(dtype=float))
    assert res["sent"] >= 1 and posted == []
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    r = [x for x in rows(d, today) if x["code"] == "3017"]
    assert r and r[0]["name"] == "奇鋐" and "第二級" in r[0]["type"]
    assert "核心投資理由" in r[0]["text"] and "<b>" not in r[0]["text"]
    # 已寫入收件匣也算「送達」：冷卻有記下，重跑不會重複寫
    again = wa.run_post(datetime(2026, 10, 16).date(), now=now, df=df, ext=NO_EXT, force=True,
                        news=lambda a, s: (None, ""), foreign=lambda c, d: pd.Series(dtype=float))
    assert again["sent"] == 0


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, check=True).stdout


def test_flush_commits_and_merges_concurrent_appends(tmp_path, monkeypatch):
    remote, a, b = tmp_path / "remote.git", tmp_path / "a", tmp_path / "b"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "data", str(remote)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "data", str(seed)], check=True)
    (seed / "state").mkdir()
    (seed / "state" / "x.json").write_text("{}", encoding="utf-8")
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        git(seed, "config", k, v)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", str(remote), "HEAD:data")
    for c in (a, b):
        subprocess.run(["git", "clone", "-q", "-b", "data", str(remote), str(c)], check=True)
    monkeypatch.setenv("TG_SENDER", "WIKI")
    now = datetime(2026, 11, 23, 9, 0, tzinfo=TZ)
    for c, msg in ((a, "甲"), (b, "乙")):
        monkeypatch.setenv("DATA_DIR", str(c))
        assert wiki_inbox.append(msg, type="t", now=now)
    (b / "state" / "x.json").write_text('{"dirty": 1}', encoding="utf-8")   # 其他未提交的變更不會被一起提交
    assert wiki_inbox.flush(a) and wiki_inbox.flush(b)
    assert wiki_inbox.flush(b) is False                                       # 沒有新內容
    git(a, "pull", "-q", "origin", "data")
    lines = (a / "inbox" / "jarvis" / "2026-11-23.jsonl").read_text(encoding="utf-8").splitlines()
    assert sorted(json.loads(x)["text"] for x in lines) == ["乙", "甲"]
    assert "state/x.json" not in git(a, "log", "-1", "--name-only", "--format=")
    assert (b / "state" / "x.json").read_text(encoding="utf-8") == '{"dirty": 1}'
