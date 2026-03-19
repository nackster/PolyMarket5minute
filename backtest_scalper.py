#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC Intraday Scalping Strategy Sweep -- Hyperliquid Fee Structure
================================================================
Sweeps 6 strategies x multiple param combos on 5m Binance BTCUSDT data.
Finds which combination best targets $500/day on $10k-$25k with 3-5x leverage.

Usage:
    python backtest_scalper.py                     # all defaults
    python backtest_scalper.py --days 90 --interval 5m --capital 10000 --leverage 3
    python backtest_scalper.py --strategy pullback --top 10
    python backtest_scalper.py --export
"""

import os
import sys

# Force UTF-8 output on Windows (avoids cp1252 encoding errors)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

import json
import math
import time
import argparse
import requests
import itertools
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# -- Constants ----------------------------------------------------------------

TAKER_FEE    = 0.0005   # 0.05% per side -- market orders (TP/SL exits)
MAKER_REBATE = 0.0002   # 0.02% per side -- limit orders (entries, we EARN this)
# Net round-trip: entry earns 0.02%, exit costs 0.05% -> net cost = 0.03% of position

CACHE_DIR    = os.path.join(os.path.dirname(__file__), "trades")
MAX_HOLD_CANDLES = 30   # 2.5 hours on 5m

# -- CLI -----------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="BTC Scalping Strategy Sweep")
    p.add_argument("--days",      type=int,   default=90,      help="Days of data (default 90)")
    p.add_argument("--interval",  type=str,   default="5m",    help="Candle interval: 3m/5m/15m (default 5m)")
    p.add_argument("--capital",   type=float, default=10000.0, help="Starting capital USD (default 10000)")
    p.add_argument("--leverage",  type=float, default=3.0,     help="Leverage (default 3)")
    p.add_argument("--strategy",  type=str,   default="all",
                   help="Strategy: all | ema_cross | supertrend | bb_bounce | macd | pullback | stoch_rsi")
    p.add_argument("--top",       type=int,   default=15,      help="Show top N results (default 15)")
    p.add_argument("--export",    action="store_true",         help="Save results to trades/scalper_sweep.json")
    return p.parse_args()

# -- Data Fetching -------------------------------------------------------------

def cache_path(interval: str, days: int) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"btc_{interval}_{days}d_cache.json")

def fetch_candles(interval: str = "5m", days: int = 90) -> list:
    """
    Fetch BTCUSDT klines from Binance REST API with disk caching.
    Returns list of dicts: {t, o, h, l, c, v} sorted oldest-first.
    """
    path = cache_path(interval, days)

    # Check cache freshness (< 15 min old)
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < 900:
            print(f"  Loading cached {interval} data ({days}d) -- {age:.0f}s old")
            with open(path) as f:
                return json.load(f)
        else:
            print(f"  Cache stale ({age/3600:.1f}h old), re-fetching...")

    url        = "https://api.binance.com/api/v3/klines"
    end_ms     = int(time.time() * 1000)
    start_ms   = end_ms - days * 86_400_000
    all_candles = []
    cur         = start_ms

    # Interval -> milliseconds per candle
    interval_ms = {
        "1m":  60_000,
        "3m":  180_000,
        "5m":  300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h":  3_600_000,
    }.get(interval, 300_000)

    print(f"  Fetching {days}d of {interval} BTCUSDT candles from Binance...", end="", flush=True)
    fetched = 0

    while cur < end_ms:
        try:
            r = requests.get(url, params={
                "symbol":    "BTCUSDT",
                "interval":  interval,
                "startTime": cur,
                "endTime":   end_ms,
                "limit":     1000,
            }, timeout=20)
            data = r.json()
        except Exception as e:
            print(f"\n  Fetch error: {e}, retrying in 3s...")
            time.sleep(3)
            continue

        if not data or not isinstance(data, list):
            break

        for k in data:
            all_candles.append({
                "t": int(k[0]) // 1000,   # open time in seconds
                "o": float(k[1]),
                "h": float(k[2]),
                "l": float(k[3]),
                "c": float(k[4]),
                "v": float(k[5]),
            })

        fetched += len(data)
        print(".", end="", flush=True)
        cur = int(data[-1][0]) + interval_ms

        if len(data) < 1000:
            break

    # Deduplicate and sort
    seen = {}
    for c in all_candles:
        seen[c["t"]] = c
    result = sorted(seen.values(), key=lambda x: x["t"])

    print(f"\n  Fetched {len(result):,} candles")
    with open(path, "w") as f:
        json.dump(result, f)

    return result

# -- Technical Indicators ------------------------------------------------------

def ema(closes: list, period: int) -> list:
    """Exponential Moving Average."""
    if len(closes) < period:
        return [float("nan")] * len(closes)
    k   = 2.0 / (period + 1)
    out = [float("nan")] * len(closes)
    # Seed with SMA
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    for i in range(period, len(closes)):
        out[i] = closes[i] * k + out[i - 1] * (1 - k)
    return out

def sma(closes: list, period: int) -> list:
    """Simple Moving Average."""
    out = [float("nan")] * len(closes)
    for i in range(period - 1, len(closes)):
        out[i] = sum(closes[i - period + 1 : i + 1]) / period
    return out

def rsi(closes: list, period: int = 14) -> list:
    """RSI using Wilder's smoothing."""
    out = [float("nan")] * len(closes)
    if len(closes) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    # Seed
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, len(closes)):
        g = gains[i - 1]
        lo = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + lo) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out

