import json
import os
import sys
import time
import datetime
import html
import logging
from typing import List, Dict, Optional, Any
from urllib.parse import quote, urlencode, urlparse
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
        "urls": [
            "https://www.theblock.co/rss.xml",
            "https://www.theblockcrypto.com/rss.xml",
        ],
        "type": "rss",
    },
    {
        "name": "Decrypt",
        "url": "https://decrypt.co/feed",
        "type": "rss",
    },
    {
        "name": "Bitcoin.com",
        "url": "https://news.bitcoin.com/feed/",
        "type": "rss",
    },
    {
        "name": "BeInCrypto",
        "url": "https://beincrypto.com/feed/",
        "type": "rss",
    },
    {
        "name": "Crypto.news",
        "url": "https://crypto.news/feed/",
        "type": "rss",
    },
    {
        "name": "Bitcoin Magazine",
        "url": "https://bitcoinmagazine.com/feed/",
        "type": "rss",
    }
]

# Twitter 作为“大佬动态/今日资讯”的消息源：
# 由于 Twitter/X 原生 RSS 经常需要登录/会被风控，这里依赖第三方“Twitter -> RSS”聚合服务。
# 你可以在服务器环境变量中覆盖：
#   - TWITTER_RSS_PROVIDER：auto | rsshub | nitter | twitrss（默认 auto）
#   - TWITTER_RSS_BASE_URL：rsshub 的 base（默认 https://rsshub.app）
#   - TWITTER_NITTER_BASE_URL：nitter 的 base（默认 https://nitter.net）
#   - TWITTER_TWITTRSS_BASE_URL：twitrss.me 的 base（默认 https://twitrss.me）
#   - TWITTER_RSS_LIMIT：每人拉取条数
TWITTER_RSS_PROVIDER = os.environ.get("TWITTER_RSS_PROVIDER", "auto").strip().lower()
TWITTER_RSS_BASE_URL = os.environ.get("TWITTER_RSS_BASE_URL", "https://rsshub.app")
TWITTER_NITTER_BASE_URL = os.environ.get("TWITTER_NITTER_BASE_URL", "https://nitter.net")
TWITTER_TWITTRSS_BASE_URL = os.environ.get("TWITTER_TWITTRSS_BASE_URL", "https://twitrss.me")
TWITTER_RSS_LIMIT = int(os.environ.get("TWITTER_RSS_LIMIT", "10"))

TWITTER_PEOPLE = [
    {"name": "赵长鹏（CZ）", "handle": "cz_binance"},
    {"name": "孙宇晨（Justin Sun）", "handle": "justinsuntron"},
    {"name": "Vitalik Buterin", "handle": "vitalikbuterin"},
    {"name": "Charles Hoskinson", "handle": "IOHK_Charles"},
    {"name": "Do Kwon", "handle": "stablekwon"},
    {"name": "Anatoly Yakovenko", "handle": "aeyakovenko"},
    {"name": "Brian Armstrong", "handle": "brian_armstrong"},
    {"name": "Sam Bankman-Fried（SBF）", "handle": "SBF_FTX"},
    {"name": "Elizabeth Stark", "handle": "starkness"},
    {"name": "Gavin Wood", "handle": "gavofyork"},
    {"name": "Donald Trump（特朗普）", "handle": "realDonaldTrump"},
    {"name": "Elon Musk（马斯克）", "handle": "elonmusk"},
    {"name": "Michael Saylor（迈克尔·塞勒）", "handle": "saylor"},
    {"name": "Cathie Wood（凯西·伍德）", "handle": "CathieDWood"},
    {"name": "Jeremy Allaire（Circle）", "handle": "jerallaire"},
]

def _build_twitter_rss_url(handle: str, provider: str) -> str:
    provider = provider.strip().lower()
    if provider == "rsshub":
        base = TWITTER_RSS_BASE_URL.rstrip("/")
        q = urlencode({"limit": str(TWITTER_RSS_LIMIT)})
        return f"{base}/twitter/user/{quote(handle, safe='')}?{q}"
    if provider == "nitter":
        base = TWITTER_NITTER_BASE_URL.rstrip("/")
        return f"{base}/{quote(handle, safe='')}/rss"
    if provider == "twitrss":
        base = TWITTER_TWITTRSS_BASE_URL.rstrip("/")
        return f"{base}/twitter_user_to_rss/?user={quote(handle, safe='')}"

    # 未知 provider：退回 rsshub 格式（避免直接崩溃）
    base = TWITTER_RSS_BASE_URL.rstrip("/")
    q = urlencode({"limit": str(TWITTER_RSS_LIMIT)})
    return f"{base}/twitter/user/{quote(handle, safe='')}?{q}"


def _build_twitter_rss_urls(handle: str) -> List[str]:
    if TWITTER_RSS_PROVIDER == "auto":
        providers = ["rsshub", "nitter", "twitrss"]
    else:
        providers = [TWITTER_RSS_PROVIDER]
    return [_build_twitter_rss_url(handle, p) for p in providers]


for p in TWITTER_PEOPLE:
    SOURCES.append(
        {
            "name": f"Twitter/{p['name']}",
            # 自动回退：减少 403/404 导致的空数据
            "urls": _build_twitter_rss_urls(p["handle"]),
            "type": "rss",
        }
    )

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_STORED_NEWS = 1000
DATA_FILE = "crypto_news.json"
EVENTS_FILE = "web3_events.json"
MACRO_FILE = "macro_calendar.json"
REPORT_FILE = "report.html"

# AI 配置 (通过环境变量设置 GEMINI_API_KEY 激活)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
AI_MODEL_NAME = "gemini-1.5-flash"


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
    s = str(pub).strip()
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        # 一些 RSS 源（例如通过第三方代理/抓取后）可能没有 RFC2822，而是 ISO8601
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(s)
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


def rss_request_headers(feed_url: str) -> Dict[str, str]:
    """RSS 请求补充头：部分站点（如 Cloudflare 后）对无 Accept/Referer 的数据中心请求返回 403。"""
    try:
        p = urlparse(feed_url)
        origin = f"{p.scheme}://{p.netloc}/"
    except Exception:
        origin = ""
    return {
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": origin,
        "Cache-Control": "no-cache",
    }


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
            # 过去 12 个月：日K + 12M 区间
            "interval": "D",
            "range": "12M",
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
    session: requests.Session, coin_id: str, days: int = 365
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


def monthly_volumes_from_chart_json(
    chart_json: Optional[dict], last_n: int = 12
) -> Optional[tuple]:
    """按月汇总成交额，返回 (volumes, month_labels)，默认最近12个月。"""
    if not chart_json:
        return None
    vol_raw = chart_json.get("total_volumes") or []
    if not vol_raw:
        return None

    by_month: Dict[datetime.date, float] = {}
    for ts_ms, val in vol_raw:
        d = datetime.datetime.fromtimestamp(
            ts_ms / 1000.0, tz=datetime.timezone.utc
        ).date()
        key = d.replace(day=1)
        by_month[key] = by_month.get(key, 0.0) + float(val)

    keys = sorted(by_month.keys())
    if len(keys) > last_n:
        keys = keys[-last_n:]
    vals = [by_month[k] for k in keys]
    # 横坐标月份，跨年时保留年后两位避免混淆，如 25-11
    labels = [f"{str(k.year)[-2:]}-{k.month:02d}" for k in keys]
    return (vals, labels) if len(vals) >= 2 else None


