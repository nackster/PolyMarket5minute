"""
Momentum Strategy Backtester
=============================
Two strategies in one file:

1. SECTOR MOMENTUM — buy the best-performing S&P 500 sectors each month.
   - Universe: 11 SPDR sector ETFs (XLK, XLF, XLE, XLV, XLI, XLC, XLY, XLP, XLU, XLRE, XLB)
   - Signal: rank by prior N-month return (default 3)
   - Portfolio: buy top K sectors (default 3), equal weight
   - Rebalance: monthly
   - Trend filter: only trade when SPY > 200d MA

2. STOCK MOMENTUM — buy top-ranked individual stocks by momentum.
   - Universe: configurable (default: large-cap tech + S&P 500 leaders)
   - Signal: 12-1 month momentum (12-month return minus last 1 month, avoids reversal)
   - Portfolio: top N stocks, equal weight
   - Rebalance: monthly

Both strategies avoid the look-ahead bias present in congressional copy-trading.

Usage:
    python backtest_momentum.py                        # sector momentum, 2020-2024
    python backtest_momentum.py --strategy stock       # stock momentum
    python backtest_momentum.py --lookback 6           # 6-month lookback
    python backtest_momentum.py --top-k 3              # top 3 sectors/stocks
    python backtest_momentum.py --start 2018-01-01     # longer backtest
    python backtest_momentum.py --no-trend-filter      # trade in all markets
"""

import yfinance as yf
import json, os, argparse, statistics, contextlib, io
from datetime import datetime, timedelta
from collections import defaultdict


# ── Universe definitions ──────────────────────────────────────────────────────

SECTOR_ETFS = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLE":  "Energy",
    "XLV":  "Health Care",
    "XLI":  "Industrials",
    "XLC":  "Comm Services",
    "XLY":  "Cons Discretionary",
    "XLP":  "Cons Staples",
    "XLU":  "Utilities",
    "XLRE": "Real Estate",
    "XLB":  "Materials",
}

# Large-cap momentum universe: S&P 500 leaders + high-liquidity growth stocks
STOCK_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "AMD",
    # Financials
    "JPM", "BAC", "GS", "V", "MA", "BRK-B",
    # Healthcare
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE",
    # Consumer
    "COST", "WMT", "HD", "NKE", "SBUX", "MCD",
    # Industrials + Energy
    "CAT", "DE", "XOM", "CVX", "SLB",
    # ETFs for broad exposure
    "SPY", "QQQ", "IWM",
    # High-momentum growth (historically strong)
    "NOW", "CRM", "ADBE", "INTU", "PANW", "CRWD", "DDOG", "SNOW",
]

CACHE_FILE = "trades/momentum_prices.json"


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


# ── Price fetching ────────────────────────────────────────────────────────────

def fetch_prices(tickers, start, end, cache):
    """Fetch daily close prices, using cache for speed."""
    needed = [t for t in tickers
              if not any(k.startswith(f"{t}|{start}") for k in cache)]

    if needed:
        print(f"  Downloading prices for {len(needed)} tickers ({start} to {end})...")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            raw = yf.download(needed, start=start, end=end,
                              auto_adjust=True, progress=False)

        if hasattr(raw.columns, 'levels'):
            for tk in needed:
                try:
                    col = ("Close", tk)
                    if col not in raw.columns:
                        continue
                    series = raw[col].dropna()
                    key = f"{tk}|{start}|{end}"
                    cache[key] = {str(d.date()): float(v)
                                  for d, v in series.items()}
                except Exception:
                    pass
        else:
            # Single ticker returned flat DataFrame
            if "Close" in raw.columns and len(needed) == 1:
                series = raw["Close"].dropna()
                key = f"{needed[0]}|{start}|{end}"
                cache[key] = {str(d.date()): float(v)
                              for d, v in series.items()}

    # Reconstruct {ticker: {date: price}} from cache
    result = {}
    for tk in tickers:
        prices = {}
        for k, v in cache.items():
            if k.startswith(f"{tk}|"):
                prices.update(v)
        if prices:
            result[tk] = dict(sorted(prices.items()))
    return result


