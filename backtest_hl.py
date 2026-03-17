#!/usr/bin/env python3
"""
Backtest the Hyperliquid 5-minute momentum strategy.

Signal: BTC moves MIN_MOVE_PCT in first ENTRY_SECS of a 5-min window.
Trade:  Enter in that direction, exit at EXIT_SECS (or trailing stop).

This mirrors the Polymarket btc-updown-5m signal but executed as a
perpetual futures trade on Hyperliquid with real fees + trailing stops.

Usage:
    python backtest_hl.py              # 60 days default
    python backtest_hl.py --days 90
"""

import requests
import time
import math
import sys
import argparse
from datetime import datetime, timezone
from collections import defaultdict

# ── Strategy constants ──────────────────────────────────────────────────────
LEVERAGE      = 5
MARGIN_USD    = 1000.0
WINDOW_SECS   = 300     # 5-minute windows
ENTRY_SECS    = 120     # enter at 2 min into window
EXIT_SECS     = 240     # forced exit at 4 min (60s before window end)

STOP_LOSS_PCT = 0.003   # 0.3% hard stop  (tighter = less damage per loss)
BREAKEVEN_PCT = 0.002   # 0.2% move before stop moves to breakeven
TRAIL_PCT     = 0.002   # 0.2% trail below peak

MIN_MOVE_PCT  = 0.001   # 0.1% min BTC move to trigger entry (default)
TAKER_FEE     = 0.001   # 0.1% per side (Hyperliquid taker)


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


# ── Trade simulation ────────────────────────────────────────────────────────

def simulate_trade(candles, window_start, direction,
                   stop_loss_pct, breakeven_pct, trail_pct, exit_secs):
    """
    Simulate one trade on 1-min candles.
    Enter at ENTRY_SECS, exit at exit_secs or trailing stop.
    """
    entry_ts   = window_start + ENTRY_SECS
    close_ts   = window_start + exit_secs

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
            peak  = max(peak, c["high"])
            trail = peak * (1 - trail_pct)
            if trail > stop:
                stop = trail
        else:
            if c["high"] >= stop:
                exit_price  = stop
                exit_reason = "trailing_stop" if stop <= entry_price else "stop_loss"
                break
            if c["low"] <= entry_price * (1 - breakeven_pct) and stop > entry_price:
                stop = entry_price
            peak  = min(peak, c["low"])
            trail = peak * (1 + trail_pct)
            if trail < stop:
                stop = trail

        ts += 60

    if exit_price is None:
        last_c = candles.get(close_ts) or candles.get(close_ts - 60)
        if not last_c:
            return None
        exit_price  = last_c["close"]
        exit_reason = "time_exit"

    if is_long:
        pnl_usd = (exit_price - entry_price) / entry_price * notional - fees
    else:
        pnl_usd = (entry_price - exit_price) / entry_price * notional - fees

    return {
        "entry":       entry_price,
        "exit":        exit_price,
        "exit_reason": exit_reason,
        "pnl_usd":     round(pnl_usd, 2),
        "won":         pnl_usd > 0,
    }


# ── Backtest ────────────────────────────────────────────────────────────────

