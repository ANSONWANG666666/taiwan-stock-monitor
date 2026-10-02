"""
訊號紀錄簿：每則推播寫一筆紀錄，之後自動回填 5 日與 20 日報酬

紀錄存在 repo 的 journal 分支（workflow 會把它 checkout 到 JOURNAL_DIR），
每個來源一個 CSV，避免不同 workflow 同時寫同一個檔案：
  signals_monitor.csv   大單監控
  signals_screener.csv  盤中選股（09:30／13:00／14:00）
  signals_market.csv    收盤全市場掃描

報酬以「訊號當日收盤價」為基準（盤中訊號也一樣，讓不同來源可以比較）：
  ret_5d / ret_20d     第 5／20 個交易日收盤相對訊號日收盤的漲跌 %
  idx_5d / idx_20d     同期間加權指數漲跌 %
  excess_5d / excess_20d  超額報酬 = 個股 − 大盤
回填由 market_scan.py 每天收盤後執行（它手上有全市場近 30 日收盤）。

JOURNAL_DIR 不存在時（例如在自己電腦上執行）不記錄、不報錯。
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
INDEX_CODE = "IX0001"      # 加權指數在全市場快取中的代號

FIELDS = [
    "ts", "date", "source", "slot", "code", "name", "price", "pct",
    "score", "vol_ratio", "breakout", "strong_vol", "near_low", "rise_from_low", "streak",
    "alerts",
    "news_verdict", "news_net", "news_n_relevant", "news_top", "news_top_rumor",
    "ret_5d", "ret_20d", "idx_5d", "idx_20d", "excess_5d", "excess_20d",
    "industry",   # 新欄位一律加在最後；舊 CSV 會在下次寫入時自動補上表頭
    "sub_industry", "concepts",
]
HORIZONS = (5, 20)


def journal_dir() -> Path:
    return Path(os.environ.get("JOURNAL_DIR", "journal-data"))


def _news_fields(result: Optional[dict]) -> dict:
    if result is None:
        return {"news_verdict": "unchecked"}
    top = (result.get("top") or [None])[0]
    return {
        "news_verdict": result.get("verdict", ""),
        "news_net": f"{result.get('net', 0.0):.4f}",
        "news_n_relevant": result.get("n_relevant", 0),
        "news_top": (top or {}).get("title", "")[:80],
        "news_top_rumor": int(bool(top and top.get("rumor"))),
    }


def _ev_fields(ev: Optional[dict]) -> dict:
    if not ev:
        return {}
    return {
        "score": ev.get("score", ""), "vol_ratio": ev.get("vol_ratio", ""),
        "breakout": int(bool(ev.get("breakout"))), "strong_vol": int(bool(ev.get("strong_vol"))),
        "near_low": int(bool(ev.get("near_low"))), "rise_from_low": ev.get("rise_from_low", ""),
        "streak": ev.get("streak", ""),
    }


def _migrate_header(path: Path):
    """舊檔案的表頭少了新欄位時，用新表頭重寫（多出來的值對應回新欄位）"""
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = list(reader)
    if header == FIELDS:
        return
    fixed = []
    for row in rows:   # 舊表頭是 FIELDS 的前綴，值的順序一致，直接依位置對應
        fixed.append({FIELDS[i]: v for i, v in enumerate(row) if i < len(FIELDS)})
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(fixed)
    logger.info("訊號紀錄簿：%s 表頭更新為 %d 欄", path.name, len(FIELDS))


def record(source: str, code: str, name: str, *, slot: str = "", price: float = 0.0,
           pct: float = 0.0, ev: Optional[dict] = None, alerts: str = "",
           news: Optional[dict] = None, date: Optional[str] = None, industry: str = "",
           sub_industry: str = "", concepts: str = ""):
    """寫一筆訊號紀錄；任何錯誤都不會影響推播"""
    d = journal_dir()
    if not d.is_dir():
        return
    try:
        now = datetime.now(TZ)
        row = {"ts": now.isoformat(timespec="seconds"), "date": date or now.strftime("%Y-%m-%d"),
               "source": source, "slot": slot, "code": code, "name": name,
               "price": f"{price:.2f}" if price else "", "pct": f"{pct:.2f}",
               "alerts": alerts, "industry": industry, "sub_industry": sub_industry,
               "concepts": concepts, **_ev_fields(ev), **_news_fields(news)}
        path = d / f"signals_{source}.csv"
        _migrate_header(path)
        new = not path.exists()
        with path.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        logger.warning("訊號紀錄寫入失敗 %s %s: %s", source, code, e)


def remove(source: str, date: str) -> int:
    """刪除某來源某一天的所有紀錄（重跑時用）；回傳刪除筆數"""
    path = journal_dir() / f"signals_{source}.csv"
    if not path.exists():
        return 0
    _migrate_header(path)
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    keep = [r for r in rows if r.get("date") != date]
    if len(keep) != len(rows):
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(keep)
        logger.info("訊號紀錄簿：刪除 %s %s 舊紀錄 %d 筆", source, date, len(rows) - len(keep))
    return len(rows) - len(keep)


def backfill(cache: dict) -> int:
    """用全市場快取回填報酬；回傳本次填了幾個欄位"""
    d = journal_dir()
    if not d.is_dir():
        return 0
    days = cache.get("days", [])
    pos = {day: i for i, day in enumerate(days)}
    stocks = cache.get("stocks", {})

    def close_on(code: str, day: str) -> Optional[float]:
        for b in stocks.get(code, {}).get("bars", []):
            if b[0] == day:
                return b[1]
        return None

    filled = 0
    for path in sorted(d.glob("signals_*.csv")):
        _migrate_header(path)
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        changed = False
        for r in rows:
            i = pos.get(r["date"])
            if i is None:
                continue
            base = close_on(r["code"], r["date"])
            ibase = close_on(INDEX_CODE, r["date"])
            for n in HORIZONS:
                if r.get(f"ret_{n}d") or i + n >= len(days) or not base:
                    continue
                target = days[i + n]
                c = close_on(r["code"], target)
                if not c:
                    continue
                ret = (c / base - 1) * 100
                r[f"ret_{n}d"] = f"{ret:.2f}"
                ic = close_on(INDEX_CODE, target)
                if ibase and ic:
                    idx = (ic / ibase - 1) * 100
                    r[f"idx_{n}d"] = f"{idx:.2f}"
                    r[f"excess_{n}d"] = f"{ret - idx:.2f}"
                filled += 1
                changed = True
        if changed:
            with path.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
    if filled:
        logger.info("訊號紀錄簿：回填 %d 筆報酬", filled)
    return filled
