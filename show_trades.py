"""Paper trade viewer — run any time to see current P&L and positions.

Usage:
    python show_trades.py           # full report
    python show_trades.py --trades  # just the trade log
    python show_trades.py --open    # just open positions
    python show_trades.py --watch   # refresh every 10 seconds
"""

import argparse
import json
import math
import os
import time
from datetime import datetime


TRADES_FILE = "paper_trades.json"

# ANSI colours
G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"; C = "\033[96m"; B = "\033[1m"; X = "\033[0m"

def g(s): return f"{G}{s}{X}"
def r(s): return f"{R}{s}{X}"
def y(s): return f"{Y}{s}{X}"
def b(s): return f"{B}{s}{X}"

def money(v: float) -> str:
    return g(f"+${v:.2f}") if v >= 0 else r(f"-${abs(v):.2f}")

def pct(v: float) -> str:
    return g(f"+{v:.1%}") if v >= 0 else r(f"{v:.1%}")

def ts(unix: float) -> str:
    return datetime.fromtimestamp(unix).strftime("%m-%d %H:%M")


def load() -> dict | None:
    if not os.path.exists(TRADES_FILE):
        print(r(f"No trades file found ({TRADES_FILE}). Start paper_trade.py first."))
        return None
    with open(TRADES_FILE) as f:
        return json.load(f)


def print_summary(state: dict):
    trades = state["trades"]
    open_pos = state.get("open_positions", [])
    equity = state["equity"]
    peak = state["peak_equity"]
    max_dd = state["max_drawdown"]
    updated = datetime.fromtimestamp(state["updated_at"]).strftime("%H:%M:%S")

    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    losses = n - wins
    win_rate = wins / n if n else 0

    print(b(f"\n{'='*65}"))
    print(b(f"  PAPER TRADING REPORT  (as of {updated})"))
    print(b(f"{'='*65}"))
    print(f"  Trades completed : {n}  ({wins}W / {losses}L)")
    print(f"  Win rate         : {pct(win_rate)}")
    print(f"  Total equity     : {money(equity)}")
    print(f"  Peak equity      : {money(peak)}")
    print(f"  Max drawdown     : {r(f'${max_dd:.2f}')}")

    if n > 0:
        avg_pnl = equity / n
        avg_edge = sum(t["edge"] for t in trades) / n
        gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        pnls = [t["pnl"] for t in trades]
        sharpe = 0.0
        if len(pnls) > 1:
            mean = sum(pnls) / len(pnls)
            var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
            std = math.sqrt(var) if var > 0 else 0.001
            sharpe = (mean / std) * math.sqrt(105120)

        print(f"  Avg PnL / trade  : {money(avg_pnl)}")
        print(f"  Avg edge         : {avg_edge:+.4f}")
        print(f"  Profit factor    : {pf:.2f}")
        print(f"  Sharpe (ann.)    : {sharpe:.2f}")

    # ── Open positions ─────────────────────────────────────────────────────
    print(b(f"\n  Open Positions ({len(open_pos)}):"))
    if not open_pos:
        print("    (none)")
    else:
        print(f"  {'Opened':8}  {'Strategy':16}  {'Dir':3}  {'Strike':>9}  {'Entry':>6}  {'Resolves':8}")
        print(f"  {'-'*8}  {'-'*16}  {'-'*3}  {'-'*9}  {'-'*6}  {'-'*8}")
        for p in open_pos:
            secs_left = max(0, p["resolves_at"] - time.time())
            resolves = f"{secs_left:.0f}s"
            colour = g if p["direction"] == "YES" else r
            print(f"  {ts(p['opened_at']):8}  {p['strategy']:16}  "
                  f"{colour(p['direction']):3}  ${p['strike']:>8,.0f}  "
                  f"{p['entry_price']:>6.3f}  {resolves:>8}")

    # ── Strategy breakdown ─────────────────────────────────────────────────
    print(b(f"\n  Strategy Breakdown:"))
    strats: dict[str, dict] = {}
    for t in trades:
        s = strats.setdefault(t["strategy"], {"n": 0, "wins": 0, "pnl": 0.0, "edge_sum": 0.0})
        s["n"] += 1
        s["wins"] += int(t["won"])
        s["pnl"] += t["pnl"]
        s["edge_sum"] += t["edge"]

    if not strats:
        print("    (no completed trades yet)")
    else:
        print(f"  {'Strategy':20}  {'N':>4}  {'WR':>6}  {'Avg Edge':>9}  {'PnL':>10}")
        print(f"  {'-'*20}  {'-'*4}  {'-'*6}  {'-'*9}  {'-'*10}")
        for name, s in sorted(strats.items(), key=lambda x: -x[1]["pnl"]):
            wr = s["wins"] / s["n"] if s["n"] else 0
            ae = s["edge_sum"] / s["n"] if s["n"] else 0
            print(f"  {name:20}  {s['n']:>4}  {pct(wr):>6}  {ae:>+9.4f}  {money(s['pnl']):>10}")

    print(b(f"{'='*65}\n"))


def print_trades(state: dict, limit: int = 50):
    trades = state["trades"][-limit:]
    print(b(f"\n{'='*65}"))
    print(b(f"  TRADE LOG  (last {len(trades)} of {len(state['trades'])})"))
    print(b(f"{'='*65}"))
    if not trades:
        print("  (no trades yet)")
    else:
        print(f"  {'Time':8}  {'Strategy':16}  {'Dir':3}  {'Strike':>9}  "
              f"{'Entry':>6}  {'BTC In':>9}  {'BTC Out':>9}  {'Edge':>6}  {'PnL':>8}")
        print(f"  {'-'*8}  {'-'*16}  {'-'*3}  {'-'*9}  "
              f"{'-'*6}  {'-'*9}  {'-'*9}  {'-'*6}  {'-'*8}")
        running = 0.0
        for t in trades:
            running += t["pnl"]
            result = g("W") if t["won"] else r("L")
            colour = g if t["direction"] == "YES" else r
            print(f"  {ts(t['opened_at']):8}  {t['strategy']:16}  "
                  f"{colour(t['direction']):3}  ${t['strike']:>8,.0f}  "
                  f"{t['entry_price']:>6.3f}  "
                  f"${t['btc_at_entry']:>8,.0f}  "
                  f"${t['btc_at_expiry']:>8,.0f}  "
                  f"{t['edge']:>+6.3f}  "
                  f"{money(t['pnl']):>8}  {result}")
    print(b(f"{'='*65}\n"))


def print_open(state: dict):
    open_pos = state.get("open_positions", [])
    print(b(f"\n{'='*65}"))
    print(b(f"  OPEN POSITIONS ({len(open_pos)})"))
    print(b(f"{'='*65}"))
    if not open_pos:
        print("  (none)")
    else:
        for p in open_pos:
            secs_left = max(0, p["resolves_at"] - time.time())
            colour = g if p["direction"] == "YES" else r
            print(f"  {ts(p['opened_at'])}  {p['strategy']:16}  "
                  f"{colour(p['direction'])}  "
                  f"Strike=${p['strike']:,.0f}  "
                  f"Entry={p['entry_price']:.3f}  "
                  f"BTC=${p['btc_at_entry']:,.2f}  "
                  f"Edge={p['edge']:+.3f}  "
                  f"Resolves in {secs_left:.0f}s")
    print(b(f"{'='*65}\n"))


def main():
    ap = argparse.ArgumentParser(description="View paper trading P&L")
    ap.add_argument("--trades", action="store_true", help="Show trade log")
    ap.add_argument("--open",   action="store_true", help="Show open positions only")
    ap.add_argument("--watch",  action="store_true", help="Refresh every 10 seconds")
    ap.add_argument("--last",   type=int, default=50, help="Number of trades to show (default 50)")
    args = ap.parse_args()

    def display():
        state = load()
        if not state:
            return
        if args.open:
            print_open(state)
        elif args.trades:
            print_trades(state, limit=args.last)
        else:
            print_summary(state)
            if not args.open:
                print_trades(state, limit=args.last)

    if args.watch:
        try:
            while True:
                os.system("cls" if os.name == "nt" else "clear")
                display()
                print(f"  (refreshing every 10s — Ctrl+C to stop)\n")
                time.sleep(10)
        except KeyboardInterrupt:
            pass
    else:
        display()


if __name__ == "__main__":
    main()
