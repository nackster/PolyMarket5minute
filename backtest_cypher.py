"""
Cypher Harmonic Pattern Backtest on BTC 5-minute data from Binance.

Pattern overview:
  Bullish W: X=low, A=high, B=low, C=high (C > A)
  Bearish M: X=high, A=low, B=high, C=low (C < A)

Usage:
  python backtest_cypher.py
  python backtest_cypher.py --days 60
  python backtest_cypher.py --interval 15m
  python backtest_cypher.py --capital 25000
  python backtest_cypher.py --leverage 5
  python backtest_cypher.py --sweep
"""

import argparse
import json
import os
import time
import math
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Default parameters
# ---------------------------------------------------------------------------
CAPITAL       = 10_000
LEVERAGE      = 3.0
TP1_SIZE      = 0.5        # fraction of position closed at TP1
TAKER_FEE    = 0.0005     # 0.05% taker fee
MAKER_FEE    = -0.0002    # maker rebate for limit orders
PIVOT_LENGTH  = 5

ENTRY_CR      = 0.786
MIN_B         = 0.382
MAX_B         = 0.618
BULL_MIN_C    = 1.272
BULL_MAX_C    = 1.414
BEAR_MIN_C    = 0.41      # lower bound (distance from A below X)
BEAR_MAX_C    = 0.13      # upper bound (distance from A below X)

TP1_RATIO     = 0.236
TP2_RATIO     = -0.236    # negative = beyond X

SWEEP_LEVERAGES = [1, 2, 3, 5]

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_klines(symbol: str, interval: str, days: int) -> list[dict]:
    """Fetch historical klines from Binance for the past `days` days."""
    ms_per_candle = interval_to_ms(interval)
    now_ms = int(time.time() * 1000)
    total_ms = days * 24 * 60 * 60 * 1000
    start_ms = now_ms - total_ms

    candles = []
    end_time = now_ms
    limit = 1000

    print(f"Fetching {days} days of {interval} {symbol} data from Binance...")

    while end_time > start_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
            "endTime": end_time,
        }
        try:
            resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"  Request error: {exc}. Retrying in 2s...")
            time.sleep(2)
            continue

        data = resp.json()
        if not data:
            break

        batch = []
        for row in data:
            open_time = int(row[0])
            if open_time < start_ms:
                continue
            batch.append({
                "timestamp": open_time,
                "open":  float(row[1]),
                "high":  float(row[2]),
                "low":   float(row[3]),
                "close": float(row[4]),
            })

        if not batch:
            break

        candles.extend(batch)
        # Next batch ends just before the oldest candle in this batch
        end_time = int(data[0][0]) - 1

        print(f"  Fetched {len(candles)} candles, oldest: "
              f"{datetime.fromtimestamp(int(data[0][0])/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")

        if len(data) < limit:
            break

        time.sleep(0.1)  # be gentle with the API

    # Sort ascending by timestamp
    candles.sort(key=lambda c: c["timestamp"])
    # Deduplicate
    seen = set()
    unique = []
    for c in candles:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)

    print(f"Total candles: {len(unique)}")
    return unique


def interval_to_ms(interval: str) -> int:
    unit = interval[-1]
    value = int(interval[:-1])
    if unit == "m":
        return value * 60 * 1000
    elif unit == "h":
        return value * 3600 * 1000
    elif unit == "d":
        return value * 86400 * 1000
    raise ValueError(f"Unknown interval: {interval}")


# ---------------------------------------------------------------------------
# Pivot detection — exact port of Pine Script ZigZag streaming logic
# ---------------------------------------------------------------------------

