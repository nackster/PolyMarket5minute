#!/usr/bin/env python3
"""
Backtest the btc-updown-5m momentum strategy using Binance historical 1-minute data.

Tests whether our directional signal (BTC momentum at N seconds into the window)
actually predicts the 5-minute window outcome — before risking real money.

Usage:
    python backtest_updown.py              # 30 days, parameter sweep
    python backtest_updown.py --days 7     # 7 days only
    python backtest_updown.py --days 60    # 60 days for more data
"""

import requests
import time
import math
import csv
import sys
import argparse
from datetime import datetime, timezone
from collections import defaultdict

TRADE_SIZE = 50.0
STARTING_EQUITY = 500.0


# ── Helpers ─────────────────────────────────────────────────────────────────

def norm_cdf(x):
    """Normal CDF using math.erf — no scipy needed."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fetch_btc_1min(days=30):
    """Fetch 1-minute BTC/USDT OHLCV from Binance (free, no auth needed)."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    url = 'https://api.binance.com/api/v3/klines'
    candles = {}
    cur = start_ms
    total = 0

    while cur < end_ms:
        try:
            r = requests.get(url, params={
                'symbol': 'BTCUSDT', 'interval': '1m',
                'startTime': cur, 'endTime': end_ms, 'limit': 1000
            }, timeout=15)
            data = r.json()
        except Exception as e:
            print(f"\nFetch error: {e}, retrying...")
            time.sleep(2)
            continue

        if not data:
            break

        for k in data:
            ts = k[0] // 1000  # unix seconds (start of candle)
            candles[ts] = {
                'open':  float(k[1]),
                'close': float(k[4]),
            }

        cur = data[-1][0] + 60000
        total += len(data)
        print(f'\r  Fetched {total:,} candles...', end='', flush=True)
        time.sleep(0.05)

    print(f'\r  Fetched {total:,} candles        ')
    return candles


def build_vol_lookup(candles):
    """
    Compute rolling 30-minute realised volatility (std of 1-min returns).
    Returns dict: ts -> vol
    """
    ts_sorted = sorted(candles.keys())
    closes = [candles[ts]['close'] for ts in ts_sorted]
    vol = {}

    for i, ts in enumerate(ts_sorted):
        window = closes[max(0, i - 30):i]
        if len(window) < 3:
            vol[ts] = 0.001
            continue
        rets = [(window[j] - window[j-1]) / window[j-1] for j in range(1, len(window))]
        mean = sum(rets) / len(rets)
        variance = sum((r - mean) ** 2 for r in rets) / len(rets)
        vol[ts] = math.sqrt(variance) if variance > 0 else 0.001

    return vol


# ── Core Probability Model (mirrors real_trade.py) ───────────────────────────

def estimate_prob(move_pct, vol_30m):
    """Estimate probability of continued direction — same model as real bot."""
    if vol_30m > 0:
        z = abs(move_pct) / vol_30m
        prob = norm_cdf(z)
    else:
        prob = 0.5 + abs(move_pct) * 50
    return max(0.35, min(0.85, prob))


# ── Single Backtest Run ───────────────────────────────────────────────────────

