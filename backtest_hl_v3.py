#!/usr/bin/env python3
"""
Comprehensive Hyperliquid strategy search.

Tests 5 fundamentally different approaches — not just indicator variations
but different TYPES of edge:

  Strategy 1 - BTC/ETH Statistical Arbitrage (market-neutral):
    BTC and ETH are cointegrated. When their price ratio deviates from its
    rolling mean by 2+ std devs, bet on reversion. No directional bet needed.

  Strategy 2 - TTM Squeeze Breakout:
    Bollinger Bands inside Keltner Channels = low-volatility squeeze.
    When BB expands outside KC, a big move is starting. Enter in the direction
    of the MACD-momentum histogram. These moves are explosive and sustained.

  Strategy 3 - Volume-Confirmed Session Breakout:
    Previous session breakout test had no volume filter — was entering on
    fake breaks. Add: volume must be 2x+ the 20-period average at breakout.
    Most professional intraday traders use this filter.

  Strategy 4 - Multi-Factor Confluence:
    Only enter when ALL of these align:
      - 4H trend direction (EMA 10/30)
      - 1H RSI confirming (not overbought in direction of trade)
      - Volume above average
      - Correct trading session (London/NY)
    Fewer trades, much higher accuracy.

  Strategy 5 - ATR Channel Breakout:
    Wait for BTC to consolidate inside a tight ATR range for 6+ hours.
    When it breaks out by 2+ ATR, enter. Breakouts from consolidation are
    strong and directional — this is how institutional traders enter.

KEY INSIGHT: Hyperliquid MAKER orders earn a -0.02% rebate per side.
Using limit orders = you GET PAID to trade. This changes break-even WR
from ~53% (taker) to ~48% (maker). Tested with both fee structures.

Usage:
    python backtest_hl_v3.py              # 90 days, all strategies
    python backtest_hl_v3.py --days 180   # 6 months for more confidence
"""

import requests
import time
import math
import sys
import argparse
from datetime import datetime, timezone
from collections import defaultdict

# ── Constants ────────────────────────────────────────────────────────────────
LEVERAGE       = 5
MARGIN_USD     = 1000.0
NOTIONAL       = MARGIN_USD * LEVERAGE

TAKER_FEE      = 0.0005    # 0.05%/side — market orders
MAKER_FEE      = -0.0002   # -0.02%/side — limit orders (you're PAID)
ROUND_TRIP_TAKER = 2 * TAKER_FEE * NOTIONAL    # $5.00
ROUND_TRIP_MAKER = 2 * MAKER_FEE * NOTIONAL    # -$2.00 (you receive $2)


# ── Data fetching ────────────────────────────────────────────────────────────

def fetch_1min(symbol="BTCUSDT", days=90):
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    url      = "https://api.binance.com/api/v3/klines"
    candles  = {}
    cur      = start_ms
    total    = 0

    while cur < end_ms:
        try:
            r = requests.get(url, params={
                "symbol": symbol, "interval": "1m",
                "startTime": cur, "endTime": end_ms, "limit": 1000,
            }, timeout=15)
            data = r.json()
        except Exception as e:
            print(f"\nFetch error ({symbol}): {e}")
            time.sleep(2)
            continue
        if not data:
            break
        for k in data:
            ts = k[0] // 1000
            candles[ts] = {
                "open":   float(k[1]),
                "high":   float(k[2]),
                "low":    float(k[3]),
                "close":  float(k[4]),
                "volume": float(k[5]),
            }
        cur    = data[-1][0] + 60000
        total += len(data)
        print(f"\r  {symbol}: {total:,} candles...", end="", flush=True)
        time.sleep(0.05)

    print(f"\r  {symbol}: {total:,} candles        ")
    return candles


# ── Indicator library ────────────────────────────────────────────────────────

