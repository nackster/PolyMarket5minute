"""
scalper_bot.py — Live paper trading bot for BTC intraday scalping on Hyperliquid.

Strategy: Pullback to EMA (best from backtest sweep)
  - Long: BTC above EMA100 (uptrend), price pulls back to EMA21, RSI crosses above 45
  - Short: BTC below EMA100 (downtrend), price pops to EMA21, RSI crosses below 55
  - SL: swing low/high - 0.1 ATR
  - TP: entry + 3.0 × risk
  - Timeout: 30 candles (2.5 hours)

Simulated fills at next candle open. Taker fee 0.05% on exits, maker rebate 0.02% on entries.

Usage:
  python scalper_bot.py --status             # show current state
  python scalper_bot.py --force              # run one check now
  python scalper_bot.py --reset              # wipe state and start fresh
  python scalper_bot.py --daemon             # loop every 5 min (production)
  python scalper_bot.py --capital 25000 --leverage 5   # custom sizing
"""

import argparse
import json
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STATE_FILE    = "trades/scalper_live.json"
SYMBOL        = "BTC"
INTERVAL      = "5m"
LOOKBACK_BARS = 300        # enough for EMA100 + 200 warmup
LOOP_SECONDS  = 300        # 5 min

# Strategy params (best from sweep: $100/day on $10k 3x, MaxDD 12.2%)
FAST_EMA_P    = 21
TREND_EMA_P   = 100
RSI_PERIOD    = 14
ATR_PERIOD    = 14
RSI_ENTRY     = 45.0       # long crosses above, short crosses below 55
TP_RR         = 3.0        # reward:risk ratio
MAX_HOLD      = 30         # bars before timeout

# Fees
TAKER_FEE     = 0.0005     # 0.05%
MAKER_REBATE  = 0.0002     # 0.02%

DEFAULT_CAPITAL  = 10000.0
DEFAULT_LEVERAGE = 3.0

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_stop = False
def _handle_sigterm(signum, frame):
    global _stop
    print("\n[scalper] SIGTERM received, shutting down cleanly…")
    _stop = True
signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT,  _handle_sigterm)

# ---------------------------------------------------------------------------
# Market data — Binance 5m OHLCV (free, no key required, matches Hyperliquid)
# ---------------------------------------------------------------------------
def fetch_candles(symbol: str = "BTCUSDT", interval: str = "5m",
                  limit: int = LOOKBACK_BARS) -> list:
    """Returns list of {t, o, h, l, c} dicts, oldest first."""
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    candles = []
    for k in resp.json():
        candles.append({
            "t": int(k[0]) // 1000,   # Unix seconds
            "o": float(k[1]),
            "h": float(k[2]),
            "l": float(k[3]),
            "c": float(k[4]),
        })
    return candles

# ---------------------------------------------------------------------------
# Technical indicators (pure Python, no deps beyond stdlib)
# ---------------------------------------------------------------------------
def _ema(values: list, period: int) -> list:
    k = 2.0 / (period + 1)
    out = [float("nan")] * len(values)
    seed = None
    for i, v in enumerate(values):
        if math.isnan(v):
            continue
        if seed is None:
            seed = v
            out[i] = v
        else:
            seed = v * k + seed * (1 - k)
            out[i] = seed
    return out

def _rsi(closes: list, period: int = 14) -> list:
    out = [float("nan")] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
        if i >= period:
            ag = sum(gains[-period:]) / period
            al = sum(losses[-period:]) / period
            if al == 0:
                out[i] = 100.0
            else:
                rs = ag / al
                out[i] = 100.0 - 100.0 / (1 + rs)
    return out

def _atr(highs, lows, closes, period: int = 14) -> list:
    out = [float("nan")] * len(closes)
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i]  - closes[i - 1]))
        trs.append(tr)
        if i >= period:
            out[i] = sum(trs[-period:]) / period
    return out

