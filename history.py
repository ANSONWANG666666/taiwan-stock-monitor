#!/usr/bin/env python3
"""
歷史日 K 資料庫（上市＋上櫃，開高低收量）

資料存在 repo 的 data 分支（workflow 把它 checkout 到 DATA_DIR，預設 data-store）：
    ohlc/YYYY-MM.csv.gz   每月一檔：date,code,name,market,open,high,low,close,volume,amount,ref
    meta/industry.json    {代號: {name, market, industry}}，每 7 天更新

ref = 當日漲跌的比較基準（前一日收盤，或除權息／減資後的參考價）
    = 收盤價 − 漲跌價差。ref 與前一日收盤不同時，代表有除權息等事件，
    adjust() 用它把事件之前的價格等比例還原，讓均線與漲幅可以前後比較。
    漲跌停判斷一律用「原始價格」與 ref，不用還原價。

用法：
    python history.py backfill --months 24     第一次回補（上市＋上櫃約 1 小時）
    python history.py update                    每天收盤後更新當天
    python history.py probe 20261002            印出原始欄位，確認資料源格式
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from tw_market_utils import tick_size

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data-store"))
COLUMNS = ["date", "code", "name", "market", "open", "high", "low", "close", "volume", "amount", "ref"]
INDEX_CODE = "IX0001"          # 加權指數（與 journal.py 一致）
REQUEST_GAP = float(os.environ.get("REQUEST_GAP", "3.0"))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept": "application/json",
}

# TWSE／TPEx 產業代碼 → 名稱（與 market_scan 一致）
INDUSTRY_NAMES = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織", "05": "電機機械", "06": "電器電纜",
    "08": "玻璃陶瓷", "09": "造紙", "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造",
    "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨", "19": "綜合", "20": "其他",
    "21": "化學", "22": "生技醫療", "23": "油電燃氣", "24": "半導體", "25": "電腦週邊",
    "26": "光電", "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務",
    "31": "其他電子", "32": "文化創意", "33": "農業科技", "34": "電子商務", "35": "綠能環保",
    "36": "數位雲端", "37": "運動休閒", "38": "居家生活", "91": "存託憑證",
}


# ── 小工具 ───────────────────────────────────────────────────────
def _num(v) -> Optional[float]:
    if v is None:
        return None
    s = re.sub(r"<[^>]+>", "", str(v)).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s))


def is_common_stock(code: str) -> bool:
    """4 碼普通股；排除 00 開頭 ETF、權證等"""
    return len(code) == 4 and code.isdigit() and not code.startswith("00")


def _get_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning("%s 失敗（%d/3）：%s", url.split("/")[-1], attempt + 1, str(e)[:80])
            time.sleep(REQUEST_GAP * 2)
    return None


# ── 上市（TWSE MI_INDEX）─────────────────────────────────────────
def _twse_sign(html_sign: str) -> Optional[int]:
    s = re.sub(r"<[^>]+>", "", str(html_sign)).strip()
    if s == "+":
        return 1
    if s == "-":
        return -1
    if s == "":
        return 0
    return None   # 「X」等不比價情況


def parse_twse(js: dict, d: date) -> Tuple[List[dict], Optional[float]]:
    rows, taiex = [], None
    tables = list(js.get("tables", []) or [])
    tables += [{"fields": v, "data": js.get("data" + k[len("fields"):], [])}
               for k, v in js.items() if k.startswith("fields") and isinstance(v, list)]
    for t in tables:
        f = [_norm(x) for x in (t.get("fields") or [])]
        if "收盤指數" in f:
            for row in t.get("data") or []:
                if str(row[0]).strip().startswith("發行量加權股價指數"):
                    taiex = _num(row[f.index("收盤指數")])
        if "證券代號" in f and "收盤價" in f:
            ix = {k: f.index(k) for k in ("證券代號", "證券名稱", "成交股數", "成交金額", "開盤價",
                                          "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差")}
            for row in t.get("data") or []:
                code = str(row[ix["證券代號"]]).strip()
                if not is_common_stock(code):
                    continue
                close = _num(row[ix["收盤價"]])
                if not close:
                    continue          # 當天無成交
                sign = _twse_sign(row[ix["漲跌(+/-)"]])
                chg = _num(row[ix["漲跌價差"]])
                ref = close - sign * chg if sign is not None and chg is not None else None
                shares = _num(row[ix["成交股數"]]) or 0
                rows.append({"date": d.isoformat(), "code": code, "name": str(row[ix["證券名稱"]]).strip(),
                             "market": "TSE", "open": _num(row[ix["開盤價"]]) or close,
                             "high": _num(row[ix["最高價"]]) or close, "low": _num(row[ix["最低價"]]) or close,
                             "close": close, "volume": int(shares // 1000),
                             "amount": _num(row[ix["成交金額"]]) or 0.0,
                             "ref": round(ref, 4) if ref else None})
    return rows, taiex


def fetch_twse(d: date) -> Tuple[List[dict], Optional[float]]:
    js = _get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                   {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"})
    if not js or js.get("stat") != "OK":
        return [], None
    return parse_twse(js, d)


# ── 上櫃（TPEx）──────────────────────────────────────────────────
_TPEX_KEYS = {  # 欄位名稱（去空白）的可能寫法
    "code": ("代號", "證券代號"), "name": ("名稱", "證券名稱"), "close": ("收盤",),
    "change": ("漲跌",), "open": ("開盤",), "high": ("最高",), "low": ("最低",),
    "volume": ("成交股數",), "amount": ("成交金額(元)", "成交金額"),
}
_TPEX_OLD_POS = {"code": 0, "name": 1, "close": 2, "change": 3, "open": 4, "high": 5, "low": 6,
                 "volume": 8, "amount": 9}


def _tpex_rows(raw_rows: list, pos: Dict[str, int], d: date) -> List[dict]:
    out = []
    for row in raw_rows:
        code = str(row[pos["code"]]).strip()
        if not is_common_stock(code):
            continue
        close = _num(row[pos["close"]])
        if not close:
            continue
        chg = _num(row[pos["change"]])      # 帶正負號；「除息」等文字 → None
        shares = _num(row[pos["volume"]]) or 0
        out.append({"date": d.isoformat(), "code": code, "name": str(row[pos["name"]]).strip(),
                    "market": "OTC", "open": _num(row[pos["open"]]) or close,
                    "high": _num(row[pos["high"]]) or close, "low": _num(row[pos["low"]]) or close,
                    "close": close, "volume": int(shares // 1000), "amount": _num(row[pos["amount"]]) or 0.0,
                    "ref": round(close - chg, 4) if chg is not None else None})
    return out


def parse_tpex(js: dict, d: date) -> List[dict]:
    """支援新版（tables/fields）與舊版（aaData）兩種格式"""
    for t in js.get("tables", []) or []:
        f = [_norm(x) for x in (t.get("fields") or [])]
        pos = {}
        for key, names in _TPEX_KEYS.items():
            for i, x in enumerate(f):
                if x in names or (key in ("close", "open", "high", "low") and x.startswith(names[0])):
                    pos[key] = i
                    break
        if len(pos) == len(_TPEX_KEYS):
            return _tpex_rows(t.get("data") or [], pos, d)
    if js.get("aaData"):
        return _tpex_rows(js["aaData"], _TPEX_OLD_POS, d)
    return []


def fetch_tpex(d: date) -> List[dict]:
    js = _get_json("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
                   {"date": d.strftime("%Y/%m/%d"), "type": "EW", "response": "json"})
    rows = parse_tpex(js, d) if js else []
    if rows:
        return rows
    roc = f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"
    js = _get_json("https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php",
                   {"l": "zh-tw", "d": roc, "o": "json"})
    return parse_tpex(js, d) if js else []


# ── 儲存 ─────────────────────────────────────────────────────────
def _month_path(month: str) -> Path:
    return DATA_DIR / "ohlc" / f"{month}.csv.gz"


def load_month(month: str) -> pd.DataFrame:
    p = _month_path(month)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype={"code": str})


def save_day(d: date, rows: List[dict], taiex: Optional[float]):
    """把某一天的資料寫進該月檔案（同一天已存在會被覆蓋）"""
    if taiex:
        rows = rows + [{"date": d.isoformat(), "code": INDEX_CODE, "name": "加權指數", "market": "IDX",
                        "open": taiex, "high": taiex, "low": taiex, "close": taiex,
                        "volume": 0, "amount": 0.0, "ref": None}]
    month = d.strftime("%Y-%m")
    df = load_month(month)
    df = df[df["date"] != d.isoformat()]
    df = pd.concat([df, pd.DataFrame(rows, columns=COLUMNS)], ignore_index=True)
    df = df.sort_values(["date", "code"])
    p = _month_path(month)
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8", newline="") as f:
        df.to_csv(f, index=False)


def stored_dates() -> set:
    out = set()
    for p in sorted((DATA_DIR / "ohlc").glob("*.csv.gz")):
        out |= set(pd.read_csv(p, usecols=["date"])["date"].unique())
    return out


def load(start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """讀取期間內所有資料（長表）"""
    frames = []
    for p in sorted((DATA_DIR / "ohlc").glob("*.csv.gz")):
        m = p.name[:7]
        if start and m < start[:7]:
            continue
        if end and m > end[:7]:
            continue
        frames.append(pd.read_csv(p, dtype={"code": str}))
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.concat(frames, ignore_index=True)
    if start:
        df = df[df["date"] >= start]
    if end:
        df = df[df["date"] <= end]
    return df.sort_values(["code", "date"]).reset_index(drop=True)


def fetch_day(d: date) -> Tuple[List[dict], Optional[float]]:
    rows, taiex = fetch_twse(d)
    time.sleep(REQUEST_GAP)
    if rows:   # 上市有資料才是交易日
        otc = fetch_tpex(d)
        time.sleep(REQUEST_GAP)
        if not otc:
            logger.warning("%s 上櫃資料取得失敗（只存上市）", d)
        rows += otc
    return rows, taiex


# ── 除權息還原 ───────────────────────────────────────────────────
def adjust(df: pd.DataFrame) -> pd.DataFrame:
    """加上還原價欄位 adj_open/high/low/close 與 prev_ref（漲跌停用的原始參考價）。
    事件判斷：當天 ref 與前一個交易日收盤差距 ≥ 半個 tick（參考價本身是合法價位，正常日兩者相等）。"""
    df = df.sort_values(["code", "date"]).copy()
    prev_close = df.groupby("code")["close"].shift(1)
    ref = df["ref"].astype(float)
    df["prev_ref"] = ref.where(ref.notna(), prev_close)     # 漲跌停基準：有參考價用參考價
    ratio = ref / prev_close
    half_tick = prev_close.map(lambda p: float(tick_size(p)) / 2 if pd.notna(p) and p > 0 else 0)
    event = ref.notna() & prev_close.notna() & ((ref - prev_close).abs() >= half_tick) \
        & ratio.between(0.2, 5.0)
    f = ratio.where(event, 1.0)
    # 某天的還原係數 = 之後所有事件係數的乘積
    df["_f"] = f
    df["adj_factor"] = (df.groupby("code")["_f"]
                        .transform(lambda s: s[::-1].cumprod()[::-1].shift(-1).fillna(1.0)))
    for c in ("open", "high", "low", "close"):
        df[f"adj_{c}"] = df[c] * df["adj_factor"]
    df["is_event"] = event
    return df.drop(columns=["_f"])


# ── 產業別 ───────────────────────────────────────────────────────
def _pick(row: dict, keys) -> str:
    for k in keys:
        if k in row and str(row[k]).strip():
            return str(row[k]).strip()
    return ""


def refresh_industry(force: bool = False) -> Dict[str, dict]:
    p = DATA_DIR / "meta" / "industry.json"
    if p.exists() and not force:
        meta = json.loads(p.read_text(encoding="utf-8"))
        if (date.today() - date.fromisoformat(meta.get("_date", "2000-01-01"))).days < 7:
            return meta["stocks"]
    stocks = {}
    for market, url in (("TSE", "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"),
                        ("OTC", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O")):
        js = _get_json(url)
        if not isinstance(js, list):
            logger.warning("%s 產業別取得失敗", market)
            continue
        for row in js:
            code = _pick(row, ("公司代號", "SecuritiesCompanyCode", "CompanyCode"))
            ind = _pick(row, ("產業別", "SecuritiesIndustryCode", "IndustryCode"))
            name = _pick(row, ("公司簡稱", "CompanyAbbreviation", "公司名稱", "CompanyName"))
            if code:
                key = ind.zfill(2) if ind.isdigit() else ind
                stocks[code] = {"name": name, "market": market,
                                "industry": INDUSTRY_NAMES.get(key, ind or "未分類")}
        time.sleep(REQUEST_GAP)
    if stocks:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"_date": date.today().isoformat(), "stocks": stocks},
                                ensure_ascii=False), encoding="utf-8")
        logger.info("產業別 %d 檔", len(stocks))
        return stocks
    return json.loads(p.read_text(encoding="utf-8"))["stocks"] if p.exists() else {}


# ── git 檢查點（回補時每月提交一次，逾時也不會全部白跑）────────────
def checkpoint(msg: str):
    if os.environ.get("AUTO_COMMIT") != "1" or not (DATA_DIR / ".git").exists():
        return
    def git(*a):
        return subprocess.run(["git", "-C", str(DATA_DIR), *a], capture_output=True, text=True)
    git("add", "-A")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-q", "-m", msg)
    for i in range(3):
        if git("pull", "-q", "--rebase", "origin", "data").returncode == 0 and \
                git("push", "-q", "origin", "HEAD:data").returncode == 0:
            logger.info("已提交：%s", msg)
            return
        time.sleep(5 * (i + 1))
    logger.warning("提交失敗：%s", msg)


# ── 指令 ─────────────────────────────────────────────────────────
def backfill(months: int):
    end = datetime.now(TZ).date()
    start = (end.replace(day=1) - timedelta(days=31 * months)).replace(day=1)
    have = stored_dates()
    d, current_month, n_new = start, start.strftime("%Y-%m"), 0
    logger.info("回補 %s ～ %s（已存在 %d 天）", start, end, len(have))
    while d <= end:
        if d.strftime("%Y-%m") != current_month:
            checkpoint(f"歷史資料 {current_month}")
            current_month = d.strftime("%Y-%m")
        if d.weekday() < 5 and d.isoformat() not in have:
            rows, taiex = fetch_day(d)
            if rows:
                save_day(d, rows, taiex)
                n_new += 1
                n_otc = sum(1 for r in rows if r["market"] == "OTC")
                logger.info("%s：上市 %d、上櫃 %d", d, len(rows) - n_otc, n_otc)
        d += timedelta(days=1)
    checkpoint(f"歷史資料 {current_month}")
    logger.info("回補完成，新增 %d 個交易日", n_new)


def update(d: Optional[date] = None) -> bool:
    d = d or datetime.now(TZ).date()
    rows, taiex = fetch_day(d)
    if not rows:
        logger.info("%s 無交易資料（休市或尚未公布）", d)
        return False
    save_day(d, rows, taiex)
    # 順便補最近 10 天漏掉的交易日
    have = stored_dates()
    for k in range(1, 15):
        dd = d - timedelta(days=k)
        if dd.weekday() < 5 and dd.isoformat() not in have:
            r2, t2 = fetch_day(dd)
            if r2:
                save_day(dd, r2, t2)
                logger.info("補漏 %s", dd)
    refresh_industry()
    checkpoint(f"日K {d}")
    return True


def probe(d: date):
    js = _get_json("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
                   {"date": d.strftime("%Y/%m/%d"), "type": "EW", "response": "json"})
    print("TPEx 新版 keys:", list(js.keys()) if js else None)
    for t in (js or {}).get("tables", [])[:3]:
        print("  fields:", t.get("fields"))
        print("  row0:", (t.get("data") or [None])[0])
    rows = fetch_tpex(d)
    print(f"TPEx 解析 {len(rows)} 筆；範例：", rows[:2])
    rows, taiex = fetch_twse(d)
    print(f"TWSE 解析 {len(rows)} 筆；加權指數 {taiex}；範例：", rows[:2])


def main():
    ap = argparse.ArgumentParser(description="台股歷史日 K 資料庫")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--months", type=int, default=24)
    u = sub.add_parser("update")
    u.add_argument("--date", help="YYYYMMDD，預設今天")
    p = sub.add_parser("probe")
    p.add_argument("date", help="YYYYMMDD")
    a = ap.parse_args()
    if a.cmd == "backfill":
        refresh_industry(force=True)
        backfill(a.months)
    elif a.cmd == "update":
        update(datetime.strptime(a.date, "%Y%m%d").date() if a.date else None)
    else:
        probe(datetime.strptime(a.date, "%Y%m%d").date())


if __name__ == "__main__":
    main()
