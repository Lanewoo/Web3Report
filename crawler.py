import json
import os
import sys
import time
import datetime
import html
import logging
from typing import List, Dict, Optional, Any
from urllib.parse import quote, urlencode
from email.utils import parsedate_to_datetime
from urllib3.util.retry import Retry

import requests
from requests.adapters import HTTPAdapter
import schedule
from bs4 import BeautifulSoup


def _configure_stdio_utf8() -> None:
    """Avoid UnicodeEncodeError on Windows consoles (e.g. cp1252) for logging and print."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_configure_stdio_utf8()

# --- logging (English messages; UTF-8 console + UTF-8 log file) ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("crawler.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
logger = logging.getLogger(__name__)

# --- 常量配置 ---
SOURCES = [
    {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "type": "rss",
    },
    {
        "name": "CoinTelegraph",
        "url": "https://cointelegraph.com/rss",
        "type": "rss",
    },
    {
        "name": "The Block",
        "url": "https://www.theblock.co/rss.xml",
        "type": "rss",
    }
]

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MAX_STORED_NEWS = 1000
DATA_FILE = "crypto_news.json"
REPORT_FILE = "report.html"

# --- 工具函数 ---
def summary_to_plain_text(text: str) -> str:
    """RSS 摘要常为 HTML；转为纯文本再写入页面，避免整段标签被当正文显示。"""
    if not text or not str(text).strip():
        return ""
    s = str(text).strip()
    if "<" not in s or ">" not in s:
        return s
    soup = BeautifulSoup(s, "html.parser")
    plain = soup.get_text(separator=" ", strip=True).replace("\xa0", " ")
    plain = " ".join(plain.split())
    if not plain:
        alts = [img.get("alt", "").strip() for img in soup.find_all("img")]
        plain = " ".join(a for a in alts if a)
    if not plain:
        return "[No text summary — feed item was image/markup only.]"
    return plain


def parse_published(pub: str) -> Optional[datetime.datetime]:
    """将 RSS pubDate (RFC 2822) 解析为本地时区的 datetime 对象。"""
    if not pub or not str(pub).strip():
        return None
    try:
        dt = parsedate_to_datetime(str(pub).strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone()

def get_session():
    """创建一个带有重试机制的 requests 会话。"""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch_market_snapshot(session: requests.Session) -> Dict[str, Any]:
    """CoinGecko 公开接口：全球概况 + 主流币价（无 API Key）。"""
    out: Dict[str, Any] = {"global": None, "prices": None}
    try:
        r = session.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=15,
        )
        r.raise_for_status()
        out["global"] = r.json()
    except Exception as e:
        logger.warning("CoinGecko /global failed: %s", e)
    try:
        r = session.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "bitcoin,ethereum,solana",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=15,
        )
        r.raise_for_status()
        out["prices"] = r.json()
    except Exception as e:
        logger.warning("CoinGecko /simple/price failed: %s", e)
    return out


def _tradingview_advanced_chart_src(symbol: str) -> str:
    """
    TradingView 高级图。成交量在 K 线下方子图；iframe 过矮时子图会被裁切，需配合足够 height。
    部分参数使用 0/1，与文档示例一致。
    """
    q = urlencode(
        {
            "locale": "zh_CN",
            "symbol": symbol,
            # 4 小时 K：价量兼顾；日 K 用 "D"
            "interval": "240",
            "timezone": "Etc/UTC",
            "theme": "light",
            "style": "1",
            "hide_top_toolbar": "1",
            "hide_legend": "0",
            "save_image": "0",
            "calendar": "0",
            "hide_volume": "0",
            "allow_symbol_change": "0",
            "hide_side_toolbar": "1",
            "support_host": "https://www.tradingview.com",
        }
    )
    return f"https://www.tradingview-widget.com/embed-widget/advanced-chart/?{q}"


def fetch_coins_markets_24h(session: requests.Session) -> Dict[str, Any]:
    """各币 24h 全球成交额 total_volume（USD），来自 CoinGecko。"""
    try:
        r = session.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": "bitcoin,ethereum,solana",
                "per_page": 10,
                "page": 1,
                "sparkline": "false",
            },
            timeout=15,
        )
        r.raise_for_status()
        out: Dict[str, Any] = {}
        for row in r.json() or []:
            cid = row.get("id")
            if cid:
                out[cid] = row
        return out
    except Exception as e:
        logger.warning("CoinGecko /coins/markets: %s", e)
        return {}


def _fmt_volume_usd(v: Optional[float]) -> str:
    """格式化 24h 成交额（美元）。"""
    if v is None:
        return "—"
    v = float(v)
    if v >= 1e12:
        return f"${v / 1e12:.2f}T"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:,.0f}"


def _numeric_series_by_day_utc(raw: List, last_n: Optional[int] = None) -> List[float]:
    """将 [ts_ms, value] 按 UTC 日聚合（同日多点则相加），升序，可选只保留最后 last_n 日。"""
    if not raw:
        return []
    by_date = {}
    for ts_ms, val in raw:
        d = datetime.datetime.fromtimestamp(
            ts_ms / 1000.0, tz=datetime.timezone.utc
        ).date()
        by_date[d] = by_date.get(d, 0.0) + float(val)
    vals = [by_date[k] for k in sorted(by_date.keys())]
    if last_n is not None and len(vals) > last_n:
        vals = vals[-last_n:]
    return vals


def fetch_market_chart_json(
    session: requests.Session, coin_id: str, days: int = 30
) -> Optional[dict]:
    """拉取单币种 market_chart 原始 JSON（价+量一次返回，供多段逻辑复用）。"""
    try:
        r = session.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": str(days)},
            timeout=25,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("market_chart %s: %s", coin_id, e)
        return None


def daily_volumes_from_chart_json(
    chart_json: Optional[dict], last_n: int = 14
) -> Optional[List[float]]:
    if not chart_json:
        return None
    vol_raw = chart_json.get("total_volumes") or []
    vals = _numeric_series_by_day_utc(vol_raw, last_n=None)
    if len(vals) > last_n:
        vals = vals[-last_n:]
    return vals if len(vals) >= 2 else None


def daily_closes_from_chart_json(chart_json: Optional[dict]) -> Optional[List[float]]:
    if not chart_json:
        return None
    raw = chart_json.get("prices") or []
    daily = _closes_by_day_utc(raw)
    if len(daily) > 30:
        daily = daily[-30:]
    return daily if len(daily) >= 3 else None


def svg_volume_bars(volumes: List[float], color: str, title: str = "") -> str:
    """本地绘制成交量柱（不依赖 TradingView iframe）。"""
    if not volumes:
        return ""
    vmax = max(volumes) or 1.0
    W, Hbar = 300, 72
    pad_l, pad_r, pad_b = 4.0, 4.0, 16.0
    plot_w = W - pad_l - pad_r
    n = len(volumes)
    bw = plot_w / max(n, 1)
    rects = []
    for i, v in enumerate(volumes):
        bh = (v / vmax) * (Hbar - pad_b - 6)
        x = pad_l + i * bw + bw * 0.08
        y = Hbar - pad_b - bh
        w = bw * 0.84
        rects.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{max(bh, 1.0):.1f}" fill="{html.escape(color)}" opacity="0.78" rx="1"/>'
        )
    cap = f'<text x="{W/2:.0f}" y="11" text-anchor="middle" font-size="9" fill="currentColor" opacity="0.85">{html.escape(title)}</text>'
    foot = f'<text x="{W/2:.0f}" y="{Hbar-2:.0f}" text-anchor="middle" font-size="8" fill="currentColor" opacity="0.65">按日成交额（USD）</text>'
    return f'<svg class="vol-bars-svg" viewBox="0 0 {W} {Hbar}" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">{cap}{"".join(rects)}{foot}</svg>'


def holt_linear_forecast(
    y: List[float], horizon: int = 30, alpha: float = 0.28, beta: float = 0.12
) -> List[float]:
    """Holt 双指数平滑（水平+趋势），比一元线性回归更适应局部斜率变化；仍为启发式外推。"""
    if len(y) < 2:
        return [y[-1]] * horizon
    L = [y[0]]
    T = [y[1] - y[0]]
    for t in range(1, len(y)):
        lt = alpha * y[t] + (1.0 - alpha) * (L[-1] + T[-1])
        tt = beta * (lt - L[-1]) + (1.0 - beta) * T[-1]
        L.append(lt)
        T.append(tt)
    return [L[-1] + k * T[-1] for k in range(1, horizon + 1)]


def html_tradingview_charts(
    session: requests.Session,
    markets: Optional[Dict[str, Any]] = None,
    chart_by_coin: Optional[Dict[str, Optional[dict]]] = None,
) -> str:
    """
    TradingView 外链价图 + **本地 SVG 成交量**（CoinGecko total_volumes 按日），保证可见。
    chart_by_coin：可选，已拉取的 market_chart JSON，避免重复请求。
    """
    if markets is None:
        markets = fetch_coins_markets_24h(session)

    pairs = [
        ("BINANCE:BTCUSDT", "Bitcoin (BTC)", "bitcoin"),
        ("BINANCE:ETHUSDT", "Ethereum (ETH)", "ethereum"),
        ("BINANCE:SOLUSDT", "Solana (SOL)", "solana"),
    ]
    blocks = []
    for symbol, label, cg_id in pairs:
        src = _tradingview_advanced_chart_src(symbol)
        tv_public = f"https://www.tradingview.com/chart/?symbol={quote(symbol, safe='')}"
        row = markets.get(cg_id) or {}
        tvol = row.get("total_volume")
        vol_line = _fmt_volume_usd(tvol) if tvol is not None else "—"
        vol_series = daily_volumes_from_chart_json(
            (chart_by_coin or {}).get(cg_id), last_n=14
        )
        vol_svg = (
            svg_volume_bars(
                vol_series,
                "#2b8a3e" if "BTC" in label else ("#5c7cfa" if "ETH" in label else "#9c36b5"),
                "近14日成交额（柱高∝量）",
            )
            if vol_series
            else '<p class="muted small">日度成交量序列暂不可用。</p>'
        )
        blocks.append(
            f"""
        <div class="chart-wrap">
            <div class="chart-card-head">
              <div class="chart-title">{html.escape(label)}</div>
              <a class="chart-tv-link" href="{html.escape(tv_public)}" target="_blank" rel="noopener">TradingView 大图 ↗</a>
            </div>
            <div class="chart-tv-box">
              <iframe class="tv-iframe tv-iframe-advanced" src="{html.escape(src)}" height="300" width="100%" style="border:0;border-radius:8px" loading="lazy" title="TradingView {html.escape(label)}"></iframe>
            </div>
            <div class="chart-vol-local">{vol_svg}</div>
            <div class="chart-vol-cg">24h 成交额（约）<span class="vol-num">{html.escape(vol_line)}</span></div>
            <p class="chart-hint-mini">柱图为 CoinGecko 日度汇总，与交易所逐笔可能有差异。</p>
        </div>"""
        )
    return f'<section class="charts-section"><h2 class="section-title">主要加密资产 · 价图 + 成交量</h2><div class="chart-row">{"".join(blocks)}</div></section>'


def _closes_by_day_utc(prices_raw: List) -> List[float]:
    """CoinGecko market_chart prices → 按 UTC 日收盘序列（时间升序）。"""
    if not prices_raw:
        return []
    by_date = {}
    for ts_ms, price in prices_raw:
        d = datetime.datetime.fromtimestamp(
            ts_ms / 1000.0, tz=datetime.timezone.utc
        ).date()
        by_date[d] = float(price)
    return [by_date[k] for k in sorted(by_date.keys())]


def _linreg(xs: List[float], ys: List[float]) -> tuple:
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0, my
    slope = num / den
    intercept = my - slope * mx
    return slope, intercept


def _fetch_daily_closes(
    session: requests.Session,
    coin_id: str,
    chart_json: Optional[dict] = None,
) -> Optional[List[float]]:
    if chart_json is None:
        chart_json = fetch_market_chart_json(session, coin_id)
    return daily_closes_from_chart_json(chart_json)


def _fmt_usd_price(p: float) -> str:
    """表格与坐标轴上的美元标价。"""
    ap = abs(p)
    if ap >= 1000:
        return f"${p:,.2f}"
    if ap >= 1:
        return f"${p:,.2f}"
    return f"${p:.4f}"


def _single_asset_forecast_svg(
    title: str,
    color: str,
    closes_usd: List[float],
    fore_linear: List[float],
    fore_holt: List[float],
) -> str:
    """单币种：USD 历史 + 线性外推 + Holt 外推（双方法对照）。"""
    n = len(closes_usd)
    all_y = closes_usd + fore_linear + fore_holt
    y_min = min(all_y)
    y_max = max(all_y)
    pad = (y_max - y_min) * 0.12 or max(y_max * 0.02, 1.0)
    vmin = y_min - pad
    vmax = y_max + pad

    W, H = 400, 280
    left_pad = 56.0
    px, py = left_pad, 24.0
    pw, ph = W - left_pad - 14.0, H - py - 42.0
    x_span = float(n + 29)

    def x_to_px(i: float) -> float:
        return px + (i / x_span) * pw

    def y_to_px(v: float) -> float:
        return py + ph - (v - vmin) / (vmax - vmin) * ph

    split_x = (x_to_px(float(n - 1)) + x_to_px(float(n))) / 2.0

    hist_pts = " ".join(
        f"{x_to_px(float(i)):.1f},{y_to_px(closes_usd[i]):.1f}" for i in range(n)
    )
    fore_lin_pts = " ".join(
        f"{x_to_px(float(n + k)):.1f},{y_to_px(fore_linear[k]):.1f}" for k in range(30)
    )
    fore_holt_pts = " ".join(
        f"{x_to_px(float(n + k)):.1f},{y_to_px(fore_holt[k]):.1f}" for k in range(30)
    )

    last_hist = closes_usd[-1]
    last_lin = fore_linear[-1]
    x_end = x_to_px(float(n + 29))
    y_end = y_to_px(last_lin)
    x_lasth = x_to_px(float(n - 1))
    y_lasth = y_to_px(last_hist)

    y_ticks: List[str] = []
    for ti in range(4):
        val = vmin + (vmax - vmin) * (ti / 3.0)
        ty = y_to_px(val)
        y_ticks.append(
            f'<line x1="{px-4:.0f}" y1="{ty:.1f}" x2="{px:.0f}" y2="{ty:.1f}" stroke="currentColor" stroke-opacity="0.35"/>'
        )
        y_ticks.append(
            f'<text x="{px-6:.0f}" y="{ty+3:.0f}" text-anchor="end" font-size="9" fill="currentColor" opacity="0.8">{html.escape(_fmt_usd_price(val))}</text>'
        )

    return f"""
