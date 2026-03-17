#!/usr/bin/env python3
"""
Backtest mean-reversion and trend-filtered strategies for Hyperliquid.

Strategy 1 - RSI Mean Reversion (1H):
    Buy when RSI(14) < 25 (oversold), short when RSI > 75 (overbought).
    Exit when RSI returns to 50 OR trailing stop.

Strategy 2 - Bollinger Band Reversion (1H):
    Buy when price closes below lower BB (2 std dev).
    Exit when price returns to middle band (20 SMA).

Strategy 3 - RSI + 4H Trend Filter (best of both):
    Only take RSI signals that align with the 4H EMA(10/30) trend direction.
    This avoids fighting the trend while still capturing mean-reversion bounces.

Strategy 4 - Daily Pivot Point Breakout:
    Calculate daily pivot, R1, S1 from prior day's high/low/close.
    Enter when BTC breaks above R1 (Long) or below S1 (Short) with volume confirmation.

Usage:
    python backtest_hl_v2.py              # 60 days
    python backtest_hl_v2.py --days 90
"""

import requests
import time
import math
import sys
import argparse
from datetime import datetime, timezone
from collections import defaultdict

# ── Shared constants ────────────────────────────────────────────────────────
LEVERAGE   = 5
MARGIN_USD = 1000.0
TAKER_FEE  = 0.0005    # 0.05% per side (Hyperliquid taker)


# ── Data fetching ───────────────────────────────────────────────────────────

def fetch_btc_1min(days=60):
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    url      = "https://api.binance.com/api/v3/klines"
    candles  = {}
    cur      = start_ms
    total    = 0

    while cur < end_ms:
        try:
            r = requests.get(url, params={
                "symbol": "BTCUSDT", "interval": "1m",
                "startTime": cur, "endTime": end_ms, "limit": 1000,
            }, timeout=15)
            data = r.json()
        except Exception as e:
            print(f"\nFetch error: {e}, retrying...")
            time.sleep(2)
            continue
        if not data:
            break
        for k in data:
            ts = k[0] // 1000
            candles[ts] = {
                "open":  float(k[1]),
                "high":  float(k[2]),
                "low":   float(k[3]),
                "close": float(k[4]),
            }
        cur    = data[-1][0] + 60000
        total += len(data)
        print(f"\r  Fetched {total:,} candles...", end="", flush=True)
        time.sleep(0.05)

    print(f"\r  Fetched {total:,} candles        ")
    return candles


# ── Build higher-timeframe candles ──────────────────────────────────────────

def build_candles(candles_1m, period_secs):
    bars = {}
    for ts, c in candles_1m.items():
        bar_ts = (ts // period_secs) * period_secs
        if bar_ts not in bars:
            bars[bar_ts] = {"open": c["open"], "high": c["high"],
                             "low": c["low"],   "close": c["close"],
                             "ts": bar_ts}
        else:
            bars[bar_ts]["high"]  = max(bars[bar_ts]["high"],  c["high"])
            bars[bar_ts]["low"]   = min(bars[bar_ts]["low"],   c["low"])
            bars[bar_ts]["close"] = c["close"]
    return bars


# ── Technical indicators ────────────────────────────────────────────────────

def calc_rsi(closes, period=14):
    """RSI for a list of closes. Returns list same length, None for warmup."""
    result = [None] * len(closes)
    if len(closes) < period + 1:
        return result

    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period, len(closes)):
        if i > period:
            diff     = closes[i] - closes[i-1]
            avg_gain = (avg_gain * (period - 1) + max(diff, 0))  / period
            avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period

        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs        = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))

    return result


def calc_ema(values, period):
    k      = 2 / (period + 1)
    result = [None] * len(values)
    for i, v in enumerate(values):
        if v is None:
            continue
        if result[i-1] is None or i == 0:
            result[i] = v
        else:
            result[i] = v * k + result[i-1] * (1 - k)
    return result


def calc_bollinger(closes, period=20, std_mult=2.0):
    """Returns list of (upper, middle, lower) or None during warmup."""
    result = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1: i + 1]
        mean   = sum(window) / period
        std    = math.sqrt(sum((x - mean)**2 for x in window) / period)
        result[i] = (mean + std_mult * std, mean, mean - std_mult * std)
    return result


# ── Trade execution engine ──────────────────────────────────────────────────

