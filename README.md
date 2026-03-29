# Web3Report

从 CoinDesk、CoinTelegraph、The Block 的 RSS 抓取加密资讯，合并去重写入 `crypto_news.json`，并按**本地自然日**筛选生成日报。

`report.html` 顶部 **TradingView** 迷你图（BTC/ETH/SOL）**同一行排列**；其下为基于 CoinGecko 近 30 日日线生成的 **SVG 线性外推示意**（未来约 30 日虚线，纯数学模型，**非投资建议**）；再下为 **CoinGecko** 快照与文字情景摘要。

## 环境

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## 使用

- 单次抓取并生成报告：`python crawler.py --once`
- 立即跑一轮后每日 09:00 定时：`python crawler.py`

输出：

- 控制台：当日摘要（最多列出 10 条标题与链接）
- `report.html`：走势图 + 行情快照与情景分析 + 当日资讯列表（用浏览器打开；图表需联网加载 TradingView）

数据保存在 `crypto_news.json`（最多保留约 1000 条）。

## 依赖

见 `requirements.txt`（已锁定版本）。