<svg class="forecast-svg-single" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{html.escape(title)} forecast">
  <text x="{W/2:.0f}" y="16" text-anchor="middle" font-size="12" font-weight="600" fill="currentColor">{html.escape(title)}</text>
  <text x="{px + pw/2:.0f}" y="32" text-anchor="middle" font-size="9" fill="currentColor" opacity="0.75">实线=历史 · 彩色虚线=线性回归 · 灰点划线=Holt 双指数</text>
  {"".join(y_ticks)}
  <rect x="{px}" y="{py}" width="{pw}" height="{ph}" fill="none" stroke="currentColor" stroke-opacity="0.2"/>
  <line x1="{split_x:.1f}" y1="{py}" x2="{split_x:.1f}" y2="{py + ph}" stroke="currentColor" stroke-opacity="0.35" stroke-dasharray="4 3"/>
  <polyline fill="none" stroke="{html.escape(color)}" stroke-width="2.2" points="{hist_pts}"/>
  <polyline fill="none" stroke="{html.escape(color)}" stroke-width="2" stroke-dasharray="7 4" points="{fore_lin_pts}"/>
  <polyline fill="none" stroke="#868e96" stroke-width="1.8" stroke-dasharray="2 4" points="{fore_holt_pts}"/>
  <circle cx="{x_lasth:.1f}" cy="{y_lasth:.1f}" r="3.5" fill="{html.escape(color)}"/>
  <circle cx="{x_end:.1f}" cy="{y_end:.1f}" r="4" fill="none" stroke="{html.escape(color)}" stroke-width="2"/>
