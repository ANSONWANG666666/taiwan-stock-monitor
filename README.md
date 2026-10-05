# 歷史日 K 資料（上市＋上櫃）

由 GitHub Actions 自動寫入，請不要手動修改。

- `ohlc/YYYY-MM.csv.gz`：每月一檔，欄位 `date,code,name,market,open,high,low,close,volume,amount,ref`
  - `volume` 單位為張；`amount` 為成交金額（元）
  - `ref` 為當日漲跌的比較基準（前一日收盤或除權息參考價）＝收盤 − 漲跌價差，用來判斷漲跌停與還原除權息
  - `code = IX0001` 為加權指數
- `meta/industry.json`：產業別（每 7 天更新）

回補：Actions →「台股歷史資料」→ mode=backfill。每日更新由「台股全市場掃描」執行。