def get_price(prices_dict, date_str, forward=True, window=5):
    """Get price on date or nearest trading day (forward or backward)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(window):
        d = dt + timedelta(days=i if forward else -i)
        ds = d.strftime("%Y-%m-%d")
        if ds in prices_dict:
            return prices_dict[ds], ds
    return None, None


def month_end_dates(start_date, end_date):
    """Generate last-trading-day-of-month dates between start and end."""
    dates = []
    dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Round to first of month, then iterate
    current = dt.replace(day=1)
    while current <= end_dt:
        # Last day of this month
        if current.month == 12:
            last = current.replace(year=current.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            last = current.replace(month=current.month + 1, day=1) - timedelta(days=1)
        last = min(last, end_dt)
        dates.append(last.strftime("%Y-%m-%d"))
        # Move to first of next month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1, day=1)
        else:
            current = current.replace(month=current.month + 1, day=1)
    return dates


# ── Trend filter ──────────────────────────────────────────────────────────────

def compute_bull_dates(spy_prices, ma_days=200):
    """Return set of dates where SPY > N-day MA."""
    dates = sorted(spy_prices.keys())
    closes = [spy_prices[d] for d in dates]
    bull = set()
    for i in range(ma_days - 1, len(closes)):
        ma = sum(closes[i - ma_days + 1 : i + 1]) / ma_days
        if closes[i] > ma:
            bull.add(dates[i])
    return bull


def in_bull_market(date_str, bull_dates, spy_prices, window=5):
    """Check if market is in bull trend on given date (within 5-day window)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(window):
        ds = (dt + timedelta(days=i)).strftime("%Y-%m-%d")
        if ds in spy_prices:
            return ds in bull_dates
    return False


# ── Display helpers ───────────────────────────────────────────────────────────

def bar(val, max_val, width=35):
    if max_val == 0:
        return " " * width
    filled = int(round(abs(val) / max_val * width))
    filled = min(filled, width)
    char = "#" if val >= 0 else "-"
    return char * filled + " " * (width - filled)


def print_sep(char="-", width=72):
    print(char * width)


def print_header(title, width=72):
    print_sep("=", width)
    pad = (width - len(title) - 2) // 2
    print("=" + " " * pad + title + " " * (width - pad - len(title) - 2) + "=")
    print_sep("=", width)


# ── Sector Momentum Strategy ──────────────────────────────────────────────────

