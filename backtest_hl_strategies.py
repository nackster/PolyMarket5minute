#!/usr/bin/env python3
"""
Backtest 3 Hyperliquid strategies to find what actually has edge.

Strategy 1 - Session Breakout:
    At London (08:00 UTC) and NY (13:30 UTC) opens, if BTC moves 0.3%+
    in the first 15 minutes, enter in that direction. Hold up to 2 hours.

Strategy 2 - 4H EMA Trend Following:
    Build 4H candles. When EMA(8) crosses EMA(21), enter on the next
    1-minute candle. Trail stop. Exit on opposite crossover.

Strategy 3 - Large Move Momentum:
    When BTC moves 1%+ in any 30-minute window, enter in that direction.
    These explosive moves tend to sustain for 1-4 hours. Trail stop.

Usage:
    python backtest_hl_strategies.py              # 60 days
    python backtest_hl_strategies.py --days 90
"""

import requests
import time
import sys
import argparse
from datetime import datetime, timezone
from collections import defaultdict

# ── Shared constants ────────────────────────────────────────────────────────
LEVERAGE    = 5
MARGIN_USD  = 1000.0
TAKER_FEE   = 0.0005    # 0.05% per side (actual HL taker fee, not 0.1%)


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


# ── Shared trade engine ─────────────────────────────────────────────────────

def run_trade(candles, entry_ts, direction,
              stop_loss_pct, trail_pct, max_hold_secs,
              breakeven_pct=0.002):
    """
    Enter at entry_ts, trail stop, forced close after max_hold_secs.
    Returns trade dict or None.
    """
    entry_c = candles.get(entry_ts)
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
        c = candles.get(ts)
        if not c:
            ts += 60
            continue

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
        last = candles.get(close_ts) or candles.get(close_ts - 60)
        if not last:
            return None
        exit_price  = last["close"]
        exit_reason = "time_exit"

    pnl = ((exit_price - entry_price) / entry_price if is_long
           else (entry_price - exit_price) / entry_price)
    pnl_usd = pnl * notional - fees

    return {
        "entry":       entry_price,
        "exit":        exit_price,
        "exit_reason": exit_reason,
        "pnl_usd":     round(pnl_usd, 2),
        "won":         pnl_usd > 0,
    }


# ── Stats ───────────────────────────────────────────────────────────────────

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


def print_summary(s):
    if s is None:
        print("  No trades.")
        return
    print(f"\n  Trades:        {s['trades']:,}  ({s['trades_per_day']:.1f}/day)")
    print(f"  Win rate:      {s['win_rate']*100:.1f}%")
    print(f"  Avg PnL:       ${s['avg_pnl']:.2f}/trade")
    print(f"  Daily PnL:     ${s['daily_pnl']:.2f}")
    print(f"  Monthly est:   ${s['daily_pnl']*30:,.0f}")
    print(f"  Max drawdown:  ${s['max_dd']:,.2f}")
    reasons = s["exit_reasons"]
    total   = sum(reasons.values())
    exits   = "  ".join(f"{k}={v}({v/total*100:.0f}%)" for k, v in sorted(reasons.items(), key=lambda x:-x[1]))
    print(f"  Exits:         {exits}")


def print_hour_wr(results):
    buckets = defaultdict(list)
    for r in results:
        h = int(r["time"].split(" ")[1].split(":")[0])
        buckets[h].append(r["won"])
    print("  Win rate by hour (UTC):")
    for h in sorted(buckets):
        wl  = buckets[h]
        wr  = sum(wl) / len(wl)
        bar = "#" * int(wr * 20)
        print(f"    {h:02d}:00  {bar:<20s}  {wr*100:5.1f}%  (n={len(wl)})")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 1: SESSION BREAKOUT
# ════════════════════════════════════════════════════════════════════════════

SESSION_OPENS_UTC = [
    8 * 3600,           # London open: 08:00 UTC
    13 * 3600 + 30 * 60,  # NY open:     13:30 UTC
]
S1_BREAKOUT_WINDOW = 15 * 60   # check momentum over first 15 min
S1_MIN_MOVE        = 0.003     # 0.3% move in 15 min to trigger
S1_STOP_PCT        = 0.005     # 0.5% stop loss
S1_TRAIL_PCT       = 0.004     # 0.4% trail
S1_MAX_HOLD        = 120 * 60  # close after 2 hours max