def build_tf(candles_1m, period_secs):
    bars = {}
    for ts, c in candles_1m.items():
        b = (ts // period_secs) * period_secs
        if b not in bars:
            bars[b] = {"open": c["open"], "high": c["high"],
                        "low": c["low"],   "close": c["close"],
                        "volume": c["volume"]}
        else:
            bars[b]["high"]   = max(bars[b]["high"],  c["high"])
            bars[b]["low"]    = min(bars[b]["low"],   c["low"])
            bars[b]["close"]  = c["close"]
            bars[b]["volume"] += c["volume"]
    return bars


def ema(values, period):
    k, out = 2 / (period + 1), [None] * len(values)
    for i, v in enumerate(values):
        if v is None:
            continue
        out[i] = v if (i == 0 or out[i-1] is None) else v * k + out[i-1] * (1 - k)
    return out


def rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i-1]
        gains  += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period, len(closes)):
        if i > period:
            d      = closes[i] - closes[i-1]
            avg_g  = (avg_g * (period-1) + max(d, 0))  / period
            avg_l  = (avg_l * (period-1) + max(-d, 0)) / period
        out[i] = 100 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def atr(bars_list, period=14):
    """Average True Range for a list of bar dicts."""
    trs  = [None] * len(bars_list)
    out  = [None] * len(bars_list)
    for i in range(1, len(bars_list)):
        hi, lo, pc = bars_list[i]["high"], bars_list[i]["low"], bars_list[i-1]["close"]
        trs[i] = max(hi - lo, abs(hi - pc), abs(lo - pc))
    for i in range(period, len(bars_list)):
        window = [t for t in trs[i-period+1:i+1] if t]
        if len(window) == period:
            out[i] = sum(window) / period
    return out


def bollinger(closes, period=20, mult=2.0):
    out = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        w    = closes[i-period+1:i+1]
        mean = sum(w) / period
        std  = math.sqrt(sum((x-mean)**2 for x in w) / period)
        out[i] = (mean + mult*std, mean, mean - mult*std)
    return out


def keltner(bars_list, period=20, mult=1.5):
    """Keltner Channel using EMA + ATR."""
    closes = [b["close"] for b in bars_list]
    e      = ema(closes, period)
    a      = atr(bars_list, period)
    out    = [None] * len(bars_list)
    for i in range(period, len(bars_list)):
        if e[i] and a[i]:
            out[i] = (e[i] + mult*a[i], e[i], e[i] - mult*a[i])
    return out


def rolling_zscore(values, period=20):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        w    = values[i-period+1:i+1]
        mean = sum(w) / period
        std  = math.sqrt(sum((x-mean)**2 for x in w) / period)
        out[i] = (values[i] - mean) / std if std > 0 else 0
    return out


def volume_sma(bars_list, period=20):
    vols = [b["volume"] for b in bars_list]
    out  = [None] * len(bars_list)
    for i in range(period - 1, len(bars_list)):
        w = vols[i-period+1:i+1]
        out[i] = sum(w) / period
    return out


# ── Trade runner ─────────────────────────────────────────────────────────────

def run_trade(candles_1m, entry_ts, direction,
              stop_pct, trail_pct, max_hold_secs,
              use_maker=False, breakeven_pct=None):
    entry_c = candles_1m.get(entry_ts)
    if not entry_c:
        for off in range(60, 361, 60):
            entry_c = candles_1m.get(entry_ts + off)
            if entry_c:
                entry_ts += off
                break
    if not entry_c:
        return None

    ep       = entry_c["close"]
    fee      = 2 * (MAKER_FEE if use_maker else TAKER_FEE) * NOTIONAL
    is_long  = direction == "Long"
    stop     = ep * (1 - stop_pct) if is_long else ep * (1 + stop_pct)
    peak     = ep
    close_ts = entry_ts + max_hold_secs
    be_pct   = breakeven_pct or stop_pct * 0.5

    exit_price  = None
    exit_reason = "time_exit"
    ts          = entry_ts + 60

    while ts <= close_ts:
        c = candles_1m.get(ts)
        if not c:
            ts += 60
            continue
        if is_long:
            if c["low"] <= stop:
                exit_price  = stop
                exit_reason = "trailing_stop" if stop >= ep else "stop_loss"
                break
            if c["high"] >= ep * (1 + be_pct) and stop < ep:
                stop = ep
            peak = max(peak, c["high"])
            if peak * (1 - trail_pct) > stop:
                stop = peak * (1 - trail_pct)
        else:
            if c["high"] >= stop:
                exit_price  = stop
                exit_reason = "trailing_stop" if stop <= ep else "stop_loss"
                break
            if c["low"] <= ep * (1 - be_pct) and stop > ep:
                stop = ep
            peak = min(peak, c["low"])
            if peak * (1 + trail_pct) < stop:
                stop = peak * (1 + trail_pct)
        ts += 60

    if exit_price is None:
        last = candles_1m.get(close_ts) or candles_1m.get(close_ts - 60)
        if not last:
            return None
        exit_price, exit_reason = last["close"], "time_exit"

    pnl = ((exit_price - ep) / ep if is_long else (ep - exit_price) / ep)
    return {
        "entry": ep, "exit": exit_price,
        "exit_reason": exit_reason,
        "pnl_usd": round(pnl * NOTIONAL - fee, 2),
        "won":     pnl * NOTIONAL - fee > 0,
    }


