#!/usr/bin/env python3
"""
盤中監控（單一長時間執行）

GitHub 的排程很不可靠：每 5 分鐘的排程常常整天只跑一兩次。
這支程式只需要開盤前被觸發「一次」，之後在同一個執行裡持續工作到收盤後：

  09:00–13:35  每 5 分鐘執行一次大單監控（stock_check_once.main）
  09:00–13:30  每 20 秒盤中即時監控（realtime_monitor：昨天收盤後選出的持股／候選／觀察名單）
  09:30        盤中選股：早盤觀察
  13:00        盤中選股：中場更新
  13:20        出場提醒：持有黑飛舞且今天是 Day 2 → 用即時最高價先判斷（holdings.yaml）
  13:50        盤中選股：收盤確認
  做完 13:50 那次就結束；最晚 14:10 結束

晚啟動也沒關係：從啟動當下開始監控；錯過的選股時段只要還在該時段的有效窗口內就補做。
休市日（週末、國定假日）會自動結束。
"""

import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import stock_check_once
import stock_screener

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
MONITOR_START = (9, 0)
MONITOR_END = (13, 35)
MONITOR_EVERY = timedelta(minutes=int(os.environ.get("MONITOR_EVERY_MIN", "5")))
SCREENER_SLOTS = [("0930", (9, 30)), ("1300", (13, 0)), ("1400", (13, 50))]
RT_END = (13, 30)
RT_EVERY = timedelta(seconds=int(os.environ.get("RT_EVERY_SEC", "20")))
EXIT_CHECK = (13, 20)          # 13:20～13:30 之間做一次，來得及在收盤前賣
HARD_END = (14, 10)
TICK = 20   # 秒

now: Callable[[], datetime] = lambda: datetime.now(TZ)
sleep: Callable[[float], None] = time.sleep


def at(base: datetime, hm) -> datetime:
    return base.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def is_trading_day(t: datetime) -> Optional[bool]:
    """用 TWSE 即時資料的日期判斷今天有沒有開盤；查不到時回傳 None（當作有開盤繼續跑）"""
    try:
        items = stock_check_once.fetch_stocks(["2330"], [])
    except Exception as e:
        logger.warning("開盤檢查失敗：%s", e)
        return None
    if not items:
        return None
    return str(items[0].get("d", "")) == t.strftime("%Y%m%d")


def run_screener(slot: str):
    os.environ["SCREENER_SLOT"] = slot
    try:
        stock_screener.main()
    finally:
        os.environ.pop("SCREENER_SLOT", None)


def run_realtime():
    import realtime_monitor      # 延後載入，失敗也不影響大單監控
    realtime_monitor.tick()


def run_exit_check():
    import exit_manager          # 需要 pandas/yaml；延後載入，失敗也不影響大單監控
    exit_manager.intraday_check()


def safe(name: str, fn, *args):
    try:
        fn(*args)
    except SystemExit:
        pass
    except Exception:
        logger.exception("%s 執行失敗，下一輪繼續", name)


def main():
    start = now()
    if start.weekday() >= 5:
        logger.info("週末不開盤，結束")
        return
    hard_end = at(start, HARD_END)
    if start >= hard_end:
        logger.info("已超過 %02d:%02d，今天的盤中監控不再執行", *HARD_END)
        return

    mon_start, mon_end = at(start, MONITOR_START), at(start, MONITOR_END)
    next_monitor = max(mon_start, start)
    next_rt = max(mon_start, start)
    rt_end = at(start, RT_END)
    done = set()
    checked_open = False
    logger.info("=== 盤中監控啟動（台灣時間 %s）===", start.strftime("%H:%M"))

    while True:
        t = now()
        if t >= hard_end:
            break

        # 開盤後確認今天真的有交易（國定假日 TWSE 會回傳前一個交易日的資料）
        if not checked_open and t >= mon_start + timedelta(minutes=5):
            checked_open = True
            if is_trading_day(t) is False:
                logger.info("TWSE 今日無交易資料，判斷為休市日，結束")
                return

        if mon_start <= next_monitor <= mon_end and t >= next_monitor:
            safe("大單監控", stock_check_once.main)
            while next_monitor <= t:
                next_monitor += MONITOR_EVERY

        if mon_start <= t <= rt_end and t >= next_rt:
            safe("盤中即時監控", run_realtime)
            next_rt = t + RT_EVERY

        for slot, hm in SCREENER_SLOTS:
            if slot in done or t < at(t, hm):
                continue
            # 晚啟動時，只補做仍在有效窗口內的時段（例如 12:00 後不補 09:30）
            if stock_screener.current_slot(t) == slot:
                logger.info("— 盤中選股 %s —", slot)
                safe(f"盤中選股 {slot}", run_screener, slot)
            done.add(slot)

        if "exit" not in done and t >= at(t, EXIT_CHECK):
            if t < at(t, EXIT_CHECK) + timedelta(minutes=10):
                logger.info("— 出場提醒（黑飛舞 Day 2）—")
                safe("出場提醒", run_exit_check)
            done.add("exit")

        if "1400" in done:
            break
        sleep(TICK)

    logger.info("=== 盤中監控結束（台灣時間 %s）===", now().strftime("%H:%M"))


if __name__ == "__main__":
    main()
