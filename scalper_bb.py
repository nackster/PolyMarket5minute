"""
scalper_bb.py -- BB Reversal paper trading bot for ETH on Hyperliquid.

Strategy: Bollinger Band Reversal (best high-WR strategy from backtest_highwr.py)
  - Long:  close < lower BB(20, 1.8) AND RSI < 35 AND bullish bar (close > open)
  - Short: close > upper BB(20, 1.8) AND RSI > 65 AND bearish bar (close < open)
  - SL: entry - 0.8 x ATR14 (long) / entry + 0.8 x ATR14 (short)
  - TP: entry + 1.5 x risk
  - Timeout: 20 candles (1.67 hours)
  - Cooldown: 2 bars after any close before re-entry
  - Backtest OOS: $565/day, 56.2% WR, 5.4% MaxDD

Simulated fills at next candle open. Taker fee 0.05% on exits, maker rebate 0.02% on entries.

Usage:
  python scalper_bb.py --status             # show current state
  python scalper_bb.py --force              # run one check now
  python scalper_bb.py --reset              # wipe state and start fresh
  python scalper_bb.py --daemon             # loop every 5 min (production)
  python scalper_bb.py --capital 25000 --leverage 5
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
STATE_FILE    = "trades/scalper_bb_live.json"
SYMBOL        = "ETH"
INTERVAL      = "5m"
LOOKBACK_BARS = 300
LOOP_SECONDS  = 300        # 5 min

# Strategy params (BB Reversal -- $565/day OOS, 56.2% WR, 5.4% MaxDD)
BB_PERIOD     = 20
BB_DEV        = 1.8
RSI_PERIOD    = 14
ATR_PERIOD    = 14
SL_MULT       = 0.8        # SL = entry ± SL_MULT * ATR
TP_RR         = 1.5        # reward:risk ratio
RSI_OS        = 35.0       # oversold threshold (long)
RSI_OB        = 65.0       # overbought threshold (short)
MAX_HOLD      = 20         # bars before timeout (~1.67h)
COOLDOWN_BARS = 2

# Fees
TAKER_FEE     = 0.0005
MAKER_REBATE  = 0.0002

DEFAULT_CAPITAL  = 10000.0
DEFAULT_LEVERAGE = 3.0

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_stop = False
def _handle_sigterm(signum, frame):
    global _stop
    print("\n[bb] SIGTERM received, shutting down cleanly...")
    _stop = True
signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT,  _handle_sigterm)

# ---------------------------------------------------------------------------
# Market data -- Hyperliquid candle API
# ---------------------------------------------------------------------------
def fetch_candles(symbol: str = "ETHUSDT", interval: str = "5m",
                  limit: int = LOOKBACK_BARS) -> list:
    coin = symbol.replace("USDT", "")
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - limit * 5 * 60 * 1000
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
# Technical indicators
# ---------------------------------------------------------------------------
def _sma(values: list, period: int) -> list:
    out = [float("nan")] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if any(math.isnan(x) for x in window):
            continue
        out[i] = sum(window) / period
    return out

def _stdev(values: list, period: int) -> list:
    out = [float("nan")] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if any(math.isnan(x) for x in window):
            continue
        mean = sum(window) / period
        out[i] = math.sqrt(sum((x - mean) ** 2 for x in window) / period)
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
            out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
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
# Signal generation -- BB Reversal
# ---------------------------------------------------------------------------
def compute_signal(candles: list, last_close_bar_ts: int = 0) -> dict:
    """
    Returns signal dict or None.
    Only signals on the last completed bar (index -2).
    """
    closes = [c["c"] for c in candles]
    opens  = [c["o"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    sma_v  = _sma(closes, BB_PERIOD)
    std_v  = _stdev(closes, BB_PERIOD)
    rsi_v  = _rsi(closes, RSI_PERIOD)
    atr_v  = _atr(highs, lows, closes, ATR_PERIOD)

    i = len(candles) - 2   # last completed bar
    if i < BB_PERIOD:
        return None

    mid = sma_v[i]
    sd  = std_v[i]
    ri  = rsi_v[i]
    av  = atr_v[i]
    cl  = closes[i]
    op  = opens[i]

    if any(math.isnan(x) for x in [mid, sd, ri, av]):
        return None
    if av <= 0 or sd <= 0:
        return None

    # Cooldown
    if last_close_bar_ts > 0:
        bars_since_close = sum(1 for c in candles if c["t"] > last_close_bar_ts)
        if bars_since_close < COOLDOWN_BARS:
            return None

    lower_band = mid - BB_DEV * sd
    upper_band = mid + BB_DEV * sd

    # Long: close below lower BB, RSI oversold, bullish bar
    if cl < lower_band and ri < RSI_OS and cl > op:
        stop   = cl - SL_MULT * av
        risk   = cl - stop
        if risk <= 0:
            return None
        return {
            "direction":  1,
            "stop":       stop,
            "target":     cl + TP_RR * risk,
            "rsi":        round(ri, 1),
            "lower_band": round(lower_band, 2),
            "upper_band": round(upper_band, 2),
            "bb_mid":     round(mid, 2),
            "atr":        round(av, 2),
        }

    # Short: close above upper BB, RSI overbought, bearish bar
    if cl > upper_band and ri > RSI_OB and cl < op:
        stop   = cl + SL_MULT * av
        risk   = stop - cl
        if risk <= 0:
            return None
        return {
            "direction":  -1,
            "stop":       stop,
            "target":     cl - TP_RR * risk,
            "rsi":        round(ri, 1),
            "lower_band": round(lower_band, 2),
            "upper_band": round(upper_band, 2),
            "bb_mid":     round(mid, 2),
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
        "position":           None,
        "trades":             [],
        "total_pnl":          0.0,
        "total_fees":         0.0,
        "peak_equity":        capital,
        "max_dd_pct":         0.0,
        "started_at":         _now_iso(),
        "last_check":         None,
        "status":             "flat",
        "last_close_bar_ts":  0,
    }

def load_state(capital: float, leverage: float) -> dict:
    db_state = _db.get_bb_state()
    if db_state is not None:
        print("[bb] Loaded state from database.")
        return db_state
    os.makedirs("trades", exist_ok=True)
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    s = init_state(capital, leverage)
    save_state(s)
    return s

def save_state(state: dict):
    _db.save_bb_state(state)
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
    state["last_check"] = _now_iso()

    try:
        candles = fetch_candles(f"{SYMBOL}USDT", INTERVAL, LOOKBACK_BARS)
    except Exception as e:
        print(f"[bb] fetch error: {e}")
        save_state(state)
        return state

    last_bar   = candles[-2]
    last_close = last_bar["c"]
    last_bar_time = datetime.utcfromtimestamp(last_bar["t"]).strftime("%Y-%m-%d %H:%M")
    print(f"[bb] {_now_iso()[:19]}Z  {SYMBOL}={last_close:,.2f}  bar={last_bar_time}")

    # --- 2. Check open position ---
    pos = state.get("position")
    if pos:
        d          = pos["direction"]
        entry_px   = pos["entry_price"]
        stop_px    = pos["stop"]
        target_px  = pos["target"]
        pos_size   = pos["pos_size"]
        entry_bar_ts = pos["entry_bar_ts"]

        bars_held   = sum(1 for c in candles if c["t"] > entry_bar_ts)
        exit_price  = None
        exit_reason = None

        if d == 1:
            if last_bar["l"] <= stop_px:
                exit_price, exit_reason = stop_px, "SL"
            elif last_bar["h"] >= target_px:
                exit_price, exit_reason = target_px, "TP"
        else:
            if last_bar["h"] >= stop_px:
                exit_price, exit_reason = stop_px, "SL"
            elif last_bar["l"] <= target_px:
                exit_price, exit_reason = target_px, "TP"

        if exit_price is None and bars_held >= MAX_HOLD:
            exit_price, exit_reason = last_close, "TIMEOUT"

        if exit_price is not None:
            pnl_pct   = (exit_price - entry_px) / entry_px if d == 1 else (entry_px - exit_price) / entry_px
            entry_fee = -pos_size * MAKER_REBATE
            exit_fee  =  pos_size * TAKER_FEE
            total_fee = entry_fee + exit_fee
            pnl_usd   = pnl_pct * pos_size - total_fee

            state["equity"]    += pnl_usd
            state["total_pnl"] += pnl_usd
            state["total_fees"] += total_fee

            if state["equity"] > state["peak_equity"]:
                state["peak_equity"] = state["equity"]
            dd = (state["peak_equity"] - state["equity"]) / state["peak_equity"]
            if dd > state["max_dd_pct"]:
                state["max_dd_pct"] = dd

            trade_rec = {
                "entry_time":   pos["entry_time"],
                "exit_time":    _now_iso(),
                "direction":    "long" if d == 1 else "short",
                "entry_price":  entry_px,
                "exit_price":   exit_price,
                "exit_reason":  exit_reason,
                "pnl_pct":      round(pnl_pct * 100, 4),
                "pnl_usd":      round(pnl_usd, 2),
                "fees_usd":     round(total_fee, 2),
                "pos_size":     pos_size,
                "bars_held":    bars_held,
                "equity_after": round(state["equity"], 2),
            }
            state["trades"].append(trade_rec)
            state["position"]          = None
            state["status"]            = "flat"
            state["last_close_bar_ts"] = last_bar["t"]
            _db.append_bb_trade(trade_rec)

            emoji = "WIN" if pnl_usd > 0 else "LOSE"
            print(f"[bb]  {emoji} CLOSED {'LONG' if d==1 else 'SHORT'} "
                  f"entry={entry_px:.1f} exit={exit_price:.1f} "
                  f"reason={exit_reason} PnL=${pnl_usd:.2f} "
                  f"equity=${state['equity']:,.2f}")

    # --- 3. Check for new signal ---
    if state["position"] is None:
        last_close_bar_ts = state.get("last_close_bar_ts", 0)
        sig = compute_signal(candles, last_close_bar_ts)
        if sig:
            direction = sig["direction"]
            dir_label = "LONG" if direction == 1 else "SHORT"
            entry_px  = last_close
            pos_size  = state["equity"] * state["leverage"]

            state["position"] = {
                "direction":    direction,
                "entry_price":  entry_px,
                "stop":         sig["stop"],
                "target":       sig["target"],
                "pos_size":     pos_size,
                "entry_time":   _now_iso(),
                "entry_bar_ts": last_bar["t"],
                "signal_rsi":   sig["rsi"],
                "bb_mid":       sig["bb_mid"],
                "signal_atr":   sig["atr"],
            }
            state["status"] = dir_label.lower()

            risk = abs(entry_px - sig["stop"])
            print(f"[bb]  >> OPEN {dir_label} @ {entry_px:.1f}  "
                  f"SL={sig['stop']:.1f}  TP={sig['target']:.1f}  "
                  f"R:R=1:{TP_RR}  Risk=${risk/entry_px*pos_size:.0f}  "
                  f"RSI={sig['rsi']:.1f}")
        else:
            print(f"[bb]  -- No signal. Status: flat.")

    # --- 4. Mark-to-market ---
    pos = state.get("position")
    if pos:
        d = pos["direction"]
        unr = ((last_close - pos["entry_price"]) / pos["entry_price"] * pos["pos_size"]
               if d == 1 else
               (pos["entry_price"] - last_close) / pos["entry_price"] * pos["pos_size"])
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
    global _last_ping
    app_url = os.getenv("APP_URL", "")
    if not app_url or time.time() - _last_ping < 1500:
        return
    try:
        requests.get(f"{app_url}/health", timeout=10)
        _last_ping = time.time()
        print("[bb] Pinged web dyno.")
    except Exception:
        pass

def run_daemon(state: dict):
    print(f"[bb] Starting daemon -- checking every {LOOP_SECONDS}s")
    while not _stop:
        state = run_check(state)
        _ping_web()
        if _stop:
            break
        now  = time.time()
        wait = LOOP_SECONDS - (now % LOOP_SECONDS)
        if wait < 10:
            wait += LOOP_SECONDS
        print(f"[bb] Sleeping {wait:.0f}s until next bar...")
        for _ in range(int(wait)):
            if _stop:
                break
            time.sleep(1)
    print("[bb] Daemon stopped.")

# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------
def show_status(state: dict):
    equity   = state["equity"]
    capital  = state["capital"]
    pnl      = state["total_pnl"]
    ret_pct  = (equity - capital) / capital * 100
    n_trades = len(state["trades"])
    wins     = sum(1 for t in state["trades"] if t["pnl_usd"] > 0)
    wr       = wins / n_trades * 100 if n_trades else 0
    dd       = state.get("max_dd_pct", 0) * 100
    pos      = state.get("position")

    print("=" * 55)
    print(f"  {SYMBOL} BB Reversal Bot -- Paper Trading Status")
    print("=" * 55)
    print(f"  Equity:    ${equity:,.2f}  ({ret_pct:+.2f}%)")
    print(f"  Total P&L: ${pnl:+,.2f}")
    print(f"  Trades:    {n_trades}  (WR {wr:.1f}%)")
    print(f"  Max DD:    -{dd:.1f}%")
    print(f"  Capital:   ${capital:,.0f}  x{state['leverage']}x leverage")
    print(f"  Last chk:  {state.get('last_check','never')[:19]}")

    if pos:
        d       = pos["direction"]
        unr     = state.get("unrealized_pnl", 0)
        cp      = state.get("current_price", 0)
        dir_str = "LONG" if d == 1 else "SHORT"
        pct_move = (cp - pos["entry_price"]) / pos["entry_price"] * 100
        print(f"\n  OPEN {dir_str}: entry={pos['entry_price']:.1f}  "
              f"SL={pos['stop']:.1f}  TP={pos['target']:.1f}")
        print(f"    Current={cp:.1f} ({pct_move:+.2f}%)  UnrPnL=${unr:+.2f}")
    else:
        print("\n  Status: FLAT -- waiting for BB signal")

    if n_trades:
        print("\n  Last 5 trades:")
        for t in state["trades"][-5:]:
            e = "WIN" if t["pnl_usd"] > 0 else "LOSE"
            print(f"    {e} {t['direction']:<5} {t['exit_reason']:<8} "
                  f"${t['pnl_usd']:+.2f}  ({t['entry_time'][:16]})")
    print("=" * 55)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ETH BB Reversal Paper Trading Bot")
    parser.add_argument("--daemon",   action="store_true")
    parser.add_argument("--force",    action="store_true")
    parser.add_argument("--status",   action="store_true")
    parser.add_argument("--reset",    action="store_true")
    parser.add_argument("--capital",  type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--leverage", type=float, default=DEFAULT_LEVERAGE)
    args = parser.parse_args()

    if args.reset:
        _db.reset_bb_state(args.capital, args.leverage)
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        print(f"[bb] State reset. Starting fresh with ${args.capital:,.0f} x {args.leverage}x")
        state = load_state(args.capital, args.leverage)
    else:
        state = load_state(args.capital, args.leverage)

    if args.status:
        show_status(state)
        return

    if args.daemon:
        run_daemon(state)
        return

    state = run_check(state, force=True)
    show_status(state)

if __name__ == "__main__":
    main()