def detect_pivots(candles: list[dict], pivot_length: int) -> list[tuple]:
    """
    Streaming ZigZag pivot detection matching Pine Script logic.

    Returns list of (bar_index, price, type) where type=1 means pivot high, -1 means pivot low.

    At each bar i (0-based), we look at:
      highVal = max(high) over bars [i-pivot_length .. i-1]
      lowVal  = min(low)  over bars [i-pivot_length .. i-1]

    Trend 1=uptrend (looking for next pivot high), -1=downtrend (looking for next pivot low).
    """
    pivots = []  # list of [bar_index, price, type]

    n = len(candles)
    if n < pivot_length + 1:
        return pivots

    # Initialise: start after we have enough history for the rolling window
    # We start scanning from bar pivot_length onward
    # Use the first bar's close as seed
    trend = 1     # start looking for high
    pv    = candles[0]["high"]
    pv_idx = 0

    for i in range(1, n):
        # Rolling window: bars [i-pivot_length .. i-1] (pivot_length bars before current)
        window_start = max(0, i - pivot_length)
        window = candles[window_start:i]

        high_val = max(c["high"] for c in window)
        low_val  = min(c["low"]  for c in window)

        cur_high = candles[i]["high"]
        cur_low  = candles[i]["low"]

        hh = cur_high >= high_val  # current makes higher-high vs window
        ll = cur_low  <= low_val   # current makes lower-low vs window

        if trend == 1:
            if hh:
                # Still in uptrend, update pivot high candidate
                if cur_high >= pv:
                    pv = cur_high
                    pv_idx = i
            elif ll:
                # Trend reversal: record current pivot high, switch to downtrend
                pivots.append((pv_idx, pv, 1))
                trend = -1
                pv = cur_low
                pv_idx = i
        else:  # trend == -1
            if ll:
                # Still in downtrend, update pivot low candidate
                if cur_low <= pv:
                    pv = cur_low
                    pv_idx = i
            elif hh:
                # Trend reversal: record current pivot low, switch to uptrend
                pivots.append((pv_idx, pv, -1))
                trend = 1
                pv = cur_high
                pv_idx = i

    return pivots


# ---------------------------------------------------------------------------
# Cypher pattern detection
# ---------------------------------------------------------------------------

def check_cypher_bullish(px, pa, pb, pc):
    """
    Check if (X, A, B, C) form a bullish Cypher W pattern.
    Returns (entry, tp1, tp2, sl, cancel_upper) or None.

    X = local low, A = local high, B = local low, C = local high
    Pivot types:  X=-1 (low), A=1 (high), B=-1 (low), C=1 (high)
    """
    yx, ya, yb, yc = px, pa, pb, pc

    # Basic shape: X < A (high above low), C > A (C is higher extension)
    if not (yx < ya):
        return None
    if not (yc > ya):
        return None
    if not (yb < ya):
        return None

    xa_range = ya - yx

    # B retracement of XA: 38.2% - 61.8%
    b_low  = yx + xa_range * MIN_B
    b_high = yx + xa_range * MAX_B
    if not (b_low < yb < b_high):
        return None

    # C extension of XA above X: 127.2% - 141.4%
    c_low  = yx + xa_range * BULL_MIN_C
    c_high = yx + xa_range * BULL_MAX_C
    if not (c_low < yc < c_high):
        return None

    # Entry: 78.6% retracement from C back toward X
    entry  = yc - (yc - yx) * ENTRY_CR
    tp1    = yc - (yc - yx) * TP1_RATIO
    tp2    = yc - (yc - yx) * TP2_RATIO   # TP2_RATIO = -0.236 → yc + (yc-yx)*0.236
    sl     = yx
    cancel = yx + xa_range * BULL_MAX_C   # upper cancel zone = C_max

    # Sanity: entry should be below C and above X
    if not (yx < entry < yc):
        return None

    return entry, tp1, tp2, sl, cancel