def run_trade(candles_1m, entry_ts, direction,
              stop_loss_pct, trail_pct, max_hold_secs,
              exit_rsi=None, rsi_at_ts=None,
              exit_at_middle_bb=None, bb_at_ts=None,
              breakeven_pct=0.005):
    """
    Universal trade runner. Supports optional RSI/BB exit conditions.
    """
    entry_c = candles_1m.get(entry_ts)
    if not entry_c:
        # Try nearest candle within 5 min
        for offset in range(60, 360, 60):
            entry_c = candles_1m.get(entry_ts + offset)
            if entry_c:
                entry_ts = entry_ts + offset
                break
    if not entry_c:
        return None

    entry_price = entry_c["close"]
    notional    = MARGIN_USD * LEVERAGE
    fees        = 2 * TAKER_FEE * notional
    is_long     = direction == "Long"

    stop = entry_price * (1 - stop_loss_pct) if is_long else entry_price * (1 + stop_loss_pct)
    peak = entry_price

    exit_price  = None
    exit_reason = "time_exit"
    close_ts    = entry_ts + max_hold_secs

    ts = entry_ts + 60
    while ts <= close_ts:
        c = candles_1m.get(ts)
        if not c:
            ts += 60
            continue

        # Check RSI exit condition (return to mean)
        if exit_rsi is not None and rsi_at_ts is not None:
            rsi_now = rsi_at_ts.get(ts)
            if rsi_now is not None:
                if is_long  and rsi_now >= exit_rsi:
                    exit_price  = c["close"]
                    exit_reason = "rsi_exit"
                    break
                if not is_long and rsi_now <= exit_rsi:
                    exit_price  = c["close"]
                    exit_reason = "rsi_exit"
                    break

        # Check Bollinger middle-band exit
        if exit_at_middle_bb and bb_at_ts is not None:
            bb = bb_at_ts.get(ts)
            if bb is not None:
                _, middle, _ = bb
                if is_long  and c["close"] >= middle:
                    exit_price  = c["close"]
                    exit_reason = "bb_middle_exit"
                    break
                if not is_long and c["close"] <= middle:
                    exit_price  = c["close"]
                    exit_reason = "bb_middle_exit"
                    break

        if is_long:
            if c["low"] <= stop:
                exit_price  = stop
                exit_reason = "trailing_stop" if stop >= entry_price else "stop_loss"
                break
            if c["high"] >= entry_price * (1 + breakeven_pct) and stop < entry_price:
                stop = entry_price
            peak = max(peak, c["high"])
            if peak * (1 - trail_pct) > stop:
                stop = peak * (1 - trail_pct)
        else:
            if c["high"] >= stop:
                exit_price  = stop
                exit_reason = "trailing_stop" if stop <= entry_price else "stop_loss"
                break
            if c["low"] <= entry_price * (1 - breakeven_pct) and stop > entry_price:
                stop = entry_price
            peak = min(peak, c["low"])
            if peak * (1 + trail_pct) < stop:
                stop = peak * (1 + trail_pct)

        ts += 60

    if exit_price is None:
        last = candles_1m.get(close_ts) or candles_1m.get(close_ts - 60)
        if not last:
            return None
        exit_price  = last["close"]
        exit_reason = "time_exit"

    pnl = ((exit_price - entry_price) / entry_price if is_long
           else (entry_price - exit_price) / entry_price)
    pnl_usd = pnl * notional - fees

    return {
        "entry_price": entry_price,
        "exit_price":  exit_price,
        "exit_reason": exit_reason,
        "pnl_usd":     round(pnl_usd, 2),
        "won":         pnl_usd > 0,
    }


# ── Stats helpers ───────────────────────────────────────────────────────────

def summarise(results, days, label=""):
    if not results:
        return None
    n         = len(results)
    wins      = sum(1 for r in results if r["won"])
    total_pnl = sum(r["pnl_usd"] for r in results)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in results:
        equity += r["pnl_usd"]
        peak    = max(peak, equity)
        max_dd  = min(max_dd, equity - peak)
    reasons = defaultdict(int)
    for r in results:
        reasons[r["exit_reason"]] += 1
    return {
        "label":          label,
        "trades":         n,
        "win_rate":       wins / n,
        "total_pnl":      total_pnl,
        "avg_pnl":        total_pnl / n,
        "daily_pnl":      total_pnl / days,
        "trades_per_day": n / days,
        "max_dd":         max_dd,
        "exit_reasons":   dict(reasons),
        "results":        results,
    }


def print_header(title):
    print(f"\n{'=' * 100}")
    print(f"  {title}")
    print("=" * 100)


