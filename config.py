"""
策略參數集中管理。所有門檻都可以用同名環境變數覆蓋，例如：
    CHUANER_STAGE3_PCT=0.04 python screener.py

數字的意義都寫在旁邊；修改後請重跑 backtest.py 確認效果，不要只看單一天的結果。
"""

import os
from dataclasses import dataclass, field, fields


def _env(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    if isinstance(default, bool):
        return v.lower() in ("1", "true", "yes")
    return type(default)(v)


@dataclass
class Config:
    # ── 交易成本（回測用）──────────────────────────────────────────
    FEE_RATE: float = 0.001425        # 券商手續費（單邊）
    FEE_DISCOUNT: float = 0.6         # 手續費折扣：網路下單一般 5～6 折，取 6 折較保守；不打折填 1.0
    MIN_FEE: float = 20.0             # 最低手續費（元）；回測以報酬率計算時不使用
    TAX_RATE: float = 0.003           # 證交稅（賣出）
    SLIPPAGE: float = 0.001           # 滑價（單邊）

    # ── 族群強弱 ──────────────────────────────────────────────────
    GROUP_MIN_STRONG: int = 4         # 強勢族群：至少幾檔「站上 20MA 或 5 日跑贏大盤」
    GROUP_MIN_RATIO: float = 0.30     # 且占族群成員比例至少
    GROUP_MIN_MEMBERS: int = 3        # 成員少於此數的族群不計（避免 3 檔就 100%）
    RS_DAYS: int = 5                  # 跑贏大盤的比較天數

    # ── 穿山二龍 ──────────────────────────────────────────────────
    CHUANER_LOOKBACK: int = 60        # 第 1 階段：往前看幾個交易日
    CHUANER_RALLY: float = 0.30       # 第 1 階段：波段低點到高點漲幅
    CHUANER_BREAK_WINDOW: int = 20    # 第 2→3 階段：跌破 20MA 後幾天內要站回
    CHUANER_STAGE3_PCT: float = 0.03  # 第 3 階段：站回當天漲幅門檻（3%～5%）

    # ── 黑飛舞／黑飛龍 ────────────────────────────────────────────
    HFW_HIGH_DAYS: int = 60           # Day 0 前「創 N 日新高」
    HFW_RECENT_DAYS: int = 5          # 新高或漲停發生在最近幾天內
    HFW_VOL_MULT: float = 2.0         # Day 0 爆量：≥ 20 日均量的倍數
    HFW_WINDOW: int = 3               # Day 1 必須在 Day 0 後 1～N 天
    HFW_TOUCH_PCT: float = 0.01       # Day 1 最低價距 5MA 在 1% 內
    HFW_SHRINK: float = 0.60          # Day 1 量 ≤ Day 0 量的比例

    # ── 三角收斂 ──────────────────────────────────────────────────
    TRI_MIN_BARS: int = 15            # 最少整理天數
    TRI_MAX_BARS: int = 60            # 最多往前看幾天
    TRI_SWING_K: int = 3              # 轉折點：左右各 K 根
    TRI_FLAT_SLOPE: float = 0.001     # 上緣「持平」：每日斜率 / 價格 < 0.1%
    TRI_BREAK_VOL: float = 1.5        # 突破量 > 20 日均量倍數
    TRI_TOLERANCE: float = 0.02       # 整理期間收盤可超出邊線的容忍度

    # ── 領頭羊 ────────────────────────────────────────────────────
    LEADER_LIMITUP_DAYS: int = 10     # 漲停次數統計天數（無盤中資料時代替漲停速度）
    LEADER_RS_DAYS: int = 20          # 相對強弱天數
    LEADER_STALE_DAYS: int = 3        # 領頭羊幾天沒創新高就換手

    # ── 出場 ──────────────────────────────────────────────────────
    HFW_EXIT_GAIN: float = 0.05       # 黑飛舞 Day 2 最大漲幅門檻
    A_MODE: str = "rally"             # 目標價 A 的算法：rally（第 1 階段漲幅）或 amplitude（型態振幅）
    TARGET_FACTOR: float = 0.5        # 目標價 = Base × (1 + A × 此係數)
    TARGET_SELL: float = 0.5          # 達標賣出比例
    SWING_LOW_DAYS: int = 10          # 「近期波段低點」= 最近 N 根最低點
    MAX_HOLD_DAYS: int = 60           # 回測最長持有天數（到期以收盤出場）

    # ── 回測 ──────────────────────────────────────────────────────
    BT_YEARS: float = 2.0
    BT_IN_SAMPLE_MONTHS: int = 16     # 前 16 個月樣本內、其後為樣本外驗證
    BT_STOP_BEFORE_TARGET: str = "ma20"   # 達標前的停損：ma20（收盤跌破 20MA 全數出場）或 none（只靠最長持有天數）
    BT_MAX_POSITIONS: int = 10        # 資金帳戶模擬：同時最多持有幾檔（每檔投入當時資產的 1/N）
    BT_TOP_N: int = 3                 # 濾網「每日前 N 檔」：每天每種型態只取分數最高的 N 檔

    # ── 模組二：盤中即時監控（TWSE 即時行情快照，免券商帳戶）──
    RT_EVERY_SEC: int = 20            # 輪詢間隔（秒）；證交所約每 5 秒更新一次，太密會被暫時封鎖
    RT_MAX_WATCH: int = 80            # 最多監控幾檔（持股 > 候選 > 觀察名單）
    RT_LARGE_LOTS: int = 500          # 單筆 ≥ 此張數算大單
    RT_LARGE_AMT_SMALL: float = 3e6   # 單筆金額門檻：20 日均成交值 < 1 億
    RT_LARGE_AMT_MID: float = 5e6     #               1～10 億
    RT_LARGE_AMT_BIG: float = 1e7     #               ≥ 10 億
    RT_NET_WINDOW_MIN: int = 5        # 大單淨買統計窗口（分鐘）
    RT_BAR_MIN: int = 5               # 量能比較用的 K 棒長度（分鐘）
    RT_BREAK_VOL: float = 3.0         # 突破時 5 分量 > 前面 5 分K 平均的 N 倍
    RT_TOUCH_PCT: float = 0.01        # 拉回／回測均線的距離
    RT_MIN_ELAPSED_MIN: int = 30      # 開盤 N 分鐘後才用「預估全日量」判斷量縮
    RT_COOLDOWN_MIN: int = 30         # 同一檔同一訊號 N 分鐘內不重複

    def __post_init__(self):
        for f in fields(self):
            setattr(self, f.name, _env(f.name, getattr(self, f.name)))
        if self.A_MODE not in ("rally", "amplitude"):
            raise ValueError(f"A_MODE 必須是 rally 或 amplitude，收到 {self.A_MODE}")
        if self.BT_STOP_BEFORE_TARGET not in ("ma20", "none"):
            raise ValueError(f"BT_STOP_BEFORE_TARGET 必須是 ma20 或 none，收到 {self.BT_STOP_BEFORE_TARGET}")

    @property
    def buy_cost(self) -> float:
        return self.FEE_RATE * self.FEE_DISCOUNT + self.SLIPPAGE

    @property
    def sell_cost(self) -> float:
        return self.FEE_RATE * self.FEE_DISCOUNT + self.TAX_RATE + self.SLIPPAGE


CFG = Config()