def run_sector_momentum(start_date, end_date, lookback_months, top_k,
                        trend_filter, initial, cache):
    """
    Monthly sector rotation: buy top-K sectors by prior lookback-month return.
    Returns (monthly_equity, all_trades) for analysis.
    """
    print_header("SECTOR MOMENTUM BACKTEST")
    print(f"  Universe:        {len(SECTOR_ETFS)} SPDR sector ETFs")
    print(f"  Lookback:        {lookback_months} months")
    print(f"  Top K:           {top_k} sectors per month")
    print(f"  Trend filter:    {'SPY > 200d MA' if trend_filter else 'DISABLED'}")
    print(f"  Date range:      {start_date} to {end_date}")
    print()

    # Fetch prices: need lookback months of extra history before start
    fetch_start = (datetime.strptime(start_date, "%Y-%m-%d")
                   - timedelta(days=lookback_months * 35 + 250)).strftime("%Y-%m-%d")

    all_tickers = list(SECTOR_ETFS.keys()) + ["SPY"]
    prices = fetch_prices(all_tickers, fetch_start, end_date, cache)
    save_cache(cache)

    spy_prices = prices.get("SPY", {})
    bull_dates = compute_bull_dates(spy_prices) if trend_filter else set()

    rebal_dates = month_end_dates(start_date, end_date)

    portfolio = {}       # {ticker: shares}
    cash = initial
    equity = initial
    monthly_equity = {}
    all_trades = []
    skipped_bear = 0

    prev_date = None

    for i, rebal_date in enumerate(rebal_dates):
        # Compute portfolio value at rebalance
        if portfolio:
            port_val = cash
            for tk, shares in portfolio.items():
                price, _ = get_price(prices.get(tk, {}), rebal_date, forward=False)
                if price:
                    port_val += shares * price
            equity = port_val

        monthly_equity[rebal_date[:7]] = equity

        # Check trend filter
        if trend_filter and not in_bull_market(rebal_date, bull_dates, spy_prices):
            # Go to cash — liquidate all positions
            if portfolio:
                for tk in list(portfolio.keys()):
                    price, _ = get_price(prices.get(tk, {}), rebal_date, forward=False)
                    if price:
                        cash += portfolio[tk] * price
                portfolio = {}
                skipped_bear += 1
            continue

        # Compute momentum for each sector
        sector_returns = {}
        lookback_start = (datetime.strptime(rebal_date, "%Y-%m-%d")
                          - timedelta(days=lookback_months * 30)).strftime("%Y-%m-%d")

        for tk in SECTOR_ETFS:
            tk_prices = prices.get(tk, {})
            if not tk_prices:
                continue
            entry_p, _ = get_price(tk_prices, lookback_start, forward=True)
            exit_p,  _ = get_price(tk_prices, rebal_date, forward=False)
            if entry_p and exit_p and entry_p > 0:
                sector_returns[tk] = (exit_p - entry_p) / entry_p

        if len(sector_returns) < top_k:
            continue

        # Rank and select top K
        ranked = sorted(sector_returns.items(), key=lambda x: x[1], reverse=True)
        top_sectors = [tk for tk, _ in ranked[:top_k]]

        # Liquidate old positions
        if portfolio:
            for tk in list(portfolio.keys()):
                price, _ = get_price(prices.get(tk, {}), rebal_date, forward=False)
                if price:
                    cash += portfolio[tk] * price
            portfolio = {}

        # Buy top sectors equally
        alloc_per = equity / top_k
        for tk in top_sectors:
            price, actual = get_price(prices.get(tk, {}), rebal_date)
            if price and price > 0:
                shares = alloc_per / price
                portfolio[tk] = portfolio.get(tk, 0) + shares
                cash -= alloc_per
                all_trades.append({
                    "date": actual or rebal_date,
                    "ticker": tk,
                    "sector": SECTOR_ETFS[tk],
                    "momentum": sector_returns.get(tk, 0),
                    "price": price,
                    "rank": ranked.index((tk, sector_returns.get(tk, 0))) + 1,
                })

        # Display current selection
        month_label = rebal_date[:7]
        tops_str = ", ".join(f"{tk}({sector_returns[tk]*100:+.1f}%)" for tk in top_sectors)
        print(f"  {month_label}  =>  {tops_str}")

        prev_date = rebal_date

    # Final liquidation
    final_val = cash
    for tk, shares in portfolio.items():
        price, _ = get_price(prices.get(tk, {}), end_date, forward=False)
        if price:
            final_val += shares * price
    equity = final_val

    print()
    print(f"  Bear-market months skipped: {skipped_bear}")
    print()

    return monthly_equity, all_trades, equity


# ── Stock Momentum Strategy ────────────────────────────────────────────────────

