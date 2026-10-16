#!/usr/bin/env python3
"""
觀察名單收錄過濾（Jev）：從新聞或節目逐字稿裡，只挑出「講者真的提出基本面論據」的股票。
隨口提及、只說很強會漲、只有線型或目標價的一律排除。

用法：
  python watch_intake.py transcripts/節目_1016.txt [更多檔案...]
  在 GitHub 網頁把 .txt 上傳到 transcripts/ 資料夾 → 自動執行，結果推到 Telegram

流程（程式負責流程、Jev 只做判斷）：
  1. 用全市場名稱表（data 分支 meta/industry.json）找出逐字稿提到的股票（代號或名稱）
  2. 每檔取提到它的句子＋前後各一句，合併成一段（最多 3 段）
  3. 每檔一次 Jev 請求，三個問題：
       substantive  講者有沒有提出具體的基本面理由（不是順帶一提、不是只說很強）
       checkable    有沒有可查證的具體事實（數字、日期、產品、客戶）
       stance       看多／看空／中性
  4. 程式依門檻決定：substantive ≥ 0.6 且 checkable ≥ 0.5 才列為候選
  5. 候選中「看多」的自動寫進 watchlist.yaml（每份最多 5 檔，標記 auto: true）：
     理由先放講者原文、推翻條件先用「跌破 200 日均線」，請你之後改成自己的。
     INTAKE_AUTO_ADD=0 可關閉自動加入。

未設定 TYPESAFE_API_KEY 時改用關鍵字規則（較粗糙，訊息會註明）。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from typesafe_sdk import Choice, Noul

import history
from notifier import send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MODEL = "jev-latest"
QUESTION_VERSION = "intake-v1"
SUBSTANTIVE_MIN = float(os.environ.get("INTAKE_SUBSTANTIVE_MIN", "0.6"))
CHECKABLE_MIN = float(os.environ.get("INTAKE_CHECKABLE_MIN", "0.5"))
MAX_STOCKS = int(os.environ.get("INTAKE_MAX_STOCKS", "25"))     # 一份逐字稿最多送幾檔給 Jev
MAX_SEGMENTS = 3
STANCE_LABEL = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}


# ── Jev 問題（英文問、中文原文放 state；依 TypeSafe 文件建議）──────────
def build_questions():
    return {
        "substantive": Noul(
            instructions=(
                "In `transcript`, does the speaker give a substantive reason for their view on "
                "`company` - a specific point about its revenue, margins, earnings, orders, customers, "
                "products, capacity, guidance, valuation with a stated reason, or a specific change in "
                "institutional holdings - rather than mentioning it in passing, listing it with other "
                "stocks, or only saying it looks strong, hot, or likely to rise?"
            ),
            criteria={
                "true": "The speaker explains why, using the company's business or fundamentals.",
                "false": "A passing mention, a list of names, a price or chart comment, a target "
                "price without reasons, or an opinion with no business reason.",
            },
        ),
        "checkable": Noul(
            instructions=(
                "Does `transcript` state specific, checkable facts about `company`, such as numbers, "
                "percentages, dates, named products, or named customers?"
            ),
            criteria={
                "true": "At least one concrete fact about the company that could be verified.",
                "false": "Only adjectives, feelings, or general statements about the company.",
            },
        ),
        "stance": Choice(
            instructions="What is the speaker's view on `company` in `transcript`?",
            criteria={
                "bullish": "Expects the company's business or stock to do better.",
                "bearish": "Expects the company's business or stock to do worse.",
                "neutral": "No clear positive or negative view.",
            },
        ),
    }


# ── 找出提到的股票 ───────────────────────────────────────────────
def name_table() -> Dict[str, str]:
    """{代號: 名稱}；data 分支沒有時用觀察名單"""
    p = history.DATA_DIR / "meta" / "industry.json"
    table = {}
    if p.exists():
        try:
            table = {c: v.get("name", "") for c, v in json.loads(p.read_text(encoding="utf-8"))["stocks"].items()}
        except Exception as e:
            logger.warning("讀取產業別失敗：%s", e)
    try:
        import watch_alert
        for w in watch_alert.load_watchlist()["watchlist"]:
            table.setdefault(w["code"], w.get("name", ""))
    except Exception:
        pass
    return {c: n for c, n in table.items() if history.is_common_stock(c)}


def sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"[。！？!?\n]+", text) if s.strip()]


def find_mentions(text: str, table: Dict[str, str]) -> Dict[str, List[int]]:
    """{代號: [句子索引]}；名稱至少 2 個字，代號要是 4 碼且前後不是數字"""
    sents = sentences(text)
    hits: Dict[str, List[int]] = {}
    names = sorted(((n, c) for c, n in table.items() if n and len(n) >= 2), key=lambda x: -len(x[0]))
    for i, s in enumerate(sents):
        found = set(re.findall(r"(?<!\d)(\d{4})(?!\d)", s)) & set(table)
        for n, c in names:
            if n in s:
                found.add(c)
        for c in found:
            hits.setdefault(c, []).append(i)
    return hits


def segment_for(sents: List[str], idxs: List[int]) -> str:
    parts, used = [], set()
    for i in idxs[:MAX_SEGMENTS]:
        rng = [j for j in range(i - 1, i + 2) if 0 <= j < len(sents) and j not in used]
        used.update(rng)
        parts.append("。".join(sents[j] for j in rng))
    return "。".join(parts)[:900]


# ── 判斷 ─────────────────────────────────────────────────────────
async def _ask_jev(cands: List[dict], api_key: str, transport=None) -> int:
    from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, TypeSafeAPIError
    from news_check import _answers_to_dict

    questions = build_questions()
    kwargs = {"api_key": api_key, "model": MODEL,
              "retry": RetryPolicy(max_retries=3, backoff_initial=1.0, backoff_max=10.0)}
    if transport is not None:
        kwargs["transport"] = transport
    tokens = 0
    async with AsyncTypeSafeClient(**kwargs) as client:
        async def one(c):
            nonlocal tokens
            state = {"company": {"ticker": c["code"], "name": c["name"]}, "transcript": c["segment"]}
            try:
                resp = await client.system_one(state, questions)
                c["answers"] = _answers_to_dict(resp)
                tokens += resp.usage.input_tokens or 0
            except TypeSafeAPIError as e:
                c["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        await asyncio.gather(*(one(c) for c in cands))
    return tokens


FUND_WORDS = ("營收", "毛利", "EPS", "獲利", "訂單", "客戶", "產能", "擴產", "市占", "法說", "財測",
              "指引", "本益比", "殖利率", "出貨", "外資", "投信", "庫存", "報價")
NUMBER = re.compile(r"\d+(\.\d+)?\s*(%|％|成|個?百分點|億|萬|元|倍|奈米|nm|季|年|月|張|片)")


def heuristic(c: dict) -> dict:
    # 只看提到這檔的句子本身；前後文可能是在講別檔（Jev 會依 `company` 分辨，關鍵字做不到）
    seg = c.get("core") or c["segment"]
    fund = any(w in seg for w in FUND_WORDS)
    num = bool(NUMBER.search(seg))
    return {"substantive": {"noul": 0.8 if fund else 0.2}, "checkable": {"noul": 0.8 if num else 0.2},
            "stance": {"choice": "neutral", "probabilities": {}}}


def decide(c: dict) -> dict:
    a = c.get("answers")
    if not a:
        return {**c, "accepted": False, "why": c.get("error") or "未判斷"}
    sub, chk = a["substantive"]["noul"], a["checkable"]["noul"]
    if sub < SUBSTANTIVE_MIN:
        why = f"沒有實質論點（{sub:.0%}）"
    elif chk < CHECKABLE_MIN:
        why = f"論點沒有可查證的具體事實（{chk:.0%}）"
    else:
        why = ""
    return {**c, "accepted": not why, "why": why, "sub": sub, "chk": chk,
            "stance": a["stance"].get("choice", "neutral")}


def screen_text(text: str, table: Dict[str, str], transport=None) -> dict:
    sents = sentences(text)
    mentions = find_mentions(text, table)
    cands = [{"code": c, "name": table.get(c, c), "segment": segment_for(sents, idx),
              "core": "。".join(sents[i] for i in idx[:MAX_SEGMENTS]), "n": len(idx)}
             for c, idx in sorted(mentions.items(), key=lambda kv: -len(kv[1]))]
    over = cands[MAX_STOCKS:]
    cands = cands[:MAX_STOCKS]
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    mode = "jev"
    if api_key and cands:
        from news_check import _budget_add, budget_remaining
        remaining = budget_remaining()
        if remaining is not None and remaining < len(cands):
            over = cands[remaining:] + over
            cands = cands[:remaining]
        if cands:
            tokens = asyncio.run(_ask_jev(cands, api_key, transport))
            _budget_add(len(cands), tokens)
    else:
        mode = "heuristic"
        for c in cands:
            c["answers"] = heuristic(c)
    results = [decide(c) for c in cands]
    return {"mode": mode, "results": results, "not_checked": [c["code"] for c in over]}


# ── 輸出 ─────────────────────────────────────────────────────────
def yaml_snippet(r: dict, source: str) -> str:
    return (f"  - code: \"{r['code']}\"\n    name: {r['name']}\n    added: YYYY-MM-DD\n"
            f"    source: {source}\n    thesis: >-\n      （用自己的話整理論點）\n"
            f"    invalidation:\n      text:\n        - （什麼事發生代表論點錯了）\n"
            f"      rules:\n        - {{type: break_ma, days: 200}}")


AUTO_ADD = os.environ.get("INTAKE_AUTO_ADD", "1") != "0"
MAX_ADD = int(os.environ.get("INTAKE_MAX_ADD", "5"))      # 一份逐字稿最多自動加入幾檔


def auto_add(res: dict, source: str, path: Optional[Path] = None, today: Optional[str] = None) -> List[dict]:
    """把 Jev 判定「有實質論點且看多」的股票寫進 watchlist.yaml（標記 auto: true）。
    只在 Jev 模式下做（關鍵字規則太粗糙，不自動加入）；已在名單中的不重複加入。
    理由先放講者原文、推翻條件先用「跌破 200 日均線」，等你改成自己的。"""
    import yaml
    import watch_alert

    if not AUTO_ADD or res.get("mode") != "jev":
        return []
    path = path or watch_alert.WATCHLIST_FILE
    if not path.exists():
        logger.warning("找不到 %s，不自動加入", path)
        return []
    text = path.read_text(encoding="utf-8")
    have = {w["code"] for w in watch_alert.load_watchlist(path)["watchlist"]}
    picks = [r for r in res["results"] if r["accepted"] and r.get("stance") == "bullish" and r["code"] not in have]
    picks = sorted(picks, key=lambda r: -(r["sub"] + r["chk"]))[:MAX_ADD]
    if not picks:
        return []
    today = today or datetime.now(watch_alert.TZ).strftime("%Y-%m-%d")
    q = lambda s: json.dumps(s, ensure_ascii=False)       # 雙引號字串＝合法 YAML，避免冒號、引號出錯
    blocks = []
    for r in picks:
        quote = re.sub(r"\s+", " ", r["segment"])[:300]
        blocks.append(
            f"  - code: \"{r['code']}\"\n    name: {q(r['name'])}\n    added: {today}\n"
            f"    source: {q(source + '（Jev 自動收錄）')}\n"
            f"    auto: true          # 待確認：改成自己的理由與推翻條件後刪掉這行\n"
            f"    thesis: {q('【講者原文】' + quote)}\n"
            f"    invalidation:\n      text:\n        - \"（待補：什麼事發生代表論點錯了）\"\n"
            f"      rules:\n        - {{type: break_ma, days: 200}}\n    holding: {{shares: 0}}\n")
    new = re.sub(r"(?m)^watchlist:\s*\[\]\s*$", "watchlist:", text)
    if not re.search(r"(?m)^watchlist:\s*$", new):
        logger.warning("watchlist.yaml 找不到「watchlist:」區塊，不自動加入")
        return []
    new = new.rstrip("\n") + "\n" + "".join(blocks)
    try:   # 寫入前先確認新檔案仍是正確的 YAML，而且檔數正確
        tmp = yaml.safe_load(new)
        if len([w for w in tmp.get("watchlist") or [] if w]) != len(have) + len(picks):
            raise ValueError("檔數不符")
    except Exception as e:
        logger.warning("自動加入後 YAML 驗證失敗（%s），不寫入", e)
        return []
    path.write_text(new, encoding="utf-8")
    logger.info("自動加入觀察名單：%s", "、".join(f"{r['name']}{r['code']}" for r in picks))
    return picks


def format_message(res: dict, source: str, added: Optional[List[dict]] = None) -> str:
    e = html.escape
    acc = [r for r in res["results"] if r["accepted"]]
    rej = [r for r in res["results"] if not r["accepted"]]
    mode = "Jev 判斷" if res["mode"] == "jev" else "關鍵字規則（未設定 TYPESAFE_API_KEY，較粗糙）"
    L = [f"🗂 <b>觀察名單收錄過濾｜{e(source)}</b>",
         f"提到 {len(res['results']) + len(res['not_checked'])} 檔；{mode}"]
    L.append(f"\n✅ <b>有實質論點，可列入候選（{len(acc)}）</b>")
    for r in acc:
        seg = r["segment"][:90] + ("…" if len(r["segment"]) > 90 else "")
        L.append(f"• <b>{e(r['name'])} {r['code']}</b>｜{STANCE_LABEL.get(r['stance'], r['stance'])}"
                 f"｜論點 {r['sub']:.0%}・具體 {r['chk']:.0%}\n  「{e(seg)}」")
    if not acc:
        L.append("（沒有）")
    if rej:
        L.append(f"\n❌ <b>排除（{len(rej)}）</b>")
        L.append("、".join(f"{e(r['name'])}（{e(r['why'])}）" for r in rej[:20]))
    if res["not_checked"]:
        L.append(f"\n<i>另有 {len(res['not_checked'])} 檔因數量或 Jev 額度上限未判斷</i>")
    if added:
        L.append(f"\n🤖 <b>已自動加入觀察名單（{len(added)}）</b>：" + "、".join(f"{e(r['name'])} {r['code']}" for r in added))
        L.append("<i>理由暫用講者原文、推翻條件暫用「跌破 200 日均線」。請到 watchlist.yaml 改成自己的，"
                 "改好後刪掉 auto: true；不想追蹤就整段刪除。看空或中性的不會自動加入。</i>")
    elif acc:
        L.append("\n<i>要加入觀察請在 watchlist.yaml 補上理由與推翻條件（沒有推翻條件無法覆盤），範例：</i>")
        L.append("<pre>" + e(yaml_snippet(acc[0], source)) + "</pre>")
    return "\n".join(L)


def main():
    files = [Path(p) for p in sys.argv[1:] if p.strip()]
    files = [p for p in files if p.exists() and p.suffix.lower() in (".txt", ".md")
             and p.name.lower() != "readme.md"]
    if not files:
        logger.info("沒有要處理的逐字稿（.txt／.md）")
        return
    table = name_table()
    logger.info("名稱表 %d 檔", len(table))
    for p in files:
        res = screen_text(p.read_text(encoding="utf-8", errors="ignore"), table)
        added = auto_add(res, p.stem)
        msg = format_message(res, p.stem, added)
        logger.info("\n%s", msg)
        send_telegram(msg)


if __name__ == "__main__":
    main()
