#!/usr/bin/env python3
"""
Backtest Hyperliquid BTC funding rate arbitrage.

Strategy: Delta-neutral funding collection.
  - When BTC funding rate > ENTER_THRESHOLD, open SHORT on HL.
  - Assume a matching LONG spot hedge (Coinbase/Kraken) so BTC price moves cancel out.
  - Collect hourly funding payments from HL (longs pay shorts when rate > 0).
  - Close position when funding rate drops below EXIT_THRESHOLD.

Revenue: funding payments received each hour.
Cost:    HL open fee + HL close fee + spot hedge open/close fees.
Risk:    Funding rate goes negative (you start paying) before you exit.

Usage:
    python backtest_funding_arb.py          # use all available history
    python backtest_funding_arb.py --days 60
"""

import requests
import time
import sys
import argparse
from datetime import datetime, timezone
from collections import defaultdict

# ── Constants ────────────────────────────────────────────────────────────────
MARGIN_USD       = 1000.0
LEVERAGE         = 5
NOTIONAL         = MARGIN_USD * LEVERAGE   # $5,000

HL_TAKER_FEE     = 0.0005   # 0.05% per side (HL taker)
SPOT_FEE         = 0.001    # 0.10% per side (Coinbase/Kraken spot)
ROUND_TRIP_COST  = (HL_TAKER_FEE + SPOT_FEE) * 2 * NOTIONAL  # open+close both legs

# Entry/exit thresholds (funding rate per hour)
ENTER_THRESHOLD  = 0.00003  # 0.003%/hr = ~0.26%/day on notional (annualized ~97%)
EXIT_THRESHOLD   = 0.000010 # close when rate drops near zero/minimum


# ── Fetch HL funding history (paginates automatically) ───────────────────────

def fetch_funding_history(days=90):
    """Fetch BTC hourly funding rates from Hyperliquid (paginates 500/call)."""
    url       = "https://api.hyperliquid.xyz/info"
    start_ms  = int((time.time() - days * 86400) * 1000)
    all_data  = []
    cur_start = start_ms

    while True:
        try:
            r = requests.post(url, json={
                "type": "fundingHistory",
                "coin": "BTC",
                "startTime": cur_start,
            }, timeout=15)
            batch = r.json()
        except Exception as e:
            print(f"  Fetch error: {e}")
            break

        if not batch:
            break

        all_data.extend(batch)
        last_ts = batch[-1]["time"]
        print(f"\r  Fetched {len(all_data):,} funding entries...", end="", flush=True)

        if len(batch) < 500:
            break                   # last page
        cur_start = last_ts + 1
        time.sleep(0.1)

    print(f"\r  Fetched {len(all_data):,} funding entries        ")
    return all_data


# ── Backtest ─────────────────────────────────────────────────────────────────