def backtest_session_breakout(candles, days,
                               min_move=S1_MIN_MOVE,
                               stop_pct=S1_STOP_PCT,
                               trail_pct=S1_TRAIL_PCT,
                               max_hold=S1_MAX_HOLD):
    ts_list  = sorted(candles.keys())
    start_ts = ts_list[0]
    end_ts   = ts_list[-1]
    results  = []
    seen_sessions = set()

    for ts in ts_list:
        dt        = datetime.fromtimestamp(ts, tz=timezone.utc)
        secs_day  = dt.hour * 3600 + dt.minute * 60 + dt.second

        for session_start in SESSION_OPENS_UTC:
            if secs_day != session_start:
                continue

            # Only one trade per session
            session_key = (dt.date(), session_start)
            if session_key in seen_sessions:
                continue

            # Check breakout 15 min in
            open_c  = candles.get(ts)
            check_c = candles.get(ts + S1_BREAKOUT_WINDOW)
            if not open_c or not check_c:
                continue

            btc_open  = open_c["open"]
            btc_check = check_c["close"]
            move_pct  = (btc_check - btc_open) / btc_open

            if abs(move_pct) < min_move:
                continue

            seen_sessions.add(session_key)
            direction = "Long" if move_pct > 0 else "Short"
            entry_ts  = ts + S1_BREAKOUT_WINDOW

            trade = run_trade(candles, entry_ts, direction,
                              stop_pct, trail_pct, max_hold)
            if trade:
                results.append({
                    "time":        dt.strftime("%Y-%m-%d %H:%M"),
                    "session":     f"{dt.hour:02d}:{dt.minute:02d}",
                    "direction":   direction,
                    "move_pct":    round(move_pct * 100, 3),
                    **{k: v for k, v in trade.items()},
                })

    return summarise(results, days, "Session Breakout")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 2: 4H EMA CROSSOVER TREND FOLLOWING
# ════════════════════════════════════════════════════════════════════════════

S2_FAST_EMA  = 8
S2_SLOW_EMA  = 21
S2_STOP_PCT  = 0.015    # 1.5% stop (wider — trend trade)
S2_TRAIL_PCT = 0.010    # 1.0% trail
S2_MAX_HOLD  = 48 * 3600  # up to 48 hours


def build_4h_candles(candles_1m):
    """Aggregate 1-min candles into 4-hour candles."""
    bars  = {}
    for ts, c in candles_1m.items():
        bar_ts = (ts // 14400) * 14400
        if bar_ts not in bars:
            bars[bar_ts] = {"open": c["open"], "high": c["high"],
                             "low": c["low"],  "close": c["close"]}
        else:
            bars[bar_ts]["high"]  = max(bars[bar_ts]["high"],  c["high"])
            bars[bar_ts]["low"]   = min(bars[bar_ts]["low"],   c["low"])
            bars[bar_ts]["close"] = c["close"]
    return bars


def ema_series(values, period):
    k      = 2 / (period + 1)
    result = [None] * len(values)
    start  = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None:
        return result
    result[start] = values[start]
    for i in range(start + 1, len(values)):
        result[i] = values[i] * k + result[i-1] * (1 - k)
    return result


def backtest_4h_ema(candles_1m, days,
                    fast=S2_FAST_EMA, slow=S2_SLOW_EMA,
                    stop_pct=S2_STOP_PCT, trail_pct=S2_TRAIL_PCT,
                    max_hold=S2_MAX_HOLD):
    bars_4h = build_4h_candles(candles_1m)
    ts_4h   = sorted(bars_4h.keys())

    closes  = [bars_4h[t]["close"] for t in ts_4h]
    fast_e  = ema_series(closes, fast)
    slow_e  = ema_series(closes, slow)

    results       = []
    in_trade      = False
    trade_dir     = None
    trade_entry_ts = None

    for i in range(slow + 1, len(ts_4h)):
        fe_prev, fe_now = fast_e[i-1], fast_e[i]
        se_prev, se_now = slow_e[i-1], slow_e[i]
        if None in (fe_prev, fe_now, se_prev, se_now):
            continue

        crossed_up   = fe_prev <= se_prev and fe_now > se_now
        crossed_down = fe_prev >= se_prev and fe_now < se_now

        ts = ts_4h[i]
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)

        if in_trade:
            # Exit on opposite crossover
            should_exit = (trade_dir == "Long" and crossed_down) or \
                          (trade_dir == "Short" and crossed_up)
            if should_exit:
                in_trade = False

        if not in_trade and (crossed_up or crossed_down):
            direction     = "Long" if crossed_up else "Short"
            entry_ts      = ts_4h[i] + 60  # enter on next 1-min candle

            trade = run_trade(candles_1m, entry_ts, direction,
                              stop_pct, trail_pct, max_hold)
            if trade:
                results.append({
                    "time":      dt.strftime("%Y-%m-%d %H:%M"),
                    "direction": direction,
                    **{k: v for k, v in trade.items()},
                })
                in_trade   = True
                trade_dir  = direction

    return summarise(results, days, "4H EMA Crossover")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 3: LARGE MOVE MOMENTUM