def run_stock_momentum(start_date, end_date, lookback_months, skip_months,
                       top_k, trend_filter, initial, universe, cache):
    """
    Monthly stock momentum: buy top-K stocks by (lookback - skip)-month momentum.
    """
    print_header("STOCK MOMENTUM BACKTEST")
    print(f"  Universe:        {len(universe)} stocks")
    print(f"  Lookback:        {lookback_months} months (skip last {skip_months})")
    print(f"  Top K:           {top_k} stocks per month")
    print(f"  Trend filter:    {'SPY > 200d MA' if trend_filter else 'DISABLED'}")
    print(f"  Date range:      {start_date} to {end_date}")
    print()

    fetch_start = (datetime.strptime(start_date, "%Y-%m-%d")
                   - timedelta(days=lookback_months * 35 + 250)).strftime("%Y-%m-%d")

    all_tickers = list(set(universe) | {"SPY"})
    prices = fetch_prices(all_tickers, fetch_start, end_date, cache)
    save_cache(cache)

    spy_prices = prices.get("SPY", {})
    bull_dates = compute_bull_dates(spy_prices) if trend_filter else set()

    rebal_dates = month_end_dates(start_date, end_date)

    portfolio = {}
    cash = initial
    equity = initial
    monthly_equity = {}
    all_trades = []
    skipped_bear = 0

    for rebal_date in rebal_dates:
        # Compute portfolio value
        if portfolio:
            port_val = cash
            for tk, shares in portfolio.items():
                price, _ = get_price(prices.get(tk, {}), rebal_date, forward=False)
                if price:
                    port_val += shares * price
            equity = port_val

        monthly_equity[rebal_date[:7]] = equity

        if trend_filter and not in_bull_market(rebal_date, bull_dates, spy_prices):
            if portfolio:
                for tk in list(portfolio.keys()):
                    price, _ = get_price(prices.get(tk, {}), rebal_date, forward=False)
                    if price:
                        cash += portfolio[tk] * price
                portfolio = {}
                skipped_bear += 1
            continue

        # Compute (lookback - skip) momentum
        lb_start = (datetime.strptime(rebal_date, "%Y-%m-%d")
                    - timedelta(days=lookback_months * 30)).strftime("%Y-%m-%d")
        skip_end = (datetime.strptime(rebal_date, "%Y-%m-%d")
                    - timedelta(days=skip_months * 30)).strftime("%Y-%m-%d")

        momentum_scores = {}
        for tk in universe:
            tk_prices = prices.get(tk, {})
            if not tk_prices:
                continue
            entry_p, _ = get_price(tk_prices, lb_start, forward=True)
            exit_p,  _ = get_price(tk_prices, skip_end, forward=False)
            if entry_p and exit_p and entry_p > 0:
                momentum_scores[tk] = (exit_p - entry_p) / entry_p

        if len(momentum_scores) < top_k:
            continue

        ranked = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
        top_stocks = [tk for tk, _ in ranked[:top_k]]

        # Liquidate
        if portfolio:
            for tk in list(portfolio.keys()):
                price, _ = get_price(prices.get(tk, {}), rebal_date, forward=False)
                if price:
                    cash += portfolio[tk] * price
            portfolio = {}

        # Buy top stocks equally
        alloc_per = equity / top_k
        for tk in top_stocks:
            price, actual = get_price(prices.get(tk, {}), rebal_date)
            if price and price > 0:
                shares = alloc_per / price
                portfolio[tk] = portfolio.get(tk, 0) + shares
                cash -= alloc_per
                all_trades.append({
                    "date": actual or rebal_date,
                    "ticker": tk,
                    "momentum": momentum_scores.get(tk, 0),
                    "price": price,
                })

        month_label = rebal_date[:7]
        tops_str = ", ".join(f"{tk}({momentum_scores[tk]*100:+.0f}%)" for tk in top_stocks)
        print(f"  {month_label}  =>  {tops_str[:70]}")

    # Final liquidation
    final_val = cash
    for tk, shares in portfolio.items():
        price, _ = get_price(prices.get(tk, {}), end_date, forward=False)
        if price:
            final_val += shares * price
    equity = final_val

    print()
    print(f"  Bear-market months skipped: {skipped_bear}")
    print()

    return monthly_equity, all_trades, equity


# ── Benchmark (buy & hold SPY) ────────────────────────────────────────────────

def spy_benchmark(start_date, end_date, initial, prices):
    spy_p = prices.get("SPY", {})
    entry, _ = get_price(spy_p, start_date, forward=True)
    exit_,  _ = get_price(spy_p, end_date, forward=False)
    if entry and exit_:
        return initial * (exit_ / entry)
    return initial


# ── Display results ───────────────────────────────────────────────────────────