</svg>"""


def _single_asset_forecast_card(
    title: str,
    color: str,
    closes_usd: List[float],
) -> str:
    n = len(closes_usd)
    xs = [float(i) for i in range(n)]
    slope, intercept = _linreg(xs, closes_usd)
    fore_lin = [slope * float(n + k) + intercept for k in range(30)]
    fore_holt = holt_linear_forecast(closes_usd, horizon=30)
    last_hist = closes_usd[-1]
    last_lin = fore_lin[-1]
    last_holt = fore_holt[-1]
    d_lin = last_lin - last_hist
    p_lin = (d_lin / last_hist * 100.0) if last_hist else 0.0
    d_holt = last_holt - last_hist
    p_holt = (d_holt / last_hist * 100.0) if last_hist else 0.0
    cell_lin = f"${d_lin:+,.2f}（{p_lin:+.2f}%）"
    cell_holt = f"${d_holt:+,.2f}（{p_holt:+.2f}%）"
    svg = _single_asset_forecast_svg(title, color, closes_usd, fore_lin, fore_holt)
    return f"""
<div class="forecast-card">
  {svg}
  <table class="forecast-price-table">
    <tr><td>最近一日收盘（USD）</td><td class="num">{html.escape(_fmt_usd_price(last_hist))}</td></tr>
    <tr><td>外推第30日 · 线性回归</td><td class="num">{html.escape(_fmt_usd_price(last_lin))}</td></tr>
    <tr><td>外推第30日 · Holt 双指数平滑</td><td class="num">{html.escape(_fmt_usd_price(last_holt))}</td></tr>
    <tr><td>线性回归相对收盘</td><td class="num">{html.escape(cell_lin)}</td></tr>
    <tr><td>Holt 相对收盘</td><td class="num">{html.escape(cell_holt)}</td></tr>
  </table>
