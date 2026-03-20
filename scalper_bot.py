"""
scalper_bot.py -- Live paper trading bot for ETH intraday scalping on Hyperliquid.

Strategy: MACD Momentum (best from v2 backtest sweep, $820/day OOS)
  - Long:  MACD histogram crosses above 0 AND price > EMA50
  - Short: MACD histogram crosses below 0 AND price < EMA50
  - SL: swing low/high (5-bar) - 0.1 ATR
  - TP: entry + 3.0 x risk
  - Timeout: 30 candles (2.5 hours)
  - Cooldown: 2 bars after any close before re-entry

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
import db as _db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STATE_FILE    = "trades/scalper_live.json"
SYMBOL        = "ETH"
INTERVAL      = "5m"
LOOKBACK_BARS = 300        # enough for EMA100 + 200 warmup
LOOP_SECONDS  = 300        # 5 min

# Strategy params (MACD Momentum -- $820/day OOS on 17.5d ETH 5m backtest)
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL   = 9
TREND_EMA_P   = 50
ATR_PERIOD    = 14
TP_RR         = 3.0        # reward:risk ratio
MAX_HOLD      = 30         # bars before timeout
COOLDOWN_BARS = 2          # bars to wait after any close before re-entry

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
    print("\n[scalper] SIGTERM received, shutting down cleanly...")
    _stop = True
signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT,  _handle_sigterm)

# ---------------------------------------------------------------------------
# Market data -- Hyperliquid candle API (no US geo-blocking, Binance removed)
# ---------------------------------------------------------------------------
def fetch_candles(symbol: str = "ETHUSDT", interval: str = "5m",
                  limit: int = LOOKBACK_BARS) -> list:
    """Returns list of {t, o, h, l, c} dicts, oldest first.
    Uses Hyperliquid candle API (no US geo-blocking).
    """
    coin = symbol.replace("USDT", "")  # "ETHUSDT" -> "ETH"
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - limit * 5 * 60 * 1000  # 5m bars back
    resp = requests.post(
        "https://api.hyperliquid.xyz/info",
        headers={"Content-Type": "application/json"},
        json={"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval,
            "startTime": start_ms, "endTime": now_ms,
        }},
        timeout=15,
    )
    resp.raise_for_status()
    candles = []
    for k in resp.json():
        candles.append({
            "t": int(k["t"]) // 1000,
            "o": float(k["o"]),
            "h": float(k["h"]),
            "l": float(k["l"]),
            "c": float(k["c"]),
        })
    if not candles:
        raise ValueError("Hyperliquid returned empty candle data")
    return sorted(candles, key=lambda x: x["t"])

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

def _macd(closes: list, fast: int, slow: int, sig: int):
    """Returns (macd_line, signal_line, histogram) as parallel lists."""
    fast_e = _ema(closes, fast)
    slow_e = _ema(closes, slow)
    macd_line = [
        f - s if not (math.isnan(f) or math.isnan(s)) else float("nan")
        for f, s in zip(fast_e, slow_e)
    ]
    signal_line = _ema(macd_line, sig)
    histogram = [
        m - s if not (math.isnan(m) or math.isnan(s)) else float("nan")
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, histogram

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
# Signal generation -- MACD Momentum (matches backtest_v2.py MACD_Momentum)
# ---------------------------------------------------------------------------
def compute_signal(candles: list, last_close_bar_ts: int = 0) -> dict:
    """
    Returns signal dict or None.
    Only signals on the last completed bar (index -2). No multi-bar lookback.
    Enforces COOLDOWN_BARS wait after any trade close.
    """
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    _, _, hist  = _macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    trend_e     = _ema(closes, TREND_EMA_P)
    atr_v       = _atr(highs, lows, closes, ATR_PERIOD)

    i = len(candles) - 2   # last completed bar
    if i < 2:
        return None

    h_cur  = hist[i]
    h_prev = hist[i - 1]
    te     = trend_e[i]
    av     = atr_v[i]
    cl     = closes[i]

    if any(math.isnan(x) for x in [h_cur, h_prev, te, av]):
        return None
    if av <= 0:
        return None

    # Cooldown: skip if we closed a trade fewer than COOLDOWN_BARS bars ago
    if last_close_bar_ts > 0:
        bars_since_close = sum(1 for c in candles if c["t"] > last_close_bar_ts)
        if bars_since_close < COOLDOWN_BARS:
            return None

    # Long: MACD histogram crosses above 0 AND price above EMA50
    if h_prev <= 0 and h_cur > 0 and cl > te:
        swing_low = min(lows[max(0, i - 4):i + 1])
        stop      = swing_low - 0.1 * av
        risk      = cl - stop
        if risk <= 0:
            return None
        return {
            "direction":  1,
            "stop":       stop,
            "target":     cl + TP_RR * risk,
            "macd_hist":  round(h_cur, 4),
            "trend_ema":  round(te, 2),
            "atr":        round(av, 2),
        }

    # Short: MACD histogram crosses below 0 AND price below EMA50
    if h_prev >= 0 and h_cur < 0 and cl < te:
        swing_high = max(highs[max(0, i - 4):i + 1])
        stop       = swing_high + 0.1 * av
        risk       = stop - cl
        if risk <= 0:
            return None
        return {
            "direction":  -1,
            "stop":       stop,
            "target":     cl - TP_RR * risk,
            "macd_hist":  round(h_cur, 4),
            "trend_ema":  round(te, 2),
            "atr":        round(av, 2),
        }

    return None

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def init_state(capital: float, leverage: float) -> dict:
    return {
        "equity":             capital,
        "capital":            capital,
        "leverage":           leverage,
        "position":           None,       # open trade or null
        "trades":             [],
        "total_pnl":          0.0,
        "total_fees":         0.0,
        "peak_equity":        capital,
        "max_dd_pct":         0.0,
        "started_at":         _now_iso(),
        "last_check":         None,
        "status":             "flat",     # flat | long | short
        "last_close_bar_ts":  0,          # for cooldown after close
    }

def load_state(capital: float, leverage: float) -> dict:
    # Try DB first
    db_state = _db.get_scalper_state()
    if db_state is not None:
        print("[scalper] Loaded state from database.")
        return db_state
    # Fallback: local JSON
    os.makedirs("trades", exist_ok=True)
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    s = init_state(capital, leverage)
    save_state(s)
    return s

def save_state(state: dict):
    # Save to DB (primary)
    _db.save_scalper_state(state)
    # Also write local JSON (for dashboard fallback when no DB)
    try:
        os.makedirs("trades", exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Core check -- one 5-minute cycle
# ---------------------------------------------------------------------------
def run_check(state: dict, force: bool = False) -> dict:
    now_ts = int(time.time())
    state["last_check"] = _now_iso()

    # --- 1. Fetch candles ---------------------------------------------------
    try:
        candles = fetch_candles(f"{SYMBOL}USDT", INTERVAL, LOOKBACK_BARS)
    except Exception as e:
        print(f"[scalper] fetch error: {e}")
        save_state(state)
        return state

    last_bar = candles[-2]   # last completed bar
    last_close = last_bar["c"]
    last_bar_time = datetime.utcfromtimestamp(last_bar["t"]).strftime("%Y-%m-%d %H:%M")

    print(f"[scalper] {_now_iso()[:19]}Z  {SYMBOL}={last_close:,.2f}  bar={last_bar_time}")

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
            state["position"]          = None
            state["status"]            = "flat"
            state["last_close_bar_ts"] = last_bar["t"]
            _db.append_scalper_trade(trade_rec)

            emoji = "WIN" if pnl_usd > 0 else "LOSE"
            print(f"[scalper]  {emoji} CLOSED {'LONG' if d==1 else 'SHORT'} "
                  f"entry={entry_px:.1f} exit={exit_price:.1f} "
                  f"reason={exit_reason} PnL=${pnl_usd:.2f} "
                  f"equity=${state['equity']:,.2f}")

    # --- 3. Check for new signal (only if flat) -----------------------------
    if state["position"] is None:
        last_close_bar_ts = state.get("last_close_bar_ts", 0)
        sig = compute_signal(candles, last_close_bar_ts)
        if sig:
            direction = sig["direction"]
            dir_label = "LONG" if direction == 1 else "SHORT"

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
                "macd_hist":    sig["macd_hist"],
                "trend_ema":    sig["trend_ema"],
                "signal_atr":   sig["atr"],
            }
            state["status"] = dir_label.lower()

            risk = abs(entry_px - sig["stop"])
            print(f"[scalper]  >> OPEN {dir_label} @ {entry_px:.1f}  "
                  f"SL={sig['stop']:.1f}  TP={sig['target']:.1f}  "
                  f"R:R=1:{TP_RR}  Risk=${risk/entry_px*pos_size:.0f}  "
                  f"MACD={sig['macd_hist']:.4f}")
        else:
            print(f"[scalper]  -- No signal. Status: flat.")

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
_last_ping = 0

def _ping_web():
    """Ping our own web dyno every 25 min to prevent Eco dyno idling."""
    global _last_ping
    app_url = os.getenv("APP_URL", "")
    if not app_url:
        return
    if time.time() - _last_ping < 1500:  # 25 minutes
        return
    try:
        requests.get(f"{app_url}/health", timeout=10)
        _last_ping = time.time()
        print("[scalper] Pinged web dyno to prevent idle sleep.")
    except Exception:
        pass

def run_daemon(state: dict):
    print(f"[scalper] Starting daemon -- checking every {LOOP_SECONDS}s")
    while not _stop:
        state = run_check(state)
        _ping_web()
        if _stop:
            break
        # Sleep until next 5-min bar boundary (aligned to clock)
        now  = time.time()
        wait = LOOP_SECONDS - (now % LOOP_SECONDS)
        if wait < 10:
            wait += LOOP_SECONDS
        print(f"[scalper] Sleeping {wait:.0f}s until next bar...")
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
    print(f"  {SYMBOL} Scalper Bot -- Paper Trading Status")
    print("=" * 55)
    print(f"  Equity:    ${equity:,.2f}  ({ret_pct:+.2f}%)")
    print(f"  Total P&L: ${pnl:+,.2f}")
    print(f"  Trades:    {n_trades}  (WR {wr:.1f}%)")
    print(f"  Max DD:    -{dd:.1f}%")
    print(f"  Capital:   ${capital:,.0f}  x{state['leverage']}x leverage")
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
        print("\n  Status: FLAT -- waiting for signal")

    if n_trades:
        print("\n  Last 5 trades:")
        for t in state["trades"][-5:]:
            e = "WIN" if t["pnl_usd"] > 0 else "LOSE"
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
        _db.reset_scalper_state(args.capital, args.leverage)
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        print(f"[scalper] State reset. Starting fresh with ${args.capital:,.0f} x {args.leverage}x")
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