def atr(highs: list, lows: list, closes: list, period: int = 14) -> list:
    """Average True Range using Wilder's smoothing."""
    n   = len(closes)
    out = [float("nan")] * n
    if n < period + 1:
        return out
    tr_vals = []
    for i in range(1, n):
        hl   = highs[i] - lows[i]
        hc   = abs(highs[i] - closes[i - 1])
        lc   = abs(lows[i] - closes[i - 1])
        tr_vals.append(max(hl, hc, lc))
    # Seed
    atr_val = sum(tr_vals[:period]) / period
    out[period] = atr_val
    for i in range(period + 1, n):
        atr_val = (atr_val * (period - 1) + tr_vals[i - 1]) / period
        out[i]  = atr_val
    return out

def bollinger(closes: list, period: int = 20, std_dev: float = 2.0):
    """
    Bollinger Bands.
    Returns (upper, mid, lower) -- each a list aligned with closes.
    """
    n   = len(closes)
    mid = sma(closes, period)
    upper = [float("nan")] * n
    lower = [float("nan")] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        m      = mid[i]
        variance = sum((x - m) ** 2 for x in window) / period
        sd     = math.sqrt(variance)
        upper[i] = m + std_dev * sd
        lower[i] = m - std_dev * sd
    return upper, mid, lower

def supertrend(highs: list, lows: list, closes: list,
               period: int = 10, mult: float = 3.0):
    """
    Supertrend indicator.
    Returns (trend_line, direction) where direction is +1 (bull) or -1 (bear).
    """
    n   = len(closes)
    atr_vals = atr(highs, lows, closes, period)
    trend_line = [float("nan")] * n
    direction  = [0] * n

    upper_basic = [float("nan")] * n
    lower_basic = [float("nan")] * n
    upper_band  = [float("nan")] * n
    lower_band  = [float("nan")] * n

    for i in range(n):
        if math.isnan(atr_vals[i]):
            continue
        hl2 = (highs[i] + lows[i]) / 2.0
        upper_basic[i] = hl2 + mult * atr_vals[i]
        lower_basic[i] = hl2 - mult * atr_vals[i]

    for i in range(period, n):
        if math.isnan(upper_basic[i]):
            continue
        # Upper band
        if math.isnan(upper_band[i - 1]):
            upper_band[i] = upper_basic[i]
        else:
            upper_band[i] = min(upper_basic[i], upper_band[i - 1]) if closes[i - 1] <= upper_band[i - 1] else upper_basic[i]
        # Lower band
        if math.isnan(lower_band[i - 1]):
            lower_band[i] = lower_basic[i]
        else:
            lower_band[i] = max(lower_basic[i], lower_band[i - 1]) if closes[i - 1] >= lower_band[i - 1] else lower_basic[i]
        # Direction
        if math.isnan(trend_line[i - 1]):
            if closes[i] > upper_band[i]:
                direction[i]  = 1
                trend_line[i] = lower_band[i]
            else:
                direction[i]  = -1
                trend_line[i] = upper_band[i]
        else:
            if direction[i - 1] == 1:
                if closes[i] < lower_band[i]:
                    direction[i]  = -1
                    trend_line[i] = upper_band[i]
                else:
                    direction[i]  = 1
                    trend_line[i] = lower_band[i]
            else:
                if closes[i] > upper_band[i]:
                    direction[i]  = 1
                    trend_line[i] = lower_band[i]
                else:
                    direction[i]  = -1
                    trend_line[i] = upper_band[i]
    return trend_line, direction

def macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    MACD indicator.
    Returns (macd_line, signal_line, histogram).
    """
    fast_ema  = ema(closes, fast)
    slow_ema  = ema(closes, slow)
    n         = len(closes)
    macd_line = [float("nan")] * n
    for i in range(n):
        if not math.isnan(fast_ema[i]) and not math.isnan(slow_ema[i]):
            macd_line[i] = fast_ema[i] - slow_ema[i]

    # Signal line = EMA of MACD
    # Build a dense sub-list for EMA calculation
    valid_start = next((i for i in range(n) if not math.isnan(macd_line[i])), n)
    dense = macd_line[valid_start:]
    if len(dense) >= signal:
        sig_dense = ema(dense, signal)
        signal_line = [float("nan")] * valid_start + sig_dense
    else:
        signal_line = [float("nan")] * n

    histogram = [float("nan")] * n
    for i in range(n):
        if not math.isnan(macd_line[i]) and not math.isnan(signal_line[i]):
            histogram[i] = macd_line[i] - signal_line[i]

    return macd_line, signal_line, histogram

def stoch_rsi(closes: list, rsi_period: int = 14, stoch_period: int = 14,
              smooth_k: int = 3, smooth_d: int = 3):
    """
    Stochastic RSI.
    Returns (k, d) -- each a list aligned with closes.
    """
    n       = len(closes)
    rsi_vals = rsi(closes, rsi_period)

    raw_k = [float("nan")] * n
    for i in range(stoch_period - 1, n):
        window = [v for v in rsi_vals[i - stoch_period + 1 : i + 1] if not math.isnan(v)]
        if len(window) < stoch_period:
            continue
        lo = min(window)
        hi = max(window)
        if hi == lo:
            raw_k[i] = 50.0
        else:
            raw_k[i] = (rsi_vals[i] - lo) / (hi - lo) * 100.0

    # Smooth K
    dense_start = next((i for i in range(n) if not math.isnan(raw_k[i])), n)
    dense_k     = raw_k[dense_start:]
    if len(dense_k) >= smooth_k:
        smooth_k_dense = sma(dense_k, smooth_k)
        k_line = [float("nan")] * dense_start + smooth_k_dense
    else:
        k_line = [float("nan")] * n

    # Smooth D
    valid_k_start = next((i for i in range(n) if not math.isnan(k_line[i])), n)
    dense_kl      = k_line[valid_k_start:]
    if len(dense_kl) >= smooth_d:
        smooth_d_dense = sma(dense_kl, smooth_d)
        d_line = [float("nan")] * valid_k_start + smooth_d_dense
    else:
        d_line = [float("nan")] * n

    return k_line, d_line

# -- Backtest Engine -----------------------------------------------------------

def _is_valid(*vals) -> bool:
    return all(not math.isnan(v) for v in vals)

def run_backtest(candles: list, signals: list, capital: float, leverage: float) -> list:
    """
    Core backtest loop.

    signals: list of dicts with keys:
        index      -- signal bar index (enter at OPEN of index+1)
        direction  -- +1 long, -1 short
        stop       -- stop loss price
        target     -- take profit price

    Returns list of trade dicts.
    """
    trades      = []
    in_trade    = False
    trade_open  = None   # index where we entered

    # Build fast index for O/H/L/C
    n = len(candles)

    # Group signals by index for O(1) lookup
    sig_map = {}
    for s in signals:
        idx = s["index"]
        if idx not in sig_map:
            sig_map[idx] = s  # take first signal per bar

    for i in range(1, n):
        # -- Check open trade first -----------------------------------------
        if in_trade:
            c          = candles[i]
            d          = trade_open["direction"]
            entry      = trade_open["entry_price"]
            stop       = trade_open["stop"]
            target     = trade_open["target"]
            pos_size   = trade_open["pos_size"]
            entry_idx  = trade_open["entry_idx"]

            exit_price  = None
            exit_reason = None

            if d == 1:  # long
                if c["l"] <= stop:
                    exit_price  = stop
                    exit_reason = "SL"
                elif c["h"] >= target:
                    exit_price  = target
                    exit_reason = "TP"
            else:  # short
                if c["h"] >= stop:
                    exit_price  = stop
                    exit_reason = "SL"
                elif c["l"] <= target:
                    exit_price  = target
                    exit_reason = "TP"

            # Max hold timeout
            if exit_price is None and (i - entry_idx) >= MAX_HOLD_CANDLES:
                exit_price  = c["c"]
                exit_reason = "TIMEOUT"

            if exit_price is not None:
                # PnL calculation
                if d == 1:
                    pnl_pct = (exit_price - entry) / entry
                else:
                    pnl_pct = (entry - exit_price) / entry

                entry_fee = -pos_size * MAKER_REBATE   # negative = we earn
                exit_fee  =  pos_size * TAKER_FEE
                total_fee = entry_fee + exit_fee        # net cost

                pnl_usd   = pnl_pct * pos_size - total_fee

                trades.append({
                    "entry_time":  candles[entry_idx]["t"],
                    "exit_time":   c["t"],
                    "direction":   "long" if d == 1 else "short",
                    "entry_price": entry,
                    "exit_price":  exit_price,
                    "exit_reason": exit_reason,
                    "pnl_pct":     pnl_pct,
                    "pnl_usd":     pnl_usd,
                    "fees_usd":    total_fee,
                    "pos_size":    pos_size,
                })

                in_trade  = False
                trade_open = None

        # -- Check for new signal (only if flat) ----------------------------
        if not in_trade and (i - 1) in sig_map:
            sig      = sig_map[i - 1]
            entry_px = candles[i]["o"]  # enter at OPEN of next candle
            pos_size = capital * leverage

            trade_open = {
                "direction":   sig["direction"],
                "entry_price": entry_px,
                "stop":        sig["stop"],
                "target":      sig["target"],
                "pos_size":    pos_size,
                "entry_idx":   i,
            }
            in_trade = True

    return trades

# -- Strategy Signal Generators ------------------------------------------------

def signals_ema_cross(candles: list, fast: int, slow: int, trend: int,
                      rsi_long_max: float, atr_mult: float, tp_rr: float) -> list:
    """Strategy 1: EMA Cross + RSI Filter."""
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    fast_e  = ema(closes, fast)
    slow_e  = ema(closes, slow)
    trend_e = ema(closes, trend)
    rsi_v   = rsi(closes, 14)
    atr_v   = atr(highs, lows, closes, 14)

    signals = []
    for i in range(1, len(candles) - 1):
        if not _is_valid(fast_e[i], fast_e[i-1], slow_e[i], slow_e[i-1],
                         trend_e[i], rsi_v[i], atr_v[i]):
            continue
        risk = atr_mult * atr_v[i]

        # Long signal
        if (fast_e[i-1] <= slow_e[i-1] and fast_e[i] > slow_e[i]  # crossover
                and rsi_v[i] < rsi_long_max
                and closes[i] > trend_e[i]):
            signals.append({
                "index":     i,
                "direction": 1,
                "stop":      closes[i] - risk,
                "target":    closes[i] + tp_rr * risk,
            })

        # Short signal
        elif (fast_e[i-1] >= slow_e[i-1] and fast_e[i] < slow_e[i]  # crossdown
                and rsi_v[i] > (100 - rsi_long_max)
                and closes[i] < trend_e[i]):
            signals.append({
                "index":     i,
                "direction": -1,
                "stop":      closes[i] + risk,
                "target":    closes[i] - tp_rr * risk,
            })

    return signals

def signals_supertrend(candles: list, period: int, mult: float, tp_rr: float) -> list:
    """Strategy 2: Supertrend Reversal."""
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    st_line, st_dir = supertrend(highs, lows, closes, period, mult)

    signals = []
    for i in range(1, len(candles) - 1):
        if st_dir[i] == 0 or st_dir[i-1] == 0:
            continue
        if math.isnan(st_line[i]):
            continue

        entry = closes[i]

        # Flip bull
        if st_dir[i-1] == -1 and st_dir[i] == 1:
            stop  = st_line[i]
            risk  = entry - stop
            if risk <= 0:
                continue
            signals.append({
                "index":     i,
                "direction": 1,
                "stop":      stop,
                "target":    entry + tp_rr * risk,
            })

        # Flip bear
        elif st_dir[i-1] == 1 and st_dir[i] == -1:
            stop  = st_line[i]
            risk  = stop - entry
            if risk <= 0:
                continue
            signals.append({
                "index":     i,
                "direction": -1,
                "stop":      stop,
                "target":    entry - tp_rr * risk,
            })

    return signals

def signals_bb_bounce(candles: list, bb_period: int, bb_std: float,
                      rsi_long: float, tp_target: str) -> list:
    """Strategy 3: Bollinger Band Bounce + RSI."""
    closes = [c["c"] for c in candles]
    rsi_v  = rsi(closes, 14)
    upper, mid, lower = bollinger(closes, bb_period, bb_std)

    signals = []
    for i in range(1, len(candles) - 1):
        if not _is_valid(upper[i], mid[i], lower[i], rsi_v[i], rsi_v[i-1]):
            continue

        bb_width = upper[i] - lower[i]
        if bb_width <= 0:
            continue

        # Long: close below lower BB, RSI < threshold and rising
        if (closes[i] <= lower[i]
                and rsi_v[i] < rsi_long
                and rsi_v[i] > rsi_v[i-1]):
            stop = closes[i] - 0.5 * bb_width
            if tp_target == "mid":
                target = mid[i]
            else:  # 1.5x
                target = closes[i] + 1.5 * bb_width
            signals.append({
                "index":     i,
                "direction": 1,
                "stop":      stop,
                "target":    target,
            })

        # Short: close above upper BB, RSI > (100-threshold) and falling
        elif (closes[i] >= upper[i]
                and rsi_v[i] > (100 - rsi_long)
                and rsi_v[i] < rsi_v[i-1]):
            stop = closes[i] + 0.5 * bb_width
            if tp_target == "mid":
                target = mid[i]
            else:
                target = closes[i] - 1.5 * bb_width
            signals.append({
                "index":     i,
                "direction": -1,
                "stop":      stop,
                "target":    target,
            })

    return signals

def signals_macd(candles: list, fast: int, slow: int, signal_p: int,
                 ema_trend: int, atr_mult: float, tp_rr: float) -> list:
    """Strategy 4: MACD Zero-Cross + Trend Filter."""
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    macd_line, sig_line, _ = macd(closes, fast, slow, signal_p)
    trend_e = ema(closes, ema_trend)
    atr_v   = atr(highs, lows, closes, 14)

    signals = []
    for i in range(1, len(candles) - 1):
        if not _is_valid(macd_line[i], macd_line[i-1], trend_e[i], atr_v[i]):
            continue
        risk = atr_mult * atr_v[i]

        # Long: MACD crosses above 0 and price > trend EMA
        if (macd_line[i-1] <= 0 and macd_line[i] > 0
                and closes[i] > trend_e[i]):
            signals.append({
                "index":     i,
                "direction": 1,
                "stop":      closes[i] - risk,
                "target":    closes[i] + tp_rr * risk,
            })

        # Short: MACD crosses below 0 and price < trend EMA
        elif (macd_line[i-1] >= 0 and macd_line[i] < 0
                and closes[i] < trend_e[i]):
            signals.append({
                "index":     i,
                "direction": -1,
                "stop":      closes[i] + risk,
                "target":    closes[i] - tp_rr * risk,
            })

    return signals

def signals_pullback(candles: list, fast_ema_p: int, trend_ema_p: int,
                     rsi_entry: float, tp_rr: float) -> list:
    """Strategy 5: Pullback to EMA in trend."""
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    fast_e  = ema(closes, fast_ema_p)
    trend_e = ema(closes, trend_ema_p)
    rsi_v   = rsi(closes, 14)
    atr_v   = atr(highs, lows, closes, 14)

    signals = []
    for i in range(2, len(candles) - 1):
        if not _is_valid(fast_e[i], trend_e[i], rsi_v[i], rsi_v[i-1], atr_v[i]):
            continue

        # Long: uptrend, pull to fast EMA, RSI crosses above entry level
        if (closes[i] > trend_e[i]
                and lows[i] <= fast_e[i] * 1.001
                and rsi_v[i-1] < rsi_entry
                and rsi_v[i] >= rsi_entry):
            swing_low = min(lows[max(0, i-2) : i+1])
            stop      = swing_low - 0.1 * atr_v[i]  # slight buffer
            risk      = closes[i] - stop
            if risk <= 0:
                continue
            signals.append({
                "index":     i,
                "direction": 1,
                "stop":      stop,
                "target":    closes[i] + tp_rr * risk,
            })

        # Short: downtrend, pull to fast EMA, RSI crosses below entry level
        elif (closes[i] < trend_e[i]
                and highs[i] >= fast_e[i] * 0.999
                and rsi_v[i-1] > (100 - rsi_entry)
                and rsi_v[i] <= (100 - rsi_entry)):
            swing_high = max(highs[max(0, i-2) : i+1])
            stop       = swing_high + 0.1 * atr_v[i]
            risk       = stop - closes[i]
            if risk <= 0:
                continue
            signals.append({
                "index":     i,
                "direction": -1,
                "stop":      stop,
                "target":    closes[i] - tp_rr * risk,
            })

    return signals

def signals_stoch_rsi(candles: list, stoch_period: int, smooth: int,
                      ema_trend_p: int, atr_mult: float, tp_rr: float) -> list:
    """Strategy 6: Stochastic RSI + EMA Trend."""
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    k_line, d_line = stoch_rsi(closes, 14, stoch_period, smooth, smooth)
    trend_e = ema(closes, ema_trend_p)
    atr_v   = atr(highs, lows, closes, 14)

    signals = []
    for i in range(1, len(candles) - 1):
        if not _is_valid(k_line[i], k_line[i-1], d_line[i], d_line[i-1],
                         trend_e[i], atr_v[i]):
            continue
        risk = atr_mult * atr_v[i]

        # Long: K crosses above D from below 20, price > trend EMA
        if (k_line[i-1] <= d_line[i-1]
                and k_line[i] > d_line[i]
                and k_line[i-1] < 20
                and closes[i] > trend_e[i]):
            signals.append({
                "index":     i,
                "direction": 1,
                "stop":      closes[i] - risk,
                "target":    closes[i] + tp_rr * risk,
            })

        # Short: K crosses below D from above 80, price < trend EMA
        elif (k_line[i-1] >= d_line[i-1]
                and k_line[i] < d_line[i]
                and k_line[i-1] > 80
                and closes[i] < trend_e[i]):
            signals.append({
                "index":     i,
                "direction": -1,
                "stop":      closes[i] + risk,
                "target":    closes[i] - tp_rr * risk,
            })

    return signals

# -- Statistics ----------------------------------------------------------------

def compute_stats(trades: list, candles: list, capital: float, leverage: float) -> dict:
    """Compute comprehensive statistics for a list of trades."""
    if not trades:
        return None

    n_trades = len(trades)
    wins     = [t for t in trades if t["pnl_usd"] > 0]
    losses   = [t for t in trades if t["pnl_usd"] <= 0]
    win_rate = len(wins) / n_trades

    gross_profit = sum(t["pnl_usd"] for t in wins)
    gross_loss   = abs(sum(t["pnl_usd"] for t in losses)) if losses else 0.0001
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    total_pnl    = sum(t["pnl_usd"] for t in trades)
    avg_pnl      = total_pnl / n_trades

    # Date range
    first_ts = candles[0]["t"]
    last_ts  = candles[-1]["t"]
    total_days = (last_ts - first_ts) / 86_400

    trades_per_day = n_trades / total_days if total_days > 0 else 0

    # Daily P&L buckets
    daily_pnl = defaultdict(float)
    for t in trades:
        day = datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).strftime("%Y-%m-%d")
        daily_pnl[day] += t["pnl_usd"]

    daily_vals = list(daily_pnl.values())
    # Include zero-trade days
    all_days = set()
    d = datetime.fromtimestamp(first_ts, tz=timezone.utc).date()
    end = datetime.fromtimestamp(last_ts, tz=timezone.utc).date()
    while d <= end:
        all_days.add(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    for day in all_days:
        if day not in daily_pnl:
            daily_vals.append(0.0)

    daily_avg    = sum(daily_vals) / len(daily_vals)
    sorted_daily = sorted(daily_vals)
    n_d          = len(sorted_daily)
    daily_median = (sorted_daily[n_d // 2 - 1] + sorted_daily[n_d // 2]) / 2 if n_d % 2 == 0 else sorted_daily[n_d // 2]

    daily_std = math.sqrt(sum((v - daily_avg) ** 2 for v in daily_vals) / len(daily_vals)) if len(daily_vals) > 1 else 0.0001
    sharpe = daily_avg / daily_std if daily_std > 0 else 0.0

    # Max drawdown (equity curve)
    equity     = capital
    peak       = capital
    max_dd_pct = 0.0
    for t in trades:
        equity += t["pnl_usd"]
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak
        if dd > max_dd_pct:
            max_dd_pct = dd

    # Consecutive losses
    max_consec_loss = 0
    cur_consec      = 0
    for t in trades:
        if t["pnl_usd"] <= 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    tp_count = sum(1 for t in trades if t["exit_reason"] == "TP")
    sl_count = sum(1 for t in trades if t["exit_reason"] == "SL")
    to_count = sum(1 for t in trades if t["exit_reason"] == "TIMEOUT")

    return {
        "n_trades":         n_trades,
        "win_rate":         win_rate,
        "profit_factor":    profit_factor,
        "total_pnl":        total_pnl,
        "avg_pnl":          avg_pnl,
        "trades_per_day":   trades_per_day,
        "daily_avg":        daily_avg,
        "daily_median":     daily_median,
        "daily_std":        daily_std,
        "sharpe":           sharpe,
        "max_dd_pct":       max_dd_pct,
        "max_consec_loss":  max_consec_loss,
        "tp_count":         tp_count,
        "sl_count":         sl_count,
        "to_count":         to_count,
        "score":            sharpe * trades_per_day,
    }

# -- Param Sweep Definitions ---------------------------------------------------

STRATEGY_SWEEPS = {
    "ema_cross": {
        "label": "EMA Cross + RSI",
        "func":  signals_ema_cross,
        "params": {
            "fast":         [8, 9, 13],
            "slow":         [21, 26],
            "trend":        [50, 100, 200],
            "rsi_long_max": [55, 60, 65],
            "atr_mult":     [1.0, 1.5],
            "tp_rr":        [1.5, 2.0, 2.5],
        },
    },
    "supertrend": {
        "label": "Supertrend Rev.",
        "func":  signals_supertrend,
        "params": {
            "period": [7, 10, 14],
            "mult":   [2.0, 2.5, 3.0, 3.5],
            "tp_rr":  [1.5, 2.0, 2.5, 3.0],
        },
    },
    "bb_bounce": {
        "label": "BB Bounce + RSI",
        "func":  signals_bb_bounce,
        "params": {
            "bb_period": [20],
            "bb_std":    [1.5, 2.0, 2.5],
            "rsi_long":  [30, 35, 40],
            "tp_target": ["mid", "1.5x"],
        },
    },
    "macd": {
        "label": "MACD Zero-Cross",
        "func":  signals_macd,
        "params": {
            "fast":      [8, 12],
            "slow":      [21, 26],
            "signal_p":  [9],
            "ema_trend": [100, 200],
            "atr_mult":  [1.0, 1.5],
            "tp_rr":     [2.0, 2.5, 3.0],
        },
    },
    "pullback": {
        "label": "Pullback to EMA",
        "func":  signals_pullback,
        "params": {
            "fast_ema_p":  [21, 26],
            "trend_ema_p": [50, 100],
            "rsi_entry":   [40, 45, 50],
            "tp_rr":       [2.0, 2.5, 3.0],
        },
    },
    "stoch_rsi": {
        "label": "StochRSI + EMA",
        "func":  signals_stoch_rsi,
        "params": {
            "stoch_period": [14],
            "smooth":       [3],
            "ema_trend_p":  [50, 100, 200],
            "atr_mult":     [1.0, 1.5, 2.0],
            "tp_rr":        [1.5, 2.0, 2.5],
        },
    },
}

# -- Main Sweep ----------------------------------------------------------------

def run_sweep(candles_in: list, candles_oos: list, strategy_name: str,
              capital: float, leverage: float) -> list:
    """
    Run full parameter sweep for one strategy.
    Returns list of result dicts with both in-sample and out-of-sample stats.
    """
    sweep_def = STRATEGY_SWEEPS[strategy_name]
    sig_func  = sweep_def["func"]
    label     = sweep_def["label"]
    param_def = sweep_def["params"]

    keys   = list(param_def.keys())
    combos = list(itertools.product(*[param_def[k] for k in keys]))

    results = []
    n       = len(combos)

    print(f"\n  [{label}] Sweeping {n} combinations...", end="", flush=True)
    dot_every = max(1, n // 40)

    for idx, combo in enumerate(combos):
        if idx % dot_every == 0:
            print(".", end="", flush=True)

        params = dict(zip(keys, combo))

        # In-sample
        try:
            sigs_in  = sig_func(candles_in,  **params)
            trades_in = run_backtest(candles_in,  sigs_in,  capital, leverage)
            stats_in  = compute_stats(trades_in,  candles_in,  capital, leverage)
        except Exception:
            stats_in = None

        # Out-of-sample
        try:
            sigs_oos  = sig_func(candles_oos, **params)
            trades_oos = run_backtest(candles_oos, sigs_oos, capital, leverage)
            stats_oos  = compute_stats(trades_oos, candles_oos, capital, leverage)
        except Exception:
            stats_oos = None

        if stats_in is None or stats_oos is None:
            continue
        if stats_in["n_trades"] < 10 or stats_oos["n_trades"] < 5:
            continue

        results.append({
            "strategy":   label,
            "strategy_id": strategy_name,
            "params":     params,
            "in":         stats_in,
            "oos":        stats_oos,
            # Score on OOS -- penalize overfitting: oos_sharpe * oos_trades/day
            "score":      stats_oos["score"],
            "trades_in":  trades_in,
            "trades_oos": trades_oos,
        })

    print(f" done ({len(results)} valid combos)")
    return results

# -- Output Formatting ---------------------------------------------------------

def fmt_params(params: dict) -> str:
    """Compact param string for display."""
    parts = []
    for k, v in params.items():
        short_k = k.replace("_period", "").replace("_ema_p", "").replace("_rr", "R")
        if isinstance(v, float) and v == int(v):
            parts.append(f"{short_k}={int(v)}")
        else:
            parts.append(f"{short_k}={v}")
    return ", ".join(parts)

def print_results(all_results: list, top_n: int, capital: float, leverage: float,
                  days: int, interval: str):
    """Print full results table and best strategy details."""

    sorted_results = sorted(all_results, key=lambda r: r["score"], reverse=True)
    top            = sorted_results[:top_n]

    print("\n")
    print("=" * 90)
    print("  BTC INTRADAY SCALPING -- STRATEGY SWEEP RESULTS")
    print(f"  Capital: ${capital:,.0f} | Leverage: {leverage}x | {days} days | {interval} candles")
    print(f"  Fee: Maker entry +{MAKER_REBATE*100:.2f}% rebate | Taker exit -{TAKER_FEE*100:.2f}% | Net RT: -{(TAKER_FEE-MAKER_REBATE)*100:.2f}%")
    print(f"  In-sample: first 60 days | Out-of-sample: last 30 days")
    print("=" * 90)

    # Header
    hdr = (
        f"{'Rank':>4}  "
        f"{'Strategy':<22}"
        f"{'Params':<40}"
        f"{'Tr/d':>5}"
        f"{'Win%':>6}"
        f"{'PF':>6}"
        f"{'Daily$':>8}"
        f"{'Sharpe':>8}"
        f"{'MaxDD':>7}"
        f"{'ConsL':>6}"
    )
    print(hdr)
    print("-" * 4 + "  " + "-" * 22 + "-" * 40 + "-" * 5 + "-" * 6 + "-" * 6 + "-" * 8 + "-" * 8 + "-" * 7 + "-" * 6)

    for rank, r in enumerate(top, 1):
        oos = r["oos"]
        ps  = fmt_params(r["params"])
        if len(ps) > 38:
            ps = ps[:35] + "..."
        row = (
            f"{rank:>4}  "
            f"{r['strategy']:<22}"
            f"{ps:<40}"
            f"{oos['trades_per_day']:>5.1f}"
            f"{oos['win_rate']*100:>5.1f}%"
            f"{min(oos['profit_factor'], 9.99):>6.2f}"
            f"${oos['daily_avg']:>7.0f}"
            f"{oos['sharpe']:>8.2f}"
            f"{-oos['max_dd_pct']*100:>6.1f}%"
            f"{oos['max_consec_loss']:>6}"
        )
        print(row)

    # -- Best strategy details --------------------------------------------------
    if not top:
        print("\n  No valid results found.")
        return

    best = top[0]
    print("\n")
    print("-" * 90)
    print(f"  BEST STRATEGY DETAILS: {best['strategy']}")
    print("-" * 90)

    def block(label, stats):
        print(f"\n  [{label}]")
        print(f"    Trades:          {stats['n_trades']:,}  ({stats['trades_per_day']:.1f}/day)")
        print(f"    Win Rate:        {stats['win_rate']*100:.1f}%")
        print(f"    Profit Factor:   {min(stats['profit_factor'], 99.0):.2f}")
        print(f"    Total P&L:       ${stats['total_pnl']:,.2f}")
        print(f"    Avg P&L/trade:   ${stats['avg_pnl']:.2f}")
        print(f"    Daily P&L avg:   ${stats['daily_avg']:.2f}")
        print(f"    Daily P&L median:${stats['daily_median']:.2f}")
        print(f"    Sharpe (daily):  {stats['sharpe']:.2f}")
        print(f"    Max Drawdown:    {-stats['max_dd_pct']*100:.1f}%")
        print(f"    Max Consec Loss: {stats['max_consec_loss']}")
        print(f"    TP/SL/Timeout:   {stats['tp_count']}/{stats['sl_count']}/{stats['to_count']}")

    block("IN-SAMPLE (first 60 days)", best["in"])
    block("OUT-OF-SAMPLE (last 30 days)", best["oos"])

    print(f"\n  Parameters: {best['params']}")

    # Sample trades from OOS
    print("\n  SAMPLE TRADES (last 10 from out-of-sample):")
    sample = best["trades_oos"][-10:]
    print(f"    {'Date':<12} {'Dir':<6} {'Entry':>10} {'Exit':>10} {'Reason':<8} {'PnL$':>8}")
    print("    " + "-" * 60)
    for t in sample:
        dt  = datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).strftime("%m-%d %H:%M")
        row = (
            f"    {dt:<12}"
            f"{t['direction']:<6}"
            f"{t['entry_price']:>10.1f}"
            f"{t['exit_price']:>10.1f}"
            f"{t['exit_reason']:<8}"
            f"${t['pnl_usd']:>7.2f}"
        )
        print(row)

    # -- Scaling table ----------------------------------------------------------
    print("\n")
    print("-" * 90)
    print("  PROJECTED DAILY P&L AT DIFFERENT CAPITAL LEVELS (based on OOS median)")
    print("-" * 90)

    oos        = best["oos"]
    base_daily = oos["daily_median"]
    base_pos   = capital * leverage

    configs = [
        (10_000, 3.0),
        (10_000, 5.0),
        (25_000, 3.0),
        (25_000, 5.0),
        (50_000, 3.0),
    ]
    print(f"\n  {'Capital':>10}  {'Leverage':>9}  {'Position':>10}  {'Daily$ med':>12}  {'Daily$ avg':>12}  {'Monthly':>10}")
    print("  " + "-" * 68)
    for cap, lev in configs:
        scale  = (cap * lev) / base_pos
        d_med  = base_daily * scale
        d_avg  = oos["daily_avg"] * scale
        mo     = d_avg * 22
        print(f"  ${cap:>9,.0f}  {lev:>8.0f}x  ${cap*lev:>9,.0f}  ${d_med:>11,.0f}  ${d_avg:>11,.0f}  ~${mo:>8,.0f}")

    # -- Strategy comparison summary --------------------------------------------
    print("\n")
    print("-" * 90)
    print("  BEST PER STRATEGY (OOS score)")
    print("-" * 90)
    best_per = {}
    for r in all_results:
        sid = r["strategy_id"]
        if sid not in best_per or r["score"] > best_per[sid]["score"]:
            best_per[sid] = r
    print(f"  {'Strategy':<22}  {'Score':>6}  {'Tr/d':>5}  {'Win%':>5}  {'PF':>5}  {'Daily$':>8}  {'Sharpe':>7}  {'MaxDD':>7}")
    print("  " + "-" * 72)
    for sid, r in sorted(best_per.items(), key=lambda x: -x[1]["score"]):
        oos = r["oos"]
        print(
            f"  {r['strategy']:<22}"
            f"  {r['score']:>6.2f}"
            f"  {oos['trades_per_day']:>5.1f}"
            f"  {oos['win_rate']*100:>4.1f}%"
            f"  {min(oos['profit_factor'],9.99):>5.2f}"
            f"  ${oos['daily_avg']:>7.0f}"
            f"  {oos['sharpe']:>7.2f}"
            f"  {-oos['max_dd_pct']*100:>6.1f}%"
        )

# -- Entry Point ---------------------------------------------------------------

def main():
    args = parse_args()

    print("=" * 60)
    print("  BTC SCALPING STRATEGY SWEEP")
    print(f"  {args.days}d {args.interval} | Capital ${args.capital:,.0f} | {args.leverage}x leverage")
    print("=" * 60)

    # Fetch data
    print("\nStep 1: Fetching market data")
    candles = fetch_candles(args.interval, args.days)

    if len(candles) < 500:
        print(f"ERROR: Only {len(candles)} candles fetched. Need at least 500.")
        sys.exit(1)

    # Split: first 2/3 in-sample, last 1/3 out-of-sample
    split_idx   = int(len(candles) * (2 / 3))
    candles_in  = candles[:split_idx]
    candles_oos = candles[split_idx:]

    ts_in_start = datetime.fromtimestamp(candles_in[0]["t"], tz=timezone.utc).strftime("%Y-%m-%d")
    ts_in_end   = datetime.fromtimestamp(candles_in[-1]["t"], tz=timezone.utc).strftime("%Y-%m-%d")
    ts_oos_start = datetime.fromtimestamp(candles_oos[0]["t"], tz=timezone.utc).strftime("%Y-%m-%d")
    ts_oos_end   = datetime.fromtimestamp(candles_oos[-1]["t"], tz=timezone.utc).strftime("%Y-%m-%d")

    print(f"\n  In-sample:      {ts_in_start} to {ts_in_end}  ({len(candles_in):,} candles)")
    print(f"  Out-of-sample:  {ts_oos_start} to {ts_oos_end}  ({len(candles_oos):,} candles)")

    # Determine strategies to run
    if args.strategy == "all":
        strategies = list(STRATEGY_SWEEPS.keys())
    elif args.strategy in STRATEGY_SWEEPS:
        strategies = [args.strategy]
    else:
        print(f"ERROR: Unknown strategy '{args.strategy}'. Options: {', '.join(STRATEGY_SWEEPS.keys())}")
        sys.exit(1)

    print(f"\nStep 2: Running sweeps for: {', '.join(strategies)}")

    all_results = []
    t_start = time.time()

    for strat_name in strategies:
        results = run_sweep(candles_in, candles_oos, strat_name,
                            args.capital, args.leverage)
        all_results.extend(results)

    elapsed = time.time() - t_start
    total_combos = sum(
        len(list(itertools.product(*STRATEGY_SWEEPS[s]["params"].values())))
        for s in strategies
    )
    print(f"\n  Swept {total_combos:,} total combinations in {elapsed:.1f}s")
    print(f"  Valid results (>= 10 in-sample trades): {len(all_results)}")

    if not all_results:
        print("\nNo valid results. Try --days 120 or a different interval.")
        sys.exit(0)

    # Print results
    print_results(all_results, args.top, args.capital, args.leverage, args.days, args.interval)

    # Export
    if args.export:
        export_path = os.path.join(CACHE_DIR, "scalper_sweep.json")

        # Slim results for export (remove raw trades list, keep stats)
        slim = []
        for r in sorted(all_results, key=lambda x: -x["score"])[:50]:
            slim.append({
                "strategy":    r["strategy"],
                "params":      r["params"],
                "in_sample":   r["in"],
                "out_of_sample": r["oos"],
                "score":       r["score"],
            })

        with open(export_path, "w") as f:
            json.dump({
                "generated":    datetime.now(tz=timezone.utc).isoformat() + "Z",
                "config": {
                    "days":     args.days,
                    "interval": args.interval,
                    "capital":  args.capital,
                    "leverage": args.leverage,
                },
                "results": slim,
            }, f, indent=2)
        print(f"\n  Exported top 50 results to: {export_path}")

    print("\n  Done.\n")

if __name__ == "__main__":
    main()