# ---------------------------------------------------------------------------
# Signal generation — identical logic to backtest_scalper.py signals_pullback
# ---------------------------------------------------------------------------
def compute_signal(candles: list) -> dict:
    """
    Returns signal dict:
      { "direction": 1|-1, "stop": float, "target": float, "entry_bar_idx": int }
    or None if no signal on latest completed bar.

    We only look at the LAST completed bar (index -2, since -1 is the live bar).
    """
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    fast_e  = _ema(closes, FAST_EMA_P)
    trend_e = _ema(closes, TREND_EMA_P)
    rsi_v   = _rsi(closes, RSI_PERIOD)
    atr_v   = _atr(highs, lows, closes, ATR_PERIOD)

    # Check last 3 completed bars for a signal (in case we missed one)
    n = len(candles)
    for i in range(n - 4, n - 1):
        if i < 2:
            continue
        fe  = fast_e[i]
        te  = trend_e[i]
        ri  = rsi_v[i]
        rp  = rsi_v[i - 1]
        av  = atr_v[i]
        if any(math.isnan(x) for x in [fe, te, ri, rp, av]):
            continue
        if av <= 0:
            continue

        lo  = lows[i]
        hi  = highs[i]
        cl  = closes[i]

        # Long: uptrend, price pulled back to EMA21, RSI crossed above 45
        if (cl > te
                and lo <= fe * 1.001
                and rp < RSI_ENTRY
                and ri >= RSI_ENTRY):
            swing_low = min(lows[max(0, i - 2):i + 1])
            stop      = swing_low - 0.1 * av
            risk      = cl - stop
            if risk <= 0:
                continue
            return {
                "direction": 1,
                "stop":      stop,
                "target":    cl + TP_RR * risk,
                "signal_bar_idx": i,
                "signal_bar_time": candles[i]["t"],
                "signal_price": cl,
                "atr": av,
                "fast_ema": fe,
                "trend_ema": te,
                "rsi": ri,
            }

        # Short: downtrend, price popped to EMA21, RSI crossed below 55
        if (cl < te
                and hi >= fe * 0.999
                and rp > (100 - RSI_ENTRY)
                and ri <= (100 - RSI_ENTRY)):
            swing_high = max(highs[max(0, i - 2):i + 1])
            stop       = swing_high + 0.1 * av
            risk       = stop - cl
            if risk <= 0:
                continue
            return {
                "direction": -1,
                "stop":      stop,
                "target":    cl - TP_RR * risk,
                "signal_bar_idx": i,
                "signal_bar_time": candles[i]["t"],
                "signal_price": cl,
                "atr": av,
                "fast_ema": fe,
                "trend_ema": te,
                "rsi": ri,
            }

    return None

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def init_state(capital: float, leverage: float) -> dict:
    return {
        "equity":        capital,
        "capital":       capital,
        "leverage":      leverage,
        "position":      None,       # open trade or null
        "trades":        [],
        "total_pnl":     0.0,
        "total_fees":    0.0,
        "peak_equity":   capital,
        "max_dd_pct":    0.0,
        "started_at":    _now_iso(),
        "last_check":    None,
        "status":        "flat",     # flat | long | short
    }

def load_state(capital: float, leverage: float) -> dict:
    os.makedirs("trades", exist_ok=True)
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    s = init_state(capital, leverage)
    save_state(s)
    return s