def print_summary(s, days=60):
    if s is None:
        print("  No trades generated.")
        return
    print(f"\n  Trades:        {s['trades']:,}  ({s['trades_per_day']:.1f}/day)")
    print(f"  Win rate:      {s['win_rate']*100:.1f}%")
    print(f"  Avg PnL:       ${s['avg_pnl']:.2f}/trade")
    print(f"  Daily PnL:     ${s['daily_pnl']:.2f}")
    print(f"  Monthly est:   ${s['daily_pnl']*30:,.0f}")
    print(f"  Max drawdown:  ${s['max_dd']:,.2f}")
    reasons = s["exit_reasons"]
    total   = sum(reasons.values())
    for k, v in sorted(reasons.items(), key=lambda x:-x[1]):
        print(f"    {k:25s}  {v:4d}  ({v/total*100:.0f}%)")

    # Win rate by move size bucket if available
    if "move_pct" in (s["results"][0] if s["results"] else {}):
        print("\n  Win rate by move size at signal:")
        buckets = defaultdict(list)
        for r in s["results"]:
            bucket = round(abs(r.get("move_pct", 0)))
            buckets[bucket].append(r["won"])
        for m in sorted(buckets):
            wl  = buckets[m]
            wr  = sum(wl) / len(wl)
            bar = "#" * int(wr * 20)
            print(f"    ~{m:2d}%  {bar:<20s}  {wr*100:5.1f}%  (n={len(wl)})")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 1: RSI MEAN REVERSION
# ════════════════════════════════════════════════════════════════════════════

def backtest_rsi_reversion(candles_1m, days,
                            rsi_period=14,
                            oversold=25, overbought=75,
                            rsi_exit=50,
                            stop_pct=0.015,
                            trail_pct=0.008,
                            max_hold=72*3600,
                            candle_secs=3600):
    """
    1H RSI mean reversion.
    Long when RSI < oversold. Short when RSI > overbought.
    Exit when RSI returns to rsi_exit (50) or stop hit.
    """
    bars    = build_candles(candles_1m, candle_secs)
    ts_list = sorted(bars.keys())
    closes  = [bars[t]["close"] for t in ts_list]
    rsi_vals = calc_rsi(closes, rsi_period)

    # Map each 1-min ts -> RSI value of its containing bar
    rsi_1m = {}
    for i, bar_ts in enumerate(ts_list):
        if rsi_vals[i] is not None:
            for offset in range(0, candle_secs, 60):
                rsi_1m[bar_ts + offset] = rsi_vals[i]

    results     = []
    last_trade  = 0
    cooldown    = candle_secs * 3  # wait 3 bars before next trade

    for i in range(rsi_period + 1, len(ts_list)):
        rsi = rsi_vals[i]
        if rsi is None:
            continue

        bar_ts = ts_list[i]
        if bar_ts - last_trade < cooldown:
            continue

        direction = None
        if rsi < oversold:
            direction = "Long"
        elif rsi > overbought:
            direction = "Short"

        if direction is None:
            continue

        entry_ts = bar_ts + candle_secs  # enter on open of next bar
        dt       = datetime.fromtimestamp(bar_ts, tz=timezone.utc)

        trade = run_trade(
            candles_1m, entry_ts, direction,
            stop_pct, trail_pct, max_hold,
            exit_rsi=rsi_exit, rsi_at_ts=rsi_1m,
        )
        if trade:
            results.append({
                "time":      dt.strftime("%Y-%m-%d %H:%M"),
                "direction": direction,
                "rsi":       round(rsi, 1),
                **{k: v for k, v in trade.items()},
            })
            last_trade = entry_ts

    return summarise(results, days, "RSI Mean Reversion (1H)")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 2: BOLLINGER BAND REVERSION
# ════════════════════════════════════════════════════════════════════════════