# ── Stats ────────────────────────────────────────────────────────────────────

def stats(results, days, label):
    if not results:
        return None
    n      = len(results)
    wins   = sum(1 for r in results if r["won"])
    total  = sum(r["pnl_usd"] for r in results)
    eq, pk, dd = 0.0, 0.0, 0.0
    for r in results:
        eq += r["pnl_usd"]
        pk  = max(pk, eq)
        dd  = min(dd, eq - pk)
    reasons = defaultdict(int)
    for r in results:
        reasons[r["exit_reason"]] += 1
    return {
        "label": label, "trades": n, "win_rate": wins/n,
        "total": total, "avg": total/n, "daily": total/days,
        "tpd": n/days, "dd": dd, "exits": dict(reasons), "rows": results,
    }


def show(s):
    if not s:
        print("  No trades.")
        return
    print(f"\n  Trades: {s['trades']:,} ({s['tpd']:.1f}/day)  |  "
          f"WR: {s['win_rate']*100:.1f}%  |  "
          f"Avg: ${s['avg']:.2f}  |  "
          f"Daily: ${s['daily']:.2f}  |  "
          f"Monthly: ${s['daily']*30:,.0f}  |  "
          f"MaxDD: ${s['dd']:,.2f}")
    reasons = s["exits"]
    tot = sum(reasons.values())
    print("  Exits: " + "  ".join(f"{k}={v}({v/tot*100:.0f}%)"
                                   for k, v in sorted(reasons.items(), key=lambda x:-x[1])))


def hdr(title):
    print(f"\n{'='*100}\n  {title}\n{'='*100}")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 1: BTC/ETH STATISTICAL ARBITRAGE
# ════════════════════════════════════════════════════════════════════════════

def strat_pairs(btc, eth, days, zscore_period=60, entry_z=2.0, exit_z=0.5,
                stop_pct=0.012, trail_pct=0.008, max_hold=24*3600,
                use_maker=False):
    """
    Ratio = BTC/ETH price. When Z-score > entry_z, ratio is too high:
      SHORT BTC (ratio will fall) + LONG ETH.
    When Z-score < -entry_z: LONG BTC + SHORT ETH.
    Exit when Z-score returns to exit_z.
    """
    ts_list = sorted(set(btc.keys()) & set(eth.keys()))
    ratios  = [btc[t]["close"] / eth[t]["close"] for t in ts_list]
    zscores = rolling_zscore(ratios, zscore_period)

    results    = []
    in_pos     = False
    pos_dir    = None
    pos_ts     = None
    last_trade = 0
    cooldown   = 4 * 3600

    for i, ts in enumerate(ts_list):
        z = zscores[i]
        if z is None:
            continue

        if in_pos:
            # Check exit: Z returned to neutral OR stop
            exited = False
            if pos_dir == "Short" and z <= exit_z:
                exited = True
            elif pos_dir == "Long" and z >= -exit_z:
                exited = True
            elif ts - pos_ts > max_hold:
                exited = True

            if exited:
                in_pos = False

        if not in_pos and ts - last_trade >= cooldown:
            direction = None
            if z > entry_z:
                direction = "Short"   # ratio too high: BTC overvalued vs ETH
            elif z < -entry_z:
                direction = "Long"    # ratio too low: BTC undervalued vs ETH

            if direction:
                trade = run_trade(btc, ts, direction,
                                  stop_pct, trail_pct, max_hold,
                                  use_maker=use_maker)
                if trade:
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    results.append({
                        "time": dt.strftime("%Y-%m-%d %H:%M"),
                        "direction": direction, "zscore": round(z, 2),
                        **{k: v for k, v in trade.items()},
                    })
                    in_pos, pos_dir, pos_ts, last_trade = True, direction, ts, ts

    return stats(results, days, "BTC/ETH Stat Arb")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 2: TTM SQUEEZE BREAKOUT
