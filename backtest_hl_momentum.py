"""
Hyperliquid Perpetuals Momentum Strategy Backtester
=====================================================
Extends spot momentum with perpetual futures mechanics:
  - LONG the strongest asset (above 200d MA) in bull markets
  - SHORT the weakest asset (below 200d MA) in bear markets
  - Funding rate: longs pay 0.01%/8hr, shorts receive 0.01%/8hr
  - Taker fee: 0.05% per side (0.10% round trip) on rotations
  - Optional leverage (default 1x)

Usage:
    python backtest_hl_momentum.py                         # top3, 2016-2026
    python backtest_hl_momentum.py --universe btc_eth      # BTC+ETH only
    python backtest_hl_momentum.py --lookback 1            # 1-month momentum
    python backtest_hl_momentum.py --no-short-bear         # go flat in bear market
    python backtest_hl_momentum.py --leverage 2            # 2x leverage
    python backtest_hl_momentum.py --export                # save JSON output
    python backtest_hl_momentum.py --compare               # vs spot backtest
"""

import yfinance as yf
import json, os, argparse, contextlib, io
from datetime import datetime, timedelta

CACHE_FILE    = "trades/crypto_prices.json"
HL_TRADES_FILE = "trades/hl_momentum_backtest.json"

FUNDING_RATE_8H = 0.00005  # 0.005%/8hr (~5.5%/yr) — realistic long-run HL average
TAKER_FEE       = 0.0005   # 0.05% per side
PERIODS_PER_DAY = 3        # funding settles 3x/day
REBAL_DAYS      = 7        # weekly rebalancing

UNIVERSES = {
    "btc":     ["BTC-USD"],
    "btc_eth": ["BTC-USD", "ETH-USD"],
    "top3":    ["BTC-USD", "ETH-USD", "SOL-USD"],
}

NAMES = {
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
    "SOL-USD": "Solana",
}


# ── Cache ──────────────────────────────────────────────────────────────────────

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


# ── Display helpers ────────────────────────────────────────────────────────────

def fmt_pct(v):
    if abs(v) >= 10:
        return f"{v:>+,.0f}%"
    return f"{v:>+.1f}%"


def print_sep(char="-", width=78):
    print(char * width)


def print_header(title, width=78):
    print_sep("=", width)
    pad = (width - len(title) - 2) // 2
    print("=" + " " * pad + title + " " * (width - pad - len(title) - 2) + "=")
    print_sep("=", width)


# ── Core HL strategy ──────────────────────────────────────────────────────────

