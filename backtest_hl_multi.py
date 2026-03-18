#!/usr/bin/env python3
"""
Multi-pair Hyperliquid stat arb backtest — maximize returns at $20k capital.

Tests BTC/ETH stat arb plus additional correlated pairs:
  - BTC/ETH  (primary, proven)
  - BTC/SOL
  - ETH/SOL

Answers the question: what's the optimal capital split across pairs
to target $10k/month at $20k margin with 5x leverage?

Usage:
    python backtest_hl_multi.py            # 90 days
    python backtest_hl_multi.py --days 180
"""

import requests
import time
import math
import sys
import argparse
from datetime import datetime, timezone

# ── Constants ─────────────────────────────────────────────────────────────────

LEVERAGE    = 5
TAKER_FEE   = 0.0005
MAKER_FEE   = -0.0002   # rebate

ZSCORE_PERIOD  = 60    # 60-minute rolling window
ENTRY_Z        = 2.5
EXIT_Z         = 0.5
STOP_LOSS_PCT  = 0.012
TRAIL_PCT      = 0.008
BREAKEVEN_PCT  = 0.006
MAX_HOLD_MINS  = 1440  # 24 hours
COOLDOWN_MINS  = 240   # 4 hours


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_1min(symbol, days=90):
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    url      = "https://api.binance.com/api/v3/klines"
    candles  = {}
    cur      = start_ms

    while cur < end_ms:
        try:
            r = requests.get(url, params={
                "symbol": symbol, "interval": "1m",
                "startTime": cur, "endTime": end_ms, "limit": 1000,
            }, timeout=15)
            data = r.json()
        except Exception as e:
            print(f"\n  Fetch error ({symbol}): {e}")
            time.sleep(2)
            continue
        if not isinstance(data, list) or not data:
            break
        for k in data:
            ts = k[0] // 1000
            candles[ts] = {"close": float(k[4]), "volume": float(k[5])}
        cur = data[-1][0] + 60000
        print(f"\r  {symbol}: {len(candles):,} candles...", end="", flush=True)
        time.sleep(0.05)

    print(f"\r  {symbol}: {len(candles):,} candles        ")
    return candles


# ── Stat arb backtest for one pair ────────────────────────────────────────────