def daily_closes_from_chart_json(chart_json: Optional[dict]) -> Optional[List[float]]:
    if not chart_json:
        return None
    raw = chart_json.get("prices") or []
    daily = _closes_by_day_utc(raw)
    if len(daily) > 30:
        daily = daily[-30:]
    return daily if len(daily) >= 3 else None


def all_daily_closes_from_chart_json(chart_json: Optional[dict]) -> Optional[List[float]]:
    """CoinGecko market_chart prices -> 全量 UTC 日收盘序列（不截断）。"""
    if not chart_json:
        return None
    raw = chart_json.get("prices") or []
    daily = _closes_by_day_utc(raw)
    return daily if len(daily) >= 3 else None


def svg_volume_bars(
    volumes: List[float], color: str, title: str = "", x_labels: Optional[List[str]] = None
) -> str:
    """本地绘制成交量柱（不依赖 TradingView iframe）。"""
    if not volumes:
        return ""
    vmax = max(volumes) or 1.0
    W, Hbar = 300, 72
    pad_l, pad_r, pad_b = 4.0, 4.0, 18.0
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
    x_texts = []
    if x_labels and len(x_labels) == n:
        for i, lb in enumerate(x_labels):
            tx = pad_l + i * bw + bw * 0.5
            x_texts.append(
                f'<text x="{tx:.1f}" y="{Hbar-2:.0f}" text-anchor="middle" font-size="7" fill="currentColor" opacity="0.72">{html.escape(lb)}</text>'
            )
    cap = f'<text x="{W/2:.0f}" y="11" text-anchor="middle" font-size="9" fill="currentColor" opacity="0.85">{html.escape(title)}</text>'
    return f'<svg class="vol-bars-svg" viewBox="0 0 {W} {Hbar}" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">{cap}{"".join(rects)}{"".join(x_texts)}</svg>'


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


def _sample_std(values: List[float]) -> float:
    """样本标准差（n-1 作为分母），用于“残差驱动”的不确定性估计。"""
    n = len(values)
    if n <= 1:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return var ** 0.5


def _linear_residuals_std(y: List[float]) -> float:
    """线性回归在样本内的残差标准差（用于近似置信区间宽度）。"""
    n = len(y)
    if n <= 2:
        return 0.0
    xs = [float(i) for i in range(n)]
    slope, intercept = _linreg(xs, y)
    residuals = []
    for i, yi in enumerate(y):
        x = float(i)
        y_hat = slope * x + intercept
        residuals.append(float(yi) - y_hat)
    return _sample_std(residuals)


def _holt_one_step_residuals_std(
    y: List[float], alpha: float = 0.28, beta: float = 0.12
) -> float:
    """Holt 的“一步前瞻残差”标准差（t 时刻预测 t 的误差）。"""
    if len(y) <= 2:
        return 0.0
    L = float(y[0])
    T = float(y[1] - y[0])
    residuals: List[float] = []
    for t in range(1, len(y)):
        # t 时刻的一步预测使用上一步的 level + trend
        y_hat = L + T
        residuals.append(float(y[t]) - y_hat)
        lt = alpha * float(y[t]) + (1.0 - alpha) * (L + T)
        tt = beta * (lt - L) + (1.0 - beta) * T
        L, T = lt, tt
    return _sample_std(residuals)


def _backtest_mae_linear(y: List[float], horizon: int, min_train: int = 10) -> Optional[float]:
    """Walk-forward 回测：用过去样本拟合线性回归，预测 horizon 后的 MAE。"""
    n = len(y)
    if n < (min_train + horizon):
        return None
    errs: List[float] = []
    for t in range(min_train - 1, n - horizon):
        train = y[: t + 1]
        xs = [float(i) for i in range(len(train))]
        slope, intercept = _linreg(xs, train)
        x_fore = float(t + horizon)  # 预测落在原序列的索引
        pred = slope * x_fore + intercept
        true_v = float(y[t + horizon])
        errs.append(abs(pred - true_v))
    if not errs:
        return None
    return sum(errs) / len(errs)


def _backtest_mae_holt(y: List[float], horizon: int, min_train: int = 10) -> Optional[float]:
    """Walk-forward 回测：用 Holt 拟合训练集，预测 horizon 后的 MAE。"""
    n = len(y)
    if n < (min_train + horizon):
        return None
    errs: List[float] = []
    for t in range(min_train - 1, n - horizon):
        train = y[: t + 1]
        fore = holt_linear_forecast(train, horizon=horizon)
        pred = float(fore[-1])
        true_v = float(y[t + horizon])
        errs.append(abs(pred - true_v))
    if not errs:
        return None
    return sum(errs) / len(errs)


def _interval_heuristic(point: float, sigma: float, horizon: int, train_len: int, z: float) -> tuple:
    """
    残差驱动的启发式区间：
    - sigma：来自样本内残差的标准差
    - horizon：预测步数
    - train_len：训练窗口长度
    """
    scale = (1.0 + float(horizon) / max(float(train_len), 1.0)) ** 0.5
    w = z * sigma * scale
    return (point - w, point + w)


