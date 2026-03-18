"""Crypto Momentum Paper Trading Bot
=====================================
Paper trades the BTC+ETH+SOL weekly momentum strategy in real-time.
Same strategy that returned +709,301% in backtesting (2016-2026).

Strategy:
  - Universe: BTC, ETH, SOL
  - Rebalance: every 7 days
  - Lookback: 2 months (60 days)
  - Trend filter: 200d MA — go to cash if all assets below it
  - Hold: #1 ranked asset by momentum

Usage:
    python crypto_momentum_bot.py              # Check signal + run rebalance if due
    python crypto_momentum_bot.py --status     # Show current portfolio status
    python crypto_momentum_bot.py --force      # Force a rebalance now (ignore schedule)
    python crypto_momentum_bot.py --reset      # Reset paper portfolio to $10k
    python crypto_momentum_bot.py --daemon     # Run weekly loop (for Heroku worker)
"""

import json
import os
import time
import argparse
import contextlib
import io
from datetime import datetime, timedelta

import yfinance as yf

# ── Config ─────────────────────────────────────────────────────────────────────

UNIVERSE    = ["BTC-USD", "ETH-USD", "SOL-USD"]
LOOKBACK_DAYS = 60      # 2 months
TREND_MA    = 200       # 200-day MA trend filter
REBAL_DAYS  = 7         # rebalance every 7 days
INITIAL     = 10_000.0

STATE_FILE  = "trades/crypto_momentum_live.json"
PRICE_CACHE = "trades/crypto_prices.json"

NAMES = {
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
    "SOL-USD": "Solana",
}


# ── State persistence ─────────────────────────────────────────────────────────

