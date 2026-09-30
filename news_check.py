#!/usr/bin/env python3
"""
Jev 新聞查證模組
量價異動由程式判斷；這裡只回答「這個異動背後有沒有基本面理由」。

流程：Google News RSS 抓近 N 小時新聞 → 每則新聞送 Jev 判斷 6 個問題
      → 程式依規則得出結論 → 產生附在 Telegram 訊息後面的一段文字。

結論（verdict）：
  confirmed_positive  有證實的利多消息
  confirmed_negative  有證實的利空消息
  neutral             有相關新聞，但沒有明確方向
  rumor_only          只有傳聞、未證實消息
  rumor_driven        有證實新聞但沒有方向，主要訊息來自傳聞或題材
  none                查無相關新聞
  error               查證失敗（不影響原本的推播）

未設定 TYPESAFE_API_KEY 時 check_news() 回傳 None，推播照常送出、不附查證。
"""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import html
import json
import logging
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from jev_questions import QUESTION_VERSION, build_questions, build_state

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
MODEL = "jev-latest"
CACHE_FILE = Path("jev_news_cache.json")

LOOKBACK_HOURS = int(os.environ.get("NEWS_LOOKBACK_HOURS", "48"))
MAX_ITEMS = int(os.environ.get("NEWS_MAX_ITEMS", "8"))
SYMBOL_TTL_MIN = int(os.environ.get("NEWS_SYMBOL_TTL_MIN", "30"))  # 同一檔股票多久內不重查

# ── 每日呼叫上限 ─────────────────────────────────────────────────────
# 三支程式各有自己的額度（在各自的 workflow 設定），合計預設 300 次／天。
# 超過後推播照常送出，只是不附新聞查證。0 = 不限制。
BUDGET_FILE = Path("jev_budget.json")
DAILY_LIMIT = int(os.environ.get("JEV_DAILY_LIMIT", "0"))
PRICE_PER_M_INPUT = 0.042   # US$／百萬 input tokens（TypeSafe 官方定價，輸出不計費）


def _today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def budget_status() -> dict:
    b = {}
    if BUDGET_FILE.exists():
        try:
            b = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
        except Exception:
            b = {}
    if b.get("date") != _today():
        b = {"date": _today(), "calls": 0, "tokens": 0}
    return b


def _budget_add(calls: int, tokens: int):
    b = budget_status()
    b["calls"] += calls
    b["tokens"] += tokens
    try:
        BUDGET_FILE.write_text(json.dumps(b), encoding="utf-8")
    except Exception as e:
        logger.warning("Jev 用量紀錄寫入失敗: %s", e)
    limit = f"／上限 {DAILY_LIMIT}" if DAILY_LIMIT else ""
    logger.info("Jev 今日用量：%d 次%s、%s tokens（約 US$%.4f）", b["calls"], limit,
                f"{b['tokens']:,}", b["tokens"] / 1e6 * PRICE_PER_M_INPUT)


def budget_remaining() -> Optional[int]:
    """剩餘可呼叫次數；None = 不限制"""
    if not DAILY_LIMIT:
        return None
    return max(0, DAILY_LIMIT - budget_status()["calls"])


# ── 判斷規則（全部在程式碼，可自行調整）──────────────────────────────
RELEVANCE_MIN = 0.5      # about_company 低於此值視為不相關
RUMOR_MIN = 0.5          # unconfirmed 高於此值視為傳聞
NET_THRESHOLD = 0.08     # 淨分數超過此值才判定利多 / 利空
LOW_CONF = 0.5           # 方向信心低於此值標示「判斷不確定」
HALF_LIFE_H = 24         # 新聞影響力半衰期（小時）
PERSISTENCE_W = {"one_off": 0.5, "short_term": 0.8, "structural": 1.2}
EVENT_W = {"analyst_market_view": 0.4, "management_governance": 0.7,
           "corporate_action": 0.8, "other": 0.6}   # 未列出者 = 1.0

# 論壇、社群貼文不是新聞，不送 Jev（例：CMoney 股市爆料同學會、PTT、Mobile01）
FORUM_MARKERS = ("爆料同學會", "同學會", "PTT", "Mobile01", "Dcard", "論壇", "討論區")

DIR_LABEL = {"positive": "利多", "negative": "利空", "mixed": "多空互見", "neutral": "中性"}


# ── 快取 ─────────────────────────────────────────────────────────────
def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": {}, "symbols": {}}


def _save_cache(cache: dict):
    cutoff = time.time() - 7 * 86400          # 只保留 7 天內的判斷
    cache["items"] = {k: v for k, v in cache.get("items", {}).items() if v.get("ts", 0) > cutoff}
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _item_key(symbol: str, title: str) -> str:
    return hashlib.sha1(f"{QUESTION_VERSION}|{MODEL}|{symbol}|{title}".encode("utf-8")).hexdigest()


# ── 抓新聞 ───────────────────────────────────────────────────────────
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).strip()


