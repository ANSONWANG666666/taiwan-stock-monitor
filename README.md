# 訊號紀錄簿

這個分支只存推播紀錄，由 GitHub Actions 自動寫入，請不要手動修改 CSV。

| 檔案 | 來源 |
|---|---|
| `signals_monitor.csv` | 大單監控（盤中每 5 分鐘） |
| `signals_screener.csv` | 盤中選股（09:30／13:00／14:00） |
| `signals_market.csv` | 收盤全市場掃描（15:05） |

## 欄位

| 欄位 | 說明 |
|---|---|
| `ts` / `date` | 推播時間／訊號日期 |
| `slot` | 盤中選股的時段、大單監控的時間、全市場掃描為 `close` |
| `code` / `name` | 股票代號／名稱 |
| `price` / `pct` | 推播當下價格／當日漲跌 % |
| `score` `vol_ratio` `breakout` `strong_vol` `near_low` `rise_from_low` `streak` | 起漲評分與各條件（1 = 符合） |
| `alerts` | 大單監控的警報類型 |
| `news_verdict` | Jev 新聞查證結論：`confirmed_positive` 證實利多、`confirmed_negative` 證實利空、`rumor_driven` 題材／傳聞帶動、`rumor_only` 只有傳聞、`neutral` 無明確方向、`none` 查無新聞、`budget` 額度用完、`unchecked` 未查證 |
| `news_net` `news_n_relevant` `news_top` `news_top_rumor` | 新聞淨分數、相關則數、最重要的一則、是否為傳聞 |
| `ret_5d` / `ret_20d` | 第 5／20 個交易日收盤，相對訊號日收盤的漲跌 % |
| `idx_5d` / `idx_20d` | 同期間加權指數漲跌 % |
| `excess_5d` / `excess_20d` | 超額報酬 = 個股 − 大盤 |

報酬由每天 15:05 的全市場掃描自動回填。

## 怎麼看

用 Excel 開啟 CSV，建立樞紐分析表：列＝`news_verdict`，值＝`excess_5d`、`excess_20d` 的平均與個數。
同一天同一檔可能被記錄多次（例如大單監控），比較時建議先只取每天第一筆。
累積至少 100 筆以上再下結論。