</div>"""


def html_forecast_trend_chart(
    session: requests.Session,
    chart_by_coin: Optional[Dict[str, Optional[dict]]] = None,
) -> str:
    """
    三币种各一张图：近若干日 USD 收盘 + 线性外推 30 日，纵轴与表格为具体美元价（示意）。
    """
    assets = [
        ("bitcoin", "BTC / Bitcoin", "#f7931a"),
        ("ethereum", "ETH / Ethereum", "#627eea"),
        ("solana", "SOL / Solana", "#9945FF"),
    ]
    cards: List[str] = []
    for cid, title, color in assets:
        j = (chart_by_coin or {}).get(cid)
        closes = _fetch_daily_closes(session, cid, j)
        if not closes:
            cards.append(
                f'<div class="forecast-card forecast-card-miss"><p class="muted">无法获取 {html.escape(title)} 行情数据。</p></div>'
            )
            continue
        cards.append(_single_asset_forecast_card(title, color, closes))

    inner = f'<div class="forecast-three">{"".join(cards)}</div>'

    return f"""<section class="forecast-section">
<h2 class="section-title">未来约30日 · 分币种外推示意（美元标价，非投资建议）</h2>
<div class="forecast-note-box">
<p class="forecast-legal"><strong>方法：</strong>（1）<strong>一元线性回归</strong>：假设价格沿直线漂移，简单但易在拐点处失真。（2）<strong>Holt 双指数平滑</strong>：同时估计「水平」与「趋势」，对近期变化更敏感，通常比纯直线更贴近短序列，但仍<strong>无法</strong>刻画暴涨暴跌与黑天鹅。
纵轴与表格为模型输出，非实时盘口。**更优方法**（需更多数据与算力）包括：ARIMA/Prophet、状态空间模型、GARCH 类波动率模型、变点检测与机器学习；加密市场非平稳，任何外推均可能失败。</p>
</div>
{inner}
<p class="muted small">数据来源：CoinGecko <code>market_chart</code> · 与本项目作者/模型无法律上的保证关系</p>
</section>"""


def build_trend_analysis_html(snapshot: Dict[str, Any]) -> str:
    """
    基于公开行情数据的「情景」摘要（非价格预测、非投资建议）。
    """
    disclaimer = """<div class="disclaimer-box">
