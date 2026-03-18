"""Hyperliquid Perpetuals Momentum Paper Trading Bot
=====================================================
Paper trades the BTC+ETH+SOL weekly momentum strategy on HL perps.
Same strategy that returned +459,753% in backtesting (2016-2026).

Strategy:
  - Universe: BTC, ETH, SOL
  - Rebalance: every 7 days
  - Lookback: 2 months (60 days)
  - Trend filter: 200d MA — go FLAT if all assets below it
  - Hold: LONG #1 momentum asset (when any above MA)
  - Bear market: FLAT (capital preserved, no shorts)
  - Funding: 0.005%/8hr applied to position weekly
  - Fees: 0.05% per side on rotations

Usage:
    python hl_momentum_bot.py              # Check signal + rebalance if due
    python hl_momentum_bot.py --status     # Show current portfolio status
    python hl_momentum_bot.py --force      # Force a rebalance now
    python hl_momentum_bot.py --reset      # Reset paper portfolio to $10k
    python hl_momentum_bot.py --daemon     # Run weekly loop (for Heroku)
    python hl_momentum_bot.py --leverage 2 # Use 2x leverage
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

UNIVERSE      = ["BTC-USD", "ETH-USD", "SOL-USD"]
LOOKBACK_DAYS = 60        # 2 months
TREND_MA      = 200       # 200-day MA trend filter
REBAL_DAYS    = 7         # rebalance every 7 days
INITIAL       = 10_000.0
FUNDING_RATE  = 0.00005   # 0.005%/8hr (~5.5%/yr)
PERIODS_PER_DAY = 3       # HL settles funding 3x/day
TAKER_FEE     = 0.0005    # 0.05% per side

STATE_FILE  = "trades/hl_momentum_live.json"
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


def init_state(initial=INITIAL, leverage=1.0):
    state = {
        "started_at":        datetime.utcnow().strftime("%Y-%m-%d"),
        "initial":           initial,
        "equity":            initial,
        "peak_equity":       initial,
        "leverage":          leverage,
        "status":            "flat",   # "long", "short", "flat"
        "position":          None,     # {direction, ticker, name, entry_price, entry_date, entry_equity}
        "last_check":        None,
        "next_check":        datetime.utcnow().strftime("%Y-%m-%d"),
        "signal":            None,
        "trades":            [],
        "total_funding_paid": 0.0,
        "total_fees_paid":   0.0,
    }
    save_state(state)
    print(f"  Portfolio initialized: ${initial:,.0f} paper capital, {leverage:.1f}x leverage")
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

    print(f"  Fetching latest prices for {', '.join(NAMES.get(t, t) for t in tickers)}...")
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        raw = yf.download(tickers, start=start, end=end,
                          auto_adjust=True, progress=False)

    result = {}
    if raw.empty:
        print("  Warning: no price data returned.")
        return result

    if hasattr(raw.columns, "levels"):
        for tk in tickers:
            try:
                series = raw[("Close", tk)].dropna()
                result[tk] = {str(d.date()): float(v) for d, v in series.items()}
            except Exception:
                pass
    else:
        if len(tickers) == 1 and "Close" in raw.columns:
            series = raw["Close"].dropna()
            result[tickers[0]] = {str(d.date()): float(v) for d, v in series.items()}

    # Update shared price cache
    cache = load_price_cache()
    for tk, prices in result.items():
        key = f"{tk}|{start}|{end}"
        cache[key] = prices
    save_price_cache(cache)

    return result


def get_latest_price(prices_dict):
    if not prices_dict:
        return None, None
    dates = sorted(prices_dict.keys())
    latest = dates[-1]
    return prices_dict[latest], latest


def get_price_on(prices_dict, date_str, window=7):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(window):
        ds = (dt - timedelta(days=i)).strftime("%Y-%m-%d")
        if ds in prices_dict:
            return prices_dict[ds], ds
    return None, None


# ── Strategy logic ─────────────────────────────────────────────────────────────

def compute_ma(prices_dict, ma_days=200):
    dates  = sorted(prices_dict.keys())
    closes = [prices_dict[d] for d in dates]
    ma = {}
    for i in range(ma_days - 1, len(closes)):
        ma[dates[i]] = sum(closes[i - ma_days + 1: i + 1]) / ma_days
    return ma


def compute_signal(prices):
    """
    Compute current trading signal.
    Returns dict: winner, scores, uptrend, in_cash, details, direction.
    """
    lb_date = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    uptrend = {}
    scores  = {}
    details = {}

    for tk in UNIVERSE:
        p = prices.get(tk, {})
        if not p:
            uptrend[tk] = False
            continue

        ma = compute_ma(p, ma_days=TREND_MA)
        price_now, date_now = get_latest_price(p)

        ma_now = ma.get(date_now)
        if ma_now is None:
            for i in range(7):
                d = (datetime.strptime(date_now, "%Y-%m-%d") - timedelta(days=i)).strftime("%Y-%m-%d")
                if d in ma:
                    ma_now = ma[d]
                    break

        in_up = (price_now is not None and ma_now is not None and price_now > ma_now)
        uptrend[tk] = in_up

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
            "in_cash":   True,
            "direction": "FLAT",
            "winner":    None,
            "scores":    scores,
            "uptrend":   uptrend,
            "details":   details,
            "reason":    "All assets below 200d MA — go FLAT",
        }

    eligible = {tk: s for tk, s in scores.items() if uptrend.get(tk)}
    if not eligible:
        return {
            "in_cash":   True,
            "direction": "FLAT",
            "winner":    None,
            "scores":    scores,
            "uptrend":   uptrend,
            "details":   details,
            "reason":    "No eligible assets with momentum data",
        }

    ranked = sorted(eligible.items(), key=lambda x: x[1], reverse=True)
    winner = ranked[0][0]

    return {
        "in_cash":   False,
        "direction": "LONG",
        "winner":    winner,
        "scores":    scores,
        "uptrend":   uptrend,
        "details":   details,
        "ranked":    ranked,
        "reason":    f"LONG {NAMES.get(winner, winner)} ({scores.get(winner, 0)*100:+.1f}% momentum)",
    }


# ── Mark-to-market & funding ──────────────────────────────────────────────────

def apply_mark_to_market(state, prices):
    """
    Settle current position's P&L and funding since last check.
    Updates equity in-place. Returns (pnl, funding_cost).
    """
    position = state.get("position")
    if not position:
        return 0.0, 0.0

    today      = datetime.utcnow().strftime("%Y-%m-%d")
    last_check = state.get("last_check") or position.get("entry_date") or today
    days_held  = max(0, (datetime.strptime(today, "%Y-%m-%d") -
                         datetime.strptime(last_check, "%Y-%m-%d")).days)

    tk          = position["ticker"]
    entry_price = position["entry_price"]
    leverage    = state.get("leverage", 1.0)
    equity      = state["equity"]

    cur_price, _ = get_latest_price(prices.get(tk, {}))
    if cur_price is None or entry_price <= 0:
        return 0.0, 0.0

    # Unrealized P&L (not settled — just for display unless rebalancing)
    if position["direction"] == "LONG":
        pnl = equity * leverage * (cur_price / entry_price - 1)
    else:  # SHORT
        pnl = equity * leverage * (1 - cur_price / entry_price)

    # Funding cost (longs pay, shorts receive)
    funding_periods = days_held * PERIODS_PER_DAY
    funding_amount  = equity * leverage * FUNDING_RATE * funding_periods
    if position["direction"] == "LONG":
        funding_cost = -funding_amount  # negative = paid
    else:
        funding_cost = funding_amount   # positive = received

    return pnl, funding_cost, cur_price


# ── Rebalance execution ────────────────────────────────────────────────────────

def rebalance(state, signal, prices, force=False):
    """Execute rebalance. Settles P&L + funding, rotates position if needed."""
    today    = datetime.utcnow().strftime("%Y-%m-%d")
    position = state.get("position")
    leverage = state.get("leverage", 1.0)

    current_dir    = position["direction"] if position else "FLAT"
    current_ticker = position["ticker"]    if position else None
    target_dir     = signal["direction"]
    target_ticker  = signal["winner"]

    # ── Settle current position ──────────────────────────────────────────────
    if position:
        last_check = state.get("last_check") or position.get("entry_date") or today
        days_held  = max(0, (datetime.strptime(today, "%Y-%m-%d") -
                             datetime.strptime(last_check, "%Y-%m-%d")).days)

        tk          = position["ticker"]
        entry_price = position["entry_price"]
        equity      = state["equity"]

        cur_price, _ = get_latest_price(prices.get(tk, {}))
        if cur_price and entry_price > 0:
            # Settle P&L
            if position["direction"] == "LONG":
                pnl = equity * leverage * (cur_price / entry_price - 1)
            else:
                pnl = equity * leverage * (1 - cur_price / entry_price)
            equity = max(0.0, equity + pnl)

            # Settle funding
            funding_periods = days_held * PERIODS_PER_DAY
            funding_amount  = equity * leverage * FUNDING_RATE * funding_periods
            if position["direction"] == "LONG":
                equity = max(0.0, equity - funding_amount)
                state["total_funding_paid"] = state.get("total_funding_paid", 0) + funding_amount
            else:
                equity += funding_amount
                state["total_funding_paid"] = state.get("total_funding_paid", 0) - funding_amount

            state["equity"] = equity
            # Update entry_price for the next period's reference
            position["entry_price"] = round(cur_price, 4)

    # ── Check if position change is needed ───────────────────────────────────
    position_changed = (target_dir != current_dir or target_ticker != current_ticker)

    if not position_changed:
        print(f"  No change — still {current_dir} {NAMES.get(current_ticker, 'FLAT')}")
        state["last_check"] = today
        state["next_check"] = (datetime.utcnow() + timedelta(days=REBAL_DAYS)).strftime("%Y-%m-%d")
        state["signal"]     = signal
        if position:
            state["position"] = position  # updated entry_price
        save_state(state)
        return state, "hold"

    # ── Exit current position ────────────────────────────────────────────────
    fee_cost = 0.0
    if position:
        exit_fee    = state["equity"] * leverage * TAKER_FEE
        fee_cost   += exit_fee
        state["equity"] = max(0.0, state["equity"] - exit_fee)
        state["total_fees_paid"] = state.get("total_fees_paid", 0) + exit_fee

        exit_price, _ = get_latest_price(prices.get(current_ticker, {}))
        entry_eq = position.get("entry_equity", state["equity"])
        pnl_total = state["equity"] - entry_eq

        trade = {
            "date":          today,
            "action":        "CLOSE",
            "direction":     current_dir,
            "ticker":        current_ticker,
            "name":          NAMES.get(current_ticker, current_ticker),
            "entry_price":   position.get("entry_price"),
            "exit_price":    round(exit_price, 4) if exit_price else None,
            "entry_date":    position.get("entry_date"),
            "pnl":           round(pnl_total, 2),
            "fee_cost":      round(exit_fee, 2),
            "equity_after":  round(state["equity"], 2),
        }
        state["trades"].append(trade)
        state["position"] = None
        state["status"]   = "flat"

        print(f"  CLOSE {current_dir} {NAMES.get(current_ticker, current_ticker)}"
              f"  @ ${exit_price:,.2f}  fee=${exit_fee:.2f}"
              f"  equity=${state['equity']:,.2f}")

    # ── Enter new position ───────────────────────────────────────────────────
    if target_dir != "FLAT" and target_ticker:
        entry_fee   = state["equity"] * leverage * TAKER_FEE
        fee_cost   += entry_fee
        state["equity"] = max(0.0, state["equity"] - entry_fee)
        state["total_fees_paid"] = state.get("total_fees_paid", 0) + entry_fee

        entry_price, _ = get_latest_price(prices.get(target_ticker, {}))
        position_new = {
            "direction":    target_dir,
            "ticker":       target_ticker,
            "name":         NAMES.get(target_ticker, target_ticker),
            "entry_price":  round(entry_price, 4) if entry_price else 0.0,
            "entry_date":   today,
            "entry_equity": round(state["equity"], 2),
        }
        trade = {
            "date":         today,
            "action":       target_dir,  # "LONG" or "SHORT"
            "direction":    target_dir,
            "ticker":       target_ticker,
            "name":         NAMES.get(target_ticker, target_ticker),
            "entry_price":  round(entry_price, 4) if entry_price else None,
            "exit_price":   None,
            "momentum_pct": round(signal["scores"].get(target_ticker, 0) * 100, 1),
            "fee_cost":     round(fee_cost, 2),
            "equity_after": round(state["equity"], 2),
        }
        state["trades"].append(trade)
        state["position"] = position_new
        state["status"]   = target_dir.lower()

        print(f"  OPEN  {target_dir} {NAMES.get(target_ticker, target_ticker)}"
              f"  @ ${entry_price:,.2f}  fee=${entry_fee:.2f}"
              f"  equity=${state['equity']:,.2f}")
    else:
        print(f"  Going FLAT — {signal['reason']}")
        state["position"] = None
        state["status"]   = "flat"

    if state["equity"] > state.get("peak_equity", state["equity"]):
        state["peak_equity"] = state["equity"]

    state["last_check"] = today
    state["next_check"] = (datetime.utcnow() + timedelta(days=REBAL_DAYS)).strftime("%Y-%m-%d")
    state["signal"]     = signal
    save_state(state)
    return state, "rebalanced"


# ── Status display ─────────────────────────────────────────────────────────────

def print_status(state, prices=None):
    print()
    print("=" * 64)
    print("  HYPERLIQUID MOMENTUM BOT — PAPER PORTFOLIO")
    print("=" * 64)

    initial   = state.get("initial", INITIAL)
    equity    = state.get("equity", initial)
    peak      = state.get("peak_equity", initial)
    leverage  = state.get("leverage", 1.0)
    total_ret = (equity - initial) / initial * 100
    peak_ret  = (peak   - initial) / initial * 100

    print(f"  Started:       {state.get('started_at', '—')}")
    print(f"  Initial:       ${initial:>12,.2f}")
    print(f"  Equity:        ${equity:>12,.2f}  ({total_ret:+.1f}%)")
    print(f"  Peak:          ${peak:>12,.2f}  ({peak_ret:+.1f}%)")
    print(f"  Leverage:      {leverage:.1f}x")
    print(f"  Funding paid:  ${state.get('total_funding_paid', 0):>10,.2f}")
    print(f"  Fees paid:     ${state.get('total_fees_paid', 0):>10,.2f}")
    print(f"  Last check:    {state.get('last_check', '—')}")
    print(f"  Next check:    {state.get('next_check', '—')}")
    print()

    position = state.get("position")
    if position:
        cur_price, _ = get_latest_price(prices.get(position["ticker"], {})) if prices else (None, None)
        entry_price  = position.get("entry_price", 0)
        entry_eq     = position.get("entry_equity", equity)

        print(f"  STATUS: {position['direction']} {position['name']}")
        print(f"    Entry price:  ${entry_price:>12,.4f}  on {position.get('entry_date', '—')}")
        if cur_price:
            if position["direction"] == "LONG":
                unr_pct = (cur_price / entry_price - 1) * 100 if entry_price > 0 else 0
            else:
                unr_pct = (1 - cur_price / entry_price) * 100 if entry_price > 0 else 0
            unr_pnl = equity * leverage * (unr_pct / 100)
            print(f"    Current:      ${cur_price:>12,.4f}")
            print(f"    Unrealized:   ${unr_pnl:>+12,.2f}  ({unr_pct * leverage:+.2f}% × {leverage:.1f}x)")
    else:
        print(f"  STATUS: FLAT (waiting for 200d MA signal)")

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
            print(f"    {trend} {NAMES.get(tk, tk):<12} {price}  {ma}  {mom}")

    # Trade history
    entries = [t for t in state.get("trades", []) if t.get("action") in ("LONG", "SHORT")]
    if entries:
        print()
        print(f"  Trade History ({len(entries)} positions opened):")
        print(f"  {'Date':<12} {'Dir':<6} {'Asset':<12} {'Entry':>10}  {'Momentum':>9}  {'Equity After':>14}")
        print("  " + "-" * 68)
        for t in entries:
            print(f"  {t['date']:<12} {t['direction']:<6} {t['name']:<12} "
                  f"${t.get('entry_price', 0):>9,.2f}  "
                  f"{t.get('momentum_pct', 0):>+8.1f}%  "
                  f"${t.get('equity_after', 0):>13,.2f}")
    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def run_check(force=False, leverage=1.0):
    """Run one check cycle: fetch prices, compute signal, rebalance if due."""
    state = load_state()
    if state is None:
        print("  No portfolio found. Initializing...")
        state = init_state(leverage=leverage)

    today      = datetime.utcnow().strftime("%Y-%m-%d")
    next_check = state.get("next_check", today)
    is_due     = (today >= next_check) or force

    print()
    print(f"  Today: {today}  |  Next rebalance: {next_check}  |  Due: {'YES' if is_due else 'NO'}")

    prices = fetch_history(UNIVERSE, days=TREND_MA + 30)
    if not prices:
        print("  Failed to fetch prices.")
        return

    signal = compute_signal(prices)

    print()
    print(f"  Signal: {signal['reason']}")
    if not signal["in_cash"] and signal.get("ranked"):
        print("  Rankings:")
        for tk, score in signal["ranked"]:
            trend = "[UP]" if signal["uptrend"].get(tk) else "[DN]"
            print(f"    {trend} {NAMES.get(tk, tk):<12} {score*100:+.1f}% momentum")

    # Update unrealized equity even when not rebalancing
    position = state.get("position")
    if position and not is_due:
        cur_price, _ = get_latest_price(prices.get(position["ticker"], {}))
        if cur_price and position.get("entry_price", 0) > 0:
            lev = state.get("leverage", 1.0)
            if position["direction"] == "LONG":
                pnl = state["equity"] * lev * (cur_price / position["entry_price"] - 1)
            else:
                pnl = state["equity"] * lev * (1 - cur_price / position["entry_price"])
            unrealized_equity = max(0.0, state["equity"] + pnl)
            if unrealized_equity > state.get("peak_equity", unrealized_equity):
                state["peak_equity"] = unrealized_equity

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


def run_daemon(leverage=1.0):
    print("  HL Momentum Bot — daemon mode")
    print(f"  Universe: {', '.join(NAMES.get(t,t) for t in UNIVERSE)}")
    print(f"  Leverage: {leverage:.1f}x  |  Rebalancing every {REBAL_DAYS} days")
    print()

    while True:
        try:
            run_check(leverage=leverage)
        except Exception as e:
            print(f"  Error in check cycle: {e}")

        sleep_hours = 6
        print(f"  Sleeping {sleep_hours}h until next check...")
        time.sleep(sleep_hours * 3600)


def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Momentum Paper Trader")
    parser.add_argument("--status",   action="store_true", help="Show current portfolio status")
    parser.add_argument("--force",    action="store_true", help="Force rebalance now")
    parser.add_argument("--reset",    action="store_true", help="Reset portfolio")
    parser.add_argument("--daemon",   action="store_true", help="Run weekly loop")
    parser.add_argument("--initial",  type=float, default=INITIAL, help="Starting capital")
    parser.add_argument("--leverage", type=float, default=1.0,     help="Leverage (default: 1x)")
    args = parser.parse_args()

    if args.reset:
        print()
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        init_state(initial=args.initial, leverage=args.leverage)
        print("  Reset complete.")
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
        run_daemon(leverage=args.leverage)
        return

    run_check(force=args.force, leverage=args.leverage)


if __name__ == "__main__":
    main()
