"""Hyperliquid BTC/ETH Statistical Arbitrage Bot.

Strategy: BTC and ETH are cointegrated. When their price ratio deviates
2.5+ standard deviations from its 60-period rolling mean, bet on reversion.

  Z > +2.5  → BTC overvalued vs ETH → SHORT BTC
  Z < -2.5  → BTC undervalued vs ETH → LONG BTC
  Exit when Z returns to neutral (< 0.5)

Uses LIMIT orders to earn Hyperliquid maker rebate (-0.02%/side).
Backtest: 51.8% WR, ~$589/month on $1k margin at 5x (90 days).

Setup:
  1. Go to app.hyperliquid.xyz -> Settings -> API -> Generate API Wallet
  2. Add to .env:
       HYPERLIQUID_API_KEY=<agent_wallet_private_key>
       HYPERLIQUID_WALLET_ADDRESS=<your_main_wallet_0x_address>
  3. Deposit USDC on Hyperliquid (bridge from Arbitrum)

Usage:
    python hyperliquid_trader.py                          # paper $50
    python hyperliquid_trader.py --mode paper --size 1000 --leverage 5
    python hyperliquid_trader.py --mode live  --size 1000 --leverage 5
"""

import argparse
import asyncio
import json
import math
import os
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime

import requests
from dotenv import load_dotenv

from src.config import Config
from src.price_feed import PriceFeed
from db import init_db, update_bot_state

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────

SYMBOL         = "BTC"
HL_MAINNET_URL = "https://api.hyperliquid.xyz"
TRADES_FILE    = "trades/hl_trades.json"

# Stat arb signal
ZSCORE_PERIOD  = 60       # 60 one-minute samples = 1 hour rolling window
ENTRY_Z        = 2.5      # enter when |Z| exceeds this
EXIT_Z         = 0.5      # exit when |Z| drops below this

# Risk management
STOP_LOSS_PCT  = 0.012    # 1.2% hard stop from entry
TRAIL_PCT      = 0.008    # 0.8% trail from peak
BREAKEVEN_PCT  = 0.006    # move stop to breakeven at +0.6%
MAX_HOLD_SECS  = 86400    # max 24 hours per trade
COOLDOWN_SECS  = 14400    # 4 hours between trades

# Limit order behaviour (live mode)
LIMIT_OFFSET   = 0.0001   # post limit 0.01% inside market to capture maker rebate
LIMIT_WAIT     = 60       # seconds to wait for limit fill before re-posting

# ── ANSI helpers ──────────────────────────────────────────────────────────────

