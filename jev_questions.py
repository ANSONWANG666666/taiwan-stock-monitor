"""Jev 問題組：每則新聞一次請求，六個問題平行判斷。

依 TypeSafe 文件建議：
- 問題與評分標準用英文寫（Jev 對中文的準確度較低），新聞原文保留中文放在 state。
- 每個問題只問一個維度；Score 等級用「具體情境」描述，不寫「中度」這類程度詞。
- 權重、時間衰減、門檻都放在程式碼（與 HTML 報告）裡，不放進問題。
"""

from typesafe_sdk import Choice, Noul, Score

# 問題版本：修改任何問題文字時請遞增，快取會自動失效重跑
QUESTION_VERSION = "v1"


def build_questions():
    return {
        # 1. 相關性閘門：新聞是否真的在講這家公司
        "about_company": Noul(
            instructions=(
                "Is `news` mainly about `company` itself - its business, earnings, "
                "monthly revenue, products, customers, capacity, or corporate actions - "
                "rather than mentioning it only in passing, in a list of many stocks, "
                "or in a general market wrap-up?"
            ),
            criteria={
                "true": "The company is the main subject or a central subject of the news.",
                "false": "The company is only mentioned briefly, listed among many stocks, "
                "or the news is a general market or index summary.",
            },
        ),
        # 2. 方向：對營收/獲利是利多還是利空（用機率分布算期望方向）
        "direction": Choice(
            instructions=(
                "What does `news` imply for the future revenue or profit of `company`?"
            ),
            criteria={
                "positive": "Points to higher revenue, profit, margin, orders, or market "
                "share for the company.",
                "negative": "Points to lower revenue, profit, margin, orders, or market "
                "share for the company, or to added costs, losses, or risks.",
                "mixed": "Contains both a clear positive and a clear negative implication "
                "for the company's revenue or profit.",
                "neutral": "No clear implication for the company's revenue or profit, "
                "such as a routine announcement, event notice, or price-movement report "
                "without a business reason.",
            },
        ),
        # 3. 影響幅度（Score 0–4，低到高）
        "impact": Score(
            instructions=(
                "Assuming what `news` reports is accurate, how large is its effect on "
                "the revenue or profit of `company`?"
            ),
            criteria=[
                "No effect on the company's revenue or profit.",
                "A small order, product, or event unlikely to be visible in the "
                "company's quarterly results.",
                "Affects one product line, customer, or plant, but is modest compared "
                "with the company's total revenue.",
                "Likely to be visible in the company's quarterly revenue, gross margin, "
                "or earnings.",
                "Could change the company's annual earnings outlook, such as winning or "
                "losing a key customer, a large capacity change, or a major shift in "
                "product prices.",
            ],
        ),
        # 4. 持續性
        "persistence": Choice(
            instructions="How long is the effect described in `news` likely to last for `company`?",
            criteria={
                "one_off": "A single event with no lasting effect, such as a one-time gain "
                "or loss, or a single month's fluctuation.",
                "short_term": "An effect likely to last a few months up to one or two quarters.",
                "structural": "A lasting change to the business, such as a new long-term "
                "customer, a new product line, a capacity expansion, or an industry shift.",
            },
        ),
        # 5. 事件類型（報告篩選用，也可在報告中給不同權重）
        "event_type": Choice(
            instructions="Which category best describes the main event in `news` about `company`?",
            criteria={
                "monthly_revenue": "Monthly revenue report or revenue growth figures.",
                "earnings": "Quarterly or annual earnings, margins, EPS, or company guidance.",
                "orders_customers": "New or lost orders, customers, certifications, or design wins.",
                "capacity_capex": "Capacity expansion, new plants, capital expenditure, or production lines.",
                "pricing_supply": "Product prices, supply and demand, inventory, or raw material costs.",
                "corporate_action": "Dividends, buybacks, capital raising, mergers, acquisitions, or spin-offs.",
                "regulation_policy": "Government policy, tariffs, export controls, subsidies, or regulation.",
                "management_governance": "Management changes, board matters, or governance issues.",
                "incident_litigation": "Accidents, outages, fines, lawsuits, or other adverse incidents.",
                "analyst_market_view": "Analyst ratings, target prices, institutional investor flows, "
                "or market commentary rather than a company event.",
                "other": "None of the above.",
            },
        ),
        # 6. 可信度：是否為傳聞/未經證實
        "unconfirmed": Noul(
            instructions=(
                "Is the key information in `news` unconfirmed - based on rumors, anonymous "
                "or supply-chain sources, or speculation (for example wording such as "
                "傳出, 據悉, 業界傳, 法人預估, 市場傳聞) - rather than confirmed by the "
                "company, an official filing, or reported data?"
            ),
            criteria={
                "true": "The key claim is a rumor, estimate, or unnamed-source report.",
                "false": "The key claim is confirmed by the company, an official filing, "
                "or published figures.",
            },
        ),
    }


def build_state(stock, item):
    """state 用具名 JSON 欄位，讓問題可以用 `company`、`news` 引用。"""
    news = {"title": item["title"]}
    if item.get("summary") and item["summary"] != item["title"]:
        news["summary"] = item["summary"]
    if item.get("source"):
        news["source"] = item["source"]
    if item.get("published"):
        news["published"] = item["published"]
    company = {"ticker": stock["ticker"], "name": stock["name"]}
    if stock.get("industry"):
        company["industry"] = stock["industry"]
    return {"company": company, "news": news}
