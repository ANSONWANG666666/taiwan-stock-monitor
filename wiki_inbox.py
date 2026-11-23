#!/usr/bin/env python3
"""
Wiki 收件匣：Wiki 身分的提醒預設不推 Telegram，改寫進 data 分支給盤前早報分析用

    data 分支  inbox/jarvis/YYYY-MM-DD.jsonl（台北日期），一行一則：
    {"time": "2026-11-23T15:12:03+08:00", "type": "推翻條件", "code": "2330", "name": "台積電",
     "text": "完整訊息文字（已去掉 HTML 標籤）"}

哪些訊息寫收件匣：
  觀察名單提醒（watch_alert.py post：回檔、推翻條件、價格異常、新加入狀態、大型股回檔摘要）
      不管用誰的 bot，一律寫收件匣
  其他步驟  workflow 用 TG_SENDER 標出實際用哪個成員的 bot；是 Wiki 的才寫收件匣
      （例如開盤前溫度卡，或 Sentry 沒設金鑰、退回 Wiki 的 bot 時）
WIKI_TELEGRAM=true（config.py 或 repo 變數）時恢復直接推播。

    python wiki_inbox.py flush    把收件匣提交並推到 data 分支（workflow 最後一步，沒有新內容就略過）
"""

from __future__ import annotations

import contextlib
import contextvars
import html
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from config import Config, _env

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Taipei")
REL_DIR = Path("inbox") / "jarvis"

_meta: contextvars.ContextVar[dict] = contextvars.ContextVar("wiki_inbox_meta", default={})
_always: contextvars.ContextVar[bool] = contextvars.ContextVar("wiki_inbox_always", default=False)


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "data-store"))


def inbox_dir() -> Path:
    return Path(os.environ.get("WIKI_INBOX_DIR") or data_dir() / REL_DIR)


def diverted() -> bool:
    """這則訊息是 Wiki 身分（或觀察名單提醒）、且 WIKI_TELEGRAM 沒開 → 寫收件匣不推播"""
    if not _always.get() and os.environ.get("TG_SENDER", "").strip().upper() != "WIKI":
        return False
    return not _env("WIKI_TELEGRAM", Config.WIKI_TELEGRAM)


def always(send: Callable[[str], bool]) -> Callable[[str], bool]:
    """包住送出函式：透過它送出的訊息不管用誰的 bot，一律寫收件匣（觀察名單提醒用）"""
    def wrapped(text: str) -> bool:
        tok = _always.set(True)
        try:
            return send(text)
        finally:
            _always.reset(tok)
    return wrapped


@contextlib.contextmanager
def meta(type: str = "", code: str = "", name: str = ""):
    """標記接下來送出的訊息屬於哪一類、哪一檔（只影響收件匣欄位，不影響推播內容）"""
    tok = _meta.set({"type": type, "code": str(code or ""), "name": str(name or "")})
    try:
        yield
    finally:
        _meta.reset(tok)


def plain(text: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def _guess_type(text: str) -> str:
    first = plain(text).split("\n", 1)[0].strip()
    return first[:40]


def append(text: str, type: str = "", code: str = "", name: str = "",
           now: Optional[datetime] = None) -> bool:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    m = _meta.get()
    row = {
        "time": now.isoformat(timespec="seconds"),
        "type": type or m.get("type") or _guess_type(text),
        "code": str(code or m.get("code") or ""),
        "name": str(name or m.get("name") or ""),
        "text": plain(text),
    }
    p = inbox_dir() / f"{now:%Y-%m-%d}.jsonl"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("寫入 Wiki 收件匣失敗：%s", e)
        return False
    _union_merge(data_dir())
    logger.info("Wiki 收件匣（不推播）：%s %s %s", row["type"], row["code"], row["name"])
    return True


def _union_merge(repo: Path):
    """同一天的 jsonl 可能被兩個 workflow 同時追加；rebase 時兩邊的行都保留，不產生衝突"""
    info = repo / ".git" / "info"
    if not info.is_dir():
        return
    rule = "inbox/**/*.jsonl merge=union"
    f = info / "attributes"
    try:
        cur = f.read_text(encoding="utf-8") if f.exists() else ""
        if rule not in cur:
            f.write_text(cur + ("" if cur.endswith("\n") or not cur else "\n") + rule + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("設定收件匣合併規則失敗：%s", e)


def flush(repo: Optional[Path] = None) -> bool:
    repo = repo or data_dir()
    if not (repo / ".git").exists():
        logger.info("%s 不是 data 分支的 checkout，略過收件匣提交", repo)
        return False
    _union_merge(repo)

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)

    rel = str(REL_DIR)
    if not (repo / REL_DIR).exists():
        return False
    git("add", "--", rel)
    if git("diff", "--cached", "--quiet", "--", rel).returncode == 0:
        logger.info("收件匣沒有新內容")
        return False
    msg = f"Wiki 收件匣 {datetime.now(TZ):%F %H:%M}"
    git("-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-q", "-m", msg, "--", rel)
    for i in range(3):
        if git("pull", "-q", "--rebase", "--autostash", "origin", "data").returncode == 0 and \
                git("push", "-q", "origin", "HEAD:data").returncode == 0:
            logger.info("已提交：%s", msg)
            return True
        git("rebase", "--abort")
        time.sleep(5 * (i + 1))
    logger.warning("收件匣提交失敗：%s", msg)
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if (sys.argv[1] if len(sys.argv) > 1 else "") == "flush":
        flush()
    else:
        print(__doc__)