def display_results(strategy_name, monthly_equity, final_equity, initial,
                    all_trades, prices, start_date, end_date):
    print_header(f"RESULTS: {strategy_name}")

    total_ret = (final_equity - initial) / initial * 100
    years = (datetime.strptime(end_date, "%Y-%m-%d") -
             datetime.strptime(start_date, "%Y-%m-%d")).days / 365.25
    cagr = ((final_equity / initial) ** (1 / years) - 1) * 100 if years > 0 else 0

    # SPY benchmark
    spy_cache = load_cache()
    spy_prices_all = {}
    for k, v in spy_cache.items():
        if k.startswith("SPY|"):
            spy_prices_all.update(v)
    spy_final = spy_benchmark(start_date, end_date, initial, {"SPY": spy_prices_all})
    spy_ret = (spy_final - initial) / initial * 100
    spy_cagr = ((spy_final / initial) ** (1 / years) - 1) * 100 if years > 0 else 0

    print(f"  Initial:          ${initial:>10,.2f}")
    print(f"  Final equity:     ${final_equity:>10,.2f}  ({total_ret:+.1f}%)")
    print(f"  CAGR:             {cagr:>+10.2f}%/yr")
    print(f"  SPY benchmark:    ${spy_final:>10,.2f}  ({spy_ret:+.1f}%, {spy_cagr:+.1f}%/yr)")
    print(f"  Excess return:    {total_ret - spy_ret:>+10.2f}%  (total)")
    print(f"  CAGR alpha:       {cagr - spy_cagr:>+10.2f}%/yr")
    print(f"  Months traded:    {len(monthly_equity)}")
    print()

    # Equity curve
    if monthly_equity:
        months = sorted(monthly_equity.keys())
        max_eq = max(monthly_equity.values())
        min_eq = min(monthly_equity.values())
        span = max_eq - min_eq if max_eq != min_eq else 1

        print(f"  {'Month':<10}  {'Equity':>12}  {'Return':>8}  Curve")
        print_sep()
        prev_eq = initial
        for ym in months:
            eq = monthly_equity[ym]
            month_ret = (eq - prev_eq) / prev_eq * 100 if prev_eq else 0
            frac = (eq - min_eq) / span
            b = "#" * int(frac * 30)
            print(f"  {ym:<10}  ${eq:>11,.0f}  {month_ret:>+7.1f}%  {b}")
            prev_eq = eq
        print()

    # Most selected tickers/sectors
    ticker_count = defaultdict(int)
    for t in all_trades:
        ticker_count[t["ticker"]] += 1
    print(f"  {'Ticker/Sector':<12}  {'Times Selected':>14}")
    print_sep("-", 40)
    for tk, cnt in sorted(ticker_count.items(), key=lambda x: -x[1])[:15]:
        b = bar(cnt, max(ticker_count.values()), 20)
        print(f"  {tk:<12}  {cnt:>14}  {b}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Momentum Strategy Backtester")
    parser.add_argument("--strategy", default="sector",
                        choices=["sector", "stock"],
                        help="Strategy type: sector (rotation) or stock (momentum)")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--lookback", type=int, default=3, metavar="MONTHS",
                        help="Momentum lookback period in months (default: 3)")
    parser.add_argument("--skip", type=int, default=0, metavar="MONTHS",
                        help="Skip last N months for stock momentum (avoids reversal, default: 1)")
    parser.add_argument("--top-k", type=int, default=3,
                        help="Number of sectors/stocks to hold (default: 3)")
    parser.add_argument("--initial", type=float, default=10000)
    parser.add_argument("--no-trend-filter", action="store_true",
                        help="Disable SPY > 200d MA trend filter")
    parser.add_argument("--universe", default="default",
                        help="Stock universe: 'default', 'tech', or comma-sep tickers")
    args = parser.parse_args()

    cache = load_cache()

    trend_filter = not args.no_trend_filter

    if args.strategy == "sector":
        monthly_equity, all_trades, final_equity = run_sector_momentum(
            args.start, args.end,
            lookback_months=args.lookback,
            top_k=args.top_k,
            trend_filter=trend_filter,
            initial=args.initial,
            cache=cache,
        )
        display_results("SECTOR MOMENTUM", monthly_equity, final_equity,
                        args.initial, all_trades,
                        load_cache(), args.start, args.end)

    else:  # stock
        if args.universe == "tech":
            universe = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META",
                        "TSLA", "AVGO", "AMD", "NOW", "CRM", "ADBE", "INTU",
                        "PANW", "CRWD", "DDOG", "SNOW", "PLTR", "QQQ"]
        elif args.universe == "default":
            universe = STOCK_UNIVERSE
        else:
            universe = [t.strip().upper() for t in args.universe.split(",")]

        monthly_equity, all_trades, final_equity = run_stock_momentum(
            args.start, args.end,
            lookback_months=args.lookback,
            skip_months=args.skip,
            top_k=args.top_k,
            trend_filter=trend_filter,
            initial=args.initial,
            universe=universe,
            cache=cache,
        )
        display_results("STOCK MOMENTUM", monthly_equity, final_equity,
                        args.initial, all_trades,
                        load_cache(), args.start, args.end)


if __name__ == "__main__":
    main()
