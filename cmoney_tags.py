"""
CMoney 細產業與概念股標籤（與「股市籌碼K線」App 相同的分類）

從 CMoney 個股頁（https://www.cmoney.tw/forum/stock/<代號>）讀取：
  細產業：麵包屑中的分類連結 /forum/category/Cxxxxx，例如「電子中游-機殼」
  概念股：「相關概念股」區塊中的連結 /forum/concept/Cxxxxx，例如「Apple」

這不是官方 API，CMoney 改版或擋爬蟲時會抓不到；抓不到就回傳空值，
推播改用 TWSE 大類，不影響掃描。結果快取 30 天，每天只需要查少數新股票。
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

URL = "https://www.cmoney.tw/forum/stock/{code}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept-Language": "zh-TW,zh;q=0.9",
}
CACHE_DAYS = 30
REQUEST_GAP = 1.5
MAX_FETCH = 80            # 每次最多查幾檔（控制執行時間）
MAX_CONCEPTS = 3

_A = r'<a\b[^>]*href="(?:https?://www\.cmoney\.tw)?/forum/{kind}/(C\d+)[^"]*"[^>]*>(.*?)</a>'


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse(page: str) -> dict:
    """從 CMoney 個股頁 HTML 取出 {sub_industry, concepts}"""
    cat = ""
    # 細產業：優先找「台股大盤行情」麵包屑之後的第一個分類連結
    start = page.find("台股大盤行情")
    for m in re.finditer(_A.format(kind="category"), page[start if start >= 0 else 0:], re.S):
        t = _text(m.group(2))
        if t:
            cat = t
            break
    # 概念股：只看「相關概念股」區塊之後，避免抓到選單裡的熱門概念
    concepts: List[str] = []
    k = page.find("相關概念股")
    if k >= 0:
        for m in re.finditer(_A.format(kind="concept"), page[k:k + 30000], re.S):
            t = _text(m.group(2))
            t = re.sub(r"\s*[-+]?\d+(\.\d+)?%.*$", "", t).strip()   # 去掉連結文字裡的漲跌幅
            if t and t not in concepts:
                concepts.append(t)
            if len(concepts) >= MAX_CONCEPTS:
                break
    return {"sub_industry": cat, "concepts": concepts}


def fetch(code: str) -> Optional[dict]:
    try:
        r = requests.get(URL.format(code=code), headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.debug("CMoney %s 取得失敗：%s", code, str(e)[:80])
        return None
    return parse(r.text)


def get_tags(codes: List[str], cache: dict, today_iso: str) -> Dict[str, dict]:
    """回傳 {代號: {sub_industry, concepts}}；cache 會被更新（存在全市場快取裡）"""
    store = cache.setdefault("cmoney", {})
    cutoff = (datetime.fromisoformat(today_iso) - timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%d")
    todo = [c for c in codes if store.get(c, {}).get("date", "") < cutoff]
    fetched = failed = 0
    for code in todo[:MAX_FETCH]:
        tags = fetch(code)
        time.sleep(REQUEST_GAP)
        if tags is None:
            failed += 1
            if failed >= 5 and fetched == 0:      # 連續失敗：多半是被擋或改版，今天先放棄
                logger.warning("CMoney 標籤連續取得失敗，今天略過細分類")
                break
            continue
        tags["date"] = today_iso
        store[code] = tags
        fetched += 1
    if todo:
        logger.info("CMoney 細分類：查詢 %d 檔、成功 %d、失敗 %d", min(len(todo), MAX_FETCH), fetched, failed)
    return {c: store[c] for c in codes if c in store}


def count_tags(hits: List[dict], tags: Dict[str, dict]) -> tuple:
    """統計符合股票的細產業與概念股出現次數，各自依次數排序"""
    sub, con = {}, {}
    for h in hits:
        t = tags.get(h["code"])
        if not t:
            continue
        if t.get("sub_industry"):
            sub[t["sub_industry"]] = sub.get(t["sub_industry"], 0) + 1
        for c in t.get("concepts", []):
            con[c] = con.get(c, 0) + 1
    order = lambda d: sorted(d.items(), key=lambda kv: kv[1], reverse=True)
    return order(sub), order(con)


def label(tags: Optional[dict]) -> str:
    """一行標籤：電子中游-機殼｜Apple・雲伺服器"""
    if not tags:
        return ""
    parts = [tags.get("sub_industry", "")]
    if tags.get("concepts"):
        parts.append("・".join(tags["concepts"]))
    return "｜".join(p for p in parts if p)