def backtest(candles, vol_lookup,
             entry_secs=120,
             min_move_pct=0.0005,
             slippage=0.04,
             max_entry=0.72,
             min_edge=0.03):
    """
    Simulate the strategy over all 5-minute windows in the dataset.

    entry_secs: how many seconds into the window to check signal (60-240)
    min_move_pct: minimum BTC move to trigger a trade
    slippage: extra cents above ask we pay to get filled
    max_entry: refuse if entry price exceeds this
    min_edge: refuse if edge (prob - entry) < this
    """
    entry_min = entry_secs // 60   # which 1-minute candle index to use
    results = []

    ts_list = sorted(candles.keys())
    if not ts_list:
        return results

    start_ts = ts_list[0]
    end_ts   = ts_list[-1]

    w = (start_ts // 300) * 300
    while w + 300 <= end_ts:

        open_ts  = w
        entry_ts = w + entry_min * 60
        final_ts = w + 240   # 4-minute candle close ≈ window result

        open_c  = candles.get(open_ts)
        entry_c = candles.get(entry_ts)
        final_c = candles.get(final_ts)

        if not open_c or not entry_c or not final_c:
            w += 300
            continue

        btc_open  = open_c['open']
        btc_entry = entry_c['close']
        btc_final = final_c['close']
        vol       = vol_lookup.get(entry_ts, 0.001)

        move_pct = (btc_entry - btc_open) / btc_open

        # ── Filters ──────────────────────────────────────────────────────────
        if abs(move_pct) < min_move_pct:
            w += 300
            continue

        direction = 'Up' if move_pct > 0 else 'Down'
        prob      = estimate_prob(move_pct, vol)

        # Simulate what the Polymarket ask price would be.
        # When BTC moves X%, market makers price the winning side at ~prob.
        # We add slippage to cross the spread.
        ask         = prob
        entry_price = min(ask + slippage, max_entry)
        edge        = prob - entry_price

        if edge < min_edge:
            w += 300
            continue

        # ── Outcome (would Chainlink have resolved Up or Down?) ───────────────
        oracle_up = btc_final >= btc_open
        won       = (direction == 'Up') == oracle_up
        pnl       = (1.0 - entry_price) * TRADE_SIZE if won else -entry_price * TRADE_SIZE

        results.append({
            'time':       datetime.fromtimestamp(w, tz=timezone.utc).strftime('%Y-%m-%d %H:%M'),
            'direction':  direction,
            'move_pct':   round(move_pct * 100, 4),
            'entry':      round(entry_price, 3),
            'edge':       round(edge, 3),
            'prob':       round(prob, 3),
            'won':        won,
            'pnl':        round(pnl, 2),
        })

        w += 300

    return results


# ── Stats ────────────────────────────────────────────────────────────────────

def summarise(results):
    if not results:
        return {'trades': 0}

    n        = len(results)
    wins     = sum(1 for r in results if r['won'])
    wr       = wins / n
    total    = sum(r['pnl'] for r in results)
    avg      = total / n

    # Max drawdown
    equity, peak, max_dd = STARTING_EQUITY, STARTING_EQUITY, 0.0
    for r in results:
        equity += r['pnl']
        peak    = max(peak, equity)
        max_dd  = min(max_dd, equity - peak)

    return {
        'trades':    n,
        'win_rate':  wr,
        'total_pnl': total,
        'avg_pnl':   avg,
        'max_dd':    max_dd,
        'final_eq':  STARTING_EQUITY + total,
    }


def print_summary(label, s):
    if s['trades'] == 0:
        print(f"  {label}: no trades")
        return
    print(
        f"  {label:60s}  "
        f"n={s['trades']:4d}  "
        f"WR={s['win_rate']*100:5.1f}%  "
        f"PnL=${s['total_pnl']:>8.2f}  "
        f"Avg=${s['avg_pnl']:>6.2f}  "
        f"DD=${s['max_dd']:>7.2f}  "
        f"Eq=${s['final_eq']:>8.2f}"
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Backtest btc-updown-5m strategy')
    parser.add_argument('--days', type=int, default=30, help='Days of history (default 30)')
    args = parser.parse_args()

    print('=' * 70)
    print('  BTC Up/Down 5-Minute Strategy Backtester')
    print('=' * 70)
    print(f'\nFetching {args.days} days of 1-minute BTC data from Binance...')

    candles = fetch_btc_1min(days=args.days)
    if not candles:
        print('ERROR: Could not fetch data')
        sys.exit(1)

    ts_sorted = sorted(candles.keys())
    start_dt  = datetime.fromtimestamp(ts_sorted[0],  tz=timezone.utc)
    end_dt    = datetime.fromtimestamp(ts_sorted[-1], tz=timezone.utc)
    print(f'  Range: {start_dt:%Y-%m-%d %H:%M} to {end_dt:%Y-%m-%d %H:%M}')
    print(f'  Total 5-min windows: {(ts_sorted[-1] - ts_sorted[0]) // 300:,}\n')

    print('Building volatility index...')
    vol_lookup = build_vol_lookup(candles)

    # ── Section 1: Signal accuracy (direction only, ignore price) ────────────
    print('\n' + '=' * 70)
    print('  SECTION 1: SIGNAL ACCURACY (does momentum predict direction?)')
    print('  (ignoring price — just: does the move predict the outcome?)')
    print('=' * 70)

    for entry_secs in [60, 90, 120, 150, 180, 240]:
        for min_move in [0.0003, 0.0005, 0.001, 0.002]:
            # Use very loose price filters to capture signal accuracy only
            r = backtest(candles, vol_lookup,
                         entry_secs=entry_secs,
                         min_move_pct=min_move,
                         slippage=0.0,
                         max_entry=0.99,
                         min_edge=-1.0)  # no edge filter
            s = summarise(r)
            label = f'entry={entry_secs:3d}s  min_move={min_move*100:.3f}%'
            print_summary(label, s)

    # ── Section 2: Full strategy with realistic price filters ────────────────
    print('\n' + '=' * 70)
    print('  SECTION 2: FULL STRATEGY (with price caps and edge filter)')
    print('=' * 70)

    best_pnl     = float('-inf')
    best_params  = None
    best_results = None
    all_rows     = []

    for entry_secs in [60, 90, 120, 150, 180]:
        for min_move in [0.0003, 0.0005, 0.001, 0.002, 0.003]:
            for slippage in [0.02, 0.04]:
                for max_entry in [0.60, 0.65, 0.70]:
                    r = backtest(candles, vol_lookup,
                                 entry_secs=entry_secs,
                                 min_move_pct=min_move,
                                 slippage=slippage,
                                 max_entry=max_entry,
                                 min_edge=0.03)
                    s = summarise(r)
                    if s['trades'] < 20:
                        continue

                    label = (f'entry={entry_secs:3d}s  move={min_move*100:.3f}%  '
                             f'slip={slippage:.2f}  maxE={max_entry:.2f}')
                    print_summary(label, s)

                    all_rows.append({**{'entry_secs': entry_secs,
                                        'min_move': min_move,
                                        'slippage': slippage,
                                        'max_entry': max_entry}, **s})

                    if s['total_pnl'] > best_pnl:
                        best_pnl     = s['total_pnl']
                        best_params  = {'entry_secs': entry_secs,
                                        'min_move_pct': min_move,
                                        'slippage': slippage,
                                        'max_entry': max_entry}
                        best_results = r

    # ── Section 3: Best config deep-dive ─────────────────────────────────────
    if best_results:
        print('\n' + '=' * 70)
        print('  SECTION 3: BEST CONFIG DEEP-DIVE')
        print('=' * 70)
        print(f'\n  Parameters: {best_params}')
        s = summarise(best_results)
        print(f'  Trades:     {s["trades"]}')
        print(f'  Win rate:   {s["win_rate"]*100:.1f}%')
        print(f'  Total PnL:  ${s["total_pnl"]:.2f}')
        print(f'  Avg/trade:  ${s["avg_pnl"]:.2f}')
        print(f'  Max DD:     ${s["max_dd"]:.2f}')
        print(f'  Final eq:   ${s["final_eq"]:.2f}  (started ${STARTING_EQUITY})')

        # Win rate by move size
        print('\n  Win rate by BTC move at entry:')
        buckets = defaultdict(list)
        for r in best_results:
            bucket = round(abs(r['move_pct']) * 10) / 10
            buckets[bucket].append(r['won'])
        for move in sorted(buckets):
            wl = buckets[move]
            wr = sum(wl) / len(wl)
            bar = '#' * int(wr * 20)
            print(f'    {move:.1f}%  {bar:<20s}  {wr*100:5.1f}%  (n={len(wl)})')

        # Win rate by hour of day
        print('\n  Win rate by hour of day (UTC):')
        hour_buckets = defaultdict(list)
        for r in best_results:
            hour = int(r['time'].split(' ')[1].split(':')[0])
            hour_buckets[hour].append(r['won'])
        for h in sorted(hour_buckets):
            wl = hour_buckets[h]
            wr = sum(wl) / len(wl)
            bar = '#' * int(wr * 20)
            print(f'    {h:02d}:00  {bar:<20s}  {wr*100:5.1f}%  (n={len(wl)})')

        # Save to CSV
        csv_path = 'backtest_updown_results.csv'
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=best_results[0].keys())
            writer.writeheader()
            writer.writerows(best_results)
        print(f'\n  Trades saved to {csv_path}')

    # ── Section 4: Verdict ───────────────────────────────────────────────────
    print('\n' + '=' * 70)
    print('  VERDICT')
    print('=' * 70)

    if not best_results:
        print('\n  Not enough trades to evaluate. Try --days 60 for more data.')
        return

    s = summarise(best_results)
    wr = s['win_rate']

    if wr >= 0.58 and s['total_pnl'] > 0:
        print(f'\n  [YES] STRATEGY HAS EDGE  ({wr*100:.1f}% WR, ${s["total_pnl"]:.2f} PnL)')
        print('    The momentum signal predicts direction better than chance.')
        print('    Focus on: improving fill rate and API reliability.')
    elif wr >= 0.52:
        print(f'\n  [~] MARGINAL EDGE  ({wr*100:.1f}% WR, ${s["total_pnl"]:.2f} PnL)')
        print('    Signal is slightly better than random but transaction costs')
        print('    may eat the edge. Consider larger moves or longer windows.')
    else:
        print(f'\n  [NO] NO CLEAR EDGE  ({wr*100:.1f}% WR, ${s["total_pnl"]:.2f} PnL)')
        print('    Momentum at this timescale does not predict 5-min outcomes.')
        print('    Recommend testing different approaches before more live trading.')


if __name__ == '__main__':
    main()
