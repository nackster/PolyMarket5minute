"""
Crypto Momentum Strategy Backtester
=====================================
Rotates monthly between crypto assets based on momentum.
Goes to cash when assets are in downtrend (200d MA filter).

Why crypto beats stocks for this strategy:
  - BTC: $400 (Jan 2016) -> ~$85,000 (Mar 2026) = 21,000% buy-and-hold
  - ETH: $10  (Jan 2016) -> ~$2,000 (Mar 2026) = 20,000% buy-and-hold
  - Trend filter avoids the 80% bear markets (2018, 2022) entirely
  - Rotating into the best-momentum asset compounds gains further

Usage:
    python backtest_crypto.py                        # BTC+ETH, 2016-2026
    python backtest_crypto.py --top-k 1              # only #1 ranked asset
    python backtest_crypto.py --lookback 1           # 1-month momentum
    python backtest_crypto.py --no-trend-filter      # always invested
    python backtest_crypto.py --universe btc         # BTC only, trend-timed
    python backtest_crypto.py --compare              # show vs buy-and-hold
"""

import yfinance as yf
import json, os, argparse, contextlib, io, statistics
from datetime import datetime, timedelta
from collections import defaultdict

CACHE_FILE = "trades/crypto_prices.json"

# Available universes
# XRP had a 64,000% run in 2017 ($0.006 -> $3.84)
# LTC/ETH were the 2017 alt runners before SOL existed
# BNB available from 2017, SOL from 2020
UNIVERSES = {
    "btc":       ["BTC-USD"],
    "btc_eth":   ["BTC-USD", "ETH-USD"],
    "legacy":    ["BTC-USD", "ETH-USD", "XRP-USD", "LTC-USD"],          # all have 2016 data
    "legacy5":   ["BTC-USD", "ETH-USD", "XRP-USD", "LTC-USD", "BNB-USD"],
    "modern":    ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "AVAX-USD"],
    "all":       ["BTC-USD", "ETH-USD", "XRP-USD", "LTC-USD",
                  "BNB-USD", "SOL-USD", "AVAX-USD", "DOGE-USD"],
    "top3":      ["BTC-USD", "ETH-USD", "SOL-USD"],
    "defi":      ["ETH-USD", "SOL-USD", "AVAX-USD", "BNB-USD", "LINK-USD"],
}

NAMES = {
    "BTC-USD":  "Bitcoin",
    "ETH-USD":  "Ethereum",
    "SOL-USD":  "Solana",
    "BNB-USD":  "BNB",
    "XRP-USD":  "XRP",
    "LTC-USD":  "Litecoin",
    "AVAX-USD": "Avalanche",
    "LINK-USD": "Chainlink",
    "DOGE-USD": "Dogecoin",
    "ADA-USD":  "Cardano",
}


# ── Cache ─────────────────────────────────────────────────────────────────────

def load_cache():
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


# ── Price fetching ─────────────────────────────────────────────────────────────

def fetch_prices(tickers, start, end, cache):
    """Download OHLCV from yfinance; cache by ticker+start key."""
    needed = [t for t in tickers
              if not any(k.startswith(f"{t}|{start}") for k in cache)]

    if needed:
        print(f"  Downloading {len(needed)} tickers ({start} to {end})...")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            raw = yf.download(needed, start=start, end=end,
                              auto_adjust=True, progress=False)

        if raw.empty:
            print("  Warning: no data returned.")
        elif hasattr(raw.columns, "levels"):
            for tk in needed:
                try:
                    series = raw[("Close", tk)].dropna()
                    key = f"{tk}|{start}|{end}"
                    cache[key] = {str(d.date()): float(v) for d, v in series.items()}
                except Exception:
                    pass
        else:
            if len(needed) == 1 and "Close" in raw.columns:
                series = raw["Close"].dropna()
                key = f"{needed[0]}|{start}|{end}"
                cache[key] = {str(d.date()): float(v) for d, v in series.items()}

    result = {}
    for tk in tickers:
        prices = {}
        for k, v in cache.items():
            if k.startswith(f"{tk}|"):
                prices.update(v)
        if prices:
            result[tk] = dict(sorted(prices.items()))
    return result


