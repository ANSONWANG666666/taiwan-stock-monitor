"""
台股價格規則：升降單位（tick）與漲跌停價。所有漲停判斷都必須用這裡的函式。

股票升降單位（2026 年現行制度）：
    未滿 10 元          0.01
    10 ～ 未滿 50 元    0.05
    50 ～ 未滿 100 元   0.1
    100 ～ 未滿 500 元  0.5
    500 ～ 未滿 1000 元 1
    1000 元以上         5

漲跌停：以前一日收盤價（或除權息參考價）±10%，
漲停價向下取到合法價位、跌停價向上取到合法價位。
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

_BANDS = [  # (上限（不含）, 升降單位)
    (Decimal("10"), Decimal("0.01")),
    (Decimal("50"), Decimal("0.05")),
    (Decimal("100"), Decimal("0.1")),
    (Decimal("500"), Decimal("0.5")),
    (Decimal("1000"), Decimal("1")),
    (None, Decimal("5")),
]
LIMIT_PCT = Decimal("0.10")


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def tick_size(price) -> Decimal:
    """該價格所在區間的升降單位"""
    p = _d(price)
    for upper, tick in _BANDS:
        if upper is None or p < upper:
            return tick
    return _BANDS[-1][1]


def _round_to_tick(raw: Decimal, mode) -> Decimal:
    # 先用 raw 所在區間的 tick 取整；若結果跨區間（例如 10.03 → 10.00 在 0.05 區間），再用新區間校正
    tick = tick_size(raw)
    v = (raw / tick).to_integral_value(rounding=mode) * tick
    tick2 = tick_size(v)
    if tick2 != tick:
        v = (raw / tick2).to_integral_value(rounding=mode) * tick2
    return v


def limit_up(ref_price) -> float:
    """漲停價：參考價 × 1.1，向下取到合法價位"""
    ref = _d(ref_price)
    return float(_round_to_tick(ref * (1 + LIMIT_PCT), ROUND_FLOOR))


def limit_down(ref_price) -> float:
    """跌停價：參考價 × 0.9，向上取到合法價位"""
    ref = _d(ref_price)
    return float(_round_to_tick(ref * (1 - LIMIT_PCT), ROUND_CEILING))


def is_limit_up(price, ref_price) -> bool:
    """價格是否等於（或高於，資料誤差時）漲停價"""
    if not price or not ref_price:
        return False
    return _d(price) >= _d(limit_up(ref_price))


def is_limit_down(price, ref_price) -> bool:
    if not price or not ref_price:
        return False
    return _d(price) <= _d(limit_down(ref_price))


def is_locked_limit_up(open_, high, low, close, ref_price) -> bool:
    """一字漲停：開高低收都在漲停價，盤中幾乎買不到"""
    lu = limit_up(ref_price)
    return all(v and abs(float(v) - lu) < 1e-9 for v in (open_, high, low, close))


def is_locked_limit_down(open_, high, low, close, ref_price) -> bool:
    ld = limit_down(ref_price)
    return all(v and abs(float(v) - ld) < 1e-9 for v in (open_, high, low, close))