def check_cypher_bearish(px, pa, pb, pc):
    """
    Check if (X, A, B, C) form a bearish Cypher M pattern.
    Returns (entry, tp1, tp2, sl, cancel_upper) or None.

    X = local high, A = local low, B = local high, C = local low
    Pivot types:  X=1 (high), A=-1 (low), B=1 (high), C=-1 (low)
    """
    yx, ya, yb, yc = px, pa, pb, pc

    # Basic shape: X > A (low below high), C < A (C is lower extension)
    if not (yx > ya):
        return None
    if not (yc < ya):
        return None
    if not (yb > ya):
        return None

    xa_range = yx - ya  # positive

    # B retracement of XA (from X down to A): 38.2% - 61.8%
    b_low  = ya + xa_range * MIN_B
    b_high = ya + xa_range * MAX_B
    if not (b_low < yb < b_high):
        return None

    # C extension below A: 41% - 13% of XA below A
    # cancel_upper (13%) < yC < cancel_lower (41%) → both below A
    c_upper = ya - xa_range * BEAR_MAX_C   # 13% below A (closer to A)
    c_lower = ya - xa_range * BEAR_MIN_C   # 41% below A (farther from A)
    if not (c_lower < yc < c_upper):
        return None

    # Entry: 78.6% retracement from C back up toward X
    entry  = yc + (yx - yc) * ENTRY_CR
    tp1    = yc - (yc - yx) * TP1_RATIO   # yc + (yx-yc)*0.236  → between yc and yx? No.
    # For bearish: yc < yx, so (yc - yx) < 0, TP1_RATIO=0.236 → yc - neg*0.236 = yc + pos → above yc
    # But TP for a short = price goes DOWN. Let's recalculate correctly:
    # From the spec: TP1 = yC - (yC - yX) * 0.236
    # yC < yX → (yC - yX) < 0 → -(yC-yX)*0.236 = positive → TP1 > yC → above yC (wrong for short)
    # Actually the spec says TP1 is BELOW entry for bearish. Let me re-read:
    # "TP1: yC - (yC - yX) * 0.236 (for bearish yC < yX so this goes above yC toward X, so TP1 is BELOW entry)"
    # TP1 is above yC but below entry (entry is 78.6% retracement from C toward X)
    # entry = yC + (yX - yC)*0.786 = yC + big positive chunk
    # TP1   = yC + (yX - yC)*0.236 = yC + small positive chunk → TP1 < entry ✓ (short takes profit going down)
    tp1 = yc - (yc - yx) * TP1_RATIO
    # TP2: yC - (yC - yX)*(-0.236) = yC + (yC-yX)*0.236 = yC - (yX-yC)*0.236 → below yC (even more profit for short)
    tp2 = yc - (yc - yx) * TP2_RATIO
    sl  = yx  # stop at X (above entry for short)

    # Cancel: if price hits above c_upper before entry fills → cancel
    cancel_upper = c_upper  # ya - xa_range * BEAR_MAX_C

    # Sanity: entry should be above C and below X
    if not (yc < entry < yx):
        return None
    # TP1 should be below entry (profit for short)
    if not (tp1 < entry):
        return None

    return entry, tp1, tp2, sl, cancel_upper


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def run_backtest(candles: list[dict], capital: float, leverage: float,
                 pivot_length: int = PIVOT_LENGTH) -> dict:
    """Run the Cypher pattern backtest over the candle list."""

    print(f"\nRunning backtest: capital=${capital:,.0f}, leverage={leverage}x, "
          f"pivot_length={pivot_length}, candles={len(candles)}")

    # Detect all pivots first
    all_pivots = detect_pivots(candles, pivot_length)
    print(f"  Detected {len(all_pivots)} pivots")

    trades = []
    equity_curve = [capital]
    equity = capital

    # We'll scan for patterns pivot-by-pivot.
    # For each set of 4 consecutive pivots, check if they form a Cypher.
    # Then simulate the trade on candles after the C pivot bar.

    pattern_count = 0
    cancelled_count = 0

    # Track which bar indices we've already started a trade from to avoid duplication
    used_c_bars = set()

    for pi in range(3, len(all_pivots)):
        # Last 4 pivots: indices pi-3, pi-2, pi-1, pi
        p0 = all_pivots[pi - 3]  # X
        p1 = all_pivots[pi - 2]  # A
        p2 = all_pivots[pi - 1]  # B
        p3 = all_pivots[pi]      # C

        xi, xa_val, xt = p0
        ai, aa_val, at = p1
        bi, ba_val, bt = p2
        ci, ca_val, ct = p3

        # Skip if C bar already used
        if ci in used_c_bars:
            continue

        pattern = None
        direction = None

        # Bullish: pivots should be low, high, low, high → types: -1, 1, -1, 1
        if xt == -1 and at == 1 and bt == -1 and ct == 1:
            pattern = check_cypher_bullish(xa_val, aa_val, ba_val, ca_val)
            direction = "LONG"

        # Bearish: pivots should be high, low, high, low → types: 1, -1, 1, -1
        elif xt == 1 and at == -1 and bt == 1 and ct == -1:
            pattern = check_cypher_bearish(xa_val, aa_val, ba_val, ca_val)
            direction = "SHORT"

        if pattern is None:
            continue

        pattern_count += 1
        entry, tp1, tp2, sl, cancel_upper = pattern

        # Simulate trade on bars after C pivot
        trade = simulate_trade(
            candles=candles,
            start_bar=ci + 1,
            direction=direction,
            entry=entry,
            tp1=tp1,
            tp2=tp2,
            sl=sl,
            cancel_upper=cancel_upper,
            capital=equity,
            leverage=leverage,
        )

        trade["X"] = xa_val
        trade["A"] = aa_val
        trade["B"] = ba_val
        trade["C"] = ca_val
        trade["pattern_bar"] = ci
        trade["pattern_ts"] = candles[ci]["timestamp"]

        used_c_bars.add(ci)

        outcome = trade["outcome"]

        if outcome == "CANCELLED":
            cancelled_count += 1
        elif outcome != "PENDING":
            pnl = trade["pnl"]
            equity += pnl
            equity = max(equity, 0)
            trade["equity_after"] = equity
            equity_curve.append(equity)
            trades.append(trade)
        else:
            trades.append(trade)  # include PENDING in output but no PnL

        # Print trade
        dt = datetime.fromtimestamp(trade["pattern_ts"] / 1000, tz=timezone.utc)
        print(f"  {dt.strftime('%Y-%m-%d %H:%M')} {direction:5s} "
              f"E={entry:.1f} SL={sl:.1f} TP1={tp1:.1f} TP2={tp2:.1f} "
              f"=> {outcome:10s} PnL=${trade.get('pnl', 0):+.2f}")

    # Summary
    filled_trades = [t for t in trades if t["outcome"] not in ("PENDING", "CANCELLED")]
    tp1_hits  = sum(1 for t in filled_trades if t["outcome"] in ("TP1", "TP2", "BE"))
    tp2_hits  = sum(1 for t in filled_trades if t["outcome"] == "TP2")
    be_exits  = sum(1 for t in filled_trades if t["outcome"] == "BE")
    sl_hits   = sum(1 for t in filled_trades if t["outcome"] == "SL")

    wins = sum(1 for t in filled_trades if t.get("pnl", 0) > 0)
    total_pnl = sum(t.get("pnl", 0) for t in filled_trades)
    avg_pnl = total_pnl / len(filled_trades) if filled_trades else 0

    pnls = [t.get("pnl", 0) for t in filled_trades]
    best_trade  = max(pnls) if pnls else 0
    worst_trade = min(pnls) if pnls else 0

    # Days spanned
    if candles:
        span_ms = candles[-1]["timestamp"] - candles[0]["timestamp"]
        span_days = span_ms / (1000 * 86400)
    else:
        span_days = 1

    avg_per_day = len(filled_trades) / span_days if span_days > 0 else 0
    daily_pnl   = total_pnl / span_days if span_days > 0 else 0

    # Max drawdown from equity curve
    max_dd = compute_max_drawdown(equity_curve)

    win_rate = wins / len(filled_trades) if filled_trades else 0

    summary = {
        "capital": capital,
        "leverage": leverage,
        "span_days": round(span_days, 1),
        "total_patterns": pattern_count,
        "cancelled": cancelled_count,
        "filled": len(filled_trades),
        "tp1_hits": tp1_hits,
        "tp2_hits": tp2_hits,
        "be_exits": be_exits,
        "sl_hits": sl_hits,
        "win_rate": round(win_rate * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl_per_trade": round(avg_pnl, 2),
        "best_trade": round(best_trade, 2),
        "worst_trade": round(worst_trade, 2),
        "avg_trades_per_day": round(avg_per_day, 2),
        "daily_pnl": round(daily_pnl, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "final_equity": round(equity, 2),
    }

    return {"summary": summary, "trades": trades, "equity_curve": equity_curve}


def simulate_trade(candles, start_bar, direction, entry, tp1, tp2, sl,
                   cancel_upper, capital, leverage):
    """
    Simulate a single Cypher trade starting at start_bar.

    States:
      PENDING  → waiting for entry fill
      PARTIAL  → TP1 hit, watching for BE or TP2
      DONE     → finished

    Returns a trade dict.
    """
    MAX_BARS = 200  # give up after this many bars

    outcome = "PENDING"
    pnl = 0.0
    entry_ts = None
    exit_ts = None

    # Position sizing: risk capital * leverage
    position_value = capital * leverage  # total notional
    # Size in BTC equiv at entry price (we'll use dollar-based PnL)
    size_usd = position_value  # dollar value of position

    # Fees: entry = maker (limit order), exits = taker (market fills assumed)
    entry_fee = size_usd * MAKER_FEE  # negative = rebate

    filled = False
    tp1_hit = False

    for i in range(start_bar, min(start_bar + MAX_BARS, len(candles))):
        c = candles[i]
        lo = c["low"]
        hi = c["high"]
        ts = c["timestamp"]

        if not filled:
            # Check cancel zone first (before entry)
            if direction == "LONG":
                # Cancel if price goes above cancel_upper before filling entry
                if hi >= cancel_upper:
                    outcome = "CANCELLED"
                    break
                # Entry: limit buy triggers when price dips to entry
                if lo <= entry:
                    filled = True
                    entry_ts = ts
            else:  # SHORT
                # Cancel if price drops below cancel_upper (which is above entry for bearish)
                # cancel_upper = ya - xa_range * BEAR_MAX_C (price level above C but below A)
                # If price hits above cancel_upper before entry fills → cancel
                # For SHORT: entry is above C; cancel_upper is above C but below A
                # If price drops BELOW cancel_upper before hitting entry → cancel? No:
                # Spec: "if price hits above c_upper before entry fills → cancel"
                # But for SHORT, entry is ABOVE C. So price needs to go UP to entry.
                # cancel_upper is between C and entry; if price goes above cancel_upper FIRST → cancel
                if hi >= cancel_upper:
                    outcome = "CANCELLED"
                    break
                # Entry: limit sell triggers when price rises to entry
                if hi >= entry:
                    filled = True
                    entry_ts = ts

        if filled:
            if direction == "LONG":
                # Check SL first (price drops to SL)
                if lo <= sl:
                    if tp1_hit:
                        # Remaining 50% stopped out at SL
                        half = size_usd * TP1_SIZE
                        pnl_remaining = half * (sl - entry) / entry
                        pnl += pnl_remaining - half * TAKER_FEE
                        outcome = "SL"
                    else:
                        pnl = size_usd * (sl - entry) / entry - size_usd * TAKER_FEE
                        outcome = "SL"
                    exit_ts = ts
                    break

                if not tp1_hit:
                    if hi >= tp1:
                        # Take 50% at TP1
                        half = size_usd * TP1_SIZE
                        pnl_tp1 = half * (tp1 - entry) / entry
                        fee_tp1 = half * TAKER_FEE
                        pnl += pnl_tp1 - fee_tp1
                        tp1_hit = True
                        # Continue watching remaining 50%
                else:
                    # Already hit TP1: watch for TP2 or return-to-entry (BE)
                    # If price returns to entry → close remaining at breakeven
                    if lo <= entry:
                        half = size_usd * TP1_SIZE
                        pnl_be = half * (entry - entry) / entry  # 0 PnL on remaining
                        fee_be = half * TAKER_FEE
                        pnl += pnl_be - fee_be
                        outcome = "BE"
                        exit_ts = ts
                        break
                    # TP2 hit
                    if hi >= tp2:
                        half = size_usd * TP1_SIZE
                        pnl_tp2 = half * (tp2 - entry) / entry
                        fee_tp2 = half * TAKER_FEE
                        pnl += pnl_tp2 - fee_tp2
                        outcome = "TP2"
                        exit_ts = ts
                        break

            else:  # SHORT
                # Check SL first (price rises to SL)
                if hi >= sl:
                    if tp1_hit:
                        half = size_usd * TP1_SIZE
                        pnl_remaining = half * (entry - sl) / entry
                        pnl += pnl_remaining - half * TAKER_FEE
                        outcome = "SL"
                    else:
                        pnl = size_usd * (entry - sl) / entry - size_usd * TAKER_FEE
                        outcome = "SL"
                    exit_ts = ts
                    break

                if not tp1_hit:
                    # TP1 for short: price drops to tp1
                    if lo <= tp1:
                        half = size_usd * TP1_SIZE
                        pnl_tp1 = half * (entry - tp1) / entry
                        fee_tp1 = half * TAKER_FEE
                        pnl += pnl_tp1 - fee_tp1
                        tp1_hit = True
                else:
                    # Return-to-entry (BE): price rises back to entry
                    if hi >= entry:
                        half = size_usd * TP1_SIZE
                        fee_be = half * TAKER_FEE
                        pnl += 0 - fee_be
                        outcome = "BE"
                        exit_ts = ts
                        break
                    # TP2: price drops further to tp2
                    if lo <= tp2:
                        half = size_usd * TP1_SIZE
                        pnl_tp2 = half * (entry - tp2) / entry
                        fee_tp2 = half * TAKER_FEE
                        pnl += pnl_tp2 - fee_tp2
                        outcome = "TP2"
                        exit_ts = ts
                        break

    else:
        # Loop exhausted without completion
        if filled and tp1_hit:
            outcome = "TP1"  # partial fill, TP1 done but TP2/BE not reached

    # Add entry fee rebate if filled
    if filled:
        pnl += entry_fee  # maker rebate (negative fee = positive PnL addition)
        if outcome == "PENDING":
            outcome = "PENDING"  # still open at end of data

    return {
        "direction": direction,
        "entry": round(entry, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "sl": round(sl, 2),
        "cancel_upper": round(cancel_upper, 2),
        "outcome": outcome,
        "pnl": round(pnl, 2),
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
    }


def compute_max_drawdown(equity_curve: list[float]) -> float:
    """Compute maximum drawdown as a fraction of peak equity."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ---------------------------------------------------------------------------
# Output / reporting
# ---------------------------------------------------------------------------

def print_summary(summary: dict):
    print("\n" + "=" * 60)
    print("CYPHER HARMONIC PATTERN BACKTEST SUMMARY")
    print("=" * 60)
    print(f"  Capital:             ${summary['capital']:>10,.0f}")
    print(f"  Leverage:            {summary['leverage']:>10.1f}x")
    print(f"  Span:                {summary['span_days']:>10.1f} days")
    print(f"  Total patterns:      {summary['total_patterns']:>10d}")
    print(f"  Cancelled:           {summary['cancelled']:>10d}")
    print(f"  Trades filled:       {summary['filled']:>10d}")
    print(f"  TP1 hits:            {summary['tp1_hits']:>10d}")
    print(f"  TP2 hits:            {summary['tp2_hits']:>10d}")
    print(f"  BE exits:            {summary['be_exits']:>10d}")
    print(f"  SL hits:             {summary['sl_hits']:>10d}")
    print(f"  Win rate:            {summary['win_rate']:>9.1f}%")
    print(f"  Total PnL:           ${summary['total_pnl']:>+10,.2f}")
    print(f"  Avg PnL/trade:       ${summary['avg_pnl_per_trade']:>+10,.2f}")
    print(f"  Best trade:          ${summary['best_trade']:>+10,.2f}")
    print(f"  Worst trade:         ${summary['worst_trade']:>+10,.2f}")
    print(f"  Avg trades/day:      {summary['avg_trades_per_day']:>10.2f}")
    print(f"  Est. daily PnL:      ${summary['daily_pnl']:>+10,.2f}")
    print(f"  Max drawdown:        {summary['max_drawdown_pct']:>9.2f}%")
    print(f"  Final equity:        ${summary['final_equity']:>10,.2f}")
    print("=" * 60)


def save_results(results: dict, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Convert trades for JSON serialization
    out = {
        "summary": results["summary"],
        "trades": results["trades"],
    }
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nResults saved to {path}")


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------

def run_sweep(candles: list[dict], capital: float, leverages: list[float]):
    print("\n" + "=" * 60)
    print("LEVERAGE PARAMETER SWEEP")
    print(f"{'Leverage':>10} {'Filled':>8} {'Win%':>7} {'Total PnL':>12} "
          f"{'Daily PnL':>12} {'Max DD%':>9} {'Final Eq':>12}")
    print("-" * 60)

    sweep_results = []
    for lev in leverages:
        result = run_backtest(candles, capital, lev)
        s = result["summary"]
        print(f"  {lev:>7.1f}x  {s['filled']:>8d}  {s['win_rate']:>6.1f}%  "
              f"${s['total_pnl']:>+11,.2f}  ${s['daily_pnl']:>+11,.2f}  "
              f"{s['max_drawdown_pct']:>8.2f}%  ${s['final_equity']:>11,.2f}")
        sweep_results.append(s)

    print("=" * 60)
    return sweep_results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Cypher Harmonic Pattern Backtest on BTC")
    parser.add_argument("--days",     type=int,   default=90,        help="Days of history to fetch")
    parser.add_argument("--interval", type=str,   default="5m",      help="Candle interval (e.g. 5m, 15m, 1h)")
    parser.add_argument("--symbol",   type=str,   default="BTCUSDT", help="Binance symbol")
    parser.add_argument("--capital",  type=float, default=CAPITAL,   help="Starting capital in USD")
    parser.add_argument("--leverage", type=float, default=LEVERAGE,  help="Leverage multiplier")
    parser.add_argument("--pivot",    type=int,   default=PIVOT_LENGTH, help="Pivot length for ZigZag")
    parser.add_argument("--sweep",    action="store_true",           help="Sweep leverage values")
    parser.add_argument("--output",   type=str,   default="trades/cypher_backtest.json",
                        help="Output JSON file path")
    args = parser.parse_args()

    # Fetch data
    candles = fetch_klines(args.symbol, args.interval, args.days)

    if len(candles) < 50:
        print("Not enough candles fetched. Exiting.")
        return

    if args.sweep:
        sweep_results = run_sweep(candles, args.capital, SWEEP_LEVERAGES)
        # Also run the default backtest for saving
        results = run_backtest(candles, args.capital, args.leverage, args.pivot)
        print_summary(results["summary"])
        results["sweep"] = sweep_results
    else:
        results = run_backtest(candles, args.capital, args.leverage, args.pivot)
        print_summary(results["summary"])

    save_results(results, args.output)


if __name__ == "__main__":
    main()