def backtest_bollinger(candles_1m, days,
                        bb_period=20, bb_std=2.0,
                        stop_pct=0.015,
                        trail_pct=0.008,
                        max_hold=48*3600,
                        candle_secs=3600):
    """
    1H Bollinger Band reversion.
    Long when close < lower band. Short when close > upper band.
    Exit when price returns to middle band (20 SMA).
    """
    bars    = build_candles(candles_1m, candle_secs)
    ts_list = sorted(bars.keys())
    closes  = [bars[t]["close"] for t in ts_list]
    bb_vals  = calc_bollinger(closes, bb_period, bb_std)

    # Map 1-min ts -> BB values
    bb_1m = {}
    for i, bar_ts in enumerate(ts_list):
        if bb_vals[i] is not None:
            for offset in range(0, candle_secs, 60):
                bb_1m[bar_ts + offset] = bb_vals[i]

    results     = []
    last_trade  = 0
    cooldown    = candle_secs * 3

    for i in range(bb_period + 1, len(ts_list)):
        bb = bb_vals[i]
        if bb is None:
            continue

        upper, middle, lower = bb
        bar_ts = ts_list[i]
        close  = closes[i]

        if bar_ts - last_trade < cooldown:
            continue

        direction = None
        if close < lower:
            direction = "Long"
        elif close > upper:
            direction = "Short"

        if direction is None:
            continue

        # Confirm: RSI not already at extreme in WRONG direction
        entry_ts = bar_ts + candle_secs
        dt       = datetime.fromtimestamp(bar_ts, tz=timezone.utc)

        trade = run_trade(
            candles_1m, entry_ts, direction,
            stop_pct, trail_pct, max_hold,
            exit_at_middle_bb=True, bb_at_ts=bb_1m,
        )
        if trade:
            dev_pct = abs(close - middle) / middle * 100
            results.append({
                "time":      dt.strftime("%Y-%m-%d %H:%M"),
                "direction": direction,
                "dev_pct":   round(dev_pct, 2),
                **{k: v for k, v in trade.items()},
            })
            last_trade = entry_ts

    return summarise(results, days, "Bollinger Band Reversion (1H)")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 3: RSI + 4H TREND FILTER
# ════════════════════════════════════════════════════════════════════════════

def backtest_rsi_trend_filtered(candles_1m, days,
                                  rsi_oversold=30, rsi_overbought=70,
                                  rsi_exit=50,
                                  ema_fast=10, ema_slow=30,
                                  stop_pct=0.012,
                                  trail_pct=0.008,
                                  max_hold=72*3600):
    """
    Only take 1H RSI oversold/overbought signals when they align with
    the 4H EMA trend (Long only when 4H EMA trending up, Short only when down).
    This cuts false signals significantly.
    """
    # Build 4H candles and EMA
    bars_4h  = build_candles(candles_1m, 14400)
    ts_4h    = sorted(bars_4h.keys())
    cls_4h   = [bars_4h[t]["close"] for t in ts_4h]
    fast_4h  = calc_ema(cls_4h, ema_fast)
    slow_4h  = calc_ema(cls_4h, ema_slow)

    # Map 1-min ts -> 4H trend ("up", "down", None)
    trend_1m = {}
    for i, bar_ts in enumerate(ts_4h):
        fe = fast_4h[i]
        se = slow_4h[i]
        if fe is not None and se is not None:
            trend = "up" if fe > se else "down"
            for offset in range(0, 14400, 60):
                trend_1m[bar_ts + offset] = trend

    # Build 1H RSI
    bars_1h  = build_candles(candles_1m, 3600)
    ts_1h    = sorted(bars_1h.keys())
    cls_1h   = [bars_1h[t]["close"] for t in ts_1h]
    rsi_vals  = calc_rsi(cls_1h, 14)

    rsi_1m = {}
    for i, bar_ts in enumerate(ts_1h):
        if rsi_vals[i] is not None:
            for offset in range(0, 3600, 60):
                rsi_1m[bar_ts + offset] = rsi_vals[i]

    results    = []
    last_trade = 0
    cooldown   = 3 * 3600

    for i in range(15, len(ts_1h)):
        rsi    = rsi_vals[i]
        bar_ts = ts_1h[i]
        if rsi is None or bar_ts - last_trade < cooldown:
            continue

        trend = trend_1m.get(bar_ts)
        if trend is None:
            continue

        direction = None
        if rsi < rsi_oversold and trend == "up":
            direction = "Long"   # oversold pullback in uptrend
        elif rsi > rsi_overbought and trend == "down":
            direction = "Short"  # overbought bounce in downtrend

        if direction is None:
            continue

        entry_ts = bar_ts + 3600
        dt       = datetime.fromtimestamp(bar_ts, tz=timezone.utc)

        trade = run_trade(
            candles_1m, entry_ts, direction,
            stop_pct, trail_pct, max_hold,
            exit_rsi=rsi_exit, rsi_at_ts=rsi_1m,
        )
        if trade:
            results.append({
                "time":      dt.strftime("%Y-%m-%d %H:%M"),
                "direction": direction,
                "rsi":       round(rsi, 1),
                "trend":     trend,
                **{k: v for k, v in trade.items()},
            })
            last_trade = entry_ts

    return summarise(results, days, "RSI + 4H Trend Filter")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 4: DAILY PIVOT POINT BREAKOUT