<p class="disclaimer"><strong>重要声明：</strong>以下为依据 CoinGecko 等公开数据自动生成的<strong>情景化整理</strong>，
不构成投资建议；加密货币波动极高，任何人无法可靠「预测」短期价格。</p>
</div>"""

    prices = snapshot.get("prices") or {}
    glob = snapshot.get("global") or {}
    gdata = glob.get("data") if isinstance(glob, dict) else None
    if not prices and not gdata:
        return disclaimer + '<p class="muted">市场数据暂不可用（接口限流或网络问题），请稍后重新生成报告。</p>'

    metric_items: List[str] = []
    for cid, name in [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]:
        row = prices.get(cid)
        if not row:
            continue
        usd = row.get("usd")
        ch24 = row.get("usd_24h_change")
        if usd is not None:
            chs = f"{ch24:+.2f}%" if ch24 is not None else "—"
            metric_items.append(
                f"<li><strong>{name}</strong>：${usd:,.2f}（24h {chs}）</li>"
            )

    mcap_ch = None
    mcap_usd = None
    btc_dom = None
    if isinstance(gdata, dict):
        mcap_usd = (gdata.get("total_market_cap") or {}).get("usd")
        mcap_ch = gdata.get("market_cap_change_percentage_24h_usd")
        btc_dom = (gdata.get("market_cap_percentage") or {}).get("btc")
        if mcap_usd is not None:
            chm = f"{mcap_ch:+.2f}%" if mcap_ch is not None else "—"
            metric_items.append(
                f"<li>全球加密总市值（约）：${mcap_usd / 1e12:.2f}T（24h {chm}）</li>"
            )
        if btc_dom is not None:
            metric_items.append(f"<li>BTC 市值占比（约）：{btc_dom:.1f}%</li>")

    metrics_html = "<ul class='metrics'>" + "".join(metric_items) + "</ul>"

    btc_c = (prices.get("bitcoin") or {}).get("usd_24h_change")
    eth_c = (prices.get("ethereum") or {}).get("usd_24h_change")
    sol_c = (prices.get("solana") or {}).get("usd_24h_change")
    changes = [x for x in (btc_c, eth_c, sol_c) if x is not None]
    avg_ch = sum(changes) / len(changes) if changes else 0.0
    max_abs = max((abs(x) for x in changes), default=0.0)

    if max_abs >= 5:
        vol_note = "主要标的 24h 波动较大，短线情绪偏激烈，注意杠杆与清算风险。"
    elif max_abs >= 2:
        vol_note = "主要标的 24h 波动中等，市场处于活跃换手区间。"
    else:
        vol_note = "主要标的 24h 波动相对温和，短线更偏区间震荡特征。"

    if avg_ch > 1.5:
        trend_core = "综合 BTC/ETH/SOL 的 24h 表现，整体动能略偏强；若量能无法持续，需防范冲高回落。"
    elif avg_ch < -1.5:
        trend_core = "综合 BTC/ETH/SOL 的 24h 表现，整体动能略偏弱；反弹是否延续取决于风险偏好与资金流。"
    else:
        trend_core = "综合 BTC/ETH/SOL 的 24h 表现，整体动能中性，资金在关键位附近博弈。"

    if mcap_ch is not None:
        if mcap_ch > 1:
            trend_core += " 全球加密总市值 24h 扩张，风险偏好略偏暖。"
        elif mcap_ch < -1:
            trend_core += " 全球加密总市值 24h 收缩，风险偏好偏冷。"

    outlook_1_7 = f"""<p><strong>未来 1–7 天（情景，非点位预测）：</strong>{vol_note}
在宏观数据、ETF 资金流与监管消息未出现新的方向性催化前，常见路径是<strong>区间震荡或假突破</strong>；
若隐含波动率维持高位，仓位与止损应优先于方向押注。</p>"""

    outlook_8_30 = """<p><strong>未来 8–30 天（情景，非点位预测）：</strong>