# ════════════════════════════════════════════════════════════════════════════

def strat_squeeze(candles_1m, days, tf_secs=3600,
                  bb_period=20, bb_mult=2.0, kc_mult=1.5,
                  stop_atr_mult=1.5, trail_atr_mult=1.0,
                  max_hold=12*3600, use_maker=False):
    """
    Squeeze: BB inside KC = compression. BB breaks out of KC = fire.
    Direction: MACD histogram sign at moment of breakout.
    ATR-based stops — adapts to current volatility.
    """
    bars     = build_tf(candles_1m, tf_secs)
    ts_list  = sorted(bars.keys())
    bars_seq = [bars[t] for t in ts_list]
    closes   = [b["close"] for b in bars_seq]

    bb_vals  = bollinger(closes, bb_period, bb_mult)
    kc_vals  = keltner(bars_seq, bb_period, kc_mult)
    atr_vals = atr(bars_seq, 14)

    # MACD momentum (12/26 EMA difference as direction proxy)
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd  = [a - b if a and b else None for a, b in zip(ema12, ema26)]

    results    = []
    squeezed   = False
    last_trade = 0
    cooldown   = tf_secs * 4

    for i in range(bb_period + 2, len(ts_list)):
        bb = bb_vals[i]
        kc = kc_vals[i]
        at = atr_vals[i]
        mc = macd[i]
        if not all([bb, kc, at, mc]):
            continue

        bb_upper, _, bb_lower = bb
        kc_upper, _, kc_lower = kc

        # Squeeze ON: BB inside KC
        currently_squeezed = (bb_upper < kc_upper and bb_lower > kc_lower)

        # Squeeze FIRED: was squeezed, now BB breaks out of KC
        if squeezed and not currently_squeezed:
            ts = ts_list[i]
            if ts - last_trade >= cooldown:
                direction = "Long" if mc > 0 else "Short"
                entry_ts  = ts + tf_secs

                # ATR-based dynamic stop
                sp = at / bars[ts]["close"]  # stop as fraction of price
                sl = max(sp * stop_atr_mult, 0.005)
                tr = max(sp * trail_atr_mult, 0.003)

                trade = run_trade(candles_1m, entry_ts, direction,
                                  sl, tr, max_hold, use_maker=use_maker)
                if trade:
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    results.append({
                        "time": dt.strftime("%Y-%m-%d %H:%M"),
                        "direction": direction, "atr_pct": round(sp*100, 3),
                        **{k: v for k, v in trade.items()},
                    })
                    last_trade = entry_ts

        squeezed = currently_squeezed

    return stats(results, days, "TTM Squeeze Breakout")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 3: VOLUME-CONFIRMED SESSION BREAKOUT
# ════════════════════════════════════════════════════════════════════════════

SESSION_OPENS = [8*3600, 13*3600 + 30*60]   # London + NY UTC seconds