def html_tradingview_charts(
    session: requests.Session,
    markets: Optional[Dict[str, Any]] = None,
    chart_by_coin: Optional[Dict[str, Optional[dict]]] = None,
) -> str:
    """
    TradingView 外链价图 + 本地 SVG 成交量（CoinGecko，按月汇总最近12个月），保证可见。
    chart_by_coin：可选，已拉取的 market_chart JSON，避免重复请求。
    """
    if markets is None:
        markets = fetch_coins_markets_24h(session)

    # 去掉 SOL 的图，仅保留 BTC/ETH
    pairs = [
        ("BINANCE:BTCUSDT", "Bitcoin (BTC)", "bitcoin"),
        ("BINANCE:ETHUSDT", "Ethereum (ETH)", "ethereum"),
    ]
    blocks = []
    for symbol, label, cg_id in pairs:
        src = _tradingview_advanced_chart_src(symbol)
        tv_public = f"https://www.tradingview.com/chart/?symbol={quote(symbol, safe='')}"
        row = markets.get(cg_id) or {}
        tvol = row.get("total_volume")
        vol_line = _fmt_volume_usd(tvol) if tvol is not None else "—"
        mv = monthly_volumes_from_chart_json((chart_by_coin or {}).get(cg_id), last_n=12)
        vol_series = mv[0] if mv else None
        month_labels = mv[1] if mv else None
        vol_svg = (
            svg_volume_bars(
                vol_series,
                "#2b8a3e" if "BTC" in label else ("#5c7cfa" if "ETH" in label else "#9c36b5"),
                "近12个月月度成交额（柱高∝量）",
                month_labels,
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
            <p class="chart-hint-mini">柱图为 CoinGecko 月度汇总（过去12个月），与交易所逐笔可能有差异。</p>
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
    window_days: Optional[int] = 30,
) -> Optional[List[float]]:
    if chart_json is None:
        chart_json = fetch_market_chart_json(session, coin_id)
    daily = all_daily_closes_from_chart_json(chart_json)
    if not daily:
        return None
    if window_days and len(daily) > window_days:
        return daily[-window_days:]
    return daily


def _fmt_usd_price(p: float) -> str:
    """表格与坐标轴上的美元标价。"""
    ap = abs(p)
    # 需求：币价去掉小数点
    if ap >= 1:
        return f"${p:,.0f}"
    return f"${p:.0f}"


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
    closes_all: Optional[List[float]] = None,
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

    # --- 投委会“定量”增强：置信区间 + 近段回测误差 ---
    # 默认采用残差正态近似的启发式区间：point ± z * sigma * sqrt(1 + horizon/train_len)
    z = 1.96  # 约 95% 两侧区间
    sigma_lin = _linear_residuals_std(closes_usd)
    sigma_holt = _holt_one_step_residuals_std(closes_usd)
    ci_lin = _interval_heuristic(
        point=last_lin, sigma=sigma_lin, horizon=30, train_len=n, z=z
    )
    ci_holt = _interval_heuristic(
        point=last_holt, sigma=sigma_holt, horizon=30, train_len=n, z=z
    )

    mae_lin_7 = _backtest_mae_linear(closes_usd, horizon=7, min_train=10)
    mae_lin_14 = _backtest_mae_linear(closes_usd, horizon=14, min_train=10)
    mae_holt_7 = _backtest_mae_holt(closes_usd, horizon=7, min_train=10)
    mae_holt_14 = _backtest_mae_holt(closes_usd, horizon=14, min_train=10)

    ci_lin_str = f"{html.escape(_fmt_usd_price(ci_lin[0]))} ~ {html.escape(_fmt_usd_price(ci_lin[1]))}"
    ci_holt_str = f"{html.escape(_fmt_usd_price(ci_holt[0]))} ~ {html.escape(_fmt_usd_price(ci_holt[1]))}"

    def _fmt_mae(v: Optional[float]) -> str:
        return html.escape(_fmt_usd_price(v)) if v is not None else "—"

    bt_lin = f"{_fmt_mae(mae_lin_7)} / {_fmt_mae(mae_lin_14)}"
    bt_holt = f"{_fmt_mae(mae_holt_7)} / {_fmt_mae(mae_holt_14)}"

    # 多窗口投委会定量表：30/60/90（若样本不足则自动跳过）
    win_rows: List[str] = []
    signal_tags: List[str] = []
    base_all = closes_all or closes_usd
    for w in (30, 60, 90):
        if len(base_all) < w:
            continue
        ww = base_all[-w:]
        w_n = len(ww)
        w_xs = [float(i) for i in range(w_n)]
        w_slope, w_intercept = _linreg(w_xs, ww)
        w_fore30 = w_slope * float(w_n + 29) + w_intercept
        w_last = float(ww[-1])
        w_delta_pct = (w_fore30 / w_last - 1.0) if w_last else 0.0
        w_sigma = _linear_residuals_std(ww)
        w_ci = _interval_heuristic(
            point=w_fore30, sigma=w_sigma, horizon=30, train_len=w_n, z=1.96
        )
        w_mae7 = _backtest_mae_linear(ww, horizon=7, min_train=10)

        if w_delta_pct > 0.01:
            w_signal = "偏多"
        elif w_delta_pct < -0.01:
            w_signal = "偏空"
        else:
            w_signal = "中性"
        signal_tags.append(w_signal)

        win_rows.append(
            f"<tr><td>{w}日</td>"
            f"<td class='num'>{html.escape(_fmt_usd_price(w_fore30))}</td>"
            f"<td class='num'>{html.escape(_fmt_usd_price(w_ci[0]))} ~ {html.escape(_fmt_usd_price(w_ci[1]))}</td>"
            f"<td class='num'>{_fmt_mae(w_mae7)}</td>"
            f"<td class='num'>{w_delta_pct*100:+.2f}%（{w_signal}）</td></tr>"
        )

    consensus = "—"
    if signal_tags:
        non_neutral = [s for s in signal_tags if s != "中性"]
        if non_neutral and len(set(non_neutral)) == 1:
            consensus = f"同向（{non_neutral[0]}）"
        elif not non_neutral:
            consensus = "同向（中性）"
        else:
            consensus = "信号分歧"

    window_table_html = ""
    if win_rows:
        window_table_html = f"""
  <table class="forecast-window-table">
    <thead>
      <tr>
        <th>窗口</th>
        <th class="num">线性 Day+30</th>
        <th class="num">95%区间</th>
        <th class="num">MAE(+7d)</th>
        <th class="num">相对收盘</th>
      </tr>
    </thead>
    <tbody>
      {''.join(win_rows)}
    </tbody>
  </table>
  <p class="forecast-window-note">多窗口一致性：{html.escape(consensus)}</p>"""

    # 需求：币价去掉小数点（百分比仍保留两位小数更直观）
    cell_lin = f"${d_lin:+,.0f}（{p_lin:+.2f}%）"
    cell_holt = f"${d_holt:+,.0f}（{p_holt:+.2f}%）"
    svg = _single_asset_forecast_svg(title, color, closes_usd, fore_lin, fore_holt)
    return f"""
<div class="forecast-card">
  {svg}
  <table class="forecast-price-table">
    <tr><td>最近一日收盘（USD）</td><td class="num">{html.escape(_fmt_usd_price(last_hist))}</td></tr>
    <tr><td>外推第30日 · 线性回归</td><td class="num">{html.escape(_fmt_usd_price(last_lin))}</td></tr>
    <tr><td>外推第30日 · 线性回归（95%区间）</td><td class="num">{ci_lin_str}</td></tr>
    <tr><td>外推第30日 · Holt 双指数平滑</td><td class="num">{html.escape(_fmt_usd_price(last_holt))}</td></tr>
    <tr><td>外推第30日 · Holt 双指数平滑（95%区间）</td><td class="num">{ci_holt_str}</td></tr>
    <tr><td>线性回归相对收盘</td><td class="num">{html.escape(cell_lin)}</td></tr>
    <tr><td>Holt 相对收盘</td><td class="num">{html.escape(cell_holt)}</td></tr>
    <tr><td>回测误差（MAE +7d/+14d）· 线性回归</td><td class="num">{bt_lin}</td></tr>
    <tr><td>回测误差（MAE +7d/+14d）· Holt</td><td class="num">{bt_holt}</td></tr>
  </table>
  {window_table_html}
</div>"""


def html_forecast_trend_chart(
    session: requests.Session,
    chart_by_coin: Optional[Dict[str, Optional[dict]]] = None,
) -> str:
    """
    两币种各一张图：近若干日 USD 收盘 + 线性外推 30 日，纵轴与表格为具体美元价（示意）。
    """
    assets = [
        ("bitcoin", "BTC / Bitcoin", "#f7931a"),
        ("ethereum", "ETH / Ethereum", "#627eea"),
    ]
    cards: List[str] = []
    for cid, title, color in assets:
        j = (chart_by_coin or {}).get(cid)
        closes_all = _fetch_daily_closes(session, cid, j, window_days=None)
        closes = _fetch_daily_closes(session, cid, j, window_days=30)
        if not closes:
            cards.append(
                f'<div class="forecast-card forecast-card-miss"><p class="muted">无法获取 {html.escape(title)} 行情数据。</p></div>'
            )
            continue
        cards.append(_single_asset_forecast_card(title, color, closes, closes_all))

    inner = f'<div class="forecast-three">{"".join(cards)}</div>'

    return f"""<section class="forecast-section">
<h2 class="section-title">未来约30日 · 分币种外推示意（美元标价，非投资建议）</h2>
<div class="forecast-note-box">
<p class="forecast-legal"><strong>预测样本窗口：</strong>当前预测使用各币<strong>过去30日（约1个月）日收盘价</strong>作为输入数据，再向后外推30日。<br><strong>方法：</strong>（1）<strong>一元线性回归</strong>：假设价格沿直线漂移，简单但易在拐点处失真。（2）<strong>Holt 双指数平滑</strong>：同时估计「水平」与「趋势」，对近期变化更敏感，通常比纯直线更贴近短序列，但仍<strong>无法</strong>刻画暴涨暴跌与黑天鹅。
说明：本区间（95%）为残差标准差驱动的正态近似启发式区间，未保证真实覆盖率；回测误差为近段 walk-forward MAE（+7d/+14d），用来衡量短期拟合稳定性，但仍无法覆盖结构性变化。纵轴与表格为模型输出，非实时盘口。**更优方法**（需更多数据与算力）包括：ARIMA/Prophet、状态空间模型、GARCH 类波动率模型、变点检测与机器学习；加密市场非平稳，任何外推均可能失败。</p>
</div>
{inner}
<p class="muted small">数据来源：CoinGecko <code>market_chart</code> · 与本项目作者/模型无法律上的保证关系</p>
</section>"""


def build_quant_summary_html(chart_by_coin: Dict[str, Optional[dict]]) -> str:
    """投委会风格定量摘要：近1d/7d/30d收益、30d波动、30d最大回撤。"""
    coin_defs = [
        ("bitcoin", "BTC"),
        ("ethereum", "ETH"),
    ]

    def _fmt_pct(p: Optional[float]) -> str:
        if p is None:
            return "—"
        return f"{p*100:+.2f}%"

    def _max_drawdown_pct(closes: List[float]) -> Optional[float]:
        if not closes:
            return None
        peak = closes[0]
        max_dd = 0.0
        for v in closes:
            if v > peak:
                peak = v
            if peak > 0:
                dd = v / peak - 1.0
                max_dd = min(max_dd, dd)
        return max_dd  # 为负数

    rows: List[str] = []
    for cid, short in coin_defs:
        closes = daily_closes_from_chart_json(chart_by_coin.get(cid))
        if not closes or len(closes) < 3:
            continue

        last = float(closes[-1])
        ret_1d = (closes[-1] / closes[-2] - 1.0) if len(closes) >= 2 else None
        ret_7d = (closes[-1] / closes[-8] - 1.0) if len(closes) >= 8 else None
        ret_30d = (closes[-1] / closes[0] - 1.0) if len(closes) >= 1 else None

        daily_returns = [
            (closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))
        ]
        recent = daily_returns[-30:]
        vol_ann = _sample_std(recent) * (365.0**0.5) if len(recent) >= 2 else None
        max_dd = _max_drawdown_pct(closes)

        rows.append(
            f"""
<tr>
  <td>{html.escape(short)}</td>
  <td class="num">{html.escape(_fmt_usd_price(last))}</td>
  <td class="num">{_fmt_pct(ret_1d)}</td>
  <td class="num">{_fmt_pct(ret_7d)}</td>
  <td class="num">{_fmt_pct(ret_30d)}</td>
  <td class="num">{_fmt_pct(vol_ann)}</td>
  <td class="num">{_fmt_pct(max_dd)}</td>
</tr>"""
        )

    if not rows:
        return ""

    return f"""
<section class="quant-section">
  <h2 class="section-title">Quant Summary（定量）</h2>
  <div class="quant-box">
    <table class="quant-table">
      <thead>
        <tr>
          <th>资产</th>
          <th class="num">最新</th>
          <th class="num">1D</th>
          <th class="num">7D</th>
          <th class="num">30D</th>
          <th class="num">Vol(年化)</th>
          <th class="num">MaxDD(30D)</th>
        </tr>
      </thead>
      <tbody>
        {"".join(rows)}
      </tbody>
    </table>
    <p class="quant-note">说明：口径为 CoinGecko UTC 日度收盘价；Vol 为 30d realized vol 年化，MaxDD 为样本内峰值回撤。</p>
  </div>
</section>
""".strip()


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
                f"<li><strong>{name}</strong>：${usd:,.0f}（24h {chs}）</li>"
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