def get_price(prices_dict, date_str, forward=True, window=7):
    """Find closest price within window days."""
    if not prices_dict:
        return None, None
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(window):
        d = dt + timedelta(days=i if forward else -i)
        ds = d.strftime("%Y-%m-%d")
        if ds in prices_dict:
            return prices_dict[ds], ds
    return None, None


def month_starts(start_date, end_date):
    """Generate first-of-month dates."""
    dates = []
    dt = datetime.strptime(start_date, "%Y-%m-%d").replace(day=1)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    while dt <= end_dt:
        dates.append(dt.strftime("%Y-%m-%d"))
        if dt.month == 12:
            dt = dt.replace(year=dt.year + 1, month=1)
        else:
            dt = dt.replace(month=dt.month + 1)
    return dates


def rebal_dates(start_date, end_date, period="monthly"):
    """Generate rebalance dates. period: monthly | biweekly | weekly."""
    if period == "monthly":
        return month_starts(start_date, end_date)
    step = 7 if period == "weekly" else 14
    dates = []
    dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    while dt <= end_dt:
        dates.append(dt.strftime("%Y-%m-%d"))
        dt += timedelta(days=step)
    return dates


# ── Trend detection ────────────────────────────────────────────────────────────

def compute_200d_ma(prices_dict, ma_days=200):
    """Return dict of {date: ma_value} for N-day MA."""
    dates = sorted(prices_dict.keys())
    closes = [prices_dict[d] for d in dates]
    ma = {}
    for i in range(ma_days - 1, len(closes)):
        ma[dates[i]] = sum(closes[i - ma_days + 1 : i + 1]) / ma_days
    return ma