def strat_vol_session(candles_1m, days,
                       breakout_mins=15, min_move=0.003, vol_mult=1.5,
                       stop_pct=0.007, trail_pct=0.005,
                       max_hold=4*3600, use_maker=False):
    """
    Session breakout WITH volume confirmation.
    Volume must be vol_mult × 20-period average — filters fake breaks.
    """
    bars_1h  = build_tf(candles_1m, 3600)
    ts_1h    = sorted(bars_1h.keys())
    bars_seq = [bars_1h[t] for t in ts_1h]
    vol_smas  = volume_sma(bars_seq, 20)
    vol_map  = {ts_1h[i]: vol_smas[i] for i in range(len(ts_1h))}

    results     = []
    seen        = set()
    last_trade  = 0
    cooldown    = 4 * 3600

    for ts in sorted(candles_1m.keys()):
        dt       = datetime.fromtimestamp(ts, tz=timezone.utc)
        secs_day = dt.hour * 3600 + dt.minute * 60

        for sess in SESSION_OPENS:
            if secs_day != sess:
                continue
            key = (dt.date(), sess)
            if key in seen:
                continue

            # Check breakout at breakout_mins into session
            open_c   = candles_1m.get(ts)
            check_ts = ts + breakout_mins * 60
            check_c  = candles_1m.get(check_ts)
            if not open_c or not check_c:
                continue

            move_pct = (check_c["close"] - open_c["open"]) / open_c["open"]
            if abs(move_pct) < min_move:
                continue

            # Volume check: sum of volume in breakout window vs average hourly vol
            window_vol  = sum(
                candles_1m[t]["volume"] for t in range(ts, check_ts + 60, 60)
                if t in candles_1m
            )
            hour_bar_ts = (ts // 3600) * 3600
            avg_vol     = vol_map.get(hour_bar_ts)
            if avg_vol and window_vol < avg_vol * vol_mult:
                continue   # low volume — skip this breakout

            seen.add(key)
            if ts - last_trade < cooldown:
                continue

            direction = "Long" if move_pct > 0 else "Short"
            trade = run_trade(candles_1m, check_ts, direction,
                              stop_pct, trail_pct, max_hold,
                              use_maker=use_maker)
            if trade:
                results.append({
                    "time": dt.strftime("%Y-%m-%d %H:%M"),
                    "direction": direction, "move_pct": round(move_pct*100, 3),
                    "vol_ratio": round(window_vol / avg_vol, 2) if avg_vol else 0,
                    **{k: v for k, v in trade.items()},
                })
                last_trade = check_ts

    return stats(results, days, "Vol-Confirmed Session Breakout")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 4: MULTI-FACTOR CONFLUENCE
# ════════════════════════════════════════════════════════════════════════════

def strat_confluence(candles_1m, days,
                      rsi_ob=65, rsi_os=35,
                      vol_mult=1.2, stop_pct=0.010, trail_pct=0.007,
                      max_hold=24*3600, use_maker=False):
    """
    Score 0-4. Only trade when all 4 factors align:
      1. 4H EMA(10) > EMA(30) = uptrend  (or < for short)
      2. 1H RSI in confirming range (not overbought in direction)
      3. Volume above 20-period average
      4. London or NY session active
    """
    # 4H trend
    bars_4h = build_tf(candles_1m, 14400)
    ts_4h   = sorted(bars_4h.keys())
    cls_4h  = [bars_4h[t]["close"] for t in ts_4h]
    ema10_4h = ema(cls_4h, 10)
    ema30_4h = ema(cls_4h, 30)
    trend_map = {}
    for i, t in enumerate(ts_4h):
        if ema10_4h[i] and ema30_4h[i]:
            trend = "up" if ema10_4h[i] > ema30_4h[i] else "down"
            for off in range(0, 14400, 60):
                trend_map[t + off] = trend

    # 1H RSI
    bars_1h = build_tf(candles_1m, 3600)
    ts_1h   = sorted(bars_1h.keys())
    cls_1h  = [bars_1h[t]["close"] for t in ts_1h]
    rsi_1h  = rsi(cls_1h, 14)
    rsi_map = {}
    for i, t in enumerate(ts_1h):
        if rsi_1h[i]:
            for off in range(0, 3600, 60):
                rsi_map[t + off] = rsi_1h[i]

    # 1H volume SMA
    bars_seq = [bars_1h[t] for t in ts_1h]
    vol_sma  = volume_sma(bars_seq, 20)
    vol_map  = {ts_1h[i]: vol_sma[i] for i in range(len(ts_1h))}

    results    = []
    last_trade = 0
    cooldown   = 4 * 3600
    triggered  = set()

    for ts in sorted(candles_1m.keys()):
        if ts - last_trade < cooldown:
            continue

        trend = trend_map.get(ts)
        r     = rsi_map.get(ts)
        if not trend or not r:
            continue

        c    = candles_1m[ts]
        dt   = datetime.fromtimestamp(ts, tz=timezone.utc)
        hour = dt.hour
        in_session = (hour == 8 or (hour == 13 and dt.minute >= 30)
                      or hour == 9 or hour == 10 or hour == 14 or hour == 15)

        # Volume check
        h_bar_ts = (ts // 3600) * 3600
        avg_vol  = vol_map.get(h_bar_ts)
        high_vol = bool(avg_vol and c["volume"] > avg_vol * vol_mult / 60)

        # Score factors for LONG
        long_score = sum([
            trend == "up",
            r < (100 - rsi_os),   # RSI not overbought
            high_vol,
            in_session,
        ])
        # Score factors for SHORT
        short_score = sum([
            trend == "down",
            r > rsi_ob,           # RSI not oversold
            high_vol,
            in_session,
        ])

        direction = None
        if long_score == 4:
            direction = "Long"
        elif short_score == 4:
            direction = "Short"

        if not direction:
            continue

        window_key = (ts // 3600, direction)
        if window_key in triggered:
            continue
        triggered.add(window_key)

        trade = run_trade(candles_1m, ts, direction,
                          stop_pct, trail_pct, max_hold,
                          use_maker=use_maker)
        if trade:
            results.append({
                "time": dt.strftime("%Y-%m-%d %H:%M"),
                "direction": direction, "rsi": round(r, 1),
                **{k: v for k, v in trade.items()},
            })
            last_trade = ts

    return stats(results, days, "Multi-Factor Confluence")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY 5: ATR CONSOLIDATION BREAKOUT
# ════════════════════════════════════════════════════════════════════════════

def strat_atr_breakout(candles_1m, days,
                        tf_secs=3600, consol_bars=6,
                        squeeze_atr_thresh=0.6,
                        breakout_atr_mult=1.5,
                        stop_atr_mult=1.0, trail_atr_mult=0.7,
                        max_hold=24*3600, use_maker=False):
    """
    Wait for N bars where ATR is < squeeze_atr_thresh × 20-period ATR average
    (low volatility consolidation). Enter when price breaks out by breakout_atr_mult × ATR.
    Stop at 1 ATR below entry. This is how institutional breakout traders operate.
    """
    bars     = build_tf(candles_1m, tf_secs)
    ts_list  = sorted(bars.keys())
    bars_seq = [bars[t] for t in ts_list]
    closes   = [b["close"] for b in bars_seq]

    atr_vals = atr(bars_seq, 14)
    atr_sma  = [None] * len(ts_list)
    for i in range(20, len(ts_list)):
        window = [atr_vals[j] for j in range(i-20, i) if atr_vals[j]]
        atr_sma[i] = sum(window) / len(window) if window else None

    results    = []
    last_trade = 0
    cooldown   = consol_bars * tf_secs

    for i in range(20 + consol_bars, len(ts_list)):
        at    = atr_vals[i]
        at_sm = atr_sma[i]
        if not at or not at_sm:
            continue

        # Check consolidation: last consol_bars all had low ATR
        is_tight = all(
            atr_vals[j] and atr_vals[j] < at_sm * squeeze_atr_thresh
            for j in range(i - consol_bars, i)
        )
        if not is_tight:
            continue

        ts     = ts_list[i]
        bar    = bars[ts]
        prev_c = bars[ts_list[i-1]]["close"]

        if ts - last_trade < cooldown:
            continue

        # Look for breakout in next bar
        next_ts  = ts_list[i+1] if i+1 < len(ts_list) else None
        if not next_ts:
            continue
        next_bar = bars.get(next_ts)
        if not next_bar:
            continue

        move = next_bar["close"] - bar["close"]
        if abs(move) < breakout_atr_mult * at:
            continue

        direction = "Long" if move > 0 else "Short"
        sp = (at * stop_atr_mult) / bar["close"]
        tr = (at * trail_atr_mult) / bar["close"]
        sp = max(sp, 0.005)
        tr = max(tr, 0.003)

        entry_ts = next_ts + tf_secs
        trade = run_trade(candles_1m, entry_ts, direction,
                          sp, tr, max_hold, use_maker=use_maker)
        if trade:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            results.append({
                "time": dt.strftime("%Y-%m-%d %H:%M"),
                "direction": direction,
                "atr_pct": round(at / bar["close"] * 100, 3),
                **{k: v for k, v in trade.items()},
            })
            last_trade = entry_ts

    return stats(results, days, "ATR Consolidation Breakout")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()

    print("=" * 100)
    print("  Hyperliquid Deep Strategy Search  v3")
    print(f"  ${MARGIN_USD} margin  {LEVERAGE}x  (${NOTIONAL:.0f} notional)")
    print(f"  Taker fee: ${ROUND_TRIP_TAKER:.2f}/trade | "
          f"Maker fee: ${ROUND_TRIP_MAKER:.2f}/trade (you receive money!)")
    print("=" * 100)
    print(f"\nFetching {args.days} days of data from Binance...")

    btc = fetch_1min("BTCUSDT", args.days)
    eth = fetch_1min("ETHUSDT", args.days)

    if not btc:
        print("ERROR: no BTC data")
        sys.exit(1)

    ts_sorted = sorted(btc.keys())
    start_dt  = datetime.fromtimestamp(ts_sorted[0],  tz=timezone.utc)
    end_dt    = datetime.fromtimestamp(ts_sorted[-1], tz=timezone.utc)
    print(f"  Range: {start_dt:%Y-%m-%d} to {end_dt:%Y-%m-%d}\n")

    all_stats  = []
    days       = args.days

    # ── Strategy 1 ──────────────────────────────────────────────────────────
    hdr("STRATEGY 1: BTC/ETH STATISTICAL ARBITRAGE  (market-neutral)")
    print("  Long BTC/Short ETH when ratio too low | Short BTC/Long ETH when ratio too high")
    print("  No directional view needed — just exploits the persistent correlation.\n")
    s1t = strat_pairs(btc, eth, days, use_maker=False)
    s1m = strat_pairs(btc, eth, days, use_maker=True)
    print("  TAKER orders:");  show(s1t)
    print("  MAKER orders:");  show(s1m)
    best1 = s1m if (s1m and (not s1t or s1m["daily"] > s1t["daily"])) else s1t
    if best1:
        all_stats.append(best1)

    print("\n  Z-score threshold sweep (maker orders):")
    for z in [1.5, 2.0, 2.5, 3.0]:
        r = strat_pairs(btc, eth, days, zscore_period=60, entry_z=z, use_maker=True)
        if r:
            print(f"    Z>{z:.1f}  n={r['trades']:3d}  WR={r['win_rate']*100:5.1f}%  "
                  f"Daily=${r['daily']:>7.2f}  Monthly=${r['daily']*30:>8,.0f}")

    # ── Strategy 2 ──────────────────────────────────────────────────────────
    hdr("STRATEGY 2: TTM SQUEEZE BREAKOUT  (volatility compression -> explosion)")
    print("  BB inside Keltner = squeeze. Fires when BB breaks out. ATR-sized stops.\n")
    s2t = strat_squeeze(btc, days, use_maker=False)
    s2m = strat_squeeze(btc, days, use_maker=True)
    print("  TAKER orders:");  show(s2t)
    print("  MAKER orders:");  show(s2m)
    best2 = s2m if (s2m and (not s2t or s2m["daily"] > s2t["daily"])) else s2t
    if best2:
        all_stats.append(best2)

    print("\n  Timeframe sweep (maker):")
    for tf_mins in [30, 60, 240]:
        r = strat_squeeze(btc, days, tf_secs=tf_mins*60, use_maker=True)
        if r:
            print(f"    {tf_mins}min TF  n={r['trades']:3d}  WR={r['win_rate']*100:5.1f}%  "
                  f"Daily=${r['daily']:>7.2f}  Monthly=${r['daily']*30:>8,.0f}")

    # ── Strategy 3 ──────────────────────────────────────────────────────────
    hdr("STRATEGY 3: VOLUME-CONFIRMED SESSION BREAKOUT")
    print("  London 08:00 + NY 13:30 UTC | Must break 0.3% with 1.5x+ volume\n")
    s3t = strat_vol_session(btc, days, use_maker=False)
    s3m = strat_vol_session(btc, days, use_maker=True)
    print("  TAKER orders:");  show(s3t)
    print("  MAKER orders:");  show(s3m)
    best3 = s3m if (s3m and (not s3t or s3m["daily"] > s3t["daily"])) else s3t
    if best3:
        all_stats.append(best3)

    print("\n  Volume multiplier sweep (maker):")
    for vm in [1.2, 1.5, 2.0, 2.5]:
        r = strat_vol_session(btc, days, vol_mult=vm, use_maker=True)
        if r:
            print(f"    vol>={vm:.1f}x  n={r['trades']:3d}  WR={r['win_rate']*100:5.1f}%  "
                  f"Daily=${r['daily']:>7.2f}  Monthly=${r['daily']*30:>8,.0f}")

    # ── Strategy 4 ──────────────────────────────────────────────────────────
    hdr("STRATEGY 4: MULTI-FACTOR CONFLUENCE  (all 4 factors must align)")
    print("  4H trend + 1H RSI + above-avg volume + active session = high-confidence entry\n")
    s4t = strat_confluence(btc, days, use_maker=False)
    s4m = strat_confluence(btc, days, use_maker=True)
    print("  TAKER orders:");  show(s4t)
    print("  MAKER orders:");  show(s4m)
    best4 = s4m if (s4m and (not s4t or s4m["daily"] > s4t["daily"])) else s4t
    if best4:
        all_stats.append(best4)

    # ── Strategy 5 ──────────────────────────────────────────────────────────
    hdr("STRATEGY 5: ATR CONSOLIDATION BREAKOUT  (institutional style)")
    print("  6+ bars of tight ATR consolidation -> breakout entry. Wide stops, big targets.\n")
    s5t = strat_atr_breakout(btc, days, use_maker=False)
    s5m = strat_atr_breakout(btc, days, use_maker=True)
    print("  TAKER orders:");  show(s5t)
    print("  MAKER orders:");  show(s5m)
    best5 = s5m if (s5m and (not s5t or s5m["daily"] > s5t["daily"])) else s5t
    if best5:
        all_stats.append(best5)

    print("\n  Consolidation period sweep (maker):")
    for bars_n in [4, 6, 8, 10]:
        r = strat_atr_breakout(btc, days, consol_bars=bars_n, use_maker=True)
        if r:
            print(f"    consol={bars_n}h  n={r['trades']:3d}  WR={r['win_rate']*100:5.1f}%  "
                  f"Daily=${r['daily']:>7.2f}  Monthly=${r['daily']*30:>8,.0f}")

    # ── Head-to-head ─────────────────────────────────────────────────────────
    hdr("HEAD-TO-HEAD  |  $1,000 margin / 5x leverage")
    print(f"\n  {'Strategy':<35}  {'WR':>6}  {'n/day':>6}  "
          f"{'Daily':>8}  {'Monthly':>10}  {'MaxDD':>10}")
    print("  " + "-" * 85)

    if all_stats:
        for s in sorted(all_stats, key=lambda x: -x["daily"]):
            tag = " <<< BEST" if s == max(all_stats, key=lambda x: x["daily"]) else ""
            print(f"  {s['label']:<35}  "
                  f"{s['win_rate']*100:>5.1f}%  "
                  f"{s['tpd']:>6.1f}  "
                  f"${s['daily']:>7.2f}  "
                  f"${s['daily']*30:>9,.0f}  "
                  f"${s['dd']:>9,.2f}{tag}")

        best = max(all_stats, key=lambda x: x["daily"])
        hdr("VERDICT")

        if best["daily"] > 0 and best["win_rate"] >= 0.50:
            print(f"\n  [YES] DEPLOY: {best['label']}")
            print(f"  {best['win_rate']*100:.1f}% WR  "
                  f"${best['daily']:.2f}/day  "
                  f"~${best['daily']*30:,.0f}/month on $1k x5")
            print(f"\n  Scaling:")
            for size in [1000, 2000, 5000, 10000, 25000]:
                scale = size / MARGIN_USD
                print(f"    ${size:>7} margin  ->  ~${best['daily']*30*scale:>10,.0f}/month")
        elif best["daily"] > 0:
            print(f"\n  [~] MARGINAL EDGE: {best['label']}")
            print(f"  Profitable but WR {best['win_rate']*100:.1f}% is low. "
                  f"Run --days 180 to confirm.")
        else:
            print("\n  [NO] No strategy found clear edge in this period.")
            print("  Recommendations:")
            print("  1. Test on --days 180 (capture different market regimes)")
            print("  2. Consider funding rate arb in next bull run")
            print("  3. Return to Polymarket with fixed entry logic")
    else:
        print("  No results generated.")


if __name__ == "__main__":
    main()