def build_ai_summary_section_html(session: requests.Session, today_news: List[Dict], market_snapshot: Optional[Dict]) -> str:
    """
    AI 投研简评区块：
    - 如果 GEMINI_API_KEY 已设置，则使用 Gemini 生成。
    - 否则，回退到基于规则的“本地智能简评”。
    """
    summary_content = ""
    is_ai = False
    gdata = ((market_snapshot or {}).get("global") or {}).get("data") or {}
    total_mcap_usd = (gdata.get("total_market_cap") or {}).get("usd")
    mcap_change = gdata.get("market_cap_change_percentage_24h_usd")
    mcap_change = float(mcap_change) if mcap_change is not None else 0.0
    mcap_text = f"${float(total_mcap_usd)/1e12:.2f}T" if total_mcap_usd else "N/A"

    if GEMINI_API_KEY:
        try:
            import google.generativeai as genai  # lazy import: no dependency crash without API usage

            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel(AI_MODEL_NAME)
            
            # 准备 AI Prompt
            news_titles = [n['title'] for n in today_news[:15]]
            market_info = ""
            if market_snapshot:
                market_info = f"当前总市值: {mcap_text}, 24h涨跌: {mcap_change:+.2f}%"
            
            prompt = f"""
            你是一名资深的 Web3 投研专家。请根据以下今日新闻标题和市场行情，生成一段约 300 字的“投研日报总结”。
            要求：
            1. 语气专业、客观、有前瞻性。
            2. 分为“核心动态”、“市场情绪”和“投研建议”三个小段落。
            3. 使用 HTML 格式输出（仅限 <p> 标签）。
            
            今日新闻：
            {chr(10).join(news_titles)}
            
            市场行情：
            {market_info}
            """
            
            response = model.generate_content(prompt)
            if response and response.text:
                summary_content = response.text.strip()
                is_ai = True
                logger.info("AI summary generated successfully via Gemini.")
        except Exception as e:
            logger.warning("Gemini AI failed: %s. Falling back to local rules.", e)

    if not summary_content:
        # 本地规则回退逻辑
        logger.info("Generating local smart summary...")
        sentiment = "中性偏稳"
        
        if mcap_change > 2: sentiment = "看涨情绪浓厚"
        elif mcap_change > 0.5: sentiment = "情绪温和修复"
        elif mcap_change < -2: sentiment = "市场恐慌抛售"
        elif mcap_change < -0.5: sentiment = "情绪持续承压"
        
        summary_content = f"""
        <p><strong>核心动态：</strong>今日 Web3 市场共捕获 {len(today_news)} 条有效资讯，涵盖监管、技术及生态多个维度。从新闻密度来看，行业正处于关键的技术更迭期。</p>
        <p><strong>市场情绪：</strong>基于 24h 总市值波动（{mcap_change}%），当前市场呈现「{sentiment}」特征。资金流向显示，头部资产仍具备较强的吸血效应，山寨币表现分化。</p>
        <p><strong>投研建议：</strong>在当前波动率环境下，建议保持审慎观望，重点关注宏观经济日历中的关键时点（如 FOMC 决议）。短期内应避开高杠杆博弈，聚焦具备基本面支撑的 Layer 2 及基础设施赛道。</p>
        """

    badge_html = '<span class="ai-badge">AI 深度分析</span>' if is_ai else '<span class="ai-badge local">智能规则简评</span>'
    
    return f"""
<section class="ai-summary-section">
    <div class="ai-card">
        <div class="ai-header">
            {badge_html}
            <span class="ai-title">今日投研简报总结</span>
        </div>
        <div class="ai-content">
            {summary_content}
        </div>
    </div>
</section>
"""


