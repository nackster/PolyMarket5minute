"""Fetch historical BTC trade data from Binance for backtesting.

Uses Binance's public REST API (no API key required for market data):
- /api/v3/aggTrades: Aggregated trades (best for tick-level simulation)
- /api/v3/klines: Candlestick data (for quick OHLCV backtests)

Data is cached locally in the data/ directory to avoid re-fetching.
"""

import json
import os
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import structlog

log = structlog.get_logger()

BASE_URL = "https://api.binance.com"
DATA_DIR = Path(__file__).parent.parent / "data"


def _get_session() -> requests.Session:
    """Create a requests session with automatic retry on transient errors."""
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_session = _get_session()


def fetch_agg_trades(
    symbol: str = "BTCUSDT",
    start_time: int = None,
    end_time: int = None,
    limit: int = 1000,
) -> list[dict]:
    """Fetch aggregated trades from Binance.

    Each trade has: agg_trade_id, price, quantity, first_trade_id,
    last_trade_id, timestamp, is_buyer_maker.

    Args:
        symbol: Trading pair
        start_time: Start time in milliseconds
        end_time: End time in milliseconds
        limit: Max trades per request (max 1000)
    """
    params = {"symbol": symbol, "limit": limit}
    if start_time:
        params["startTime"] = start_time
    if end_time:
        params["endTime"] = end_time

    resp = _session.get(f"{BASE_URL}/api/v3/aggTrades", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_klines(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    start_time: int = None,
    end_time: int = None,
    limit: int = 1000,
) -> list[list]:
    """Fetch candlestick data from Binance.

    Returns: [[open_time, open, high, low, close, volume, close_time, ...], ...]
    """
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_time:
        params["startTime"] = start_time
    if end_time:
        params["endTime"] = end_time

    resp = _session.get(f"{BASE_URL}/api/v3/klines", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_trade_history(
    hours: int = 24,
    symbol: str = "BTCUSDT",
    cache: bool = True,
) -> list[dict]:
    """Fetch N hours of aggregated trade data, with local caching.

    Returns a list of trade dicts with keys:
    - price (float)
    - qty (float)
    - timestamp (float, seconds)
    - is_buyer_maker (bool)

    This fetches in chunks since Binance limits to 1000 trades per request.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (hours * 3600 * 1000)

    # Round to hour boundaries for better cache reuse
    start_hour = start_ms // 3600000 * 3600000
    end_hour = end_ms // 3600000 * 3600000
    cache_file = DATA_DIR / f"trades_{symbol}_{hours}h_{start_hour}.json"
    if cache and cache_file.exists():
        log.info("loading_cached_trades", file=str(cache_file))
        with open(cache_file) as f:
            return json.load(f)

    log.info("fetching_trade_history", hours=hours, symbol=symbol)

    all_trades = []
    current_start = start_ms
    request_count = 0

    consecutive_errors = 0
    while current_start < end_ms:
        try:
            raw = fetch_agg_trades(
                symbol=symbol,
                start_time=current_start,
                end_time=end_ms,
                limit=1000,
            )
            if not raw:
                break

            for t in raw:
                all_trades.append({
                    "price": float(t["p"]),
                    "qty": float(t["q"]),
                    "timestamp": t["T"] / 1000.0,
                    "is_buyer_maker": t["m"],
                })

            # Move start forward past the last trade
            current_start = raw[-1]["T"] + 1
            request_count += 1
            consecutive_errors = 0

            if request_count % 10 == 0:
                log.info("fetch_progress", trades=len(all_trades), requests=request_count)

            # Binance rate limit: be conservative - sleep every request
            time.sleep(0.3)
            # Extra pause every 50 requests to avoid SSL drops
            if request_count % 50 == 0:
                log.info("cooling_down", requests=request_count)
                time.sleep(3)

        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
            consecutive_errors += 1
            wait = min(60, 5 * consecutive_errors)
            log.warning("connection_error_retrying", error=str(e)[:100], wait=wait, attempts=consecutive_errors)
            if consecutive_errors >= 5:
                log.warning("too_many_errors_saving_partial", trades=len(all_trades))
                break
            # Reset the session to get a fresh connection
            global _session
            _session = _get_session()
            time.sleep(wait)

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                log.warning("rate_limited_waiting", wait=30)
                time.sleep(30)
            else:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    log.warning("too_many_http_errors_saving_partial", trades=len(all_trades))
                    break
                time.sleep(5)

    log.info("trades_fetched", total=len(all_trades), requests=request_count)

    if cache and all_trades:
        with open(cache_file, "w") as f:
            json.dump(all_trades, f)
        log.info("trades_cached", file=str(cache_file))

    return all_trades


def klines_to_trades(klines: list[dict]) -> list[dict]:
    """Convert OHLCV klines into synthetic tick-level trade data.

    Each 1-min kline produces 4 synthetic ticks tracing the OHLC path.
    Taker buy volume is used to correctly assign aggressive buy/sell pressure
    so CVD, book imbalance and tape signals remain accurate.

    OHLC path heuristic:
      Bullish candle (close >= open): O -> L -> H -> C  (sellers first, then buyers win)
      Bearish candle (close <  open): O -> H -> L -> C  (buyers first, then sellers win)
    """
    trades = []
    for k in klines:
        o, h, l, c = k["open"], k["high"], k["low"], k["close"]
        t0 = k["open_time"]
        total_vol = k["volume"]
        buy_vol = k["taker_buy_volume"]
        sell_vol = total_vol - buy_vol

        is_bullish = c >= o
        if is_bullish:
            ohlc_path = [o, l, h, c]  # dip first then rally
        else:
            ohlc_path = [o, h, l, c]  # spike first then sell-off

        # 4 ticks spaced evenly across the minute
        for i, px in enumerate(ohlc_path):
            ts = t0 + i * 15  # 0s, 15s, 30s, 45s into the candle
            # First two ticks carry the countertrend pressure, last two follow trend
            if i < 2:
                qty = (sell_vol if is_bullish else buy_vol) / 2
                is_buyer_maker = is_bullish  # bearish tick = buyer is market maker
            else:
                qty = (buy_vol if is_bullish else sell_vol) / 2
                is_buyer_maker = not is_bullish
            trades.append({
                "price": px,
                "qty": max(qty, 0.001),
                "timestamp": ts,
                "is_buyer_maker": is_buyer_maker,
            })

    return trades


def fetch_kline_history(
    hours: int = 24,
    interval: str = "1m",
    symbol: str = "BTCUSDT",
    cache: bool = True,
) -> list[dict]:
    """Fetch N hours of candlestick data, with local caching.

    Returns list of dicts with: open_time, open, high, low, close,
    volume, close_time, quote_volume, trades, taker_buy_volume.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (hours * 3600 * 1000)

    cache_file = DATA_DIR / f"klines_{symbol}_{interval}_{start_ms}_{end_ms}.json"
    if cache and cache_file.exists():
        log.info("loading_cached_klines", file=str(cache_file))
        with open(cache_file) as f:
            return json.load(f)

    log.info("fetching_kline_history", hours=hours, interval=interval)

    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        raw = fetch_klines(
            symbol=symbol,
            interval=interval,
            start_time=current_start,
            end_time=end_ms,
            limit=1000,
        )
        if not raw:
            break

        for k in raw:
            all_klines.append({
                "open_time": k[0] / 1000.0,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": k[6] / 1000.0,
                "quote_volume": float(k[7]),
                "trades": int(k[8]),
                "taker_buy_volume": float(k[9]),
            })

        current_start = int(raw[-1][6]) + 1
        time.sleep(0.2)

    log.info("klines_fetched", total=len(all_klines))

    if cache and all_klines:
        with open(cache_file, "w") as f:
            json.dump(all_klines, f)

    return all_klines