def backtest(funding_data,
             enter_threshold=ENTER_THRESHOLD,
             exit_threshold=EXIT_THRESHOLD):
    """
    Simulate funding arb over all available history.
    Returns list of completed positions.
    """
    positions = []
    in_pos    = False
    pos_start = None
    pos_start_rate = None
    collected = 0.0
    entry_cost = (HL_TAKER_FEE + SPOT_FEE) * NOTIONAL  # one-way cost

    for entry in funding_data:
        rate = float(entry["fundingRate"])
        ts   = entry["time"] / 1000
        dt   = datetime.fromtimestamp(ts, tz=timezone.utc)

        if not in_pos:
            if rate >= enter_threshold:
                # Open position
                in_pos         = True
                pos_start      = ts
                pos_start_dt   = dt
                pos_start_rate = rate
                collected      = -entry_cost   # pay open fee immediately
        else:
            # Collect this hour's funding
            collected += rate * NOTIONAL

            # Check exit
            if rate < exit_threshold:
                # Close position
                collected -= entry_cost  # pay close fee
                duration_h = (ts - pos_start) / 3600
                positions.append({
                    "open_dt":    pos_start_dt.strftime("%Y-%m-%d %H:%M"),
                    "close_dt":   dt.strftime("%Y-%m-%d %H:%M"),
                    "duration_h": round(duration_h, 1),
                    "entry_rate": round(pos_start_rate * 100, 5),
                    "exit_rate":  round(rate * 100, 5),
                    "pnl_usd":    round(collected, 2),
                    "won":        collected > 0,
                })
                in_pos    = False
                collected = 0.0

    # Close any open position at end of data
    if in_pos and collected != 0:
        collected -= entry_cost
        last = funding_data[-1]
        ts   = last["time"] / 1000
        dt   = datetime.fromtimestamp(ts, tz=timezone.utc)
        positions.append({
            "open_dt":    pos_start_dt.strftime("%Y-%m-%d %H:%M"),
            "close_dt":   dt.strftime("%Y-%m-%d %H:%M") + " (open)",
            "duration_h": round((ts - pos_start) / 3600, 1),
            "entry_rate": round(pos_start_rate * 100, 5),
            "exit_rate":  round(float(last["fundingRate"]) * 100, 5),
            "pnl_usd":    round(collected, 2),
            "won":        collected > 0,
        })

    return positions


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()

    print("=" * 80)
    print("  Hyperliquid BTC Funding Rate Arbitrage Backtester")
    print(f"  ${MARGIN_USD} margin  {LEVERAGE}x leverage  (${NOTIONAL:.0f} notional)")
    print(f"  Round-trip cost: ${ROUND_TRIP_COST:.2f}  (HL {HL_TAKER_FEE*100:.2f}% + spot {SPOT_FEE*100:.2f}% each side)")
    print("=" * 80)
    print(f"\nFetching BTC funding history from Hyperliquid (last {args.days} days)...")

    funding = fetch_funding_history(args.days)
    if not funding:
        print("ERROR: no data returned")
        sys.exit(1)

    # ── Overview of funding data ─────────────────────────────────────────────
    rates     = [float(f["fundingRate"]) for f in funding]
    start_dt  = datetime.fromtimestamp(funding[0]["time"]/1000,  tz=timezone.utc)
    end_dt    = datetime.fromtimestamp(funding[-1]["time"]/1000, tz=timezone.utc)
    days_actual = (funding[-1]["time"] - funding[0]["time"]) / 1000 / 86400

    print(f"\n  Period:   {start_dt:%Y-%m-%d} to {end_dt:%Y-%m-%d}  ({days_actual:.0f} days)")
    print(f"  Entries:  {len(rates):,} hourly payments")
    print(f"\n  Funding rate stats (per hour):")
    print(f"    Min:      {min(rates)*100:.5f}%  (annualized {min(rates)*8760*100:.1f}%)")
    print(f"    Max:      {max(rates)*100:.5f}%  (annualized {max(rates)*8760*100:.1f}%)")
    avg = sum(rates) / len(rates)
    print(f"    Average:  {avg*100:.5f}%  ({avg*24*100:.4f}%/day on notional)")
    print(f"    Positive: {sum(1 for r in rates if r > 0):,}/{len(rates):,}  "
          f"({sum(1 for r in rates if r > 0)/len(rates)*100:.1f}% of hours)")

    # Passive income if always short (no threshold)
    passive_pnl = sum(r * NOTIONAL for r in rates) - ROUND_TRIP_COST
    print(f"\n  If always SHORT (no filter):")
    print(f"    Total funding collected: ${sum(r*NOTIONAL for r in rates):.2f}")
    print(f"    Minus round-trip fees:   -${ROUND_TRIP_COST:.2f}")
    print(f"    Net PnL:                 ${passive_pnl:.2f}  over {days_actual:.0f} days")
    print(f"    Daily avg:               ${passive_pnl/days_actual:.2f}/day")
    print(f"    Monthly est:             ${passive_pnl/days_actual*30:,.0f}/month")

    # ── Threshold backtest ───────────────────────────────────────────────────
    print(f"\n{'=' * 80}")
    print("  THRESHOLD SWEEP  (enter only when rate exceeds threshold)")
    print("=" * 80)

    best_pnl    = float("-inf")
    best_thresh = None
    best_pos    = None

    for thresh in [0.000010, 0.000020, 0.000030, 0.000050, 0.000075, 0.000100]:
        pos = backtest(funding, enter_threshold=thresh)
        if not pos:
            print(f"  enter>={thresh*100:.4f}%/hr  —  no trades triggered")
            continue

        total_pnl   = sum(p["pnl_usd"] for p in pos)
        wins        = sum(1 for p in pos if p["won"])
        avg_dur     = sum(p["duration_h"] for p in pos) / len(pos)
        daily       = total_pnl / days_actual

        print(f"  enter>={thresh*100:.4f}%/hr  "
              f"positions={len(pos):2d}  "
              f"WR={wins/len(pos)*100:5.1f}%  "
              f"avg_hold={avg_dur:5.1f}h  "
              f"total=${total_pnl:>8.2f}  "
              f"daily=${daily:>6.2f}  "
              f"monthly=${daily*30:>8,.0f}")

        if total_pnl > best_pnl:
            best_pnl    = total_pnl
            best_thresh = thresh
            best_pos    = pos

    # ── Deep dive on best config ─────────────────────────────────────────────
    if best_pos:
        print(f"\n{'=' * 80}")
        print(f"  BEST CONFIG: enter >= {best_thresh*100:.4f}%/hr")
        print("=" * 80)

        total_pnl = sum(p["pnl_usd"] for p in best_pos)
        wins      = sum(1 for p in best_pos if p["won"])

        print(f"\n  Positions:       {len(best_pos)}")
        print(f"  Win rate:        {wins/len(best_pos)*100:.1f}%")
        print(f"  Total PnL:       ${total_pnl:.2f}  ({days_actual:.0f} days)")
        print(f"  Daily avg:       ${total_pnl/days_actual:.2f}")
        print(f"  Monthly est:     ${total_pnl/days_actual*30:,.0f}")

        print(f"\n  Individual positions:")
        print(f"  {'Opened':>17}  {'Closed':>22}  {'Hold':>6}  "
              f"{'Entry rate':>10}  {'PnL':>8}")
        print("  " + "-" * 75)
        for p in best_pos:
            sym = "+" if p["won"] else "-"
            print(f"  {p['open_dt']:>17}  {p['close_dt']:>22}  "
                  f"{p['duration_h']:>5.0f}h  "
                  f"{p['entry_rate']:>9.4f}%  "
                  f"${p['pnl_usd']:>7.2f} {sym}")

        print(f"\n  Scaling projections (monthly):")
        for size in [500, 1000, 2000, 5000, 10000]:
            scale = size / MARGIN_USD
            monthly = total_pnl / days_actual * 30 * scale
            print(f"    ${size:>7} margin  {LEVERAGE}x  ->  ~${monthly:>8,.0f}/month  "
                  f"(delta-neutral, low risk)")

    # ── Distribution of funding rates ────────────────────────────────────────
    print(f"\n{'=' * 80}")
    print("  FUNDING RATE DISTRIBUTION  (% of hours in each range)")
    print("=" * 80)
    buckets = defaultdict(int)
    for r in rates:
        if r < 0:
            bucket = "negative"
        elif r < 0.000010:
            bucket = "0.000-0.001%"
        elif r < 0.000020:
            bucket = "0.001-0.002%"
        elif r < 0.000030:
            bucket = "0.002-0.003%"
        elif r < 0.000050:
            bucket = "0.003-0.005%"
        elif r < 0.000100:
            bucket = "0.005-0.010%"
        else:
            bucket = ">0.010%"
        buckets[bucket] += 1

    order = ["negative", "0.000-0.001%", "0.001-0.002%", "0.002-0.003%",
             "0.003-0.005%", "0.005-0.010%", ">0.010%"]
    for b in order:
        n   = buckets[b]
        pct = n / len(rates) * 100
        bar = "#" * int(pct / 2)
        pnl_if_in = (ENTER_THRESHOLD + (ENTER_THRESHOLD * 1.5)) / 2 * NOTIONAL * n
        print(f"  {b:>14s}  {bar:<25s}  {pct:5.1f}%  ({n:4d}h)")

    # ── Verdict ──────────────────────────────────────────────────────────────
    print(f"\n{'=' * 80}")
    print("  VERDICT")
    print("=" * 80)

    daily_passive = passive_pnl / days_actual
    if daily_passive > 0:
        print(f"\n  [YES] FUNDING ARB HAS EDGE")
        print(f"  Even without filtering, always-short earns ${daily_passive:.2f}/day "
              f"(${daily_passive*30:,.0f}/month on ${MARGIN_USD}x{LEVERAGE})")
        print(f"\n  Key advantages:")
        print(f"  - Delta-neutral (BTC price moves don't matter if hedged on spot)")
        print(f"  - No directional predictions needed")
        print(f"  - Consistent income in bull markets (longs always pay)")
        print(f"  - Hyperliquid pays funding every HOUR (not every 8h)")
        print(f"\n  Requirements:")
        print(f"  - Short BTC on Hyperliquid (${MARGIN_USD} margin @ {LEVERAGE}x = ${NOTIONAL:.0f} notional)")
        print(f"  - Long same notional of BTC spot (Coinbase/Kraken) to stay delta-neutral")
        print(f"  - Monitor: exit if funding goes consistently negative")
    else:
        print(f"\n  [~] MARGINAL  funding has been negative this period")
        print(f"  This strategy works best in bull markets. Consider waiting.")


if __name__ == "__main__":
    main()