def build_web3_events_section_html(today: datetime.date, events: List[Dict[str, Any]]) -> str:
    """
    Web3 行业活动区块：
    - 全年峰会概览
    - 最近一场活动的详细介绍
    """
    if not events:
        return '<section class="events-section"><div class="no-news">暂无活动数据。</div></section>'

    def d(s: str) -> datetime.date:
        return datetime.datetime.strptime(s, "%Y-%m-%d").date()

    # 预处理日期并排序
    processed_events = []
    for e in events:
        try:
            item = e.copy()
            item["_sd"] = d(e["start"])
            item["_ed"] = d(e["end"])
            processed_events.append(item)
        except Exception as err:
            logger.warning("Invalid event date format: %s", err)
            continue

    if not processed_events:
        return '<section class="events-section"><div class="no-news">暂无有效活动数据。</div></section>'

    events_sorted = sorted(processed_events, key=lambda x: x["_sd"])
    latest = min(
        (e for e in events_sorted if e["_sd"] >= today),
        key=lambda x: x["_sd"],
        default=events_sorted[0] if events_sorted else None,
    )

    if not latest:
        return '<section class="events-section"><div class="no-news">暂无活动数据。</div></section>'

    def fmt_range(start_d: datetime.date, end_d: datetime.date) -> str:
        if start_d == end_d:
            return start_d.isoformat()
        return f"{start_d.isoformat()} ~ {end_d.isoformat()}"

    overview_items = []
    for e in events_sorted:
        overview_items.append(
            f"<li><strong>{html.escape(e['name'])}</strong>：{html.escape(fmt_range(e['_sd'], e['_ed']))} · {html.escape(e['location'])} · <a class='events-link' href='{html.escape(e['url'])}' target='_blank' rel='noopener'>官网 ↗</a></li>"
        )

    latest_highlights = "".join(
        f"<li>{html.escape(x)}</li>" for x in latest.get("highlights") or []
    )

    overview_html = (
        "<ul class='events-list'>"
        + "".join(overview_items)
        + "</ul>"
    )

    latest_html = f"""
<h3>最新活动：{html.escape(latest['name'])}</h3>
<div class="events-meta">{html.escape(fmt_range(latest['_sd'], latest['_ed']))} · {html.escape(latest['location'])}</div>
<div class="events-kv">
  <a class="events-link" href="{html.escape(latest['url'])}" target="_blank" rel="noopener">查看官网 ↗</a>
</div>
<ul class="events-list">{latest_highlights}</ul>
"""

    return f"""
<section class="events-section">
  <h2 class="section-title">新增活动 · Web3 峰会速览</h2>
  <div class="events-grid">
    <div class="events-card">
      <h3>全年峰会活动概览（2026）</h3>
      {overview_html}
    </div>
    <div class="events-card events-latest">
      {latest_html}
    </div>
  </div>
</section>
""".strip()


