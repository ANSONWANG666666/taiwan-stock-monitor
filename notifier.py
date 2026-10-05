"""
Telegram 推播（共用）

沿用 repo 原本的 Telegram bot：環境變數 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID。
訊息使用 HTML 格式；超過 Telegram 單則 4096 字上限時自動分段。
"""

import html
import logging
import os
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)
LIMIT = 4000


def _split(text: str, limit: int = LIMIT) -> List[str]:
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit and buf:
            parts.append(buf)
            buf = ""
        buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return parts


def send_telegram(text: str, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("Telegram 未設定，略過推播")
        return False
    ok = True
    for part in _split(text):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=10,
            )
            if not r.ok:
                logger.error("Telegram 回應 %s: %s", r.status_code, r.text[:200])
            ok = ok and r.ok
        except Exception as e:
            logger.error("Telegram 發送失敗: %s", e)
            ok = False
    return ok


def format_alert(stock: str, code: str, group: str, signal: str, reason: str,
                 price: Optional[float] = None, net_large: Optional[float] = None,
                 extra: str = "") -> str:
    """規格的推播格式：股票、族群、訊號類型、原因、價格、大單淨額"""
    lines = [f"<b>【{html.escape(signal)}】{html.escape(stock)}（{code}）</b>"]
    if group:
        lines.append(f"🏷️ 族群：{html.escape(group)}")
    lines.append(f"📝 {html.escape(reason)}")
    if price is not None:
        lines.append(f"💵 價格：{price:,.2f}")
    if net_large is not None:
        lines.append(f"💰 大單淨額：{net_large / 1e4:+,.0f} 萬")
    if extra:
        lines.append(extra)
    lines.append(f"🔗 <a href='https://tw.stock.yahoo.com/quote/{code}'>查看行情</a>")
    return "\n".join(lines)