def backtest_pair(candles_a, candles_b, pair_name, margin_usd,
                  use_maker=True, entry_z=ENTRY_Z, cooldown_mins=COOLDOWN_MINS):
    """
    Backtest BTC/ETH-style stat arb on any two price series.
    candles_a is the LONG/SHORT asset (BTC), candles_b is the ratio denominator (ETH).
    Returns dict of results.
    """
    notional   = margin_usd * LEVERAGE
    # fee_rt = net fee impact on PnL (positive = income, negative = cost)
    # MAKER_FEE = -0.0002 (rebate), TAKER_FEE = +0.0005 (cost)
    # Negate so MAKER gives +income and TAKER gives -cost
    fee_rt     = -(2 * (MAKER_FEE if use_maker else TAKER_FEE) * notional)
    fee_label  = "maker" if use_maker else "taker"

    # Align timestamps
    all_ts = sorted(set(candles_a) & set(candles_b))
    if len(all_ts) < ZSCORE_PERIOD + 10:
        return None

    ratios      = []
    positions   = []
    in_pos      = False
    last_trade  = -1e9
    pos_entry   = 0.0
    pos_dir     = 0           # +1 long, -1 short
    pos_open_ts = 0
    peak_price  = 0.0
    stop_price  = 0.0
    be_moved    = False
    collected   = 0.0

    for i, ts in enumerate(all_ts):
        a = candles_a[ts]["close"]
        b = candles_b[ts]["close"]
        ratio = a / b
        ratios.append(ratio)

        if len(ratios) < ZSCORE_PERIOD:
            continue

        window = ratios[-ZSCORE_PERIOD:]
        mean   = sum(window) / len(window)
        std    = math.sqrt(sum((x - mean)**2 for x in window) / len(window))
        z      = (window[-1] - mean) / std if std > 0 else 0.0

        mins_since_trade = (ts - last_trade) / 60

        if not in_pos:
            if mins_since_trade < cooldown_mins:
                continue
            if z > entry_z:
                # Short A (overvalued vs B)
                in_pos      = True
                pos_entry   = a
                pos_dir     = -1
                pos_open_ts = ts
                peak_price  = a
                stop_price  = a * (1 + STOP_LOSS_PCT)
                be_moved    = False
            elif z < -entry_z:
                # Long A (undervalued vs B)
                in_pos      = True
                pos_entry   = a
                pos_dir     = +1
                pos_open_ts = ts
                peak_price  = a
                stop_price  = a * (1 - STOP_LOSS_PCT)
                be_moved    = False
        else:
            held_mins = (ts - pos_open_ts) / 60

            # Update trailing stop
            if pos_dir == 1:
                if a > peak_price:
                    peak_price = a
                    trail      = peak_price * (1 - TRAIL_PCT)
                    if trail > stop_price:
                        stop_price = trail
                if not be_moved and a >= pos_entry * (1 + BREAKEVEN_PCT):
                    stop_price = pos_entry
                    be_moved   = True
            else:
                if a < peak_price:
                    peak_price = a
                    trail      = peak_price * (1 + TRAIL_PCT)
                    if trail < stop_price:
                        stop_price = trail
                if not be_moved and a <= pos_entry * (1 - BREAKEVEN_PCT):
                    stop_price = pos_entry
                    be_moved   = True

            # Exit conditions
            stop_hit  = (pos_dir == 1 and a <= stop_price) or \
                        (pos_dir == -1 and a >= stop_price)
            z_exit    = abs(z) < EXIT_Z
            time_exit = held_mins >= MAX_HOLD_MINS

            if stop_hit or z_exit or time_exit:
                pnl_raw = pos_dir * (a - pos_entry) / pos_entry * notional
                pnl     = pnl_raw + fee_rt   # fee_rt already accounts for maker vs taker

                reason  = "stop" if stop_hit else ("z_exit" if z_exit else "time")
                positions.append({
                    "open_ts":  pos_open_ts,
                    "close_ts": ts,
                    "dir":      "Long" if pos_dir == 1 else "Short",
                    "entry":    pos_entry,
                    "exit":     a,
                    "pnl":      round(pnl, 2),
                    "z_entry":  round(z, 3),
                    "won":      pnl > 0,
                    "held_h":   round(held_mins / 60, 1),
                    "reason":   reason,
                })
                in_pos     = False
                last_trade = ts

    if not positions:
        return None

    total_days = (all_ts[-1] - all_ts[0]) / 86400
    total_pnl  = sum(p["pnl"] for p in positions)
    wins       = sum(1 for p in positions if p["won"])
    daily      = total_pnl / total_days
    monthly    = daily * 30

    return {
        "pair":       pair_name,
        "fee_mode":   fee_label,
        "margin":     margin_usd,
        "notional":   notional,
        "n":          len(positions),
        "wins":       wins,
        "wr":         wins / len(positions),
        "total_pnl":  round(total_pnl, 2),
        "daily":      round(daily, 2),
        "monthly":    round(monthly, 2),
        "trades_day": round(len(positions) / total_days, 1),
        "days":       round(total_days, 0),
        "positions":  positions,
        "roi_margin": round(total_pnl / margin_usd * 100, 1),
    }


# ── Drawdown calculator ───────────────────────────────────────────────────────

def max_drawdown(positions):
    equity = 0.0
    peak   = 0.0
    max_dd = 0.0
    for p in sorted(positions, key=lambda x: x["close_ts"]):
        equity += p["pnl"]
        peak    = max(peak, equity)
        max_dd  = max(max_dd, peak - equity)
    return round(max_dd, 2)


# ── Print result ──────────────────────────────────────────────────────────────