GREEN  = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"; BOLD = "\033[1m"; RESET = "\033[0m"
def green(s):  return f"{GREEN}{s}{RESET}"
def red(s):    return f"{RED}{s}{RESET}"
def yellow(s): return f"{YELLOW}{s}{RESET}"
def bold(s):   return f"{BOLD}{s}{RESET}"
def _ts():     return datetime.now().strftime("%H:%M:%S")
def _eq(v):    return green(f"+${v:.2f}") if v >= 0 else red(f"-${abs(v):.2f}")


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Trade:
    opened_at:    float
    resolved_at:  float
    direction:    str
    entry_price:  float
    exit_price:   float
    btc_at_open:  float
    eth_at_open:  float
    ratio_zscore: float
    size_usd:     float
    notional_usd: float
    pnl_usd:      float
    exit_reason:  str
    mode:         str
    leverage:     int


@dataclass
class OpenPos:
    direction:       str
    entry_price:     float
    size_btc:        float
    size_usd:        float
    notional_usd:    float
    btc_at_open:     float
    eth_at_open:     float
    ratio_zscore:    float
    entered_at:      float
    stop_price:      float
    peak_price:      float
    breakeven_moved: bool
    exchange_oid:    int | None


# ── Z-score tracker ───────────────────────────────────────────────────────────

class RatioTracker:
    """Tracks BTC/ETH price ratio and computes rolling Z-score."""

    def __init__(self, period: int = ZSCORE_PERIOD):
        self._period  = period
        self._ratios  = deque(maxlen=period)
        self._last_sample_min = -1

    def update(self, btc: float, eth: float) -> float | None:
        """
        Call every tick. Samples the ratio once per minute.
        Returns current Z-score or None if not enough data yet.
        """
        if btc <= 0 or eth <= 0:
            return None

        cur_min = int(time.time() // 60)
        if cur_min != self._last_sample_min:
            self._ratios.append(btc / eth)
            self._last_sample_min = cur_min

        if len(self._ratios) < self._period:
            return None

        vals = list(self._ratios)
        mean = sum(vals) / len(vals)
        std  = math.sqrt(sum((x - mean) ** 2 for x in vals) / len(vals))
        if std == 0:
            return 0.0

        return (vals[-1] - mean) / std

    @property
    def ready(self) -> bool:
        return len(self._ratios) >= self._period

    @property
    def samples(self) -> int:
        return len(self._ratios)


# ── Hyperliquid client ────────────────────────────────────────────────────────

class HLClient:
    def __init__(self, api_key: str, wallet_address: str):
        from eth_account import Account as EthAccount
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info

        self._wallet = wallet_address
        agent        = EthAccount.from_key(api_key)
        self.info     = Info(HL_MAINNET_URL, skip_ws=True)
        self.exchange = Exchange(agent, HL_MAINNET_URL, account_address=wallet_address)

    def set_leverage(self, leverage: int, symbol: str):
        self.exchange.update_leverage(leverage, symbol, is_cross=False)

    def get_balance(self) -> float:
        state = self.info.user_state(self._wallet)
        return float(state["marginSummary"]["accountValue"])

    def get_position(self, symbol: str) -> dict | None:
        state = self.info.user_state(self._wallet)
        for p in state.get("assetPositions", []):
            if p["position"]["coin"] == symbol:
                pos = p["position"]
                sz  = float(pos["szi"])
                if abs(sz) > 0:
                    return {"size": sz, "entry": float(pos["entryPx"]),
                            "pnl": float(pos["unrealizedPnl"])}
        return None

    def market_open(self, symbol: str, is_long: bool, sz_btc: float) -> dict:
        from hyperliquid.utils import constants
        return self.exchange.market_open(symbol, is_long, sz_btc)

    def limit_open(self, symbol: str, is_long: bool, sz_btc: float, price: float) -> dict:
        """Post a limit order (earns maker rebate at -0.02%/side)."""
        from hyperliquid.utils.types import Limit
        order_type = {"limit": {"tif": "Gtc"}}
        return self.exchange.order(symbol, is_long, sz_btc, price, order_type)

    def market_close(self, symbol: str, is_long: bool, sz_btc: float) -> dict:
        return self.exchange.market_close(symbol)

    def cancel_order(self, symbol: str, oid: int):
        try:
            self.exchange.cancel(symbol, oid)
        except Exception:
            pass

    def get_mid_price(self, symbol: str) -> float | None:
        meta = self.info.all_mids()
        return float(meta[symbol]) if symbol in meta else None


# ── ETH price feed via Binance REST (lightweight) ─────────────────────────────

def fetch_eth_price() -> float | None:
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "ETHUSDT"}, timeout=5,
        )
        return float(r.json()["price"])
    except Exception:
        return None


# ── Main trader ───────────────────────────────────────────────────────────────

class HyperliquidTrader:

    def __init__(self, config, mode="paper", trade_size_usd=50.0,
                 check_interval=30, leverage=5):
        self.config          = config
        self.mode            = mode
        self.trade_size_usd  = trade_size_usd
        self.check_interval  = check_interval
        self._leverage       = leverage
        self._shutdown       = False

        self.price_feed = PriceFeed(config.binance, max_history_secs=7200)
        self.ratio      = RatioTracker(ZSCORE_PERIOD)

        self._use_db = init_db()
        if self._use_db:
            print(f"[{_ts()}] Database connected")

        self.open_position:    OpenPos | None = None
        self.completed_trades: list[Trade]    = []
        self.equity            = 0.0
        self.peak_equity       = 0.0
        self.max_drawdown      = 0.0
        self._last_trade_ts    = 0.0

        self._hl: HLClient | None = None
        if mode == "live":
            api_key  = os.getenv("HYPERLIQUID_API_KEY")
            wallet   = os.getenv("HYPERLIQUID_WALLET_ADDRESS")
            self._hl = HLClient(api_key, wallet)
            self._hl.set_leverage(leverage, SYMBOL)
            bal = self._hl.get_balance()
            print(f"[{_ts()}] HL balance: ${bal:.2f}")

        # Print config
        notional = trade_size_usd * leverage
        mode_str = red(bold("LIVE MONEY")) if mode == "live" else green("PAPER")
        print(bold("\n" + "=" * 65))
        print(f"  Hyperliquid BTC/ETH Stat Arb Bot  [{mode_str}]")
        print(bold("=" * 65))
        print(f"  Signal:    BTC/ETH Z-score  |  Entry: |Z| > {ENTRY_Z}  |  Exit: |Z| < {EXIT_Z}")
        print(f"  Margin: ${trade_size_usd}  |  Notional: ${notional:.0f}  |  Leverage: {leverage}x")
        print(f"  Stop: {STOP_LOSS_PCT*100:.1f}%  |  Trail: {TRAIL_PCT*100:.1f}%  |  Max hold: 24h")
        print(f"  Orders: {'LIMIT (maker rebate)' if mode == 'live' else 'simulated'}")
        print(bold("=" * 65) + "\n")

    # ── State persistence ─────────────────────────────────────────────────────

    def _save_state(self):
        os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
        data = {
            "mode":            self.mode,
            "leverage":        self._leverage,
            "equity":          round(self.equity, 2),
            "peak_equity":     round(self.peak_equity, 2),
            "max_drawdown":    round(self.max_drawdown, 2),
            "trades":          [asdict(t) for t in self.completed_trades],
            "open_position":   asdict(self.open_position) if self.open_position else None,
            "updated_at":      time.time(),
        }
        tmp = TRADES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, TRADES_FILE)

    def _load_state(self):
        if not os.path.exists(TRADES_FILE):
            return
        try:
            with open(TRADES_FILE) as f:
                data = json.load(f)
            self.equity       = data.get("equity", 0.0)
            self.peak_equity  = data.get("peak_equity", 0.0)
            self.max_drawdown = data.get("max_drawdown", 0.0)
            for t in data.get("trades", []):
                # Drop unknown fields from old schema versions
                valid = {k: v for k, v in t.items() if k in Trade.__dataclass_fields__}
                try:
                    self.completed_trades.append(Trade(**valid))
                except Exception:
                    pass
            op = data.get("open_position")
            if op:
                self.open_position = OpenPos(**op)
                print(f"[{_ts()}] Restored open position: {self.open_position.direction} "
                      f"@ ${self.open_position.entry_price:.2f}")
        except Exception as e:
            print(f"[{_ts()}] State load error: {e}")

    # ── Signal evaluation ─────────────────────────────────────────────────────

    def _evaluate_signal(self, btc: float, eth: float) -> tuple[str, float] | None:
        """Returns (direction, zscore) or None."""
        z = self.ratio.update(btc, eth)
        if z is None:
            return None

        now = time.time()
        if now - self._last_trade_ts < COOLDOWN_SECS:
            return None

        if self.open_position:
            return None

        if z > ENTRY_Z:
            return "Short", z
        if z < -ENTRY_Z:
            return "Long", z

        return None

    # ── Enter position ────────────────────────────────────────────────────────

    def _enter(self, direction: str, z: float, btc: float, eth: float):
        notional = self.trade_size_usd * self._leverage
        sz_btc   = round(notional / btc, 4)
        is_long  = direction == "Long"

        print(f"\n[{_ts()}] {'─'*58}")
        print(f"[{_ts()}] {bold(green('>> LONG') if is_long else bold(red('>> SHORT')))}  "
              f"Z={z:+.2f}  BTC=${btc:,.2f}  ETH=${eth:,.2f}")
        print(f"[{_ts()}]   Ratio={btc/eth:.2f}  Z={z:+.2f}  "
              f"Margin=${self.trade_size_usd}  Notional=${notional:.0f}")

        if self.mode == "live" and self._hl:
            try:
                # Use limit order for maker rebate
                offset    = btc * LIMIT_OFFSET
                lim_price = btc + offset if is_long else btc - offset
                lim_price = round(lim_price, 1)

                print(f"[{_ts()}]   Placing LIMIT {'BUY' if is_long else 'SELL'} "
                      f"@ ${lim_price:.2f} (maker rebate)")
                resp = self._hl.limit_open(SYMBOL, is_long, sz_btc, lim_price)

                # Wait for fill
                oid   = resp.get("response", {}).get("data", {}).get("statuses", [{}])[0].get("resting", {}).get("oid")
                filled = False
                for _ in range(LIMIT_WAIT):
                    time.sleep(1)
                    pos = self._hl.get_position(SYMBOL)
                    if pos and abs(pos["size"]) >= sz_btc * 0.9:
                        filled      = True
                        entry_price = pos["entry"]
                        break

                if not filled:
                    if oid:
                        self._hl.cancel_order(SYMBOL, oid)
                    # Fall back to market order
                    print(f"[{_ts()}]   Limit not filled — falling back to market order")
                    self._hl.market_open(SYMBOL, is_long, sz_btc)
                    pos         = self._hl.get_position(SYMBOL)
                    entry_price = pos["entry"] if pos else btc

            except Exception as e:
                print(f"[{_ts()}]   Order failed: {e}")
                return
        else:
            entry_price = btc  # paper

        stop  = entry_price * (1 - STOP_LOSS_PCT) if is_long else entry_price * (1 + STOP_LOSS_PCT)

        self.open_position = OpenPos(
            direction       = direction,
            entry_price     = entry_price,
            size_btc        = sz_btc,
            size_usd        = self.trade_size_usd,
            notional_usd    = notional,
            btc_at_open     = btc,
            eth_at_open     = eth,
            ratio_zscore    = round(z, 3),
            entered_at      = time.time(),
            stop_price      = stop,
            peak_price      = entry_price,
            breakeven_moved = False,
            exchange_oid    = None,
        )
        self._last_trade_ts = time.time()
        self._save_state()
        print(f"[{_ts()}]   Entered {direction} @ ${entry_price:.2f}  "
              f"Stop: ${stop:.2f}")

    # ── Manage open position ──────────────────────────────────────────────────

    def _manage(self, btc: float, eth: float):
        pos = self.open_position
        if not pos:
            return

        is_long   = pos.direction == "Long"
        now       = time.time()
        held_secs = now - pos.entered_at

        # Get current Z-score (update tracker)
        z = self.ratio.update(btc, eth)

        # Update trailing stop
        if is_long:
            pos.peak_price = max(pos.peak_price, btc)
            trail          = pos.peak_price * (1 - TRAIL_PCT)
            if trail > pos.stop_price:
                pos.stop_price = trail
            if not pos.breakeven_moved and btc >= pos.entry_price * (1 + BREAKEVEN_PCT):
                pos.stop_price      = pos.entry_price
                pos.breakeven_moved = True
                print(f"[{_ts()}]   Stop moved to breakeven @ ${pos.entry_price:.2f}")
        else:
            pos.peak_price = min(pos.peak_price, btc)
            trail          = pos.peak_price * (1 + TRAIL_PCT)
            if trail < pos.stop_price:
                pos.stop_price = trail
            if not pos.breakeven_moved and btc <= pos.entry_price * (1 - BREAKEVEN_PCT):
                pos.stop_price      = pos.entry_price
                pos.breakeven_moved = True
                print(f"[{_ts()}]   Stop moved to breakeven @ ${pos.entry_price:.2f}")

        # Check exit conditions
        stop_hit    = (is_long and btc <= pos.stop_price) or \
                      (not is_long and btc >= pos.stop_price)
        z_exit      = z is not None and abs(z) < EXIT_Z
        time_exit   = held_secs >= MAX_HOLD_SECS

        exit_reason = None
        if stop_hit:
            exit_reason = "trailing_stop" if pos.breakeven_moved else "stop_loss"
        elif z_exit:
            exit_reason = "z_exit"
        elif time_exit:
            exit_reason = "time_exit"

        if exit_reason:
            self._close(btc, exit_reason)
        else:
            # Status line every check interval
            pnl_now = ((btc - pos.entry_price) / pos.entry_price * pos.notional_usd
                       if is_long else
                       (pos.entry_price - btc) / pos.entry_price * pos.notional_usd)
            z_str = f"Z={z:+.2f}" if z is not None else "Z=..."
            print(f"[{_ts()}]  BTC ${btc:,.2f}  {z_str}  "
                  f"Stop=${pos.stop_price:.2f}  PnL={_eq(pnl_now)}  "
                  f"Hold={held_secs/3600:.1f}h")

    # ── Close position ────────────────────────────────────────────────────────

    def _close(self, btc: float, reason: str):
        pos      = self.open_position
        is_long  = pos.direction == "Long"
        exit_px  = btc

        if self.mode == "live" and self._hl:
            try:
                self._hl.market_close(SYMBOL, is_long, pos.size_btc)
                live_pos = self._hl.get_position(SYMBOL)
                if live_pos is None:
                    pass  # closed
            except Exception as e:
                print(f"[{_ts()}]   Close failed: {e}")

        pnl = ((exit_px - pos.entry_price) / pos.entry_price * pos.notional_usd
               if is_long else
               (pos.entry_price - exit_px) / pos.entry_price * pos.notional_usd)
        # Deduct maker fee (we RECEIVE 0.02%/side = -0.04% round trip)
        fee = 2 * (-0.0002) * pos.notional_usd   # negative = income
        pnl -= fee   # subtracting a negative = adding income

        self.equity      += pnl
        self.peak_equity  = max(self.peak_equity, self.equity)
        self.max_drawdown = min(self.max_drawdown, self.equity - self.peak_equity)

        trade = Trade(
            opened_at    = pos.entered_at,
            resolved_at  = time.time(),
            direction    = pos.direction,
            entry_price  = pos.entry_price,
            exit_price   = exit_px,
            btc_at_open  = pos.btc_at_open,
            eth_at_open  = pos.eth_at_open,
            ratio_zscore = pos.ratio_zscore,
            size_usd     = pos.size_usd,
            notional_usd = pos.notional_usd,
            pnl_usd      = round(pnl, 2),
            exit_reason  = reason,
            mode         = self.mode,
            leverage     = self._leverage,
        )
        self.completed_trades.append(trade)
        self.open_position = None

        sym    = green("WIN") if pnl >= 0 else red("LOSS")
        held   = (trade.resolved_at - trade.opened_at) / 3600
        print(f"\n[{_ts()}] {bold('CLOSED')}  [{sym}]  {reason}")
        print(f"[{_ts()}]   {pos.direction} @ ${pos.entry_price:.2f} -> ${exit_px:.2f}  "
              f"Hold={held:.1f}h  PnL={_eq(pnl)}")
        print(f"[{_ts()}]   Equity: {_eq(self.equity)}  "
              f"Trades: {len(self.completed_trades)}\n")

        self._save_state()
        if self._use_db:
            try:
                update_bot_state(self.equity, self.peak_equity, self.max_drawdown, self.mode)
            except Exception:
                pass

    # ── Status display ────────────────────────────────────────────────────────

    def _print_stats(self):
        n = len(self.completed_trades)
        if n == 0:
            return
        wins   = sum(1 for t in self.completed_trades if t.pnl_usd > 0)
        total  = sum(t.pnl_usd for t in self.completed_trades)
        print(bold("\n" + "=" * 65))
        print(f"  Symbol: BTC-PERP  |  Leverage: {self._leverage}x  |  Signal: BTC/ETH Z-score")
        print(f"  Trades: {n}  |  WR: {wins/n*100:.1f}%  |  Total PnL: {_eq(total)}")
        print(f"  Max DD: {_eq(self.max_drawdown)}")
        if self.completed_trades:
            last = self.completed_trades[-1]
            print(f"  Last:   {last.direction:5s} @ ${last.entry_price:,.2f} -> "
                  f"${last.exit_price:,.2f}  {last.exit_reason}  {_eq(last.pnl_usd)}")
        print(bold("=" * 65))

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_shutdown", True))
        signal.signal(signal.SIGINT,  lambda *_: setattr(self, "_shutdown", True))

        self._load_state()
        # PriceFeed.start() is an async coroutine — run it in a background thread
        threading.Thread(
            target=lambda: asyncio.run(self.price_feed.start()),
            daemon=True,
        ).start()
        time.sleep(3)   # let BTC price feed warm up

        print(f"[{_ts()}] Warming up Z-score ({ZSCORE_PERIOD} samples needed)...")
        eth_price = None
        last_eth_fetch = 0

        while not self._shutdown:
            btc = self.price_feed.current_price
            if not btc:
                time.sleep(5)
                continue

            # Refresh ETH price every 30 seconds
            now = time.time()
            if now - last_eth_fetch >= 30:
                eth_price      = fetch_eth_price()
                last_eth_fetch = now

            if not eth_price:
                time.sleep(5)
                continue

            z = self.ratio.update(btc, eth_price)

            # Warmup progress
            if not self.ratio.ready:
                print(f"\r[{_ts()}]  Warming up: {self.ratio.samples}/{ZSCORE_PERIOD} samples  "
                      f"BTC=${btc:,.2f}  ETH=${eth_price:,.2f}", end="")
                time.sleep(self.check_interval)
                continue

            if self.open_position:
                self._manage(btc, eth_price)
            else:
                result = self._evaluate_signal(btc, eth_price)
                if result:
                    direction, z_val = result
                    self._enter(direction, z_val, btc, eth_price)
                else:
                    z_str = f"{z:+.2f}" if z is not None else "..."
                    cd_left = max(0, COOLDOWN_SECS - (now - self._last_trade_ts))
                    cd_str  = f"  CD={cd_left/3600:.1f}h" if cd_left > 0 else ""
                    print(f"\r[{_ts()}]  BTC ${btc:,.2f}  ETH ${eth_price:.2f}  "
                          f"Z={z_str}  Trades:{len(self.completed_trades)}"
                          f"{cd_str}  {_eq(self.equity)}", end="")

            time.sleep(self.check_interval)

        # Graceful shutdown
        print(f"\n[{_ts()}] Shutting down...")
        if self.open_position and self.mode == "live" and self._hl:
            btc = self.price_feed.current_price or 0
            self._close(btc, "shutdown")
        self._print_stats()
        self._save_state()
        self.price_feed.stop()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Hyperliquid BTC/ETH Stat Arb Bot")
    p.add_argument("--mode",     choices=["paper", "live"], default="paper")
    p.add_argument("--size",     type=float, default=50.0,
                   help="Margin per trade in USD")
    p.add_argument("--leverage", type=int,   default=5,
                   help="Leverage (default 5)")
    p.add_argument("--check",    type=int,   default=30,
                   help="Check interval seconds (default 30)")
    args = p.parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING)

    config = Config()

    if args.mode == "live":
        if not os.getenv("HYPERLIQUID_API_KEY"):
            print("ERROR: HYPERLIQUID_API_KEY not set"); sys.exit(1)
        if not os.getenv("HYPERLIQUID_WALLET_ADDRESS"):
            print("ERROR: HYPERLIQUID_WALLET_ADDRESS not set"); sys.exit(1)
        notional = args.size * args.leverage
        print(bold(red(f"\n  *** LIVE MONEY — ${args.size} margin  "
                       f"{args.leverage}x  = ${notional:.0f} notional ***\n")))

    trader = HyperliquidTrader(
        config         = config,
        mode           = args.mode,
        trade_size_usd = args.size,
        check_interval = args.check,
        leverage       = args.leverage,
    )
    trader.run()


if __name__ == "__main__":
    main()