中期更受<strong>美元流动性、利率预期、地缘风险与监管落地节奏</strong>驱动，单一技术指标无法锁定路径。
若 BTC 市值占比显著变化，可粗略视为资金在「大盘」与「山寨」之间再配置的信号，但<strong>不应</strong>据此推断具体价格目标。</p>"""

    evidence_items: List[str] = []
    if changes:
        evidence_items.append(
            f"<li>三币 24h 涨跌幅样本：BTC {btc_c if btc_c is not None else '—'}%，ETH {eth_c if eth_c is not None else '—'}%，SOL {sol_c if sol_c is not None else '—'}%（CoinGecko <code>simple/price</code>）。</li>"
        )
        evidence_items.append(
            f"<li>三币 24h 涨跌幅<strong>算术平均</strong>约 <strong>{avg_ch:+.2f}%</strong>；<strong>最大绝对波动</strong>约 <strong>{max_abs:.2f}%</strong> → 用于判断「波动档位」与下文措辞。</li>"
        )
    if mcap_ch is not None:
        evidence_items.append(
            f"<li>全球加密总市值 24h 变动约 <strong>{mcap_ch:+.2f}%</strong>（<code>/global</code>）→ 用于补充风险偏好冷暖。</li>"
        )
    if btc_dom is not None:
        evidence_items.append(
            f"<li>BTC 市值占比约 <strong>{btc_dom:.1f}%</strong> → 用于描述资金在 BTC 与山寨之间的相对强弱（粗略）。</li>"
        )
    evidence_items.append(
        "<li><strong>规则映射：</strong>平均涨跌 &gt; +1.5% 记为「动能略偏强」；&lt; −1.5% 为「略偏弱」；否则「中性」。最大绝对涨跌 ≥5% 为「波动较大」，≥2% 为「中等」，否则「温和」。总市值 24h &gt;+1% / &lt;−1% 分别记为风险偏好略暖/略冷。</li>"
    )

    evidence_html = (
        "<h3>依据（本次用到的量化输入）</h3><ul class='evidence-list'>"
        + "".join(evidence_items)
        + "</ul>"
    )

    analysis = f"""<section class="trend-section">