def run_hl_strategy(universe, start_date, end_date, lookback_months,
                    top_k, initial, cache,
                    trend_ma_days=200, leverage=1,
                    funding_rate=FUNDING_RATE_8H, fee_rate=TAKER_FEE,
                    short_bear=True, verbose=True):
    """
    Simulate a Hyperliquid perpetuals momentum strategy.

    Bull market (any asset above 200d MA):
        LONG the highest-momentum asset that is above the MA.

    Bear market (all assets below 200d MA):
        SHORT the weakest asset (most negative momentum) if short_bear=True.
        Go flat (cash) if short_bear=False.

    Returns: (final_equity, monthly_dict, trades_log, peak_equity, max_drawdown)
    """
    # Extra history needed for 200d MA computation
    fetch_start = (datetime.strptime(start_date, "%Y-%m-%d")
                   - timedelta(days=260)).strftime("%Y-%m-%d")

    if verbose:
        print(f"  Fetching price data...")
    prices = fetch_prices(universe, fetch_start, end_date, cache)
    save_cache(cache)

    # Compute MAs for each asset
    ma_data = {tk: compute_200d_ma(prices.get(tk, {}), ma_days=trend_ma_days)
               for tk in universe}

    # Generate weekly rebalance dates
    rebal_date_list = []
    dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    while dt <= end_dt:
        rebal_date_list.append(dt.strftime("%Y-%m-%d"))
        dt += timedelta(days=REBAL_DAYS)

    equity = float(initial)
    monthly = {}           # {date_str: equity_snapshot}
    trades_log = []

    # Current position state
    position = None        # None | {"direction": "LONG"|"SHORT", "ticker": str,
                           #          "entry_price": float, "entry_date": str,
                           #          "entry_equity": float}

    in_cash_periods  = 0
    long_periods     = 0
    short_periods    = 0
    total_funding    = 0.0
    total_fees       = 0.0

    peak_equity = equity
    max_dd      = 0.0

    prev_date = None

    for rebal_date in rebal_date_list:
        # ── Days held since last rebal ───────────────────────────────────────
        days_held = 0
        if prev_date is not None:
            days_held = (datetime.strptime(rebal_date, "%Y-%m-%d") -
                         datetime.strptime(prev_date, "%Y-%m-%d")).days

        # ── Mark-to-market & apply funding ─────────────────────────────────
        funding_cost = 0.0
        pnl_this_period = 0.0

        if position is not None and days_held > 0:
            tk = position["ticker"]
            cur_price, _ = get_price(prices.get(tk, {}), rebal_date, forward=False)
            entry_price  = position["entry_price"]

            if cur_price is not None and entry_price > 0:
                if position["direction"] == "LONG":
                    pnl_this_period = equity * leverage * (cur_price / entry_price - 1)
                else:  # SHORT
                    pnl_this_period = equity * leverage * (1 - cur_price / entry_price)
                equity = max(0.0, equity + pnl_this_period)
                # Reset entry price to current for next period's PnL calculation
                position["entry_price"] = cur_price

            # Funding: longs pay, shorts receive
            funding_periods = days_held * PERIODS_PER_DAY
            funding_amount  = equity * leverage * funding_rate * funding_periods

            if position["direction"] == "LONG":
                equity       = max(0.0, equity - funding_amount)
                funding_cost = -funding_amount
            else:  # SHORT receives funding
                equity       += funding_amount
                funding_cost  = funding_amount

            total_funding += funding_cost

        monthly[rebal_date] = equity

        if equity > peak_equity:
            peak_equity = equity
        if peak_equity > 0:
            dd = (peak_equity - equity) / peak_equity * 100
            if dd > max_dd:
                max_dd = dd

        # ── Compute trend for each asset ────────────────────────────────────
        uptrend = {}
        for tk in universe:
            up, price, ma = is_in_uptrend(rebal_date, prices.get(tk, {}), ma_data[tk])
            uptrend[tk] = up

        any_uptrend = any(uptrend.values())

        # ── Compute momentum for ranking ─────────────────────────────────────
        lb_date = (datetime.strptime(rebal_date, "%Y-%m-%d")
                   - timedelta(days=lookback_months * 30)).strftime("%Y-%m-%d")

        scores = {}
        for tk in universe:
            p_start, _ = get_price(prices.get(tk, {}), lb_date, forward=True)
            p_now,   _ = get_price(prices.get(tk, {}), rebal_date, forward=False)
            if p_start and p_now and p_start > 0:
                scores[tk] = (p_now - p_start) / p_start

        # ── Determine desired position ────────────────────────────────────────
        desired_direction = None
        desired_ticker    = None
        desired_momentum  = 0.0

        if any_uptrend:
            # Bull mode: pick strongest uptrending asset
            bull_scores = {tk: sc for tk, sc in scores.items() if uptrend.get(tk)}
            if bull_scores:
                ranked = sorted(bull_scores.items(), key=lambda x: x[1], reverse=True)
                desired_ticker    = ranked[0][0]
                desired_direction = "LONG"
                desired_momentum  = ranked[0][1]
        else:
            # Bear mode
            if short_bear and scores:
                ranked = sorted(scores.items(), key=lambda x: x[1])  # weakest first
                desired_ticker    = ranked[0][0]
                desired_direction = "SHORT"
                desired_momentum  = ranked[0][1]
            else:
                desired_direction = None  # go flat

        # ── Apply position change ─────────────────────────────────────────────
        current_direction = position["direction"] if position else None
        current_ticker    = position["ticker"]    if position else None
        fee_cost          = 0.0

        position_changed = (desired_direction != current_direction or
                            desired_ticker != current_ticker)

        if position_changed:
            # Exit current position (pay exit fee)
            if position is not None:
                fee_cost += equity * leverage * fee_rate
                equity    = max(0.0, equity - fee_cost)

                exit_price, _ = get_price(prices.get(current_ticker, {}),
                                          rebal_date, forward=False)
                # Log the closed trade
                for t in reversed(trades_log):
                    if t.get("ticker") == current_ticker and t.get("exit_price") is None:
                        t["exit_price"]    = round(exit_price, 4) if exit_price else None
                        t["equity_after"]  = round(equity, 2)
                        t["funding_cost"]  = round(total_funding, 4)  # cumulative to close
                        t["fee_cost"]      = round(fee_cost, 4)
                        break

            # Enter new position (pay entry fee)
            if desired_direction is not None:
                entry_fee = equity * leverage * fee_rate
                equity    = max(0.0, equity - entry_fee)
                fee_cost += entry_fee
                total_fees += fee_cost

                entry_price, _ = get_price(prices.get(desired_ticker, {}),
                                           rebal_date, forward=False)

                position = {
                    "direction":   desired_direction,
                    "ticker":      desired_ticker,
                    "entry_price": entry_price if entry_price else 0.0,
                    "entry_date":  rebal_date,
                    "entry_equity": equity,
                }

                trades_log.append({
                    "date":          rebal_date,
                    "action":        desired_direction,
                    "ticker":        desired_ticker,
                    "name":          NAMES.get(desired_ticker, desired_ticker),
                    "direction":     desired_direction,
                    "entry_price":   round(entry_price, 4) if entry_price else None,
                    "exit_price":    None,
                    "momentum_pct":  round(desired_momentum * 100, 2),
                    "equity_after":  round(equity, 2),
                    "pnl":           round(pnl_this_period, 4),
                    "funding_cost":  round(funding_cost, 4),
                    "fee_cost":      round(fee_cost, 4),
                    "days_held":     days_held,
                })

                if verbose:
                    trend_str = "  ".join(
                        f"{'[UP]' if uptrend.get(tk) else '[DN]'}{tk}"
                        for tk in universe
                    )
                    print(f"  {rebal_date}  {desired_direction:<5} {NAMES.get(desired_ticker, desired_ticker):<10}"
                          f"  mom={fmt_pct(desired_momentum*100):>7}"
                          f"  eq=${equity:>11,.2f}"
                          f"  fee=${fee_cost:>6.2f}"
                          f"  {trend_str}")

            else:
                # Going flat
                position = None
                trades_log.append({
                    "date":          rebal_date,
                    "action":        "FLAT",
                    "ticker":        "CASH",
                    "name":          "Cash",
                    "direction":     "FLAT",
                    "entry_price":   None,
                    "exit_price":    None,
                    "momentum_pct":  0.0,
                    "equity_after":  round(equity, 2),
                    "pnl":           round(pnl_this_period, 4),
                    "funding_cost":  round(funding_cost, 4),
                    "fee_cost":      round(fee_cost, 4),
                    "days_held":     days_held,
                })
                if verbose:
                    print(f"  {rebal_date}  FLAT  (all assets below {trend_ma_days}d MA)  eq=${equity:>11,.2f}")

        # ── Period counters ───────────────────────────────────────────────────
        if position is None or desired_direction is None:
            in_cash_periods += 1
        elif desired_direction == "LONG":
            long_periods += 1
        else:
            short_periods += 1

        prev_date = rebal_date

    # ── Final mark-to-market ─────────────────────────────────────────────────
    if position is not None:
        tk = position["ticker"]
        cur_price, _ = get_price(prices.get(tk, {}), end_date, forward=False)
        entry_price  = position["entry_price"]
        if cur_price is not None and entry_price and entry_price > 0:
            if position["direction"] == "LONG":
                final_pnl = equity * leverage * (cur_price / entry_price - 1)
            else:
                final_pnl = equity * leverage * (1 - cur_price / entry_price)
            equity = max(0.0, equity + final_pnl)

        # Final funding
        if prev_date:
            final_days = (datetime.strptime(end_date, "%Y-%m-%d") -
                          datetime.strptime(prev_date, "%Y-%m-%d")).days
            if final_days > 0:
                funding_periods = final_days * PERIODS_PER_DAY
                funding_amount  = equity * leverage * FUNDING_RATE_8H * funding_periods
                if position["direction"] == "LONG":
                    equity       = max(0.0, equity - funding_amount)
                    total_funding -= funding_amount
                else:
                    equity       += funding_amount
                    total_funding += funding_amount

    final = equity

    # Peak / max drawdown final pass
    all_vals = [monthly[d] for d in sorted(monthly.keys())]
    pk = float(initial)
    max_dd = 0.0
    for v in all_vals:
        if v > pk:
            pk = v
        dd = (pk - v) / pk * 100 if pk > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return final, monthly, trades_log, peak_equity, max_dd, total_funding, total_fees, long_periods, short_periods, in_cash_periods