def build_key_people_section_html(today_news: List[Dict]) -> str:
    """
    大佬动态：
    - 以“今日抓取到的新闻摘要”为数据源
    - 按大佬名字/别名/关键词做匹配，展示每位大佬今日最相关的一条
    """

    people = [
        {
            "name": "赵长鹏（CZ）",
            "role": "Binance 联合创始人（曾任 CEO）",
            "keywords": [
                "cz",
                "changpeng",
                "changpeng zhao",
                "zhao changpeng",
                "赵长鹏",
                "binance",
            ],
        },
        {
            "name": "孙宇晨（Justin Sun）",
            "role": "TRON（波场）创始人",
            "keywords": [
                "justin sun",
                "sun yuchen",
                "孙宇晨",
                "tron",
                "波场",
            ],
        },
        {
            "name": "Vitalik Buterin",
            "role": "以太坊联合创始人",
            "keywords": ["vitalik", "buterin", "ethere"],
        },
        {
            "name": "Charles Hoskinson",
            "role": "Cardano / IOHK 创始人",
            "keywords": ["charles hoskinson", "hoskinson", "cardano"],
        },
        {
            "name": "Do Kwon",
            "role": "Terraform Labs（Terra/LUNA）联合创始人",
            "keywords": ["do kwon", "kwon", "terra", "luna"],
        },
        {
            "name": "Anatoly Yakovenko",
            "role": "Solana 联合创始人",
            "keywords": ["anatoly", "yakovenko", "solana"],
        },
        {
            "name": "Brian Armstrong",
            "role": "Coinbase 联合创始人兼 CEO",
            "keywords": ["brian armstrong", "armstrong", "coinbase"],
        },
        {
            "name": "Sam Bankman-Fried（SBF）",
            "role": "FTX / Alameda Research 创始人",
            "keywords": ["sam bankman-fried", "sbf", "bankman-fried", "ftx"],
        },
        {
            "name": "Elizabeth Stark",
            "role": "Lightning Labs 联合创始人兼 CEO",
            "keywords": ["elizabeth stark", "stark", "lightning labs", "lightning"],
        },
        {
            "name": "Gavin Wood",
            "role": "Polkadot / Parity / Web3 基金会联合创始人之一",
            "keywords": ["gavin wood", "wood", "polkadot", "parity"],
        },
        {
            "name": "Donald Trump（特朗普）",
            "role": "美国前总统（加密与监管议题常涉公开表态）",
            "keywords": [
                "trump",
                "donald",
                "realdonaldtrump",
                "bitcoin",
                "crypto",
                "cryptocurrency",
                "ethereum",
                "web3",
            ],
        },
        {
            "name": "Elon Musk（马斯克）",
            "role": "Tesla / SpaceX / X（原推特）等 CEO",
            "keywords": [
                "elon",
                "musk",
                "elonmusk",
                "tesla",
                "spacex",
                "xai",
                "bitcoin",
                "crypto",
                "dogecoin",
                "web3",
            ],
        },
        {
            "name": "Michael Saylor（迈克尔·塞勒）",
            "role": "Strategy（原 MicroStrategy）执行董事长",
            "keywords": [
                "michael saylor",
                "saylor",
                "microstrategy",
                "bitcoin",
                "btc",
                "crypto",
            ],
        },
        {
            "name": "Cathie Wood（凯西·伍德）",
            "role": "ARK Invest 创始人兼 CIO",
            "keywords": [
                "cathie wood",
                "cathiedwood",
                "ark invest",
                "ark",
                "bitcoin",
                "crypto",
            ],
        },
        {
            "name": "Jeremy Allaire（Circle）",
            "role": "Circle 联合创始人兼 CEO",
            "keywords": [
                "jeremy allaire",
                "jerallaire",
                "circle",
                "usdc",
                "stablecoin",
                "crypto",
                "web3",
            ],
        },
    ]

    def match_item(item: Dict, person: Dict[str, Any]) -> bool:
        hay = (item.get("title") or "") + " " + (item.get("summary") or "")
        hay = str(hay).lower()
        for k in person["keywords"]:
            if str(k).lower() in hay:
                return True
        return False

    def to_time_str(published: str) -> str:
        dt = parse_published(published)
        return dt.strftime("%Y-%m-%d %H:%M") if dt else published

    def llm_style_summary_100(item: Dict) -> str:
        """
        轻量“模型整理感”摘要：优先用标题+摘要拼接，去噪后压缩到 100 字以内。
        （本地规则生成，不依赖外部模型服务）
        """
        title = str(item.get("title") or "").strip()
        body = summary_to_plain_text(item.get("summary") or "")
        raw = f"{title}。{body}" if body else title
        raw = " ".join(raw.split())
        raw = raw.replace("\n", " ").replace("\r", " ").strip(" .;；，,")
        if len(raw) <= 100:
            return raw
        # 优先在常见停顿符号处截断，避免生硬截字
        for sep in ("。", "；", ";", "，", ",", " "):
            idx = raw.find(sep, 40, 100)
            if idx != -1:
                cut = raw[:idx].strip()
                if len(cut) >= 28:
                    return cut + "…"
        return raw[:99].rstrip() + "…"

    def person_header_html(p: Dict[str, Any]) -> str:
        role = html.escape(str(p.get("role") or "").strip())
        role_line = (
            f'<div class="whales-role">{role}</div>' if role else ""
        )
        return (
            f'<div class="whales-person">{html.escape(p["name"])}</div>\n  {role_line}'
        )

    cards: List[str] = []
    for person in people:
        latest = next((it for it in today_news if match_item(it, person)), None)
        if not latest:
            cards.append(
                f"""
<div class="whales-card">
  {person_header_html(person)}
  <div class="whales-empty">今日暂无匹配动态。</div>
</div>"""
            )
            continue

        summary_plain = llm_style_summary_100(latest)

        time_str = to_time_str(latest.get("published", ""))
        cards.append(
            f"""
<div class="whales-card">
  {person_header_html(person)}
  <div class="whales-latest">
    <strong>最新：</strong>{html.escape(latest.get('title',''))}
  </div>
  <div class="whales-meta">📅 {html.escape(time_str)} · 来源：{html.escape(latest.get('source',''))} · <a class="whales-link" href="{html.escape(latest.get('link',''))}" target="_blank" rel="noopener">链接</a></div>
  <div class="whales-snippet">{html.escape(summary_plain)}</div>
</div>"""
        )

    return """
<section class="whales-section">
  <h2 class="section-title">大佬动态（今日相关）</h2>
  <div class="whales-grid">
    {cards}
  </div>
  <p class="whales-note muted small">说明：本区块基于今日 RSS 新闻标题/摘要关键词匹配，展示“相关新闻中的大佬相关动态”，非官方社媒抓取。</p>
</section>
""".format(cards="".join(cards)).strip()

def fetch_fear_and_greed_index(session: requests.Session) -> Optional[Dict[str, Any]]:
    """获取恐惧与贪婪指数。"""
    try:
        r = session.get("https://api.alternative.me/fng/", timeout=15)
        r.raise_for_status()
        data = r.json()
        if data and "data" in data and len(data["data"]) > 0:
            return data["data"][0]
    except Exception as e:
        logger.warning("Fear & Greed Index failed: %s", e)
    return None


def build_macro_section_html(today: datetime.date, macro_events: List[Dict[str, Any]]) -> str:
    """构建宏观经济日历区块。"""
    if not macro_events:
        return '<section class="macro-section"><div class="no-news">暂无宏观数据。</div></section>'

    def d(s: str) -> datetime.date:
        return datetime.datetime.strptime(s, "%Y-%m-%d").date()

    processed = []
    for e in macro_events:
        try:
            item = e.copy()
            item["_date"] = d(e["date"])
            processed.append(item)
        except Exception as err:
            logger.warning("Invalid macro date format: %s", err)
            continue

    if not processed:
        return '<section class="macro-section"><div class="no-news">暂无有效宏观数据。</div></section>'

    # 按日期排序
    sorted_macro = sorted(processed, key=lambda x: x["_date"])
    # 筛选即将到来的或最近的 5 条
    upcoming = [e for e in sorted_macro if e["_date"] >= today][:5]
    
    rows = []
    for e in upcoming:
        impact_class = f"impact-{e.get('impact', 'Medium').lower()}"
        rows.append(f"""
        <tr>
            <td>{html.escape(e['date'])}</td>
            <td><span class="macro-type">{html.escape(e.get('type', 'Other'))}</span></td>
            <td><strong>{html.escape(e['name'])}</strong></td>
            <td><span class="impact-tag {impact_class}">{html.escape(e.get('impact', 'Medium'))}</span></td>
            <td class="macro-desc">{html.escape(e.get('description', ''))}</td>
        </tr>""")

    return f"""
<section class="macro-section">
  <h2 class="section-title">宏观预警 · 关键经济日历</h2>
  <div class="macro-card">
    <table class="macro-table">
      <thead>
        <tr>
          <th>日期</th>
          <th>类型</th>
          <th>事件</th>
          <th>重要性</th>
          <th>影响说明</th>
        </tr>
      </thead>
      <tbody>
        {"".join(rows) if rows else '<tr><td colspan="5" class="muted">近期无重大宏观事件。</td></tr>'}
      </tbody>
    </table>
  </div>
</section>"""