def load_state():
    os.makedirs("trades", exist_ok=True)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_state(state):
    os.makedirs("trades", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def init_state():
    state = {
        "started_at":   datetime.utcnow().strftime("%Y-%m-%d"),
        "initial":      INITIAL,
        "cash":         INITIAL,
        "position":     None,   # {ticker, name, units, entry_price, entry_date, entry_equity}
        "equity":       INITIAL,
        "peak_equity":  INITIAL,
        "status":       "cash",  # "holding" or "cash"
        "last_check":   None,
        "next_check":   datetime.utcnow().strftime("%Y-%m-%d"),
        "signal":       None,   # last computed signal
        "trades":       [],
    }
    save_state(state)
    print(f"  Portfolio initialized: ${INITIAL:,.0f} cash")
    return state


# ── Price fetching ─────────────────────────────────────────────────────────────

def load_price_cache():
    if os.path.exists(PRICE_CACHE):
        try:
            with open(PRICE_CACHE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_price_cache(cache):
    with open(PRICE_CACHE, "w") as f:
        json.dump(cache, f)


def fetch_history(tickers, days=260):
    """Fetch N days of daily close prices. Returns {ticker: {date: price}}."""
    start = (datetime.utcnow() - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    end   = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")

    cache = load_price_cache()
    needed = tickers  # always refresh for live trading

    print(f"  Fetching latest prices for {', '.join(NAMES.get(t,t) for t in tickers)}...")
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        raw = yf.download(needed, start=start, end=end,
                          auto_adjust=True, progress=False)

    result = {}
    if raw.empty:
        print("  Warning: no price data returned.")
        return result

    if hasattr(raw.columns, "levels"):
        for tk in needed:
            try:
                series = raw[("Close", tk)].dropna()
                result[tk] = {str(d.date()): float(v) for d, v in series.items()}
            except Exception:
                pass
    else:
        if len(needed) == 1 and "Close" in raw.columns:
            series = raw["Close"].dropna()
            result[needed[0]] = {str(d.date()): float(v) for d, v in series.items()}

    # Merge into cache for the backtest file too
    for tk, prices in result.items():
        key = f"{tk}|{start}|{end}"
        cache[key] = prices
    save_price_cache(cache)

    return result


def get_latest_price(prices_dict):
    """Get the most recent price from a {date: price} dict."""
    if not prices_dict:
        return None, None
    dates = sorted(prices_dict.keys())
    latest = dates[-1]
    return prices_dict[latest], latest


def get_price_on(prices_dict, date_str, window=7):
    """Find closest price at or before date_str."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(window):
        ds = (dt - timedelta(days=i)).strftime("%Y-%m-%d")
        if ds in prices_dict:
            return prices_dict[ds], ds
    return None, None


# ── Strategy logic ─────────────────────────────────────────────────────────────

def compute_ma(prices_dict, ma_days=200):
    """Compute N-day moving average. Returns {date: ma_value}."""
    dates  = sorted(prices_dict.keys())
    closes = [prices_dict[d] for d in dates]
    ma = {}
    for i in range(ma_days - 1, len(closes)):
        ma[dates[i]] = sum(closes[i - ma_days + 1: i + 1]) / ma_days
    return ma


def compute_signal(prices):
    """
    Compute current trading signal.
    Returns dict with: winner, scores, uptrend, in_cash, details.
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")
    lb_date = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    uptrend = {}
    scores  = {}
    details = {}

    for tk in UNIVERSE:
        p = prices.get(tk, {})
        if not p:
            uptrend[tk] = False
            continue

        # Trend filter
        ma = compute_ma(p, ma_days=TREND_MA)
        price_now, date_now = get_latest_price(p)
        ma_now = ma.get(date_now)
        if ma_now is None:
            # Look back a few days for MA value
            for i in range(7):
                d = (datetime.strptime(date_now, "%Y-%m-%d") - timedelta(days=i)).strftime("%Y-%m-%d")
                if d in ma:
                    ma_now = ma[d]
                    break

        in_up = (price_now is not None and ma_now is not None and price_now > ma_now)
        uptrend[tk] = in_up

        # Momentum score
        p_lb, _ = get_price_on(p, lb_date)
        if p_lb and price_now and p_lb > 0:
            scores[tk] = (price_now - p_lb) / p_lb

        details[tk] = {
            "price":    round(price_now, 2) if price_now else None,
            "ma200":    round(ma_now, 2) if ma_now else None,
            "uptrend":  in_up,
            "momentum": round(scores.get(tk, 0) * 100, 1),
            "date":     date_now,
        }

    any_uptrend = any(uptrend.values())

    if not any_uptrend:
        return {
            "in_cash": True,
            "winner":  None,
            "scores":  scores,
            "uptrend": uptrend,
            "details": details,
            "reason":  "All assets below 200d MA — stay in cash",
        }

    # Filter to uptrend assets only, rank by momentum
    eligible = {tk: s for tk, s in scores.items() if uptrend.get(tk)}
    if not eligible:
        return {
            "in_cash": True,
            "winner":  None,
            "scores":  scores,
            "uptrend": uptrend,
            "details": details,
            "reason":  "No eligible assets with momentum data",
        }

    ranked = sorted(eligible.items(), key=lambda x: x[1], reverse=True)
    winner = ranked[0][0]

    return {
        "in_cash": False,
        "winner":  winner,
        "scores":  scores,
        "uptrend": uptrend,
        "details": details,
        "ranked":  ranked,
        "reason":  f"BUY {NAMES.get(winner, winner)} ({scores.get(winner, 0)*100:+.1f}% momentum)",
    }


# ── Rebalance execution ────────────────────────────────────────────────────────

def rebalance(state, signal, prices, force=False):
    """Execute rebalance based on signal. Returns updated state."""
    today    = datetime.utcnow().strftime("%Y-%m-%d")
    position = state.get("position")
    current_ticker = position["ticker"] if position else None

    target_ticker = None if signal["in_cash"] else signal["winner"]

    # Compute current equity
    if position:
        p_now, _ = get_latest_price(prices.get(position["ticker"], {}))
        if p_now:
            state["equity"] = position["units"] * p_now
        else:
            state["equity"] = state["cash"]
    else:
        state["equity"] = state["cash"]

    if state["equity"] > state["peak_equity"]:
        state["peak_equity"] = state["equity"]

    # Check if we need to change
    if current_ticker == target_ticker:
        print(f"  No change — still holding {NAMES.get(current_ticker, 'cash')}")
        state["last_check"] = today
        state["next_check"] = (datetime.utcnow() + timedelta(days=REBAL_DAYS)).strftime("%Y-%m-%d")
        state["signal"]     = signal
        save_state(state)
        return state, "hold"

    action_taken = "hold"

    # Sell current position
    if position:
        p_sell, _ = get_latest_price(prices.get(position["ticker"], {}))
        if p_sell:
            proceeds = position["units"] * p_sell
            pnl      = proceeds - position["entry_equity"]
            pnl_pct  = (proceeds / position["entry_equity"] - 1) * 100

            trade = {
                "date":          today,
                "action":        "SELL",
                "ticker":        position["ticker"],
                "name":          position["name"],
                "price":         round(p_sell, 2),
                "units":         round(position["units"], 6),
                "entry_price":   position["entry_price"],
                "entry_date":    position["entry_date"],
                "pnl":           round(pnl, 2),
                "pnl_pct":       round(pnl_pct, 2),
                "equity_after":  round(proceeds, 2),
            }
            state["trades"].append(trade)
            state["cash"]     = proceeds
            state["position"] = None
            state["status"]   = "cash"
            state["equity"]   = proceeds

            print(f"  SELL {position['name']} @ ${p_sell:,.2f}  "
                  f"PnL: ${pnl:+,.2f} ({pnl_pct:+.1f}%)  Equity: ${proceeds:,.2f}")
            action_taken = "sell"

    # Buy new position
    if target_ticker:
        p_buy, _ = get_latest_price(prices.get(target_ticker, {}))
        if p_buy and p_buy > 0:
            units = state["cash"] / p_buy
            position_new = {
                "ticker":       target_ticker,
                "name":         NAMES.get(target_ticker, target_ticker),
                "units":        units,
                "entry_price":  round(p_buy, 2),
                "entry_date":   today,
                "entry_equity": state["cash"],
            }
            trade = {
                "date":          today,
                "action":        "BUY",
                "ticker":        target_ticker,
                "name":          NAMES.get(target_ticker, target_ticker),
                "price":         round(p_buy, 2),
                "units":         round(units, 6),
                "momentum_pct":  round(signal["scores"].get(target_ticker, 0) * 100, 1),
                "equity_after":  round(state["cash"], 2),
            }
            state["trades"].append(trade)
            state["position"] = position_new
            state["cash"]     = 0.0
            state["status"]   = "holding"

            print(f"  BUY  {NAMES.get(target_ticker, target_ticker)} @ ${p_buy:,.2f}  "
                  f"Units: {units:.6f}  Equity: ${state['equity']:,.2f}")
            action_taken = "buy"
    else:
        print(f"  Going to CASH — {signal['reason']}")
        action_taken = "cash"

    state["last_check"] = today
    state["next_check"] = (datetime.utcnow() + timedelta(days=REBAL_DAYS)).strftime("%Y-%m-%d")
    state["signal"]     = signal
    save_state(state)
    return state, action_taken


# ── Status display ─────────────────────────────────────────────────────────────

def print_status(state, prices=None):
    print()
    print("=" * 60)
    print("  CRYPTO MOMENTUM BOT — PAPER PORTFOLIO")
    print("=" * 60)

    initial  = state.get("initial", INITIAL)
    equity   = state.get("equity", initial)
    peak     = state.get("peak_equity", initial)
    total_ret = (equity - initial) / initial * 100
    peak_ret  = (peak   - initial) / initial * 100

    print(f"  Started:     {state.get('started_at', '—')}")
    print(f"  Initial:     ${initial:>12,.2f}")
    print(f"  Equity:      ${equity:>12,.2f}  ({total_ret:+.1f}%)")
    print(f"  Peak:        ${peak:>12,.2f}  ({peak_ret:+.1f}%)")
    print(f"  Last check:  {state.get('last_check', '—')}")
    print(f"  Next check:  {state.get('next_check', '—')}")
    print()

    position = state.get("position")
    if position:
        p_now = None
        if prices:
            p_now, _ = get_latest_price(prices.get(position["ticker"], {}))
        if p_now:
            current_val = position["units"] * p_now
            unrealized  = current_val - position["entry_equity"]
            unr_pct     = unrealized / position["entry_equity"] * 100
            print(f"  HOLDING: {position['name']}")
            print(f"    Entry:      ${position['entry_price']:>10,.2f}  on {position['entry_date']}")
            print(f"    Current:    ${p_now:>10,.2f}")
            print(f"    Units:      {position['units']:.6f}")
            print(f"    Unrealized: ${unrealized:>+10,.2f}  ({unr_pct:+.1f}%)")
        else:
            print(f"  HOLDING: {position['name']} @ ${position['entry_price']:,.2f}")
    else:
        print(f"  STATUS: CASH (waiting for signal)")

    # Market status
    sig = state.get("signal")
    if sig and sig.get("details"):
        print()
        print("  Current Market Status:")
        for tk in UNIVERSE:
            d = sig["details"].get(tk, {})
            trend = "[UP]" if d.get("uptrend") else "[DN]"
            price = f"${d.get('price', 0):>10,.2f}" if d.get("price") else "    N/A"
            ma    = f"MA200=${d.get('ma200', 0):>10,.2f}" if d.get("ma200") else ""
            mom   = f"Mom={d.get('momentum', 0):+.1f}%"
            name  = NAMES.get(tk, tk)
            print(f"    {trend} {name:<12} {price}  {ma}  {mom}")

    # Trade history
    trades = state.get("trades", [])
    buys = [t for t in trades if t["action"] == "BUY"]
    if buys:
        print()
        print(f"  Trade History ({len(buys)} rotations):")
        print(f"  {'Date':<12} {'Asset':<12} {'Price':>10}  {'Momentum':>9}  {'Equity After':>14}")
        print("  " + "-" * 62)
        for t in buys:
            print(f"  {t['date']:<12} {t['name']:<12} ${t['price']:>9,.2f}  "
                  f"{t.get('momentum_pct', 0):>+8.1f}%  ${t['equity_after']:>13,.2f}")

    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def run_check(force=False):
    """Run one check cycle: fetch prices, compute signal, rebalance if due."""
    state = load_state()
    if state is None:
        print("  No portfolio found. Initializing...")
        state = init_state()

    today = datetime.utcnow().strftime("%Y-%m-%d")

    # Check if rebalance is due
    next_check = state.get("next_check", today)
    is_due = (today >= next_check) or force

    print()
    print(f"  Today: {today}  |  Next rebalance: {next_check}  |  Due: {'YES' if is_due else 'NO'}")

    # Always fetch prices for status display
    prices = fetch_history(UNIVERSE, days=TREND_MA + 30)
    if not prices:
        print("  Failed to fetch prices.")
        return

    # Compute signal
    signal = compute_signal(prices)

    print()
    print(f"  Signal: {signal['reason']}")
    if not signal["in_cash"] and signal.get("ranked"):
        print(f"  Rankings:")
        for tk, score in signal["ranked"]:
            trend = "[UP]" if signal["uptrend"].get(tk) else "[DN]"
            name  = NAMES.get(tk, tk)
            print(f"    {trend} {name:<12} {score*100:+.1f}% momentum")

    # Update equity even if not rebalancing
    position = state.get("position")
    if position:
        p_now, _ = get_latest_price(prices.get(position["ticker"], {}))
        if p_now:
            state["equity"] = position["units"] * p_now
            if state["equity"] > state["peak_equity"]:
                state["peak_equity"] = state["equity"]
            save_state(state)

    if is_due:
        print()
        print("  Executing rebalance...")
        state, action = rebalance(state, signal, prices, force=force)
    else:
        print(f"  Not due yet. Next check: {next_check}")
        state["signal"] = signal
        save_state(state)

    print()
    print_status(state, prices)


def run_daemon():
    """Run as a weekly loop — for Heroku worker dyno."""
    print("  Crypto Momentum Bot — daemon mode")
    print(f"  Universe: {', '.join(NAMES.get(t,t) for t in UNIVERSE)}")
    print(f"  Rebalancing every {REBAL_DAYS} days")
    print()

    while True:
        try:
            run_check()
        except Exception as e:
            print(f"  Error in check cycle: {e}")

        # Sleep until next check (check every 6 hours, rebalance logic handles frequency)
        sleep_hours = 6
        print(f"  Sleeping {sleep_hours}h until next check...")
        time.sleep(sleep_hours * 3600)


def main():
    parser = argparse.ArgumentParser(description="Crypto Momentum Paper Trader")
    parser.add_argument("--status", action="store_true", help="Show current portfolio status")
    parser.add_argument("--force",  action="store_true", help="Force rebalance now")
    parser.add_argument("--reset",  action="store_true", help="Reset portfolio to $10k")
    parser.add_argument("--daemon", action="store_true", help="Run weekly loop")
    parser.add_argument("--initial", type=float, default=INITIAL, help="Starting capital")
    args = parser.parse_args()

    if args.reset:
        print()
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        # Re-init with custom starting capital
        os.makedirs("trades", exist_ok=True)
        state = {
            "started_at":   datetime.utcnow().strftime("%Y-%m-%d"),
            "initial":      args.initial,
            "cash":         args.initial,
            "position":     None,
            "equity":       args.initial,
            "peak_equity":  args.initial,
            "status":       "cash",
            "last_check":   None,
            "next_check":   datetime.utcnow().strftime("%Y-%m-%d"),
            "signal":       None,
            "trades":       [],
        }
        save_state(state)
        print(f"  Portfolio reset to ${args.initial:,.0f}.")
        return

    if args.status:
        state = load_state()
        if state is None:
            print("  No portfolio found. Run without --status to initialize.")
            return
        prices = fetch_history(UNIVERSE, days=TREND_MA + 30)
        print_status(state, prices)
        return

    if args.daemon:
        run_daemon()
        return

    # Default: run one check
    run_check(force=args.force)


if __name__ == "__main__":
    main()