# ════════════════════════════════════════════════════════════════════════════

def backtest_pivot_breakout(candles_1m, days,
                             stop_pct=0.008,
                             trail_pct=0.006,
                             max_hold=8*3600):
    """
    Classic pivot point breakout.
    Pivot = (prev_high + prev_low + prev_close) / 3
    R1 = 2*Pivot - prev_low   (resistance)
    S1 = 2*Pivot - prev_high  (support)
    Enter Long on break above R1, Short on break below S1.
    """
    bars_day = build_candles(candles_1m, 86400)
    ts_days  = sorted(bars_day.keys())

    results     = []
    last_trade  = 0
    cooldown    = 4 * 3600

    for day_i in range(1, len(ts_days)):
        prev   = bars_day[ts_days[day_i - 1]]
        today  = ts_days[day_i]

        pivot  = (prev["high"] + prev["low"] + prev["close"]) / 3
        r1     = 2 * pivot - prev["low"]
        s1     = 2 * pivot - prev["high"]

        day_end = today + 86400
        ts      = today

        traded_long  = False
        traded_short = False

        while ts < day_end:
            if ts - last_trade < cooldown:
                ts += 60
                continue

            c = candles_1m.get(ts)
            if not c:
                ts += 60
                continue

            dt = datetime.fromtimestamp(ts, tz=timezone.utc)

            if not traded_long and c["close"] > r1:
                trade = run_trade(candles_1m, ts, "Long",
                                  stop_pct, trail_pct, max_hold)
                if trade:
                    results.append({
                        "time":      dt.strftime("%Y-%m-%d %H:%M"),
                        "direction": "Long",
                        "level":     "R1",
                        **{k: v for k, v in trade.items()},
                    })
                    last_trade  = ts
                    traded_long = True

            elif not traded_short and c["close"] < s1:
                trade = run_trade(candles_1m, ts, "Short",
                                  stop_pct, trail_pct, max_hold)
                if trade:
                    results.append({
                        "time":      dt.strftime("%Y-%m-%d %H:%M"),
                        "direction": "Short",
                        "level":     "S1",
                        **{k: v for k, v in trade.items()},
                    })
                    last_trade   = ts
                    traded_short = True

            ts += 60

    return summarise(results, days, "Daily Pivot Breakout")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()

    print("=" * 100)
    print("  Hyperliquid Mean-Reversion & Breakout Strategy Backtester  v2")
    print(f"  ${MARGIN_USD} margin  {LEVERAGE}x leverage  "
          f"(${MARGIN_USD*LEVERAGE:.0f} notional)  "
          f"Fee: {TAKER_FEE*100:.3f}% per side")
    print("=" * 100)
    print(f"\nFetching {args.days} days of 1-min BTC data...")

    candles = fetch_btc_1min(days=args.days)
    if not candles:
        print("ERROR: no data")
        sys.exit(1)

    ts_sorted = sorted(candles.keys())
    start_dt  = datetime.fromtimestamp(ts_sorted[0],  tz=timezone.utc)
    end_dt    = datetime.fromtimestamp(ts_sorted[-1], tz=timezone.utc)
    print(f"  Range: {start_dt:%Y-%m-%d} to {end_dt:%Y-%m-%d}\n")

    all_stats = []

    # ── Strategy 1 ──────────────────────────────────────────────────────────
    print_header("STRATEGY 1: RSI MEAN REVERSION (1H)")
    print("  Long when RSI(14) < 25 | Short when RSI > 75 | Exit when RSI returns to 50")
    s1 = backtest_rsi_reversion(candles, args.days)
    print_summary(s1)
    if s1:
        all_stats.append(s1)

    print("\n  RSI threshold sweep:")
    for ov, ob in [(20, 80), (25, 75), (30, 70), (35, 65)]:
        r = backtest_rsi_reversion(candles, args.days, oversold=ov, overbought=ob)
        if r:
            print(f"    RSI<{ov:2d} / >{ob:2d}  n={r['trades']:3d}  "
                  f"WR={r['win_rate']*100:5.1f}%  "
                  f"Daily=${r['daily_pnl']:>7.2f}  "
                  f"Monthly=${r['daily_pnl']*30:>8,.0f}")

    # ── Strategy 2 ──────────────────────────────────────────────────────────
    print_header("STRATEGY 2: BOLLINGER BAND REVERSION (1H)")
    print("  Long when close < lower BB | Short when close > upper BB | Exit at middle band")
    s2 = backtest_bollinger(candles, args.days)
    print_summary(s2)
    if s2:
        all_stats.append(s2)

    print("\n  BB std-dev sweep:")
    for std in [1.5, 2.0, 2.5, 3.0]:
        r = backtest_bollinger(candles, args.days, bb_std=std)
        if r:
            print(f"    std={std:.1f}  n={r['trades']:3d}  "
                  f"WR={r['win_rate']*100:5.1f}%  "
                  f"Daily=${r['daily_pnl']:>7.2f}  "
                  f"Monthly=${r['daily_pnl']*30:>8,.0f}")

    # ── Strategy 3 ──────────────────────────────────────────────────────────
    print_header("STRATEGY 3: RSI + 4H TREND FILTER (best of both)")
    print("  Long only when 4H EMA(10>30) AND 1H RSI < 30 | Short only when trending down + RSI > 70")
    s3 = backtest_rsi_trend_filtered(candles, args.days)
    print_summary(s3)
    if s3:
        all_stats.append(s3)

    print("\n  RSI threshold sweep (with trend filter):")
    for ov, ob in [(25, 75), (30, 70), (35, 65), (40, 60)]:
        r = backtest_rsi_trend_filtered(candles, args.days,
                                         rsi_oversold=ov, rsi_overbought=ob)
        if r:
            print(f"    RSI<{ov:2d} / >{ob:2d}  n={r['trades']:3d}  "
                  f"WR={r['win_rate']*100:5.1f}%  "
                  f"Daily=${r['daily_pnl']:>7.2f}  "
                  f"Monthly=${r['daily_pnl']*30:>8,.0f}")

    # ── Strategy 4 ──────────────────────────────────────────────────────────
    print_header("STRATEGY 4: DAILY PIVOT POINT BREAKOUT")
    print("  Long above R1 | Short below S1 | Max hold 8h")
    s4 = backtest_pivot_breakout(candles, args.days)
    print_summary(s4)
    if s4:
        all_stats.append(s4)

    # ── Head-to-head ────────────────────────────────────────────────────────
    print_header("HEAD-TO-HEAD COMPARISON")
    print(f"\n  {'Strategy':<35s}  {'WR':>6}  {'n/day':>6}  "
          f"{'Daily':>8}  {'Monthly':>10}  {'MaxDD':>10}")
    print("  " + "-" * 85)
    for s in sorted(all_stats, key=lambda x: -x["daily_pnl"]):
        tag = " <<< BEST" if s == max(all_stats, key=lambda x: x["daily_pnl"]) else ""
        print(f"  {s['label']:<35s}  "
              f"{s['win_rate']*100:>5.1f}%  "
              f"{s['trades_per_day']:>6.1f}  "
              f"${s['daily_pnl']:>7.2f}  "
              f"${s['daily_pnl']*30:>9,.0f}  "
              f"${s['max_dd']:>9,.2f}{tag}")

    best = max(all_stats, key=lambda x: x["daily_pnl"]) if all_stats else None

    # ── Verdict ─────────────────────────────────────────────────────────────
    print_header("VERDICT")

    if not best:
        print("\n  No results. Try --days 90.")
        return

    if best["daily_pnl"] > 0 and best["win_rate"] >= 0.50:
        print(f"\n  [YES] DEPLOY: {best['label']}")
        print(f"  {best['win_rate']*100:.1f}% WR  |  "
              f"${best['daily_pnl']:.2f}/day  |  "
              f"~${best['daily_pnl']*30:,.0f}/month on ${MARGIN_USD}x{LEVERAGE}")
        print(f"\n  Scaling:")
        for size in [1000, 2000, 5000, 10000]:
            scale = size / MARGIN_USD
            print(f"    ${size:>7} margin  {LEVERAGE}x  ->  "
                  f"~${best['daily_pnl']*30*scale:>10,.0f}/month")
    else:
        print(f"\n  [~] BEST FOUND: {best['label']}")
        print(f"  {best['win_rate']*100:.1f}% WR, ${best['daily_pnl']:.2f}/day")
        print("  Edge exists but not yet strong enough. Suggest --days 90 for more data.")


if __name__ == "__main__":
    main()