def build_sentiment_section_html(fng: Optional[Dict[str, Any]]) -> str:
    """构建市场情绪（恐惧与贪婪）区块。"""
    if not fng:
        return ""
    
    val = int(fng.get("value", 50))
    label = fng.get("value_classification", "Neutral")
    
    # 根据数值决定颜色
    color = "#e74c3c" # 红色 (Fear)
    if val >= 75: color = "#27ae60" # 深绿 (Extreme Greed)
    elif val >= 55: color = "#2ecc71" # 浅绿 (Greed)
    elif val >= 45: color = "#f1c40f" # 黄色 (Neutral)
    elif val >= 25: color = "#e67e22" # 橙色 (Fear)

    return f"""
<div class="sentiment-box">
  <div class="sentiment-title">市场情绪指数 (Fear & Greed)</div>
  <div class="sentiment-value" style="color: {color};">{val}</div>
  <div class="sentiment-label" style="background: {color};">{html.escape(label)}</div>
  <div class="sentiment-meter">
    <div class="sentiment-bar" style="width: {val}%; background: {color};"></div>
  </div>
</div>"""


class CryptoNewsCrawler:
    def __init__(self, data_file: str = DATA_FILE, events_file: str = EVENTS_FILE, macro_file: str = MACRO_FILE, report_file: str = REPORT_FILE):
        self.data_file = data_file
        self.events_file = events_file
        self.macro_file = macro_file
        self.report_file = report_file
        self.session = get_session()
        self.news_data = self._load_from_disk()
        self.events_data = self._load_events()
        self.macro_data = self._load_macro()

    def _load_from_disk(self) -> List[Dict]:
        """从磁盘加载现有新闻数据。"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error("Failed to load data file: %s", e)
        return []

    def _load_events(self) -> List[Dict]:
        """从磁盘加载 Web3 活动数据。"""
        if os.path.exists(self.events_file):
            try:
                with open(self.events_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info("Loaded %d events from %s", len(data), self.events_file)
                    return data
            except (json.JSONDecodeError, IOError) as e:
                logger.error("Failed to load events file: %s", e)
        return []

    def _load_macro(self) -> List[Dict]:
        """从磁盘加载宏观经济日历。"""
        if os.path.exists(self.macro_file):
            try:
                with open(self.macro_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info("Loaded %d macro events from %s", len(data), self.macro_file)
                    return data
            except (json.JSONDecodeError, IOError) as e:
                logger.error("Failed to load macro file: %s", e)
        return []

    def fetch_rss_source(self, source: Dict) -> List[Dict]:
        """抓取单个 RSS 源；支持 urls 列表依次重试。"""
        logger.info("Fetching %s...", source["name"])
        feed_urls = source.get("urls") or [source["url"]]
        last_err: Optional[Exception] = None

        for feed_url in feed_urls:
            try:
                merged = {**dict(self.session.headers), **rss_request_headers(feed_url)}
                response = self.session.get(feed_url, timeout=15, headers=merged)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, "lxml-xml")
                items = soup.find_all("item")

                new_articles = []
                for item in items:
                    link = item.link.text.strip() if item.link else ""
                    if not link:
                        continue

                    title = item.title.text.strip() if item.title else "无标题"
                    pub_date = item.pubDate.text.strip() if item.pubDate else ""
                    # 部分 RSS（如 Twitter 经第三方聚合）不一定携带 pubDate；回填抓取时间保证能进入“今日”分组
                    if not pub_date:
                        pub_date = datetime.datetime.now().isoformat()

                    raw_desc = item.description.text if item.description else ""
                    desc_soup = BeautifulSoup(raw_desc, "html.parser")
                    clean_desc = desc_soup.get_text(separator=" ", strip=True).replace(
                        "\xa0", " "
                    )
                    summary = (
                        clean_desc[:250] + "..."
                        if len(clean_desc) > 250
                        else clean_desc
                    )

                    new_articles.append(
                        {
                            "source": source["name"],
                            "title": title,
                            "link": link,
                            "published": pub_date,
                            "summary": summary,
                            "fetched_at": datetime.datetime.now().isoformat(),
                        }
                    )
                if feed_url != feed_urls[0]:
                    logger.info(
                        "Used fallback RSS URL for %s: %s",
                        source["name"],
                        feed_url,
                    )
                return new_articles
            except Exception as e:
                last_err = e
                logger.warning(
                    "RSS attempt failed for %s (%s): %s",
                    source["name"],
                    feed_url,
                    e,
                )

        logger.error("Fetch failed for %s: %s", source["name"], last_err)
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
        for _cid in ("bitcoin", "ethereum"):
            chart_by_coin[_cid] = fetch_market_chart_json(self.session, _cid)
        quant_block = build_quant_summary_html(chart_by_coin)
        charts_block = html_tradingview_charts(
            self.session, markets_24h, chart_by_coin
        )
        forecast_block = html_forecast_trend_chart(self.session, chart_by_coin)
        trend_block = build_trend_analysis_html(market_snapshot)
        events_block = build_web3_events_section_html(today, self.events_data)
        whales_block = build_key_people_section_html(today_news)
        
        # AI 摘要简评
        ai_summary_block = build_ai_summary_section_html(self.session, today_news, market_snapshot)
        
        # 获取情绪指数与宏观日历
        fng = fetch_fear_and_greed_index(self.session)
        sentiment_block = build_sentiment_section_html(fng)
        macro_block = build_macro_section_html(today, self.macro_data)

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
            /* 现在只保留 BTC/ETH 两张图，保持两列更紧凑 */
            .chart-row {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.35rem; }}
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
            .forecast-three {{ grid-template-columns: 1fr 1fr; }}
        }}
        .forecast-card {{ background: var(--card-bg); border-radius: 12px; padding: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .forecast-card-miss {{ padding: 1rem; }}
        .forecast-svg-single {{ width: 100%; height: auto; max-height: 320px; display: block; }}
        .forecast-price-table {{ width: 100%; border-collapse: collapse; font-size: 0.86rem; margin-top: 10px; }}
        .forecast-price-table td {{ padding: 7px 4px; border-top: 1px solid rgba(128,128,128,0.22); vertical-align: top; }}
        .forecast-price-table tr:first-child td {{ border-top: none; }}
        .forecast-price-table td:first-child {{ color: var(--muted); padding-right: 8px; }}
        .forecast-price-table .num {{ text-align: right; font-weight: 600; white-space: nowrap; }}
        .forecast-window-table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; margin-top: 10px; }}
        .forecast-window-table th {{ text-align: left; color: var(--muted); font-weight: 600; padding: 6px 4px; border-bottom: 1px solid rgba(128,128,128,0.18); }}
        .forecast-window-table th.num {{ text-align: right; white-space: nowrap; }}
        .forecast-window-table td {{ padding: 6px 4px; border-bottom: 1px solid rgba(128,128,128,0.12); }}
        .forecast-window-table td.num {{ text-align: right; white-space: nowrap; font-weight: 600; }}
        .forecast-window-note {{ margin: 6px 0 0; font-size: 0.78rem; color: var(--muted); }}
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
        .events-section {{ margin-bottom: 2rem; }}
        .events-grid {{ display: grid; grid-template-columns: 1fr; gap: 1.25rem; }}
        @media (min-width: 920px) {{
            .events-grid {{ grid-template-columns: 1.15fr 0.85fr; }}
        }}
        .events-card {{ background: var(--card-bg); border-radius: 12px; padding: 16px 18px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid rgba(128,128,128,0.12); }}
        .events-card h3 {{ margin-top: 0; font-size: 1.05rem; }}
        .events-meta {{ color: var(--muted); font-size: 0.9rem; margin-top: 6px; line-height: 1.35; }}
        .events-kv {{ margin: 8px 0 0; }}
        .events-link {{ color: var(--accent); text-decoration: none; }}
        .events-link:hover {{ text-decoration: underline; }}
        .events-list {{ margin: 10px 0 0; padding-left: 1.2rem; font-size: 0.95rem; }}
        .events-list li {{ margin-bottom: 0.5rem; }}
        .quant-section {{ margin-bottom: 2rem; }}
        .quant-box {{ background: var(--card-bg); border-radius: 14px; padding: 14px 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); border: 1px solid rgba(128,128,128,0.12); }}
        .quant-table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
        .quant-table th {{ text-align: left; color: var(--muted); font-weight: 600; padding: 8px 8px; border-bottom: 1px solid rgba(128,128,128,0.15); }}
        .quant-table th.num {{ text-align: right; white-space: nowrap; }}
        .quant-table td {{ padding: 8px 8px; border-bottom: 1px solid rgba(128,128,128,0.12); vertical-align: top; }}
        .quant-table td.num {{ text-align: right; font-weight: 600; white-space: nowrap; }}
        .quant-note {{ margin: 8px 0 0; font-size: 0.78rem; color: var(--muted); }}
        .whales-section {{ margin-bottom: 2rem; }}
        .whales-grid {{ display: grid; grid-template-columns: 1fr; gap: 1rem; }}
        @media (min-width: 720px) {{
            .whales-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
        }}
        @media (min-width: 1100px) {{
            .whales-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
        }}
        .whales-card {{ background: var(--card-bg); border-radius: 12px; padding: 14px 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); border: 1px solid rgba(128,128,128,0.12); }}
        .whales-person {{ font-weight: 650; margin-bottom: 2px; }}
        .whales-role {{ font-size: 0.82rem; color: var(--muted); line-height: 1.35; margin-bottom: 8px; }}
        .whales-latest {{ font-size: 0.95rem; }}
        .whales-link {{ color: var(--accent); text-decoration: none; }}
        .whales-link:hover {{ text-decoration: underline; }}
        .whales-meta {{ color: var(--muted); font-size: 0.85rem; margin-top: 6px; }}
        .whales-snippet {{ color: var(--muted); font-size: 0.9rem; margin-top: 8px; line-height: 1.4; }}
        .whales-note {{ margin-top: 10px; }}
        .sentiment-box {{ background: var(--card-bg); border-radius: 14px; padding: 15px; box-shadow: 0 4px 15px rgba(0,0,0,0.08); display: flex; flex-direction: column; align-items: center; text-align: center; margin: 0 auto 2.5rem; max-width: 300px; border: 1px solid rgba(128,128,128,0.1); }}
        .sentiment-title {{ font-size: 0.9rem; font-weight: 600; color: var(--muted); margin-bottom: 5px; }}
        .sentiment-value {{ font-size: 2.2rem; font-weight: 800; line-height: 1; margin: 5px 0; }}
        .sentiment-label {{ font-size: 0.75rem; font-weight: 700; color: white; padding: 2px 10px; border-radius: 20px; text-transform: uppercase; margin-bottom: 12px; }}
        .sentiment-meter {{ width: 100%; height: 6px; background: rgba(0,0,0,0.05); border-radius: 10px; overflow: hidden; }}
        .sentiment-bar {{ height: 100%; transition: width 0.5s ease-out; }}
        .macro-section {{ margin-bottom: 2.5rem; }}
        .macro-card {{ background: var(--card-bg); border-radius: 14px; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,0.06); border: 1px solid rgba(128,128,128,0.15); }}
        .macro-table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 0.9rem; }}
        .macro-table th {{ background: rgba(0,0,0,0.02); padding: 12px 15px; font-weight: 600; color: var(--muted); border-bottom: 1px solid rgba(128,128,128,0.15); }}
        .macro-table td {{ padding: 12px 15px; border-bottom: 1px solid rgba(128,128,128,0.1); }}
        .macro-table tr:last-child td {{ border-bottom: none; }}
        .macro-type {{ font-size: 0.75rem; color: var(--accent); background: rgba(0,123,255,0.1); padding: 2px 6px; border-radius: 4px; font-weight: 500; }}
        .impact-tag {{ font-size: 0.7rem; font-weight: 700; padding: 2px 6px; border-radius: 4px; text-transform: uppercase; color: white; }}
        .impact-high {{ background: #e74c3c; }}
        .impact-medium {{ background: #f39c12; }}
        .impact-low {{ background: #95a5a6; }}
        .macro-desc {{ color: var(--muted); font-size: 0.85rem; line-height: 1.4; }}
        .ai-summary-section {{ margin-bottom: 2.5rem; }}
        .ai-card {{ background: linear-gradient(135deg, rgba(0,123,255,0.05) 0%, rgba(0,123,255,0.01) 100%); border: 1px solid rgba(0,123,255,0.2); border-radius: 16px; padding: 20px; box-shadow: 0 4px 12px rgba(0,123,255,0.05); }}
        .ai-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 15px; }}
        .ai-badge {{ background: var(--accent); color: white; padding: 3px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; }}
        .ai-badge.local {{ background: #6c757d; }}
        .ai-title {{ font-size: 1.1rem; font-weight: 700; color: var(--text); }}
        .ai-content {{ font-size: 0.95rem; line-height: 1.7; color: var(--text); opacity: 0.9; }}
        .ai-content p {{ margin: 0 0 12px; }}
        .ai-content p:last-child {{ margin-bottom: 0; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🚀 Web3 资讯简报</h1>
            <div class="stats">{today.isoformat()} · 今日更新 {len(today_news)} 条 · 总计存储 {len(self.news_data)} 条</div>
        </header>

        {ai_summary_block}
        {quant_block}
        {sentiment_block}
        {charts_block}
        {forecast_block}
        {trend_block}
        {macro_block}
        {events_block}
        {whales_block}

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