<h2 class="section-title">市场快照与趋势情景（自动生成）</h2>
{metrics_html}
<div class="analysis-block">
<h3>结论摘要</h3>
<p>{trend_core}</p>
{evidence_html}
{outlook_1_7}
{outlook_8_30}
<p class="muted small">数据来源：CoinGecko 公开 API · 生成时间 {html.escape(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}</p>
</div>
</section>"""

    return disclaimer + analysis

class CryptoNewsCrawler:
    def __init__(self, data_file: str = DATA_FILE, report_file: str = REPORT_FILE):
        self.data_file = data_file
        self.report_file = report_file
        self.session = get_session()
        self.news_data = self._load_from_disk()

    def _load_from_disk(self) -> List[Dict]:
        """从磁盘加载现有新闻数据。"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error("Failed to load data file: %s", e)
        return []

    def fetch_rss_source(self, source: Dict) -> List[Dict]:
        """抓取单个 RSS 源。"""
        logger.info("Fetching %s...", source["name"])
        try:
            response = self.session.get(source["url"], timeout=15)
            response.raise_for_status()
            # 使用 lxml-xml 加速解析
            soup = BeautifulSoup(response.content, "lxml-xml")
            items = soup.find_all("item")

            new_articles = []
            for item in items:
                link = item.link.text.strip() if item.link else ""
                if not link: continue

                title = item.title.text.strip() if item.title else "无标题"
                pub_date = item.pubDate.text.strip() if item.pubDate else ""
                
                # 提取并清理摘要
                raw_desc = item.description.text if item.description else ""
                desc_soup = BeautifulSoup(raw_desc, "html.parser")
                clean_desc = desc_soup.get_text(separator=" ", strip=True).replace("\xa0", " ")
                summary = clean_desc[:250] + "..." if len(clean_desc) > 250 else clean_desc

                new_articles.append({
                    "source": source["name"],
                    "title": title,
                    "link": link,
                    "published": pub_date,
                    "summary": summary,
                    "fetched_at": datetime.datetime.now().isoformat(),
                })
            return new_articles
        except Exception as e:
            logger.error("Fetch failed for %s: %s", source["name"], e)
            return []

    def run_crawl(self):
        """执行全量抓取并保存。"""
        all_new_items = []
        for source in SOURCES:
            all_new_items.extend(self.fetch_rss_source(source))

        # 排重逻辑
        existing_links = {item["link"] for item in self.news_data}
        unique_new = []
        seen_now = set()
        
        for item in all_new_items:
            link = item["link"]
            if link not in existing_links and link not in seen_now:
                unique_new.append(item)
                seen_now.add(link)

        if unique_new:
            # 新闻按发布时间排序（如果有解析结果）
            self.news_data = unique_new + self.news_data
            self.news_data = self.news_data[:MAX_STORED_NEWS]
            self._save_to_disk()
            logger.info(
                "Crawl complete. Added %s new item(s), %s stored total.",
                len(unique_new),
                len(self.news_data),
            )
        else:
            logger.info("No new articles.")

    def _save_to_disk(self):
        """保存数据到磁盘。"""
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.news_data, f, ensure_ascii=False, indent=4)
        except IOError as e:
            logger.error("Failed to save data: %s", e)

    def _today_news_sorted(self) -> List[Dict]:
        """本地自然日下的今日条目，按发布时间倒序。"""
        today = datetime.date.today()
        today_news = [
            n
            for n in self.news_data
            if (dt := parse_published(n.get("published", ""))) and dt.date() == today
        ]
        today_news.sort(
            key=lambda x: parse_published(x["published"])
            or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
            reverse=True,
        )
        return today_news

    def console_daily_summary(self, max_items: int = 10) -> str:
        """供终端快速浏览的纯文本摘要（最多 max_items 条）。"""
        today = datetime.date.today()
        if not self.news_data:
            return "本地无数据（请先成功抓取并生成 crypto_news.json）。"
        items = self._today_news_sorted()[:max_items]
        if not items:
            return f"今日（{today.isoformat()}）暂无匹配条目。"
        lines = [f"--- Crypto News Report ({today.isoformat()}) ---"]
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. [{item['source']}] {item['title']}")
            lines.append(f"   Link: {item['link']}")
        return "\n".join(lines)

    def generate_html_report(self):
        """生成现代化的 HTML 报告。"""
        today = datetime.date.today()
        today_news = self._today_news_sorted()
        market_snapshot = fetch_market_snapshot(self.session)
        markets_24h = fetch_coins_markets_24h(self.session)
        chart_by_coin: Dict[str, Optional[dict]] = {}
        for _cid in ("bitcoin", "ethereum", "solana"):
            chart_by_coin[_cid] = fetch_market_chart_json(self.session, _cid)
        charts_block = html_tradingview_charts(
            self.session, markets_24h, chart_by_coin
        )
        forecast_block = html_forecast_trend_chart(self.session, chart_by_coin)
        trend_block = build_trend_analysis_html(market_snapshot)

        html_template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Web3 资讯简报 - {today.isoformat()}</title>
    <style>
        :root {{ --bg: #ffffff; --card-bg: #ffffff; --text: #212529; --accent: #007bff; --muted: #6c757d; }}
        @media (prefers-color-scheme: dark) {{
            :root {{ --bg: #121212; --card-bg: #1e1e1e; --text: #e0e0e0; --accent: #375a7f; --muted: #a0a0a0; }}
        }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; margin: 0; padding: 20px; }}
        .container {{ max-width: 1180px; margin: 0 auto; }}
        header {{ border-bottom: 2px solid var(--accent); margin-bottom: 30px; padding-bottom: 10px; }}
        h1 {{ margin: 0; font-size: 1.8rem; }}
        .stats {{ font-size: 0.9rem; color: var(--muted); }}
        .section-title {{ font-size: 1.25rem; margin: 0 0 1rem; border-left: 4px solid var(--accent); padding-left: 0.5rem; }}
        .charts-section {{ margin-bottom: 2rem; }}
        .chart-row {{ display: grid; grid-template-columns: 1fr; gap: 1.25rem; width: 100%; align-items: start; }}
        @media (min-width: 720px) {{
            .chart-row {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
        }}
        @media (min-width: 1100px) {{
            .chart-row {{ grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1.35rem; }}
        }}
        .chart-wrap {{ background: var(--card-bg); border-radius: 14px; padding: 14px 16px 16px; box-shadow: 0 2px 10px rgba(0,0,0,0.06); border: 1px solid rgba(128,128,128,0.12); display: flex; flex-direction: column; gap: 0; min-width: 0; }}
        .chart-card-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }}
        .chart-title {{ font-weight: 600; font-size: 1rem; margin: 0; }}
        .chart-tv-link {{ font-size: 0.8rem; color: var(--accent); text-decoration: none; white-space: nowrap; }}
        .chart-tv-link:hover {{ text-decoration: underline; }}
        .chart-tv-box {{ width: 100%; min-height: 300px; border-radius: 10px; overflow: hidden; background: rgba(0,0,0,0.03); }}
        .chart-vol-local {{ margin-top: 14px; padding-top: 12px; border-top: 1px solid rgba(128,128,128,0.18); text-align: center; }}
        .vol-bars-svg {{ width: 100%; height: auto; display: block; max-width: 100%; margin: 0 auto; }}
        .chart-vol-cg {{ font-size: 0.85rem; margin-top: 10px; padding: 8px 10px; background: rgba(0,123,255,0.06); border-radius: 8px; text-align: center; color: var(--muted); }}
        .chart-vol-cg .vol-num {{ display: inline-block; font-weight: 600; color: var(--text); margin-left: 6px; }}
        .chart-hint-mini {{ font-size: 0.72rem; color: var(--muted); margin: 8px 0 0; line-height: 1.4; text-align: center; }}
        .forecast-section {{ margin-bottom: 2rem; }}
        .forecast-note-box {{ background: rgba(13,110,253,0.08); border: 1px solid rgba(13,110,253,0.25); border-radius: 8px; padding: 10px 14px; margin-bottom: 10px; }}
        .forecast-legal {{ margin: 0; font-size: 0.88rem; }}
        .forecast-three {{ display: grid; grid-template-columns: 1fr; gap: 1.25rem; margin-top: 0.5rem; }}
        @media (min-width: 920px) {{
            .forecast-three {{ grid-template-columns: 1fr 1fr 1fr; }}
        }}
        .forecast-card {{ background: var(--card-bg); border-radius: 12px; padding: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .forecast-card-miss {{ padding: 1rem; }}
        .forecast-svg-single {{ width: 100%; height: auto; max-height: 320px; display: block; }}
        .forecast-price-table {{ width: 100%; border-collapse: collapse; font-size: 0.86rem; margin-top: 10px; }}
        .forecast-price-table td {{ padding: 7px 4px; border-top: 1px solid rgba(128,128,128,0.22); vertical-align: top; }}
        .forecast-price-table tr:first-child td {{ border-top: none; }}
        .forecast-price-table td:first-child {{ color: var(--muted); padding-right: 8px; }}
        .forecast-price-table .num {{ text-align: right; font-weight: 600; white-space: nowrap; }}
        .disclaimer-box {{ background: rgba(255,193,7,0.12); border: 1px solid rgba(255,193,7,0.35); border-radius: 8px; padding: 12px 14px; margin-bottom: 1.25rem; }}
        .disclaimer {{ margin: 0; font-size: 0.9rem; }}
        .trend-section {{ margin-bottom: 2rem; }}
        .metrics {{ margin: 0.5rem 0 1rem; padding-left: 1.2rem; }}
        .metrics li {{ margin-bottom: 0.35rem; }}
        .analysis-block {{ background: var(--card-bg); border-radius: 12px; padding: 16px 18px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .analysis-block h3 {{ margin-top: 0; font-size: 1.05rem; }}
        .evidence-list {{ font-size: 0.88rem; margin: 0.75rem 0 1rem; padding-left: 1.2rem; }}
        .evidence-list li {{ margin-bottom: 0.45rem; }}
        .small {{ font-size: 0.8rem; }}
        article {{ background: var(--card-bg); border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); transition: transform 0.2s; }}
        article:hover {{ transform: translateY(-3px); }}
        .source-tag {{ background: var(--accent); color: white; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; text-transform: uppercase; }}
        h2 {{ margin: 10px 0; font-size: 1.2rem; }}
        h2 a {{ color: inherit; text-decoration: none; }}
        h2 a:hover {{ color: var(--accent); }}
        .pub-date {{ font-size: 0.85rem; color: var(--muted); }}
        .summary {{ font-size: 0.95rem; margin-top: 10px; opacity: 0.9; }}
        .footer {{ text-align: center; margin-top: 50px; color: var(--muted); font-size: 0.8rem; }}
        .no-news {{ text-align: center; padding: 50px; background: var(--card-bg); border-radius: 12px; color: var(--muted); }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🚀 Web3 资讯简报</h1>
            <div class="stats">{today.isoformat()} · 今日更新 {len(today_news)} 条 · 总计存储 {len(self.news_data)} 条</div>
        </header>

        {charts_block}
        {forecast_block}
        {trend_block}

        <h2 class="section-title">今日资讯</h2>
        {"".join(self._format_article(item) for item in today_news) if today_news else '<div class="no-news">今日暂无新资讯。</div>'}

        <div class="footer">
            自动抓取于 {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · 由 Web3Report 提供支持
        </div>
    </div>
</body>
</html>"""
        
        with open(self.report_file, "w", encoding="utf-8") as f:
            f.write(html_template)
        logger.info("Report updated: %s", os.path.abspath(self.report_file))

    def _format_article(self, item: Dict) -> str:
        """格式化单条文章为 HTML。"""
        dt = parse_published(item["published"])
        time_str = dt.strftime("%Y-%m-%d %H:%M") if dt else item["published"]
        summary_plain = summary_to_plain_text(item.get("summary") or "")
        return f"""
        <article>
            <span class="source-tag">{html.escape(item['source'])}</span>
            <h2><a href="{html.escape(item['link'])}" target="_blank" rel="noopener">{html.escape(item['title'])}</a></h2>
            <div class="pub-date">📅 {html.escape(time_str)}</div>
            <div class="summary">{html.escape(summary_plain)}</div>
        </article>"""

def job():
    crawler = CryptoNewsCrawler()
    crawler.run_crawl()
    crawler.generate_html_report()
    report_path = os.path.abspath(crawler.report_file)
    print(crawler.console_daily_summary())
    print(f"HTML report: {report_path}")

if __name__ == "__main__":
    if "--once" in sys.argv:
        logger.info("Running one-shot job...")
        job()
        sys.exit(0)

    # 启动时先运行一次
    job()

    # 每天 09:00 运行
    schedule.every().day.at("09:00").do(job)
    logger.info(
        "Scheduler running; daily job at 09:00. Press Ctrl+C to exit."
    )

    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Crawler stopped.")