def save_state(state: dict):
    os.makedirs("trades", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ---------------------------------------------------------------------------
# Core check — one 5-minute cycle
# ---------------------------------------------------------------------------
def run_check(state: dict, force: bool = False) -> dict:
    now_ts = int(time.time())
    state["last_check"] = _now_iso()

    # --- 1. Fetch candles ---------------------------------------------------
    try:
        candles = fetch_candles("BTCUSDT", INTERVAL, LOOKBACK_BARS)
    except Exception as e:
        print(f"[scalper] fetch error: {e}")
        save_state(state)
        return state

    last_bar = candles[-2]   # last completed bar
    last_close = last_bar["c"]
    last_bar_time = datetime.utcfromtimestamp(last_bar["t"]).strftime("%Y-%m-%d %H:%M")

    print(f"[scalper] {_now_iso()[:19]}Z  BTC={last_close:,.1f}  bar={last_bar_time}")

    # --- 2. Check open position ---------------------------------------------
    pos = state.get("position")
    if pos:
        d          = pos["direction"]   # 1 or -1
        entry_px   = pos["entry_price"]
        stop_px    = pos["stop"]
        target_px  = pos["target"]
        pos_size   = pos["pos_size"]
        entry_bar_ts = pos["entry_bar_ts"]

        # Count bars since entry
        bars_held = sum(1 for c in candles if c["t"] > entry_bar_ts)

        # Check each bar since last check (simplified: just check last completed bar)
        exit_price  = None
        exit_reason = None

        if d == 1:   # long
            if last_bar["l"] <= stop_px:
                exit_price  = stop_px
                exit_reason = "SL"
            elif last_bar["h"] >= target_px:
                exit_price  = target_px
                exit_reason = "TP"
        else:        # short
            if last_bar["h"] >= stop_px:
                exit_price  = stop_px
                exit_reason = "SL"
            elif last_bar["l"] <= target_px:
                exit_price  = target_px
                exit_reason = "TP"

        if exit_price is None and bars_held >= MAX_HOLD:
            exit_price  = last_close
            exit_reason = "TIMEOUT"

        if exit_price is not None:
            if d == 1:
                pnl_pct = (exit_price - entry_px) / entry_px
            else:
                pnl_pct = (entry_px - exit_price) / entry_px

            entry_fee = -pos_size * MAKER_REBATE   # earn on limit entry
            exit_fee  =  pos_size * TAKER_FEE      # pay on market exit
            total_fee = entry_fee + exit_fee
            pnl_usd   = pnl_pct * pos_size - total_fee

            state["equity"]    += pnl_usd
            state["total_pnl"] += pnl_usd
            state["total_fees"] += total_fee

            # Update max drawdown
            if state["equity"] > state["peak_equity"]:
                state["peak_equity"] = state["equity"]
            dd = (state["peak_equity"] - state["equity"]) / state["peak_equity"]
            if dd > state["max_dd_pct"]:
                state["max_dd_pct"] = dd

            trade_rec = {
                "entry_time":  pos["entry_time"],
                "exit_time":   _now_iso(),
                "direction":   "long" if d == 1 else "short",
                "entry_price": entry_px,
                "exit_price":  exit_price,
                "exit_reason": exit_reason,
                "pnl_pct":     round(pnl_pct * 100, 4),
                "pnl_usd":     round(pnl_usd, 2),
                "fees_usd":    round(total_fee, 2),
                "pos_size":    pos_size,
                "bars_held":   bars_held,
                "equity_after": round(state["equity"], 2),
            }
            state["trades"].append(trade_rec)
            state["position"] = None
            state["status"]   = "flat"

            emoji = "✓" if pnl_usd > 0 else "✗"
            print(f"[scalper]  {emoji} CLOSED {'LONG' if d==1 else 'SHORT'} "
                  f"entry={entry_px:.1f} exit={exit_price:.1f} "
                  f"reason={exit_reason} PnL=${pnl_usd:.2f} "
                  f"equity=${state['equity']:,.2f}")

    # --- 3. Check for new signal (only if flat) -----------------------------
    if state["position"] is None:
        sig = compute_signal(candles)
        if sig:
            direction = sig["direction"]
            dir_label = "LONG" if direction == 1 else "SHORT"

            # Simulated entry: next candle open ~ current last bar close
            # (in live, we'd place a limit order; here we use close as proxy)
            entry_px = last_close
            pos_size = state["equity"] * state["leverage"]

            state["position"] = {
                "direction":    direction,
                "entry_price":  entry_px,
                "stop":         sig["stop"],
                "target":       sig["target"],
                "pos_size":     pos_size,
                "entry_time":   _now_iso(),
                "entry_bar_ts": last_bar["t"],
                "signal_rsi":   round(sig["rsi"], 1),
                "signal_atr":   round(sig["atr"], 1),
                "fast_ema":     round(sig["fast_ema"], 1),
                "trend_ema":    round(sig["trend_ema"], 1),
            }
            state["status"] = dir_label.lower()

            risk      = abs(entry_px - sig["stop"])
            reward    = abs(sig["target"] - entry_px)
            print(f"[scalper]  → OPEN {dir_label} @ {entry_px:.1f}  "
                  f"SL={sig['stop']:.1f}  TP={sig['target']:.1f}  "
                  f"R:R=1:{TP_RR}  Risk=${risk/entry_px*pos_size:.0f}  "
                  f"RSI={sig['rsi']:.1f}")
        else:
            print(f"[scalper]  — No signal. Status: flat.")

    # --- 4. Mark-to-market unrealized PnL for display ----------------------
    pos = state.get("position")
    if pos:
        d = pos["direction"]
        if d == 1:
            unr = (last_close - pos["entry_price"]) / pos["entry_price"] * pos["pos_size"]
        else:
            unr = (pos["entry_price"] - last_close) / pos["entry_price"] * pos["pos_size"]
        state["unrealized_pnl"] = round(unr, 2)
        state["current_price"]  = last_close
    else:
        state["unrealized_pnl"] = 0.0
        state["current_price"]  = last_close

    save_state(state)
    return state

# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------
def run_daemon(state: dict):
    print(f"[scalper] Starting daemon — checking every {LOOP_SECONDS}s")
    while not _stop:
        state = run_check(state)
        if _stop:
            break
        # Sleep until next 5-min bar boundary (aligned to clock)
        now  = time.time()
        wait = LOOP_SECONDS - (now % LOOP_SECONDS)
        if wait < 10:
            wait += LOOP_SECONDS
        print(f"[scalper] Sleeping {wait:.0f}s until next bar…")
        for _ in range(int(wait)):
            if _stop:
                break
            time.sleep(1)
    print("[scalper] Daemon stopped.")

# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------
def show_status(state: dict):
    equity    = state["equity"]
    capital   = state["capital"]
    pnl       = state["total_pnl"]
    ret_pct   = (equity - capital) / capital * 100
    n_trades  = len(state["trades"])
    wins      = sum(1 for t in state["trades"] if t["pnl_usd"] > 0)
    wr        = wins / n_trades * 100 if n_trades else 0
    dd        = state.get("max_dd_pct", 0) * 100
    pos       = state.get("position")

    print("=" * 55)
    print("  BTC Scalper Bot — Paper Trading Status")
    print("=" * 55)
    print(f"  Equity:    ${equity:,.2f}  ({ret_pct:+.2f}%)")
    print(f"  Total P&L: ${pnl:+,.2f}")
    print(f"  Trades:    {n_trades}  (WR {wr:.1f}%)")
    print(f"  Max DD:    -{dd:.1f}%")
    print(f"  Capital:   ${capital:,.0f}  ×{state['leverage']}x leverage")
    print(f"  Last chk:  {state.get('last_check','never')[:19]}")

    if pos:
        d        = pos["direction"]
        unr      = state.get("unrealized_pnl", 0)
        cp       = state.get("current_price", 0)
        dir_str  = "LONG" if d == 1 else "SHORT"
        pct_move = (cp - pos["entry_price"]) / pos["entry_price"] * 100
        print(f"\n  OPEN {dir_str}: entry={pos['entry_price']:.1f}  "
              f"SL={pos['stop']:.1f}  TP={pos['target']:.1f}")
        print(f"    Current={cp:.1f} ({pct_move:+.2f}%)  UnrPnL=${unr:+.2f}")
    else:
        print("\n  Status: FLAT — waiting for signal")

    if n_trades:
        print("\n  Last 5 trades:")
        for t in state["trades"][-5:]:
            e = "✓" if t["pnl_usd"] > 0 else "✗"
            print(f"    {e} {t['direction']:<5} {t['exit_reason']:<8} "
                  f"${t['pnl_usd']:+.2f}  ({t['entry_time'][:16]})")
    print("=" * 55)

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="BTC Scalper Paper Trading Bot")
    parser.add_argument("--daemon",   action="store_true", help="Run continuously every 5 min")
    parser.add_argument("--force",    action="store_true", help="Run one check immediately")
    parser.add_argument("--status",   action="store_true", help="Show current status")
    parser.add_argument("--reset",    action="store_true", help="Reset state to fresh start")
    parser.add_argument("--capital",  type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--leverage", type=float, default=DEFAULT_LEVERAGE)
    args = parser.parse_args()

    if args.reset:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            print(f"[scalper] State reset. Starting fresh with ${args.capital:,.0f} × {args.leverage}x")
        state = load_state(args.capital, args.leverage)
    else:
        state = load_state(args.capital, args.leverage)

    if args.status:
        show_status(state)
        return

    if args.daemon:
        run_daemon(state)
        return

    # Default: run one check (--force or bare invocation)
    state = run_check(state, force=True)
    show_status(state)

if __name__ == "__main__":
    main()