def print_result(r, indent=""):
    print(f"{indent}Pair: {r['pair']:12s}  Margin: ${r['margin']:,.0f}  "
          f"Notional: ${r['notional']:,.0f}  ({r['fee_mode']})")
    print(f"{indent}  Trades: {r['n']:3d}  ({r['trades_day']:.1f}/day)  "
          f"WR: {r['wr']*100:.1f}%  "
          f"Days: {r['days']:.0f}")
    print(f"{indent}  Total PnL: ${r['total_pnl']:,.2f}  "
          f"Monthly: ${r['monthly']:,.0f}  "
          f"Daily: ${r['daily']:.2f}")
    print(f"{indent}  Max DD: ${max_drawdown(r['positions']):,.2f}  "
          f"ROI on margin: {r['roi_margin']:.1f}%")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    print("=" * 80)
    print("  Multi-Pair Hyperliquid Stat Arb — $20k Capital Target $10k/month")
    print("=" * 80)

    # ── Fetch price data ─────────────────────────────────────────────────────
    print(f"\nFetching {args.days}-day 1-min candles from Binance...")
    btc = fetch_1min("BTCUSDT", args.days)
    eth = fetch_1min("ETHUSDT", args.days)
    sol = fetch_1min("SOLUSDT", args.days)
    print(f"  BTC: {len(btc):,}  ETH: {len(eth):,}  SOL: {len(sol):,} candles\n")

    # ── Single-pair analysis at $1k margin (baseline) ────────────────────────
    print("=" * 80)
    print("  BASELINE: Single pair at $1,000 margin x 5x (for reference)")
    print("=" * 80)

    pairs_1k = [
        ("BTC/ETH", btc, eth),
        ("BTC/SOL", btc, sol),
        ("ETH/SOL", eth, sol),
    ]
    baseline = {}
    for name, a, b in pairs_1k:
        for use_maker in [True, False]:
            r = backtest_pair(a, b, name, 1000, use_maker=use_maker)
            if r:
                label = f"{name}_{'maker' if use_maker else 'taker'}"
                baseline[label] = r
                mode = "maker (+rebate)" if use_maker else "taker"
                print(f"\n  {name}  [{mode}]:")
                print_result(r, "  ")

    # ── Z-score threshold sweep for best pair ────────────────────────────────
    print(f"\n{'='*80}")
    print("  Z-SCORE SWEEP: BTC/ETH maker (find optimal entry threshold)")
    print("="*80)
    best_z = 2.5  # fallback
    best_monthly = float("-inf")
    for z in [1.5, 2.0, 2.5, 3.0, 3.5]:
        r = backtest_pair(btc, eth, "BTC/ETH", 1000, use_maker=True, entry_z=z)
        if r:
            marker = " <-- BEST" if r["monthly"] > best_monthly else ""
            if r["monthly"] > best_monthly:
                best_monthly = r["monthly"]
                best_z = z
            print(f"  Z>{z:.1f}:  trades={r['n']:3d} ({r['trades_day']:.1f}/day)  "
                  f"WR={r['wr']*100:.1f}%  monthly=${r['monthly']:,.0f}{marker}")

    # ── Cooldown sweep ────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  COOLDOWN SWEEP: BTC/ETH maker, Z>{best_z:.1f}")
    print("="*80)
    best_cd = 240  # fallback
    best_cd_monthly = float("-inf")
    for cd in [60, 120, 240, 480]:
        r = backtest_pair(btc, eth, "BTC/ETH", 1000, use_maker=True,
                          entry_z=best_z, cooldown_mins=cd)
        if r:
            marker = " <-- BEST" if r["monthly"] > best_cd_monthly else ""
            if r["monthly"] > best_cd_monthly:
                best_cd_monthly = r["monthly"]
                best_cd = cd
            print(f"  CD={cd:3d}m:  trades={r['n']:3d} ({r['trades_day']:.1f}/day)  "
                  f"WR={r['wr']*100:.1f}%  monthly=${r['monthly']:,.0f}{marker}")

    # ── Capital allocation across pairs at $20k ───────────────────────────────
    print(f"\n{'='*80}")
    print("  $20k CAPITAL ALLOCATION — BEST Z + COOLDOWN")
    print(f"  (Z>{best_z:.1f}, CD={best_cd}min, maker orders)")
    print("="*80)

    # Strategy A: All-in on BTC/ETH
    alloc_a = [("BTC/ETH", btc, eth, 20000)]

    # Strategy B: Split BTC/ETH + BTC/SOL
    alloc_b = [("BTC/ETH", btc, eth, 10000), ("BTC/SOL", btc, sol, 10000)]

    # Strategy C: Three-way split
    alloc_c = [
        ("BTC/ETH", btc, eth, 7000),
        ("BTC/SOL", btc, sol, 7000),
        ("ETH/SOL", eth, sol, 6000),
    ]

    for label, alloc in [("A: All-in BTC/ETH", alloc_a),
                         ("B: BTC/ETH + BTC/SOL", alloc_b),
                         ("C: Three-way split", alloc_c)]:
        print(f"\n  Strategy {label}:")
        total_monthly = 0
        total_trades  = 0
        total_dd      = 0
        for name, a, b, margin in alloc:
            r = backtest_pair(a, b, name, margin, use_maker=True,
                              entry_z=best_z, cooldown_mins=best_cd)
            if r:
                print_result(r, "    ")
                total_monthly += r["monthly"]
                total_trades  += r["trades_day"]
                total_dd      = max(total_dd, max_drawdown(r["positions"]) * (margin/1000))
        print(f"    {'-'*50}")
        print(f"    TOTAL: ${total_monthly:,.0f}/month  "
              f"{total_trades:.1f} trades/day  "
              f"Max DD est: ${total_dd:,.0f}")

    # ── Compounding projection ────────────────────────────────────────────────
    r_best = backtest_pair(btc, eth, "BTC/ETH", 20000, use_maker=True,
                           entry_z=best_z, cooldown_mins=best_cd)
    if r_best:
        monthly_roi = r_best["monthly"] / 20000

        print(f"\n{'='*80}")
        print(f"  COMPOUNDING PROJECTION — BTC/ETH at $20k (best config)")
        print(f"  Monthly ROI on margin: {monthly_roi*100:.1f}%")
        print("="*80)
        equity = 20000.0
        print(f"\n  Month  Capital       Monthly PnL   Cumulative")
        print(f"  {'-'*52}")
        for month in range(1, 13):
            notional   = equity * LEVERAGE
            monthly_pnl = notional * (r_best["monthly"] / r_best["notional"])
            equity     += monthly_pnl
            print(f"  {month:5d}  ${equity-monthly_pnl:>10,.0f}  "
                  f"+${monthly_pnl:>9,.0f}   ${equity:>10,.0f}")

    # ── Risk analysis ─────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("  RISK ANALYSIS")
    print("="*80)
    r_base = backtest_pair(btc, eth, "BTC/ETH", 20000, use_maker=True,
                           entry_z=best_z, cooldown_mins=best_cd)
    if r_base:
        positions = r_base["positions"]
        dd        = max_drawdown(positions)
        stops     = sum(1 for p in positions if p["reason"] == "stop")
        z_exits   = sum(1 for p in positions if p["reason"] == "z_exit")
        time_outs = sum(1 for p in positions if p["reason"] == "time")
        losses    = [p["pnl"] for p in positions if not p["won"]]
        wins_pnl  = [p["pnl"] for p in positions if p["won"]]

        print(f"\n  Max drawdown (actual):  ${dd:,.2f}")
        print(f"  Max drawdown / capital: {dd/20000*100:.1f}%")
        print(f"\n  Exit reasons:  z_exit={z_exits}  stop={stops}  timeout={time_outs}")
        print(f"\n  Avg win:   +${sum(wins_pnl)/len(wins_pnl) if wins_pnl else 0:,.2f}")
        print(f"  Avg loss:  -${abs(sum(losses)/len(losses)) if losses else 0:,.2f}")
        if wins_pnl and losses:
            rr = (sum(wins_pnl)/len(wins_pnl)) / (abs(sum(losses)/len(losses)))
            print(f"  Win/loss ratio: {rr:.2f}x")

        # Consecutive loss analysis
        consec = 0
        max_consec = 0
        for p in sorted(positions, key=lambda x: x["close_ts"]):
            if not p["won"]:
                consec += 1
                max_consec = max(max_consec, consec)
            else:
                consec = 0
        print(f"\n  Max consecutive losses: {max_consec}")
        print(f"  Capital at risk per trade: ${20000 * LEVERAGE * STOP_LOSS_PCT:,.0f} "
              f"({STOP_LOSS_PCT*100:.1f}% of notional)")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("  VERDICT — path to $10k/month")
    print("="*80)
    if r_best:
        m = r_best["monthly"]
        print(f"\n  BTC/ETH stat arb at $20k x 5x projects ${m:,.0f}/month")
        if m >= 10000:
            print(f"  [YES] Target $10k/month is achievable with current strategy.")
        elif m >= 7000:
            print(f"  [CLOSE] ${m:,.0f}/month at $20k. Need more pairs or tighter params.")
        else:
            print(f"  [NO] Current single-pair can't reach $10k. Need multi-pair.")

        print(f"\n  Recommended approach:")
        print(f"  1. Paper trade until 20+ trades validate backtest WR ({r_best['wr']*100:.1f}%)")
        print(f"  2. Start live at $1k, verify fills and maker rebate rate")
        print(f"  3. Scale to $5k once WR matches (should see ~${r_best['monthly']/4:,.0f}/month)")
        print(f"  4. Scale to $20k for full ${r_best['monthly']:,.0f}/month target")
        print(f"  5. Enable multi-pair (SOL) to add frequency and reduce single-pair risk")
        print(f"\n  Key risk: maker rebate requires limit orders to fill.")
        print(f"  If 50% fall back to taker, monthly drops ~${r_best['monthly']*0.35:,.0f}")
        print(f"  Live test: watch 'exit_reason' stats — want >70% z_exit, <20% stop")


if __name__ == "__main__":
    main()