# ── Results display ────────────────────────────────────────────────────────────

def display_hl_results(final, initial, monthly, trades_log,
                       peak_equity, max_dd,
                       total_funding, total_fees,
                       long_periods, short_periods, flat_periods,
                       start_date, end_date, universe, leverage):

    years = (datetime.strptime(end_date,   "%Y-%m-%d") -
             datetime.strptime(start_date, "%Y-%m-%d")).days / 365.25

    total_ret  = (final - initial) / initial * 100
    cagr       = ((final / initial) ** (1 / years) - 1) * 100 if years > 0 and final > 0 else 0
    peak_ret   = (peak_equity - initial) / initial * 100

    print_header("HYPERLIQUID MOMENTUM BACKTEST RESULTS")
    print(f"  Initial capital:   ${initial:>12,.2f}")
    print(f"  Final equity:      ${final:>12,.2f}  ({fmt_pct(total_ret)})")
    print(f"  Peak equity:       ${peak_equity:>12,.2f}  ({fmt_pct(peak_ret)})")
    print(f"  CAGR:              {cagr:>+12.2f}%/yr  ({years:.1f} years)")
    print(f"  Max drawdown:      {-max_dd:>+11.1f}%")
    print(f"  Leverage:          {leverage:.1f}x")
    print()
    total_periods = long_periods + short_periods + flat_periods
    print(f"  Long periods:      {long_periods:>4}  ({long_periods/max(total_periods,1)*100:.0f}%)")
    print(f"  Short periods:     {short_periods:>4}  ({short_periods/max(total_periods,1)*100:.0f}%)")
    print(f"  Flat periods:      {flat_periods:>4}  ({flat_periods/max(total_periods,1)*100:.0f}%)")
    print()
    funding_sign = "received" if total_funding > 0 else "paid"
    print(f"  Total funding:     ${abs(total_funding):>10,.2f}  ({funding_sign})")
    print(f"  Total fees paid:   ${total_fees:>10,.2f}")
    print()

    # Monthly equity curve
    if monthly:
        all_dates = sorted(monthly.keys())
        month_snap = {}
        for d in all_dates:
            ym = d[:7]
            month_snap[ym] = monthly[d]

        months  = sorted(month_snap.keys())
        max_eq  = max(month_snap.values())
        min_eq  = min(initial * 0.5, min(month_snap.values()))

        print(f"  {'Month':<10}  {'Equity':>14}  {'Total Return':>13}  Curve")
        print_sep()
        for ym in months:
            eq  = month_snap[ym]
            ret = (eq - initial) / initial * 100
            frac = (eq - min_eq) / (max_eq - min_eq) if max_eq != min_eq else 0
            b = "#" * int(frac * 30)
            print(f"  {ym:<10}  ${eq:>13,.0f}  {fmt_pct(ret):>13}  {b}")
        print()

    # Trade log
    position_trades = [t for t in trades_log if t.get("action") in ("LONG", "SHORT")]
    if position_trades:
        print_header("TRADE LOG")
        print(f"  {'Date':<12}  {'Dir':<5}  {'Asset':<10}  {'Entry':>10}  {'Exit':>10}"
              f"  {'Days':>4}  {'PnL':>9}  {'Funding':>9}  {'Fees':>7}")
        print_sep()
        for t in position_trades:
            entry = f"${t['entry_price']:,.2f}" if t.get("entry_price") else "  —"
            exit_ = f"${t['exit_price']:,.2f}" if t.get("exit_price") else "  —"
            pnl_s = f"${t['pnl']:+,.2f}" if t.get("pnl") else "$0.00"
            fund_s = f"${t['funding_cost']:+,.2f}" if t.get("funding_cost") else "$0.00"
            fee_s  = f"${t['fee_cost']:,.2f}" if t.get("fee_cost") else "$0.00"
            print(f"  {t['date']:<12}  {t['direction']:<5}  {t['name']:<10}  "
                  f"{entry:>10}  {exit_:>10}  {t['days_held']:>4}  "
                  f"{pnl_s:>9}  {fund_s:>9}  {fee_s:>7}")
        print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Hyperliquid Perpetuals Momentum Backtester")
    parser.add_argument("--universe", default="top3",
                        choices=list(UNIVERSES.keys()),
                        help="Asset universe (default: top3)")
    parser.add_argument("--tickers", default="",
                        help="Custom comma-sep tickers, e.g. BTC-USD,ETH-USD,SOL-USD")
    parser.add_argument("--start",    default="2016-01-01")
    parser.add_argument("--end",      default="2026-03-01")
    parser.add_argument("--lookback", type=int, default=2, metavar="MONTHS",
                        help="Momentum lookback in months (default: 2)")
    parser.add_argument("--top-k",    type=int, default=1,
                        help="# assets to rank (default: 1 = best only)")
    parser.add_argument("--leverage", type=float, default=1.0,
                        help="Leverage multiplier (default: 1.0)")
    parser.add_argument("--initial",  type=float, default=10000)
    parser.add_argument("--short-bear", action="store_true",
                        help="SHORT weakest asset in bear markets (default: go flat)")
    parser.add_argument("--funding-rate", type=float, default=FUNDING_RATE_8H,
                        metavar="RATE", help=f"8-hr funding rate (default: {FUNDING_RATE_8H})")
    parser.add_argument("--trend-ma", type=int, default=200, metavar="DAYS",
                        help="MA period for trend filter (default: 200)")
    parser.add_argument("--export", action="store_true",
                        help=f"Save results to {HL_TRADES_FILE}")
    parser.add_argument("--compare", action="store_true",
                        help="Show comparison vs spot backtest results")
    args = parser.parse_args()

    universe = ([t.strip() for t in args.tickers.split(",") if t.strip()]
                if args.tickers else UNIVERSES[args.universe])
    short_bear = args.short_bear

    cache = load_cache()

    print()
    print_header("HYPERLIQUID PERPETUALS MOMENTUM BACKTESTER")
    print(f"  Universe:         {', '.join(NAMES.get(t, t) for t in universe)}")
    print(f"  Date range:       {args.start} to {args.end}")
    print(f"  Lookback:         {args.lookback} months")
    print(f"  Top K:            {args.top_k} asset(s)")
    print(f"  Leverage:         {args.leverage:.1f}x")
    print(f"  Trend MA:         {args.trend_ma}d")
    print(f"  Bear market:      {'SHORT weakest (--short-bear)' if short_bear else 'go FLAT (cash) [default]'}")
    print(f"  Funding rate:     {args.funding_rate*100:.4f}%/8hr  ({args.funding_rate*PERIODS_PER_DAY*365*100:.1f}%/yr)")
    print(f"  Taker fee:        {TAKER_FEE*100:.3f}% per side")
    print(f"  Initial capital:  ${args.initial:,.0f}")
    print()

    result = run_hl_strategy(
        universe        = universe,
        start_date      = args.start,
        end_date        = args.end,
        lookback_months = args.lookback,
        top_k           = args.top_k,
        initial         = args.initial,
        cache           = cache,
        trend_ma_days   = args.trend_ma,
        leverage        = args.leverage,
        funding_rate    = args.funding_rate,
        fee_rate        = TAKER_FEE,
        short_bear      = short_bear,
        verbose         = True,
    )

    (final, monthly, trades_log, peak_equity, max_dd,
     total_funding, total_fees, long_periods, short_periods, flat_periods) = result

    print()
    display_hl_results(
        final         = final,
        initial       = args.initial,
        monthly       = monthly,
        trades_log    = trades_log,
        peak_equity   = peak_equity,
        max_dd        = max_dd,
        total_funding = total_funding,
        total_fees    = total_fees,
        long_periods  = long_periods,
        short_periods = short_periods,
        flat_periods  = flat_periods,
        start_date    = args.start,
        end_date      = args.end,
        universe      = universe,
        leverage      = args.leverage,
    )

    # ── Comparison with spot backtest ─────────────────────────────────────────
    if args.compare:
        spot_path = "trades/backtest_crypto_trades.json"
        if os.path.exists(spot_path):
            try:
                with open(spot_path) as f:
                    spot = json.load(f)
                s = spot.get("summary", {})
                print_header("COMPARISON vs SPOT BACKTEST")
                years = (datetime.strptime(args.end,   "%Y-%m-%d") -
                         datetime.strptime(args.start, "%Y-%m-%d")).days / 365.25
                hl_ret  = (final - args.initial) / args.initial * 100
                hl_cagr = ((final / args.initial) ** (1/years) - 1) * 100 if years > 0 and final > 0 else 0
                print(f"  {'Strategy':<25}  {'Final':>13}  {'Return':>10}  {'CAGR':>9}  {'MaxDD':>7}")
                print_sep()
                print(f"  {'HL Perpetuals':<25}  ${final:>12,.0f}  {fmt_pct(hl_ret):>10}  "
                      f"{hl_cagr:>+8.1f}%  -{max_dd:.0f}%")
                spot_final = s.get("final", args.initial)
                spot_ret   = s.get("total_return", 0)
                spot_cagr  = ((spot_final / args.initial) ** (1/years) - 1) * 100 if years > 0 and spot_final > 0 else 0
                spot_dd    = s.get("max_dd", 0)
                print(f"  {'Spot Momentum':<25}  ${spot_final:>12,.0f}  {fmt_pct(spot_ret):>10}  "
                      f"{spot_cagr:>+8.1f}%  -{spot_dd:.0f}%")
                print()
            except Exception as e:
                print(f"  Could not load spot backtest: {e}")
        else:
            print(f"  Spot backtest file not found: {spot_path}")
            print(f"  Run:  python backtest_crypto.py --export  to generate it.")
            print()

    # ── Export ────────────────────────────────────────────────────────────────
    if args.export:
        years = (datetime.strptime(args.end,   "%Y-%m-%d") -
                 datetime.strptime(args.start, "%Y-%m-%d")).days / 365.25
        total_ret  = (final - args.initial) / args.initial * 100
        cagr       = ((final / args.initial) ** (1/years) - 1) * 100 if years > 0 and final > 0 else 0
        peak_ret   = (peak_equity - args.initial) / args.initial * 100

        export_trades = []
        for t in trades_log:
            export_trades.append({
                "date":          t.get("date"),
                "action":        t.get("action"),
                "ticker":        t.get("ticker"),
                "name":          t.get("name", NAMES.get(t.get("ticker",""), t.get("ticker",""))),
                "direction":     t.get("direction"),
                "entry_price":   t.get("entry_price"),
                "exit_price":    t.get("exit_price"),
                "momentum_pct":  t.get("momentum_pct", 0),
                "equity_after":  t.get("equity_after", 0),
                "pnl":           t.get("pnl", 0),
                "funding_cost":  t.get("funding_cost", 0),
                "fee_cost":      t.get("fee_cost", 0),
                "days_held":     t.get("days_held", 0),
            })

        export_data = {
            "settings": {
                "universe":         [NAMES.get(t, t) for t in universe],
                "tickers":          universe,
                "lookback":         args.lookback,
                "trend_ma":         args.trend_ma,
                "leverage":         args.leverage,
                "funding_rate_8h":  FUNDING_RATE_8H,
                "fee_rate":         TAKER_FEE,
                "short_bear":       short_bear,
                "start":            args.start,
                "end":              args.end,
                "initial":          args.initial,
            },
            "summary": {
                "final":            round(final, 2),
                "peak":             round(peak_equity, 2),
                "total_return":     round(total_ret, 1),
                "peak_return":      round(peak_ret, 1),
                "cagr":             round(cagr, 2),
                "max_dd":           round(max_dd, 1),
                "total_funding":    round(total_funding, 4),
                "total_fees":       round(total_fees, 4),
                "long_periods":     long_periods,
                "short_periods":    short_periods,
                "flat_periods":     flat_periods,
            },
            "trades": export_trades,
        }

        os.makedirs("trades", exist_ok=True)
        with open(HL_TRADES_FILE, "w") as f:
            json.dump(export_data, f, indent=2)
        print(f"  Exported {len(export_trades)} trades to {HL_TRADES_FILE}")

        try:
            from db import save_backtest_trades
            save_backtest_trades("hl_momentum", export_trades)
            print(f"  Also saved {len(export_trades)} trades to database")
        except Exception as e:
            print(f"  DB save skipped: {e}")


if __name__ == "__main__":
    main()