def fetch_news(symbol: str, name: str, hours: int = LOOKBACK_HOURS, limit: int = MAX_ITEMS) -> list:
    days = max(1, (hours + 23) // 24)
    q = f"{name} {symbol} when:{days}d"
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"})
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    cutoff = datetime.now(TZ) - timedelta(hours=hours)
    items, seen = [], set()
    for it in root.iter("item"):
        title = _clean(it.findtext("title", ""))
        source = _clean(it.findtext("source", ""))
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        pub = it.findtext("pubDate")
        dt = email.utils.parsedate_to_datetime(pub).astimezone(TZ) if pub else None
        if dt and dt < cutoff:
            continue
        if any(m in title or m in source for m in FORUM_MARKERS):
            continue
        key = re.sub(r"[\W_]+", "", title)[:40]
        if not key or key in seen:
            continue
        seen.add(key)
        items.append({"title": title, "source": source,
                      "published": dt.isoformat(timespec="minutes") if dt else "",
                      "link": it.findtext("link", "")})
    return items[:limit]


# ── 呼叫 Jev ─────────────────────────────────────────────────────────
def _answers_to_dict(resp) -> dict:
    out = {}
    for qid, a in resp.answers.items():
        d = a.model_dump()
        d.pop("legend", None)
        if "probabilities" in d:
            d["probabilities"] = {str(k): v for k, v in d["probabilities"].items()}
        out[qid] = d
    return out


async def _score_items(stock: dict, items: list, api_key: str, transport=None) -> int:
    """逐則送 Jev；回傳本次使用的 input tokens"""
    from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, TypeSafeAPIError

    questions = build_questions()
    kwargs = {"api_key": api_key, "model": MODEL,
              "retry": RetryPolicy(max_retries=3, backoff_initial=1.0, backoff_max=10.0)}
    if transport is not None:
        kwargs["transport"] = transport
    tokens = 0
    async with AsyncTypeSafeClient(**kwargs) as client:
        async def one(it):
            nonlocal tokens
            try:
                resp = await client.system_one(build_state(stock, it), questions)
                it["answers"] = _answers_to_dict(resp)
                tokens += resp.usage.input_tokens or 0
            except TypeSafeAPIError as e:
                it["error"] = f"{type(e).__name__}: {str(e)[:100]}"
        await asyncio.gather(*(one(it) for it in items))
    return tokens


# ── 規則：單則分數與結論 ─────────────────────────────────────────────
def item_score(a: dict, age_h: float) -> float:
    d = a["direction"]["probabilities"]
    signed = d.get("positive", 0) - d.get("negative", 0)
    impact = a["impact"]["score"] / 4
    pers = sum(p * PERSISTENCE_W.get(k, 1) for k, p in a["persistence"]["probabilities"].items())
    evw = EVENT_W.get(a["event_type"]["choice"], 1.0)
    cred = 1 - a["unconfirmed"]["noul"] * 0.5
    return a["about_company"]["noul"] * signed * impact * pers * evw * cred * 0.5 ** (age_h / HALF_LIFE_H)


def summarize(items: list, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(TZ)
    scored = []
    for it in items:
        a = it.get("answers")
        if not a or a["about_company"]["noul"] < RELEVANCE_MIN:
            continue
        age_h = 0.0
        if it.get("published"):
            age_h = max(0.0, (now - datetime.fromisoformat(it["published"])).total_seconds() / 3600)
        scored.append({**it, "score": item_score(a, age_h),
                       "rumor": a["unconfirmed"]["noul"] >= RUMOR_MIN})
    n_err = sum(1 for it in items if it.get("error"))
    if not scored:
        verdict = "error" if items and n_err == len(items) else "none"
        return {"verdict": verdict, "net": 0.0, "top": [], "n_news": len(items), "n_relevant": 0}

    confirmed = [s for s in scored if not s["rumor"]]
    net_conf = sum(s["score"] for s in confirmed)
    net_all = sum(s["score"] for s in scored)
    top = sorted(scored, key=lambda s: abs(s["score"]), reverse=True)[:3]
    lead = top[0]
    lead_has_direction = lead["answers"]["direction"]["choice"] in ("positive", "negative")
    if not confirmed:
        verdict = "rumor_only"
    elif net_conf >= NET_THRESHOLD:
        verdict = "confirmed_positive"
    elif net_conf <= -NET_THRESHOLD:
        verdict = "confirmed_negative"
    elif lead["rumor"] and lead_has_direction:
        verdict = "rumor_driven"      # 證實消息沒有方向，分數最高的是有方向的傳聞／題材
    else:
        verdict = "neutral"
    low_conf = any(s["answers"]["direction"]["confidence"] < LOW_CONF for s in top[:1])
    return {"verdict": verdict, "net": net_all, "top": top, "low_conf": low_conf,
            "n_news": len(items), "n_relevant": len(scored)}


# ── 對外介面 ─────────────────────────────────────────────────────────
def check_news(symbol: str, name: str, transport=None, use_symbol_cache: bool = True) -> Optional[dict]:
    """回傳結論 dict；未設定 API key 時回傳 None。任何錯誤都不會丟出例外。"""
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        return None
    cache = _load_cache()
    sym_hit = cache.get("symbols", {}).get(symbol)
    if use_symbol_cache and sym_hit and time.time() - sym_hit.get("ts", 0) < SYMBOL_TTL_MIN * 60:
        return sym_hit["result"]
    try:
        stock = {"ticker": symbol, "name": name or symbol}
        items = fetch_news(symbol, stock["name"])
        todo = []
        for it in items:
            hit = cache["items"].get(_item_key(symbol, it["title"]))
            if hit:
                it["answers"] = hit["answers"]
            else:
                todo.append(it)
        remaining = budget_remaining()
        skipped = []
        if remaining is not None and len(todo) > remaining:
            todo, skipped = todo[:remaining], todo[remaining:]
            logger.warning("%s：Jev 今日額度剩 %d 次，%d 則新聞不查證", symbol, remaining, len(skipped))
        if todo:
            tokens = asyncio.run(_score_items(stock, todo, api_key, transport))
            _budget_add(len(todo), tokens)
            for it in todo:
                if it.get("answers"):
                    cache["items"][_item_key(symbol, it["title"])] = {"answers": it["answers"], "ts": time.time()}
        scored_items = [it for it in items if it not in skipped]
        result = summarize(scored_items)
        result["jev_calls"] = len(todo)
        if skipped:
            result["budget_skipped"] = len(skipped)
            if result["verdict"] == "none":      # 全部因額度沒查，不能說「查無新聞」
                result["verdict"] = "budget"
        errs = [it["error"] for it in todo if it.get("error")]
        if errs:
            result["errors"] = errs[:3]
            logger.warning("Jev 呼叫失敗 %d／%d 則：%s", len(errs), len(todo), errs[0])
    except Exception as e:  # 查證失敗不影響原推播
        logger.warning("新聞查證失敗 %s: %s", symbol, str(e)[:120])
        result = {"verdict": "error", "net": 0.0, "top": [], "n_news": 0, "n_relevant": 0,
                  "message": str(e)[:80]}
    if not result.get("budget_skipped"):   # 額度不足的結果不快取，隔天可以重查
        cache.setdefault("symbols", {})[symbol] = {"ts": time.time(), "result": result}
    try:
        _save_cache(cache)
    except Exception as e:
        logger.warning("新聞快取寫入失敗: %s", e)
    return result


def format_news_block(result: Optional[dict], pct: Optional[float] = None) -> str:
    """產生附在 Telegram 訊息後的 HTML 片段；result 為 None 時回傳空字串。"""
    if result is None:
        return ""
    v = result["verdict"]
    head = {
        "confirmed_positive": "✅ 有證實的利多消息",
        "confirmed_negative": "🔻 有證實的利空消息",
        "neutral": "➖ 有相關新聞，但沒有明確方向",
        "rumor_only": "⚠️ 只有傳聞、尚未證實",
        "rumor_driven": "⚠️ 主要是題材或傳聞帶動，證實消息沒有明確方向",
        "none": f"⚠️ 近 {LOOKBACK_HOURS} 小時查無相關新聞，異動原因不明",
        "error": "（新聞查證暫時無法使用）",
        "budget": "（今日 Jev 額度已用完，未查證）",
    }[v]
    lines = [f"\n📰 <b>新聞查證（Jev）</b>：{head}"]
    # 價格與消息方向不一致時特別提醒（規則在程式碼）
    if pct is not None:
        if pct > 0 and v == "confirmed_negative":
            lines.append("❗ 股價上漲但新聞偏利空，留意是否為出貨")
        elif pct > 0 and v in ("none", "rumor_only", "rumor_driven"):
            lines.append("❗ 上漲缺乏證實消息支撐，留意純籌碼或喊單")
        elif pct < 0 and v == "confirmed_positive":
            lines.append("❗ 有利多但股價下跌，可能利多已反映或另有利空")
    for s in result.get("top", []):
        a = s["answers"]
        tag = DIR_LABEL.get(a["direction"]["choice"], a["direction"]["choice"])
        extra = "・傳聞" if s["rumor"] else ""
        title = html.escape(s["title"][:40] + ("…" if len(s["title"]) > 40 else ""))
        if s.get("link"):
            title = f"<a href='{html.escape(s['link'], quote=True)}'>{title}</a>"
        lines.append(f"  • {title}（{tag}・影響 {a['impact']['score']:.1f}/4{extra}）")
    if result.get("low_conf"):
        lines.append("  <i>Jev 對主要新聞的方向判斷不確定，建議自行看原文</i>")
    if result.get("budget_skipped") and v != "budget":
        lines.append(f"  <i>今日 Jev 額度不足，另有 {result['budget_skipped']} 則新聞未查證</i>")
    return "\n".join(lines)
