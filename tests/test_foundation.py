"""階段 1：升降單位／漲跌停、設定、歷史資料解析與除權息還原"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config            # noqa: E402
import history           # noqa: E402
import notifier          # noqa: E402
import tw_market_utils as tw   # noqa: E402


# ── 升降單位與漲跌停（每個價格區間）──────────────────────────────────
@pytest.mark.parametrize("ref,up,down", [
    (5.00, 5.50, 4.50),         # 未滿 10 元：0.01
    (9.50, 10.45, 8.55),        # 漲停跨進 10 元區間：10.45 是 0.05 的倍數
    (9.99, 10.95, 9.00),        # 10.989 → 向下取 0.05 → 10.95；8.991 → 向上取 0.01 → 9.00
    (25.30, 27.80, 22.80),      # 10～50：0.05（27.83→27.80、22.77→22.80）
    (47.30, 52.00, 42.60),      # 52.03 進 50～100 區間取 0.1 → 52.0
    (75.00, 82.50, 67.50),      # 50～100：0.1
    (92.70, 101.50, 83.50),     # 101.97 進 100～500 區間取 0.5 → 101.5；83.43 → 83.5
    (250.00, 275.00, 225.00),   # 100～500：0.5
    (455.00, 500.00, 409.50),   # 500.5 進 500～1000 區間取 1 → 500；409.5 是 0.5 的倍數
    (850.00, 935.00, 765.00),   # 500～1000：1
    (1010.00, 1110.00, 909.00), # 1111 進 1000 以上取 5 → 1110；909 在 1 元區間是合法價位
    (2000.00, 2200.00, 1800.00),
])
def test_limit_prices(ref, up, down):
    assert tw.limit_up(ref) == pytest.approx(up)
    assert tw.limit_down(ref) == pytest.approx(down)


def test_tick_size_bands():
    assert [float(tw.tick_size(p)) for p in (9.99, 10, 49.95, 50, 99.9, 100, 499.5, 500, 999, 1000)] == \
           [0.01, 0.05, 0.05, 0.1, 0.1, 0.5, 0.5, 1, 1, 5]


def test_limit_flags():
    assert tw.is_limit_up(27.80, 25.30) and not tw.is_limit_up(27.75, 25.30)
    assert tw.is_limit_down(22.80, 25.30)
    assert tw.is_locked_limit_up(27.8, 27.8, 27.8, 27.8, 25.30)
    assert not tw.is_locked_limit_up(27.0, 27.8, 26.9, 27.8, 25.30)
    assert not tw.is_limit_up(None, 25.3)


# ── 設定 ─────────────────────────────────────────────────────────
def test_config_env_override(monkeypatch):
    monkeypatch.setenv("CHUANER_STAGE3_PCT", "0.05")
    monkeypatch.setenv("A_MODE", "amplitude")
    c = config.Config()
    assert c.CHUANER_STAGE3_PCT == 0.05 and c.A_MODE == "amplitude"
    monkeypatch.setenv("A_MODE", "abc")
    with pytest.raises(ValueError):
        config.Config()


def test_config_costs():
    c = config.Config()
    assert c.buy_cost == pytest.approx(0.001425 + 0.001)
    assert c.sell_cost == pytest.approx(0.001425 + 0.003 + 0.001)


# ── 歷史資料解析 ─────────────────────────────────────────────────
TWSE_FIELDS = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價",
               "收盤價", "漲跌(+/-)", "漲跌價差", "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比"]


def test_parse_twse():
    js = {"stat": "OK", "tables": [
        {"fields": ["指數", "收盤指數", "漲跌(+/-)", "漲跌點數", "漲跌百分比(%)", "特殊處理註記"],
         "data": [["寶島股價指數", "30,000.00", "", "", "", ""],
                  ["發行量加權股價指數", "22,500.50", "", "", "", ""]]},
        {"fields": TWSE_FIELDS, "data": [
            ["0050", "元大台灣50", "1,000", "1", "190,000", "190", "191", "189", "190", "<p style= color:red>+</p>", "1.00", "", "", "", "", ""],
            ["3013", "晟銘電", "4,569,107", "1", "423,000,000", "94.00", "94.30", "91.50", "92.70", "<p style= color:green>-</p>", "1.30", "", "", "", "", ""],
            ["2330", "台積電", "20,000,000", "1", "1", "1,000.00", "1,010.00", "995.00", "1,005.00", " ", "0.00", "", "", "", "", ""],
            ["2317", "鴻海", "1,000", "1", "1", "--", "--", "--", "--", " ", "0.00", "", "", "", "", ""],
            ["1101", "台泥", "1,000", "1", "1", "30.00", "30.00", "30.00", "30.00", "X", "0.00", "", "", "", "", ""],
        ]}]}
    rows, taiex = history.parse_twse(js, date(2026, 10, 2))
    assert taiex == 22500.5
    by = {r["code"]: r for r in rows}
    assert set(by) == {"3013", "2330", "1101"}          # 排除 ETF 與無成交
    assert by["3013"]["ref"] == 94.0 and by["3013"]["volume"] == 4569 and by["3013"]["high"] == 94.3
    assert by["2330"]["ref"] == 1005.0                  # 平盤
    assert by["1101"]["ref"] is None                    # 不比價


def test_parse_tpex_new_and_old():
    new = {"tables": [{"fields": ["代號", "名稱", "收盤 ", "漲跌", "開盤 ", "最高 ", "最低", "均價 ",
                                  "成交股數  ", "成交金額(元)", "成交筆數 "],
                       "data": [["6488", "環球晶", "500.00", "+10.00", "490.00", "505.00", "488.00", "498",
                                 "1,234,000", "615,000,000", "999"],
                                ["700001", "某權證", "1.00", "0.00", "1", "1", "1", "1", "1000", "1000", "1"],
                                ["8069", "元太", "200.00", "除息", "200", "201", "198", "200", "1,000", "200,000", "1"]]}]}
    rows = history.parse_tpex(new, date(2026, 10, 2))
    assert [r["code"] for r in rows] == ["6488", "8069"]
    assert rows[0]["ref"] == 490.0 and rows[0]["volume"] == 1234 and rows[0]["market"] == "OTC"
    assert rows[1]["ref"] is None
    old = {"aaData": [["6488", "環球晶", "500.00", "-5.00", "505", "506", "499", "500", "2,000", "1,000,000", "1"]]}
    rows = history.parse_tpex(old, date(2026, 10, 2))
    assert rows[0]["ref"] == 505.0 and rows[0]["high"] == 506.0


# ── 儲存與除權息還原 ─────────────────────────────────────────────
def _row(d, code, o, h, l, c, ref, v=1000):
    return {"date": d, "code": code, "name": code, "market": "TSE", "open": o, "high": h, "low": l,
            "close": c, "volume": v, "amount": c * v * 1000, "ref": ref}


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DATA_DIR", tmp_path)
    history.save_day(date(2026, 9, 30), [_row("2026-09-30", "3013", 80, 87, 80, 86.8, 80.0)], 22000.0)
    history.save_day(date(2026, 10, 1), [_row("2026-10-01", "3013", 87.1, 94, 85.8, 94.0, 86.8)], 22100.0)
    history.save_day(date(2026, 10, 1), [_row("2026-10-01", "3013", 87.1, 94, 85.8, 94.0, 86.8)], 22100.0)  # 重複寫
    df = history.load()
    assert len(df) == 4 and history.stored_dates() == {"2026-09-30", "2026-10-01"}
    assert set(df[df.code == "IX0001"]["close"]) == {22000.0, 22100.0}


def test_adjust_ex_dividend():
    # 9/1 收 100、9/2 除息 5 元（參考價 95）收 96、9/3 收 97
    df = pd.DataFrame([
        _row("2026-09-01", "1234", 99, 101, 98, 100, 99),
        _row("2026-09-02", "1234", 95, 97, 94, 96, 95),
        _row("2026-09-03", "1234", 96, 98, 95, 97, 96),
        _row("2026-09-01", "5678", 50, 51, 49, 50, 50),
        _row("2026-09-02", "5678", 50, 52, 50, 51, 50),
    ])
    a = history.adjust(df).set_index(["code", "date"])
    assert a.loc[("1234", "2026-09-02"), "is_event"]
    assert a.loc[("1234", "2026-09-01"), "adj_close"] == pytest.approx(95.0)    # 100 × 0.95
    assert a.loc[("1234", "2026-09-02"), "adj_close"] == pytest.approx(96.0)    # 事件當天以後不變
    assert a.loc[("1234", "2026-09-02"), "prev_ref"] == 95                      # 漲跌停用參考價
    assert not a.loc[("5678", "2026-09-02"), "is_event"]
    assert a.loc[("5678", "2026-09-01"), "adj_close"] == 50


def test_notifier_split_and_format(monkeypatch):
    parts = notifier._split("\n".join("x" * 100 for _ in range(100)), 1000)
    assert len(parts) > 1 and all(len(p) <= 1000 for p in parts)
    msg = notifier.format_alert("晟銘電", "3013", "電子中游-機殼", "黑飛舞 進場", "Day1 量縮回測 5MA",
                                price=92.7, net_large=12_500_000)
    assert "【黑飛舞 進場】晟銘電（3013）" in msg and "+1,250 萬" in msg and "92.70" in msg
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    class R:  # noqa: E701
        ok = True; status_code = 200; text = ""
    monkeypatch.setattr("requests.post", lambda url, json=None, timeout=None: sent.append(json) or R())
    assert notifier.send_telegram("hi") and sent[0]["text"] == "hi"