def backtest(candles, min_move_pct=MIN_MOVE_PCT,
             stop_loss_pct=STOP_LOSS_PCT, breakeven_pct=BREAKEVEN_PCT,
             trail_pct=TRAIL_PCT, exit_secs=EXIT_SECS):

    ts_list  = sorted(candles.keys())
    if not ts_list:
        return []

    start_ts = ts_list[0]
    end_ts   = ts_list[-1]
    results  = []

    w = (start_ts // WINDOW_SECS) * WINDOW_SECS
    while w + WINDOW_SECS <= end_ts:
        open_c  = candles.get(w)
        entry_c = candles.get(w + ENTRY_SECS)

        if open_c and entry_c:
            btc_open  = open_c["open"]
            btc_entry = entry_c["close"]
            move_pct  = (btc_entry - btc_open) / btc_open

            if abs(move_pct) >= min_move_pct:
                direction = "Long" if move_pct > 0 else "Short"
                trade = simulate_trade(
                    candles, w, direction,
                    stop_loss_pct, breakeven_pct, trail_pct, exit_secs
                )
                if trade:
                    results.append({
                        "time":        datetime.fromtimestamp(w, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        "direction":   direction,
                        "move_pct":    round(move_pct * 100, 4),
                        "exit_reason": trade["exit_reason"],
                        "entry":       round(trade["entry"], 2),
                        "exit":        round(trade["exit"], 2),
                        "pnl_usd":     trade["pnl_usd"],
                        "won":         trade["won"],
                    })

        w += WINDOW_SECS

    return results


# ── Stats ───────────────────────────────────────────────────────────────────

def summarise(results, days):
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
        "trades":         n,
        "win_rate":       wins / n,
        "total_pnl":      total_pnl,
        "avg_pnl":        total_pnl / n,
        "daily_pnl":      total_pnl / days,
        "trades_per_day": n / days,
        "max_dd":         max_dd,
        "exit_reasons":   dict(reasons),
    }


def print_row(label, s):
    if s is None:
        print(f"  {label}: no trades")
        return
    print(
        f"  {label:50s}"
        f"  n={s['trades']:5d}"
        f"  WR={s['win_rate']*100:5.1f}%"
        f"  Avg=${s['avg_pnl']:>7.2f}"
        f"  /day={s['trades_per_day']:5.1f}"
        f"  Daily=${s['daily_pnl']:>8.2f}"
        f"  DD=${s['max_dd']:>9.2f}"
    )


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()

    print("=" * 100)
    print("  Hyperliquid 5-Min Momentum Backtester")
    print(f"  ${MARGIN_USD} margin  {LEVERAGE}x leverage  "
          f"entry={ENTRY_SECS}s  exit={EXIT_SECS}s  "
          f"stop={STOP_LOSS_PCT*100:.1f}%  trail={TRAIL_PCT*100:.1f}%")
    print("=" * 100)
    print(f"\nFetching {args.days} days of 1-min BTC/USDT from Binance...")

    candles = fetch_btc_1min(days=args.days)
    if not candles:
        print("ERROR: no data")
        sys.exit(1)

    ts_sorted = sorted(candles.keys())
    start_dt  = datetime.fromtimestamp(ts_sorted[0],  tz=timezone.utc)
    end_dt    = datetime.fromtimestamp(ts_sorted[-1], tz=timezone.utc)
    print(f"  Range: {start_dt:%Y-%m-%d} to {end_dt:%Y-%m-%d}\n")

    # ── Section 1: Min-move threshold ──────────────────────────────────────
    print("=" * 100)
    print("  SECTION 1: MIN-MOVE THRESHOLD  (how big a move do we need to enter?)")
    print("=" * 100)

    best_s, best_r, best_move = None, None, None
    for min_move in [0.0005, 0.001, 0.0015, 0.002, 0.003, 0.004, 0.005]:
        r = backtest(candles, min_move_pct=min_move)
        s = summarise(r, args.days)
        label = f"min_move={min_move*100:.2f}%"
        print_row(label, s)
        if s and (best_s is None or s["daily_pnl"] > best_s["daily_pnl"]):
            best_s, best_r, best_move = s, r, min_move

    # ── Section 2: Stop loss sweep ──────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print(f"  SECTION 2: STOP LOSS SWEEP  (min_move={best_move*100:.2f}%)")
    print("=" * 100)

    best2_s, best2_r, best2_stop = None, None, None
    for sl in [0.001, 0.002, 0.003, 0.004, 0.005, 0.007, 0.010]:
        r = backtest(candles, min_move_pct=best_move, stop_loss_pct=sl,
                     trail_pct=sl * 0.6)
        s = summarise(r, args.days)
        label = f"stop={sl*100:.1f}%  trail={sl*0.6*100:.2f}%"
        print_row(label, s)
        if s and (best2_s is None or s["daily_pnl"] > best2_s["daily_pnl"]):
            best2_s, best2_r, best2_stop = s, r, sl

    # ── Section 3: Exit timing sweep ────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print(f"  SECTION 3: EXIT TIMING  (how long to hold?)")
    print("=" * 100)

    for exit_secs in [60, 120, 180, 240, 300]:
        hold = exit_secs - ENTRY_SECS
        r = backtest(candles, min_move_pct=best_move,
                     stop_loss_pct=best2_stop, trail_pct=best2_stop * 0.6,
                     exit_secs=min(exit_secs, WINDOW_SECS - 60))
        s = summarise(r, args.days)
        label = f"hold={hold}s  exit_at={exit_secs}s"
        print_row(label, s)

    # ── Section 4: Deep dive on best config ─────────────────────────────────
    if best2_r:
        print(f"\n{'=' * 100}")
        print(f"  SECTION 4: BEST CONFIG DEEP-DIVE")
        print("=" * 100)

        s = best2_s
        notional = MARGIN_USD * LEVERAGE
        print(f"\n  min_move={best_move*100:.2f}%  stop={best2_stop*100:.1f}%  "
              f"entry={ENTRY_SECS}s  exit={EXIT_SECS}s")
        print(f"  Margin: ${MARGIN_USD}  Leverage: {LEVERAGE}x  Notional: ${notional:.0f}")
        print(f"\n  Trades:          {s['trades']}")
        print(f"  Win rate:        {s['win_rate']*100:.1f}%")
        print(f"  Avg PnL/trade:   ${s['avg_pnl']:.2f}")
        print(f"  Trades/day:      {s['trades_per_day']:.1f}")
        print(f"  Daily PnL:       ${s['daily_pnl']:.2f}")
        print(f"  Total PnL:       ${s['total_pnl']:.2f}  ({args.days} days)")
        print(f"  Max drawdown:    ${s['max_dd']:.2f}")
        print(f"  Monthly est:     ${s['daily_pnl'] * 30:.0f}")

        reasons = s["exit_reasons"]
        total   = sum(reasons.values())
        print(f"\n  Exit reasons:")
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {k:20s}  {v:5d}  ({v/total*100:.0f}%)")

        print("\n  Win rate by BTC move at entry:")
        buckets = defaultdict(list)
        for r in best2_r:
            bucket = round(abs(r["move_pct"]) * 10) / 10
            buckets[bucket].append(r["won"])
        for move in sorted(buckets):
            wl  = buckets[move]
            wr  = sum(wl) / len(wl)
            bar = "#" * int(wr * 20)
            print(f"    {move:.1f}%  {bar:<20s}  {wr*100:5.1f}%  (n={len(wl)})")

        print("\n  Win rate by hour of day (UTC):")
        hour_buckets = defaultdict(list)
        for r in best2_r:
            hour = int(r["time"].split(" ")[1].split(":")[0])
            hour_buckets[hour].append(r["won"])
        for h in sorted(hour_buckets):
            wl  = hour_buckets[h]
            wr  = sum(wl) / len(wl)
            bar = "#" * int(wr * 20)
            print(f"    {h:02d}:00  {bar:<20s}  {wr*100:5.1f}%  (n={len(wl)})")

        # Monthly projections
        daily   = s["daily_pnl"]
        monthly = daily * 30
        print(f"\n  Monthly projections (best config):")
        for size in [500, 1000, 2000, 5000]:
            scale = size / MARGIN_USD
            print(f"    ${size:>6} margin  {LEVERAGE}x  ->  ~${monthly*scale:>8,.0f}/month")
        for lev in [3, 5, 10]:
            scale = lev / LEVERAGE
            print(f"    $1000 margin  {lev:2d}x       ->  ~${monthly*scale:>8,.0f}/month  "
                  f"(max loss/trade ${MARGIN_USD*lev*best2_stop:.0f})")

    # ── Verdict ─────────────────────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print("  VERDICT")
    print("=" * 100)

    if not best2_s:
        print("\n  Not enough data.")
        return

    wr = best2_s["win_rate"]
    if wr >= 0.60 and best2_s["daily_pnl"] > 0:
        print(f"\n  [YES] STRATEGY HAS EDGE  ({wr*100:.1f}% WR, "
              f"${best2_s['daily_pnl']:.2f}/day, "
              f"${best2_s['daily_pnl']*30:.0f}/month)")
    elif wr >= 0.52:
        print(f"\n  [~] MARGINAL  ({wr*100:.1f}% WR, ${best2_s['daily_pnl']:.2f}/day)")
    else:
        print(f"\n  [NO] NO EDGE  ({wr*100:.1f}% WR)")


if __name__ == "__main__":
    main()