# ════════════════════════════════════════════════════════════════════════════

S3_WINDOW_SECS  = 30 * 60    # measure move over 30 minutes
S3_MIN_MOVE     = 0.010      # 1.0% move in 30 min to trigger
S3_STOP_PCT     = 0.007      # 0.7% stop
S3_TRAIL_PCT    = 0.005      # 0.5% trail
S3_MAX_HOLD     = 4 * 3600   # hold up to 4 hours
S3_COOLDOWN     = 4 * 3600   # wait 4 hours between trades to avoid overlap


def backtest_large_move(candles, days,
                         window_secs=S3_WINDOW_SECS,
                         min_move=S3_MIN_MOVE,
                         stop_pct=S3_STOP_PCT,
                         trail_pct=S3_TRAIL_PCT,
                         max_hold=S3_MAX_HOLD,
                         cooldown=S3_COOLDOWN):
    ts_list      = sorted(candles.keys())
    results      = []
    last_trade_ts = 0

    for ts in ts_list:
        if ts - last_trade_ts < cooldown:
            continue

        open_c  = candles.get(ts - window_secs)
        close_c = candles.get(ts)
        if not open_c or not close_c:
            continue

        move_pct = (close_c["close"] - open_c["open"]) / open_c["open"]

        if abs(move_pct) < min_move:
            continue

        direction = "Long" if move_pct > 0 else "Short"
        dt        = datetime.fromtimestamp(ts, tz=timezone.utc)

        trade = run_trade(candles, ts, direction, stop_pct, trail_pct, max_hold)
        if trade:
            results.append({
                "time":      dt.strftime("%Y-%m-%d %H:%M"),
                "direction": direction,
                "move_pct":  round(move_pct * 100, 3),
                **{k: v for k, v in trade.items()},
            })
            last_trade_ts = ts

    return summarise(results, days, "Large Move Momentum")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()

    print("=" * 100)
    print("  Hyperliquid Strategy Comparison Backtester")
    print(f"  ${MARGIN_USD} margin  {LEVERAGE}x leverage  "
          f"({MARGIN_USD*LEVERAGE:.0f} notional)  "
          f"Fee: {TAKER_FEE*100:.3f}% per side")
    print("=" * 100)
    print(f"\nFetching {args.days} days of 1-min BTC data from Binance...")

    candles = fetch_btc_1min(days=args.days)
    if not candles:
        print("ERROR: no data")
        sys.exit(1)

    ts_sorted = sorted(candles.keys())
    start_dt  = datetime.fromtimestamp(ts_sorted[0],  tz=timezone.utc)
    end_dt    = datetime.fromtimestamp(ts_sorted[-1], tz=timezone.utc)
    print(f"  Range: {start_dt:%Y-%m-%d} to {end_dt:%Y-%m-%d}\n")

    all_stats = []

    # ── Strategy 1: Session Breakout ────────────────────────────────────────
    print_header("STRATEGY 1: SESSION BREAKOUT  (London 08:00 + NY 13:30 UTC)")
    print(f"  Signal: BTC moves {S1_MIN_MOVE*100:.1f}%+ in first 15 min of session")
    print(f"  Stop: {S1_STOP_PCT*100:.1f}%  Trail: {S1_TRAIL_PCT*100:.1f}%  Max hold: 2h")

    s1 = backtest_session_breakout(candles, args.days)
    print_summary(s1)
    if s1:
        all_stats.append(s1)
        print_hour_wr(s1["results"])

    # Sweep min_move
    print("\n  Min-move sweep:")
    for mm in [0.002, 0.003, 0.004, 0.005, 0.007]:
        r = backtest_session_breakout(candles, args.days, min_move=mm)
        if r:
            print(f"    move>={mm*100:.1f}%  n={r['trades']:3d}  "
                  f"WR={r['win_rate']*100:5.1f}%  "
                  f"Daily=${r['daily_pnl']:>7.2f}  "
                  f"Monthly=${r['daily_pnl']*30:>8,.0f}")

    # ── Strategy 2: 4H EMA Crossover ────────────────────────────────────────
    print_header("STRATEGY 2: 4H EMA CROSSOVER  (trend following)")
    print(f"  Signal: EMA({S2_FAST_EMA}) crosses EMA({S2_SLOW_EMA}) on 4H chart")
    print(f"  Stop: {S2_STOP_PCT*100:.1f}%  Trail: {S2_TRAIL_PCT*100:.1f}%  Max hold: 48h")

    s2 = backtest_4h_ema(candles, args.days)
    print_summary(s2)
    if s2:
        all_stats.append(s2)

    # Sweep EMA periods
    print("\n  EMA period sweep:")
    for fast, slow in [(5, 13), (8, 21), (10, 30), (12, 26)]:
        r = backtest_4h_ema(candles, args.days, fast=fast, slow=slow)
        if r:
            print(f"    EMA({fast}/{slow})  n={r['trades']:3d}  "
                  f"WR={r['win_rate']*100:5.1f}%  "
                  f"Daily=${r['daily_pnl']:>7.2f}  "
                  f"Monthly=${r['daily_pnl']*30:>8,.0f}")

    # ── Strategy 3: Large Move Momentum ─────────────────────────────────────
    print_header("STRATEGY 3: LARGE MOVE MOMENTUM  (explosive breakouts)")
    print(f"  Signal: BTC moves {S3_MIN_MOVE*100:.1f}%+ in any 30-min window")
    print(f"  Stop: {S3_STOP_PCT*100:.1f}%  Trail: {S3_TRAIL_PCT*100:.1f}%  Max hold: 4h")

    s3 = backtest_large_move(candles, args.days)
    print_summary(s3)
    if s3:
        all_stats.append(s3)

    # Sweep min_move
    print("\n  Min-move sweep:")
    for mm in [0.005, 0.007, 0.010, 0.015, 0.020]:
        r = backtest_large_move(candles, args.days, min_move=mm)
        if r:
            print(f"    move>={mm*100:.1f}%  n={r['trades']:3d}  "
                  f"WR={r['win_rate']*100:5.1f}%  "
                  f"Daily=${r['daily_pnl']:>7.2f}  "
                  f"Monthly=${r['daily_pnl']*30:>8,.0f}")

    # ── Head-to-head comparison ──────────────────────────────────────────────
    print_header("HEAD-TO-HEAD COMPARISON  ($1000 margin / 5x leverage)")
    notional = MARGIN_USD * LEVERAGE

    if all_stats:
        print(f"\n  {'Strategy':<30s}  {'WR':>6}  {'Trades/day':>10}  "
              f"{'Daily PnL':>10}  {'Monthly':>10}  {'Max DD':>10}")
        print("  " + "-" * 80)
        for s in sorted(all_stats, key=lambda x: -x["daily_pnl"]):
            print(f"  {s['label']:<30s}  "
                  f"{s['win_rate']*100:>5.1f}%  "
                  f"{s['trades_per_day']:>10.1f}  "
                  f"${s['daily_pnl']:>9.2f}  "
                  f"${s['daily_pnl']*30:>9,.0f}  "
                  f"${s['max_dd']:>9,.2f}")

        best = max(all_stats, key=lambda x: x["daily_pnl"])

        print(f"\n  Best strategy: {best['label']}")
        print(f"\n  Scaling projections for {best['label']}:")
        for size in [500, 1000, 2000, 5000, 10000]:
            scale = size / MARGIN_USD
            print(f"    ${size:>7} margin  {LEVERAGE}x  ->  "
                  f"~${best['daily_pnl']*30*scale:>10,.0f}/month")

    # ── Verdict ─────────────────────────────────────────────────────────────
    print_header("VERDICT")

    if not all_stats:
        print("\n  No strategies produced results. Try --days 90.")
        return

    winners = [s for s in all_stats if s["daily_pnl"] > 0 and s["win_rate"] >= 0.50]
    if winners:
        best = max(winners, key=lambda x: x["daily_pnl"])
        print(f"\n  [YES] DEPLOY: {best['label']}")
        print(f"  WR={best['win_rate']*100:.1f}%  "
              f"${best['daily_pnl']:.2f}/day  "
              f"~${best['daily_pnl']*30:,.0f}/month on ${MARGIN_USD}x{LEVERAGE}")
        print(f"\n  Rebuild hyperliquid_trader.py around this signal.")
    else:
        print("\n  [NO] None of the 3 strategies show clear edge on this data.")
        print("  Consider: larger dataset (--days 180), different signals,")
        print("  or trading a different asset (ETH, SOL) with more volatility.")


if __name__ == "__main__":
    main()