def is_in_uptrend(date_str, prices_dict, ma_dict, window=7):
    """True if price > 200d MA on or near given date."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(window):
        ds = (dt + timedelta(days=i)).strftime("%Y-%m-%d")
        if ds in prices_dict and ds in ma_dict:
            return prices_dict[ds] > ma_dict[ds], prices_dict[ds], ma_dict[ds]
    return False, None, None


# ── Display helpers ───────────────────────────────────────────────────────────

def bar(val, max_val, width=35):
    if max_val == 0:
        return " " * width
    filled = min(int(round(abs(val) / max_val * width)), width)
    return ("#" if val >= 0 else "-") * filled + " " * (width - filled)


def print_sep(char="-", width=74):
    print(char * width)


def print_header(title, width=74):
    print_sep("=", width)
    pad = (width - len(title) - 2) // 2
    print("=" + " " * pad + title + " " * (width - pad - len(title) - 2) + "=")
    print_sep("=", width)


def fmt_pct(v):
    if v >= 10:
        return f"{v:>+,.0f}%"
    return f"{v:>+.1f}%"


# ── Core strategy ──────────────────────────────────────────────────────────────

def run_strategy(universe, start_date, end_date, lookback_months,
                 top_k, trend_filter, initial, cash_ticker, cache,
                 trend_ma_days=200, verbose=True, period="monthly",
                 vol_adjust=False):

    # Need extra history for 200d MA
    fetch_start = (datetime.strptime(start_date, "%Y-%m-%d")
                   - timedelta(days=260)).strftime("%Y-%m-%d")

    if verbose:
        print(f"  Fetching price data...")
    prices = fetch_prices(universe, fetch_start, end_date, cache)
    save_cache(cache)

    # Compute N-day MAs for trend filter
    ma_data = {tk: compute_200d_ma(prices.get(tk, {}), ma_days=trend_ma_days)
               for tk in universe}

    rebal_dates_list = rebal_dates(start_date, end_date, period=period)

    portfolio = {}   # {ticker: units_held}
    cash = initial
    equity = initial

    monthly = {}          # {month: equity_snapshot}
    trades_log = []
    in_cash_months = 0
    monthly_returns = []

    held_ticker = None
    prev_equity = initial

    for rebal_date in rebal_dates_list:
        # ── Mark-to-market ──────────────────────────────────────────────────
        port_val = cash
        for tk, units in portfolio.items():
            p, _ = get_price(prices.get(tk, {}), rebal_date, forward=False)
            if p:
                port_val += units * p
        equity = port_val

        period_ret = (equity - prev_equity) / prev_equity * 100 if prev_equity else 0
        monthly_returns.append(period_ret)
        monthly[rebal_date] = equity  # full date key for sub-monthly periods
        prev_equity = equity

        # ── Check trend for each asset ────────────────────────────────────
        uptrend = {}
        for tk in universe:
            up, price, ma = is_in_uptrend(rebal_date, prices.get(tk, {}), ma_data[tk])
            uptrend[tk] = up

        any_uptrend = any(uptrend.values())

        if trend_filter and not any_uptrend:
            # All assets in downtrend -> go to cash
            if portfolio:
                for tk, units in list(portfolio.items()):
                    p, _ = get_price(prices.get(tk, {}), rebal_date)
                    if p:
                        cash += units * p
                portfolio = {}
                held_ticker = None
                trades_log.append({"date": rebal_date, "action": "CASH",
                                   "ticker": "CASH", "reason": "All below 200d MA"})
            in_cash_months += 1
            continue

        # ── Compute momentum ──────────────────────────────────────────────
        lb_date = (datetime.strptime(rebal_date, "%Y-%m-%d")
                   - timedelta(days=lookback_months * 30)).strftime("%Y-%m-%d")

        scores = {}
        for tk in universe:
            if trend_filter and not uptrend[tk]:
                continue  # skip assets in downtrend
            p_start, _ = get_price(prices.get(tk, {}), lb_date, forward=True)
            p_now,   _ = get_price(prices.get(tk, {}), rebal_date, forward=False)
            if p_start and p_now and p_start > 0:
                raw_score = (p_now - p_start) / p_start
                if vol_adjust:
                    # Collect daily returns over lookback window for volatility
                    tk_prices = prices.get(tk, {})
                    lb_dt = datetime.strptime(lb_date, "%Y-%m-%d")
                    rb_dt = datetime.strptime(rebal_date, "%Y-%m-%d")
                    daily_rets = []
                    prev_p = None
                    for day_i in range((rb_dt - lb_dt).days + 1):
                        ds = (lb_dt + timedelta(days=day_i)).strftime("%Y-%m-%d")
                        if ds in tk_prices:
                            if prev_p is not None and prev_p > 0:
                                daily_rets.append(tk_prices[ds] / prev_p - 1)
                            prev_p = tk_prices[ds]
                    if len(daily_rets) >= 10:
                        vol = statistics.stdev(daily_rets) or 0.0001
                        scores[tk] = raw_score / vol
                    else:
                        scores[tk] = raw_score
                else:
                    scores[tk] = raw_score

        if not scores:
            in_cash_months += 1
            continue

        # Rank and pick top K
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        winners = [tk for tk, _ in ranked[:top_k]]

        # ── Rebalance ─────────────────────────────────────────────────────
        current_tickers = set(portfolio.keys())
        target_tickers  = set(winners)

        if current_tickers != target_tickers:
            # Liquidate what we don't want
            for tk in list(portfolio.keys()):
                if tk not in target_tickers:
                    p, _ = get_price(prices.get(tk, {}), rebal_date)
                    if p:
                        cash += portfolio[tk] * p
                        del portfolio[tk]

            # Buy what we want
            alloc = equity / len(winners)
            for tk in winners:
                if tk not in portfolio:
                    p, _ = get_price(prices.get(tk, {}), rebal_date)
                    if p and p > 0:
                        units = alloc / p
                        portfolio[tk] = units
                        cash -= alloc
                        trades_log.append({
                            "date": rebal_date,
                            "action": "BUY",
                            "ticker": tk,
                            "price": p,
                            "momentum": scores.get(tk, 0),
                            "equity": equity,
                        })

        # Display
        if verbose:
            scores_str = "  |  ".join(
                f"{tk}({scores.get(tk,0)*100:+.0f}%)" for tk in winners
            )
            trend_str = "  ".join(
                f"{'[UP]' if uptrend[tk] else '[DN]'}{tk}" for tk in universe
            )
            print(f"  {rebal_date[:7]}  BUY: {scores_str:<50}  {trend_str}")

    # ── Final liquidation ────────────────────────────────────────────────────
    final = cash
    for tk, units in portfolio.items():
        p, _ = get_price(prices.get(tk, {}), end_date, forward=False)
        if p:
            final += units * p

    return final, monthly, trades_log, monthly_returns, in_cash_months, prices


# ── Buy-and-hold comparison ───────────────────────────────────────────────────

def buy_and_hold(ticker, start_date, end_date, initial, prices):
    p_start, _ = get_price(prices.get(ticker, {}), start_date, forward=True)
    p_end,   _ = get_price(prices.get(ticker, {}), end_date,   forward=False)
    if p_start and p_end and p_start > 0:
        return initial * (p_end / p_start), p_start, p_end
    return initial, None, None


# ── Results display ───────────────────────────────────────────────────────────

def display_results(final, initial, monthly, trades_log, monthly_returns,
                    in_cash_months, start_date, end_date, universe, prices, compare):

    years = (datetime.strptime(end_date,   "%Y-%m-%d") -
             datetime.strptime(start_date, "%Y-%m-%d")).days / 365.25
    total_ret = (final - initial) / initial * 100
    cagr      = ((final / initial) ** (1 / years) - 1) * 100 if years > 0 else 0

    print_header("RESULTS")
    print(f"  Initial capital:  ${initial:>12,.2f}")
    print(f"  Final equity:     ${final:>12,.2f}  ({fmt_pct(total_ret)})")
    print(f"  CAGR:             {cagr:>+12.2f}%/yr  ({years:.1f} years)")
    print(f"  Cash months:      {in_cash_months} (trend filter blocked entries)")
    print()

    if compare:
        print_sep("-")
        print(f"  Buy-and-Hold Comparison:")
        for tk in universe:
            bh, ps, pe = buy_and_hold(tk, start_date, end_date, initial, prices)
            bh_ret  = (bh - initial) / initial * 100
            bh_cagr = ((bh / initial) ** (1 / years) - 1) * 100 if years > 0 else 0
            name = NAMES.get(tk, tk)
            price_str = f"  ${ps:.0f} -> ${pe:.0f}" if ps and pe else ""
            print(f"    {name:<12}  ${bh:>12,.0f}  ({fmt_pct(bh_ret)}, {bh_cagr:+.1f}%/yr){price_str}")
        print()

    # Drawdown calculation
    if monthly:
        vals = [monthly[m] for m in sorted(monthly.keys())]
        peak = vals[0]
        max_dd = 0
        for v in vals:
            if v > peak:
                peak = v
            dd = (peak - v) / peak * 100
            if dd > max_dd:
                max_dd = dd
        print(f"  Max drawdown:     {-max_dd:>+10.1f}%")

    if monthly_returns:
        wins = sum(1 for r in monthly_returns if r > 0)
        print(f"  Winning months:   {wins}/{len(monthly_returns)} ({wins/len(monthly_returns)*100:.0f}%)")
        print(f"  Best month:       {fmt_pct(max(monthly_returns))}")
        print(f"  Worst month:      {fmt_pct(min(monthly_returns))}")
    print()

    # Equity curve — show one row per month (last snapshot in month)
    if monthly:
        all_dates = sorted(monthly.keys())
        # Collapse to one-per-month: keep last snapshot per YYYY-MM
        month_snap = {}
        for d in all_dates:
            ym = d[:7]
            month_snap[ym] = monthly[d]  # later dates overwrite, giving last of month

        months = sorted(month_snap.keys())
        max_eq = max(month_snap.values())
        min_eq = min(initial * 0.5, min(month_snap.values()))

        print(f"  {'Month':<10}  {'Equity':>14}  {'Total Return':>13}  Curve")
        print_sep()
        for ym in months:
            eq  = month_snap[ym]
            ret = (eq - initial) / initial * 100
            frac = (eq - min_eq) / (max_eq - min_eq) if max_eq != min_eq else 0
            b = "#" * int(frac * 30)
            print(f"  {ym:<10}  ${eq:>13,.0f}  {fmt_pct(ret):>13}  {b}")
        print()

    # Trade log summary
    buys = [t for t in trades_log if t["action"] == "BUY"]
    if buys:
        print_header("TRADE FREQUENCY BY TICKER")
        counter = defaultdict(int)
        for t in buys:
            counter[t["ticker"]] += 1
        for tk, cnt in sorted(counter.items(), key=lambda x: -x[1]):
            name = NAMES.get(tk, tk)
            b = bar(cnt, max(counter.values()), 25)
            print(f"  {name:<14}  {cnt:>3} periods held  {b}")
        print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Crypto Momentum Backtester")
    parser.add_argument("--universe",  default="btc_eth",
                        choices=list(UNIVERSES.keys()),
                        help="Asset universe (default: btc_eth)")
    parser.add_argument("--tickers",   default="",
                        help="Custom comma-sep tickers, e.g. BTC-USD,ETH-USD,SOL-USD")
    parser.add_argument("--start",     default="2016-01-01")
    parser.add_argument("--end",       default="2026-03-01")
    parser.add_argument("--lookback",  type=int, default=3, metavar="MONTHS",
                        help="Momentum lookback in months (default: 3)")
    parser.add_argument("--top-k",     type=int, default=1,
                        help="# assets to hold (default: 1 = best only)")
    parser.add_argument("--initial",   type=float, default=10000)
    parser.add_argument("--no-trend-filter", action="store_true",
                        help="Stay invested regardless of trend")
    parser.add_argument("--trend-ma", type=int, default=200, metavar="DAYS",
                        help="MA period for trend filter (default 200, try 100/150 for faster re-entry)")
    parser.add_argument("--compare",   action="store_true", default=True,
                        help="Show buy-and-hold comparison")
    parser.add_argument("--no-compare", action="store_false", dest="compare")
    parser.add_argument("--period", default="monthly",
                        choices=["monthly", "biweekly", "weekly"],
                        help="Rebalance frequency (default: monthly)")
    parser.add_argument("--vol-adjust", action="store_true",
                        help="Score = momentum / volatility (Sharpe-style ranking)")
    parser.add_argument("--export", action="store_true",
                        help="Save backtest trades to trades/backtest_crypto_trades.json for dashboard")
    parser.add_argument("--optimize", action="store_true",
                        help="Sweep all universe/lookback/MA combinations and rank by return")
    args = parser.parse_args()

    universe = ([t.strip() for t in args.tickers.split(",") if t.strip()]
                if args.tickers else UNIVERSES[args.universe])
    trend_filter = not args.no_trend_filter

    cache = load_cache()

    # ── Optimization sweep ────────────────────────────────────────────────────
    if args.optimize:
        print()
        print_header("OPTIMIZATION SWEEP — finding best combination")
        print(f"  Date range: {args.start} to {args.end}   Initial: ${args.initial:,.0f}")
        print()

        sweep_universes  = ["btc_eth", "top3", "legacy"]
        sweep_lookbacks  = [1, 2, 3]
        sweep_top_k      = [1, 2]
        sweep_trend_mas  = [100, 150, 200]
        sweep_periods    = ["monthly", "weekly"]

        # Pre-fetch all needed prices once
        all_tickers = list({t for u in sweep_universes for t in UNIVERSES[u]})
        fetch_start = (datetime.strptime(args.start, "%Y-%m-%d")
                       - timedelta(days=260)).strftime("%Y-%m-%d")
        print(f"  Pre-fetching prices for {len(all_tickers)} tickers...")
        fetch_prices(all_tickers, fetch_start, args.end, cache)
        save_cache(cache)
        print()

        results = []
        total = (len(sweep_universes)*len(sweep_lookbacks)*len(sweep_top_k)
                 *len(sweep_trend_mas)*len(sweep_periods))
        done = 0

        for univ_key in sweep_universes:
            for lb in sweep_lookbacks:
                for tk_count in sweep_top_k:
                    for tma in sweep_trend_mas:
                        for per in sweep_periods:
                            done += 1
                            univ = UNIVERSES[univ_key]
                            final, monthly, _, monthly_rets, cash_mo, _ = run_strategy(
                                universe=univ, start_date=args.start, end_date=args.end,
                                lookback_months=lb, top_k=tk_count, trend_filter=True,
                                initial=args.initial, cash_ticker=None, cache=cache,
                                trend_ma_days=tma, verbose=False, period=per,
                            )
                        total_ret = (final - args.initial) / args.initial * 100
                        years = (datetime.strptime(args.end, "%Y-%m-%d") -
                                 datetime.strptime(args.start, "%Y-%m-%d")).days / 365.25
                        cagr = ((final / args.initial) ** (1/years) - 1) * 100 if years > 0 else 0

                        vals = [monthly[m] for m in sorted(monthly.keys())] if monthly else [args.initial]
                        peak = args.initial
                        max_dd = 0
                        peak_ret = 0
                        for v in vals:
                            if v > peak:
                                peak = v
                                peak_ret = (peak - args.initial) / args.initial * 100
                            dd = (peak - v) / peak * 100
                            if dd > max_dd:
                                max_dd = dd

                            results.append({
                                "universe": univ_key,
                                "lookback": lb,
                                "top_k": tk_count,
                                "trend_ma": tma,
                                "period": per,
                                "final": final,
                                "total_ret": total_ret,
                                "peak_ret": peak_ret,
                                "cagr": cagr,
                                "max_dd": max_dd,
                            })
                            print(f"  [{done:>3}/{total}] {univ_key:<8} lb={lb} k={tk_count} ma={tma:>3}d {per:<9}  "
                                  f"Return: {fmt_pct(total_ret):>10}  Peak: {fmt_pct(peak_ret):>10}  "
                                  f"CAGR: {cagr:+.1f}%/yr  MaxDD: -{max_dd:.0f}%")

        print()
        print_header("TOP 15 COMBINATIONS BY PEAK RETURN")
        results.sort(key=lambda x: x["peak_ret"], reverse=True)
        print(f"  {'Universe':<10} {'LB':>3} {'K':>2} {'MA':>4}  {'Period':<9}  {'Final Return':>13}  "
              f"{'Peak Return':>12}  {'CAGR':>9}  {'MaxDD':>7}")
        print_sep()
        for r in results[:15]:
            print(f"  {r['universe']:<10} {r['lookback']:>3} {r['top_k']:>2} {r['trend_ma']:>4}d  "
                  f"{r['period']:<9}  {fmt_pct(r['total_ret']):>13}  {fmt_pct(r['peak_ret']):>12}  "
                  f"{r['cagr']:>+8.1f}%  -{r['max_dd']:>4.0f}%")
        print()
        print("  Re-run with best settings to see full equity curve.")
        return

    # ── Single run ────────────────────────────────────────────────────────────
    print()
    print_header("CRYPTO MOMENTUM BACKTESTER")
    print(f"  Universe:        {', '.join(NAMES.get(t, t) for t in universe)}")
    print(f"  Date range:      {args.start} to {args.end}")
    print(f"  Lookback:        {args.lookback} months")
    print(f"  Top K:           {args.top_k} asset(s)")
    print(f"  Period:          {args.period}")
    print(f"  Vol-adjust:      {'ON' if args.vol_adjust else 'OFF'}")
    ma_label = f"{args.trend_ma}d MA (go to cash in bear markets)" if trend_filter else "OFF"
    print(f"  Trend filter:    {ma_label}")
    print(f"  Initial:         ${args.initial:,.0f}")
    print()

    final, monthly, trades_log, monthly_returns, in_cash_months, prices = run_strategy(
        universe        = universe,
        start_date      = args.start,
        end_date        = args.end,
        lookback_months = args.lookback,
        top_k           = args.top_k,
        trend_filter    = trend_filter,
        initial         = args.initial,
        cash_ticker     = None,
        cache           = cache,
        trend_ma_days   = args.trend_ma,
        period          = args.period,
        vol_adjust      = args.vol_adjust,
    )

    print()
    display_results(
        final, args.initial, monthly, trades_log, monthly_returns,
        in_cash_months, args.start, args.end, universe, prices, args.compare
    )

    if args.export:
        years = (datetime.strptime(args.end, "%Y-%m-%d") -
                 datetime.strptime(args.start, "%Y-%m-%d")).days / 365.25
        total_ret = (final - args.initial) / args.initial * 100
        cagr = ((final / args.initial) ** (1 / years) - 1) * 100 if years > 0 else 0

        # Peak equity
        all_dates = sorted(monthly.keys())
        peak_equity = max(monthly[d] for d in all_dates) if all_dates else final
        peak_ret = (peak_equity - args.initial) / args.initial * 100

        # Max drawdown
        vals = [monthly[d] for d in all_dates]
        pk = args.initial
        max_dd = 0
        for v in vals:
            if v > pk:
                pk = v
            dd = (pk - v) / pk * 100
            if dd > max_dd:
                max_dd = dd

        # Build enriched trade list with equity_after
        export_trades = []
        buys = [t for t in trades_log if t["action"] == "BUY"]
        for t in buys:
            export_trades.append({
                "date":         t["date"],
                "ticker":       t["ticker"],
                "name":         NAMES.get(t["ticker"], t["ticker"]),
                "price":        round(t.get("price", 0), 4),
                "momentum_pct": round(t.get("momentum", 0) * 100, 1),
                "equity_after": round(t.get("equity", 0), 2),
            })

        export_data = {
            "settings": {
                "universe":  [NAMES.get(t, t) for t in universe],
                "tickers":   universe,
                "lookback":  args.lookback,
                "top_k":     args.top_k,
                "trend_ma":  args.trend_ma,
                "period":    args.period,
                "start":     args.start,
                "end":       args.end,
                "initial":   args.initial,
            },
            "summary": {
                "final":        round(final, 2),
                "peak":         round(peak_equity, 2),
                "total_return": round(total_ret, 1),
                "peak_return":  round(peak_ret, 1),
                "cagr":         round(cagr, 2),
                "max_dd":       round(max_dd, 1),
                "cash_periods": in_cash_months,
                "total_trades": len(buys),
            },
            "trades": export_trades,
        }

        os.makedirs("trades", exist_ok=True)
        out_path = "trades/backtest_crypto_trades.json"
        with open(out_path, "w") as f:
            json.dump(export_data, f, indent=2)
        print(f"  Exported {len(export_trades)} trades to {out_path}")

        try:
            from db import save_backtest_trades
            save_backtest_trades("crypto_momentum", export_trades)
            print(f"  Also saved {len(export_trades)} trades to database")
        except Exception:
            pass


if __name__ == "__main__":
    main()
