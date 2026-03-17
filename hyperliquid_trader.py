"""Hyperliquid perpetual futures momentum bot.

Same signal as hourly Polymarket bot:
  - Enter 1-5 min into each UTC hour when BTC has moved 0.2%+
  - Long if BTC up, short if BTC down
  - Hard stop loss 0.5% from entry (placed on exchange)
  - Trailing stop: move to breakeven at +0.2%, trail at 0.3% from peak
  - Close at end of hour if still open

Advantages over Polymarket:
  - Guaranteed fills via market orders (no FOK/AMM issues)
  - Continuous P&L — profit scales with move size, not binary
  - 3x leverage = amplified returns on the same signal
  - No AMM repricing — enter at fair price any time
  - Run BTC + ETH + SOL simultaneously (future expansion)

Setup:
  1. Go to app.hyperliquid.xyz -> Settings -> API -> Generate API Wallet
  2. Add to .env:
       HYPERLIQUID_API_KEY=<agent_wallet_private_key>
       HYPERLIQUID_WALLET_ADDRESS=<your_main_wallet_0x_address>
  3. Deposit USDC on Hyperliquid (bridge from Arbitrum)

Usage:
    python hyperliquid_trader.py                         # paper mode
    python hyperliquid_trader.py --mode live             # real money
    python hyperliquid_trader.py --mode live --size 50   # $50 margin/trade (3x = $150 notional)
"""

import argparse
import asyncio
import json
import math
import os
import signal
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime

import structlog
from dotenv import load_dotenv

from src.config import Config
from src.price_feed import PriceFeed
from db import init_db, save_trade, update_bot_state

load_dotenv()
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────────────────────

SYMBOL           = "BTC"
LEVERAGE         = 3
TRADES_FILE      = "trades/hl_trades.json"
HL_MAINNET_URL   = "https://api.hyperliquid.xyz"

# Entry signal (same as Polymarket hourly bot)
MIN_SECS_INTO    = 60       # 1 min into hour
MAX_SECS_INTO    = 300      # 5 min max
MIN_MOVE_PCT     = 0.002    # 0.2% BTC move required
WINDOW_SECS      = 3600

# Risk management
STOP_LOSS_PCT        = 0.005   # 0.5% hard stop from entry
BREAKEVEN_PCT        = 0.002   # move stop to breakeven when up 0.2%
TRAIL_DISTANCE_PCT   = 0.003   # trail stop 0.3% from peak
CLOSE_BEFORE_END     = 120     # close position 2 min before hour end

# ── ANSI helpers ──────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def green(s):  return f"{GREEN}{s}{RESET}"
def red(s):    return f"{RED}{s}{RESET}"
def yellow(s): return f"{YELLOW}{s}{RESET}"
def bold(s):   return f"{BOLD}{s}{RESET}"
def _ts():     return datetime.now().strftime("%H:%M:%S")
def _eq(v):    return green(f"+${v:.2f}") if v >= 0 else red(f"-${abs(v):.2f}")


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Trade:
    opened_at:       float
    resolved_at:     float
    direction:       str         # "Long" or "Short"
    entry_price:     float
    exit_price:      float
    btc_at_open:     float       # BTC price at hour open
    size_usd:        float       # margin used
    notional_usd:    float       # size_usd * leverage
    pnl_usd:         float
    exit_reason:     str         # "stop_loss", "trailing_stop", "hour_end"
    mode:            str
    leverage:        int
    move_pct:        float


@dataclass
class OpenPos:
    direction:         str
    entry_price:       float
    size_btc:          float
    size_usd:          float       # margin
    notional_usd:      float
    btc_at_open:       float
    move_pct:          float
    entered_at:        float
    window_start:      int
    window_end:        int
    # Stop tracking
    hard_stop_price:   float
    trail_stop_price:  float
    peak_price:        float
    breakeven_moved:   bool
    exchange_oid:      int | None  # order ID of exchange stop-loss


# ── Signal evaluator ─────────────────────────────────────────────────────────

def evaluate_signal(
    btc_now: float,
    btc_open: float,
    secs_into: float,
) -> tuple[str, float, str] | None:
    """
    Returns (direction, move_pct, reason) or None if no signal.
    direction: "Long" or "Short"
    """
    if btc_open <= 0 or btc_now <= 0:
        return None

    if secs_into < MIN_SECS_INTO or secs_into > MAX_SECS_INTO:
        return None

    move_pct = (btc_now - btc_open) / btc_open
    if abs(move_pct) < MIN_MOVE_PCT:
        return None

    direction = "Long" if move_pct > 0 else "Short"

    # Same probability estimate as Polymarket bot (for logging)
    remaining_secs = max(1, WINDOW_SECS - secs_into)
    hourly_vol_per_sec = 0.004 / math.sqrt(3600)
    scaled_vol = hourly_vol_per_sec * math.sqrt(remaining_secs)
    z = abs(move_pct) / max(scaled_vol, 0.0001)
    prob = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    prob = max(0.50, min(0.95, prob))

    reason = (
        f"btc_move={move_pct:+.4%}  prob_win={prob:.3f}  "
        f"secs_into={secs_into:.0f}  lev={LEVERAGE}x"
    )
    return direction, move_pct, reason


# ── Hyperliquid client wrapper ────────────────────────────────────────────────

class HLClient:
    """Thin wrapper around hyperliquid-python-sdk Exchange + Info."""

    def __init__(self, api_key: str, wallet_address: str):
        from eth_account import Account as EthAccount
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info

        self._wallet_address = wallet_address
        agent_wallet = EthAccount.from_key(api_key)

        self.info     = Info(HL_MAINNET_URL, skip_ws=True)
        self.exchange = Exchange(
            agent_wallet,
            HL_MAINNET_URL,
            account_address=wallet_address,
        )

    def set_leverage(self, leverage: int, symbol: str = SYMBOL):
        return self.exchange.update_leverage(leverage, symbol, is_cross=True)

    def market_open(self, symbol: str, is_buy: bool, sz_btc: float, slippage: float = 0.01):
        """Open a market position. Returns fill price or None on failure."""
        result = self.exchange.market_open(symbol, is_buy, sz_btc, None, slippage)
        log.debug("hl_market_open", result=result)
        if result and result.get("status") == "ok":
            statuses = result["response"]["data"]["statuses"]
            if statuses and "filled" in statuses[0]:
                return float(statuses[0]["filled"]["avgPx"])
        return None

    def place_stop_loss(self, symbol: str, is_buy: bool, sz_btc: float, stop_px: float) -> int | None:
        """Place a stop-market order. Returns order ID or None."""
        from hyperliquid.utils.types import Cloid
        order_type = {
            "trigger": {
                "triggerPx": stop_px,
                "isMarket": True,
                "tpsl": "sl",
            }
        }
        result = self.exchange.order(
            symbol, is_buy, sz_btc, stop_px, order_type, reduce_only=True
        )
        log.debug("hl_stop_placed", result=result)
        if result and result.get("status") == "ok":
            statuses = result["response"]["data"]["statuses"]
            if statuses and "resting" in statuses[0]:
                return statuses[0]["resting"]["oid"]
        return None

    def cancel_order(self, symbol: str, oid: int):
        try:
            self.exchange.cancel(symbol, oid)
        except Exception as e:
            log.warning("hl_cancel_failed", oid=oid, error=str(e))

    def market_close(self, symbol: str, sz_btc: float, slippage: float = 0.01) -> float | None:
        """Close position with a market order. Returns fill price or None."""
        result = self.exchange.market_close(symbol, sz_btc, None, slippage)
        log.debug("hl_market_close", result=result)
        if result and result.get("status") == "ok":
            statuses = result["response"]["data"]["statuses"]
            if statuses and "filled" in statuses[0]:
                return float(statuses[0]["filled"]["avgPx"])
        return None

    def get_position(self, symbol: str) -> dict | None:
        """Returns current position dict or None if flat."""
        state = self.info.user_state(self._wallet_address)
        for pos in state.get("assetPositions", []):
            p = pos.get("position", {})
            if p.get("coin") == symbol:
                szi = float(p.get("szi", 0))
                if szi != 0:
                    return p
        return None

    def get_balance(self) -> float:
        """Returns account value in USD."""
        state = self.info.user_state(self._wallet_address)
        return float(state.get("crossMarginSummary", {}).get("accountValue", 0))


# ── Trader ───────────────────────────────────────────────────────────────────

class HyperliquidTrader:
    def __init__(self, config, mode="paper", trade_size_usd=50.0, check_interval=30):
        self.config         = config
        self.mode           = mode
        self.trade_size_usd = trade_size_usd  # margin per trade
        self.check_interval = check_interval
        self._shutdown      = False

        self.price_feed = PriceFeed(config.binance, max_history_secs=7200)

        self._use_db = init_db()
        if self._use_db:
            print(f"[{_ts()}] Database connected (PostgreSQL)")

        self.open_position:     OpenPos | None = None
        self.completed_trades:  list[Trade]    = []
        self.equity             = 0.0
        self.peak_equity        = 0.0
        self.max_drawdown       = 0.0

        self._window_opens:   dict[int, float] = {}
        self._traded_windows: set[int]         = set()

        self._hl: HLClient | None = None

    def _request_shutdown(self):
        self._shutdown = True

    def _get_hl(self) -> HLClient:
        if not self._hl:
            api_key        = os.getenv("HYPERLIQUID_API_KEY", "")
            wallet_address = os.getenv("HYPERLIQUID_WALLET_ADDRESS", "")
            if not api_key or not wallet_address:
                raise RuntimeError(
                    "HYPERLIQUID_API_KEY and HYPERLIQUID_WALLET_ADDRESS required for live mode"
                )
            self._hl = HLClient(api_key, wallet_address)
            self._hl.set_leverage(LEVERAGE, SYMBOL)
            bal = self._hl.get_balance()
            print(f"[{_ts()}]   Hyperliquid balance: ${bal:.2f} USDC")
        return self._hl

    async def run(self):
        mode_str = red(bold("LIVE MONEY")) if self.mode == "live" else green("PAPER")
        notional = self.trade_size_usd * LEVERAGE
        print(bold("\n" + "=" * 65))
        print(bold(f"  HYPERLIQUID FUTURES BOT  [{mode_str}{BOLD}]"))
        print(bold(f"  BTC-PERP momentum strategy"))
        print(bold("=" * 65))
        print(f"  Mode: {self.mode.upper()}  |  Margin: ${self.trade_size_usd}  |  "
              f"Notional: ${notional:.0f} ({LEVERAGE}x)")
        print(f"  Entry window: {MIN_SECS_INTO}-{MAX_SECS_INTO}s into each UTC hour")
        print(f"  Min BTC move: {MIN_MOVE_PCT*100:.1f}%")
        print(f"  Stop loss: {STOP_LOSS_PCT*100:.1f}%  |  Trail: {TRAIL_DISTANCE_PCT*100:.1f}%  |  "
              f"Breakeven at: +{BREAKEVEN_PCT*100:.1f}%")
        if self.mode == "live":
            print(yellow("  WARNING: Real orders will be placed on Hyperliquid!"))
        print(bold("=" * 65) + "\n")

        feed_task = asyncio.create_task(self.price_feed.start())

        print(f"[{_ts()}] Connecting to Binance WebSocket...")
        await asyncio.sleep(5)
        print(f"[{_ts()}] Warming up (60s to build price history)...")
        warmup_start = time.time()
        while time.time() - warmup_start < 60:
            if self.price_feed.has_data:
                remaining = 60 - (time.time() - warmup_start)
                price = self.price_feed.current_price
                print(f"\r[{_ts()}]  BTC ${price:,.2f}  |  warmup {remaining:.0f}s   ",
                      end="", flush=True)
            await asyncio.sleep(5)
        print()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                pass

        try:
            while not self._shutdown:
                now        = time.time()
                now_ts     = int(now)
                window_start = (now_ts // WINDOW_SECS) * WINDOW_SECS
                window_end   = window_start + WINDOW_SECS
                secs_into    = now - window_start

                # Record hourly open price
                if window_start not in self._window_opens and self.price_feed.has_data:
                    self._window_opens[window_start] = self.price_feed.current_price
                    print(f"\n[{_ts()}] Hour started — BTC open: "
                          f"${self._window_opens[window_start]:,.2f}")

                # Manage open position (trailing stop, hour-end close)
                if self.open_position:
                    await self._manage_position()

                # Check for new entry (only if flat and in signal window)
                if (not self.open_position
                        and window_start not in self._traded_windows
                        and window_start in self._window_opens
                        and self.price_feed.has_data):
                    await self._check_entry(window_start, window_end, secs_into)

                self._print_status(window_start, secs_into)
                await asyncio.sleep(self.check_interval)

        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            print(f"\n[{_ts()}] Shutting down...")
            if self.open_position and self.mode == "live":
                print(f"[{_ts()}] Closing open position on shutdown...")
                self._close_position("shutdown")
            self.price_feed.stop()
            feed_task.cancel()
            self._save_state()
            self._print_summary()

    async def _check_entry(self, window_start: int, window_end: int, secs_into: float):
        btc_now  = self.price_feed.current_price
        btc_open = self._window_opens.get(window_start, 0)

        result = evaluate_signal(btc_now, btc_open, secs_into)
        if not result:
            return

        direction, move_pct, reason = result

        # Mark this window as traded (even if order fails)
        self._traded_windows.add(window_start)

        is_long      = direction == "Long"
        notional_usd = self.trade_size_usd * LEVERAGE
        sz_btc       = round(notional_usd / btc_now, 4)

        colour = green if is_long else red
        print(f"\n[{_ts()}] {'-'*58}")
        print(f"[{_ts()}] {colour(bold(f'>> {direction.upper()}'))}  "
              f"move={move_pct:+.3%}  sz={sz_btc} BTC (${notional_usd:.0f} notional)")
        print(f"[{_ts()}]   {reason}")

        if self.mode == "live":
            fill_price = self._open_live_position(direction, sz_btc, btc_open)
            if fill_price is None:
                self._traded_windows.discard(window_start)
                return
            entry_price = fill_price
        else:
            entry_price = btc_now
            print(f"[{_ts()}]   [PAPER] Filled at ${entry_price:,.2f}")

        # Compute stop prices
        if is_long:
            hard_stop  = round(entry_price * (1 - STOP_LOSS_PCT), 1)
            trail_stop = hard_stop
            peak       = entry_price
        else:
            hard_stop  = round(entry_price * (1 + STOP_LOSS_PCT), 1)
            trail_stop = hard_stop
            peak       = entry_price

        print(f"[{_ts()}]   Entry=${entry_price:,.2f}  Hard stop=${hard_stop:,.2f}")

        self.open_position = OpenPos(
            direction=direction,
            entry_price=entry_price,
            size_btc=sz_btc,
            size_usd=self.trade_size_usd,
            notional_usd=notional_usd,
            btc_at_open=btc_open,
            move_pct=move_pct,
            entered_at=time.time(),
            window_start=window_start,
            window_end=window_end,
            hard_stop_price=hard_stop,
            trail_stop_price=trail_stop,
            peak_price=peak,
            breakeven_moved=False,
            exchange_oid=None,
        )

    def _open_live_position(self, direction: str, sz_btc: float, btc_open: float) -> float | None:
        try:
            hl        = self._get_hl()
            is_long   = direction == "Long"
            fill_price = hl.market_open(SYMBOL, is_long, sz_btc)
            if fill_price is None:
                print(f"[{_ts()}]   {red('ORDER FAILED — no fill returned')}")
                return None

            print(f"[{_ts()}]   {green('FILLED')} @ ${fill_price:,.2f}")

            # Place exchange stop-loss as safety net
            stop_px = round(fill_price * (1 - STOP_LOSS_PCT if is_long else 1 + STOP_LOSS_PCT), 1)
            oid = hl.place_stop_loss(SYMBOL, not is_long, sz_btc, stop_px)
            if oid:
                print(f"[{_ts()}]   Exchange stop placed @ ${stop_px:,.2f}  (oid={oid})")
            else:
                print(f"[{_ts()}]   {yellow('Warning: exchange stop not placed — managing in software only')}")

            return fill_price

        except Exception as e:
            print(f"[{_ts()}]   {red(f'ORDER FAILED: {e}')}")
            return None

    async def _manage_position(self):
        """Update trailing stop and close if triggered or hour ends."""
        pos     = self.open_position
        btc_now = self.price_feed.current_price
        now     = time.time()

        if not btc_now:
            return

        is_long  = pos.direction == "Long"
        time_left = pos.window_end - now

        # Update peak price
        if is_long:
            if btc_now > pos.peak_price:
                pos.peak_price = btc_now
        else:
            if btc_now < pos.peak_price:
                pos.peak_price = btc_now

        # Update trailing stop
        new_trail = pos.trail_stop_price
        if is_long:
            # Move to breakeven
            if not pos.breakeven_moved and btc_now >= pos.entry_price * (1 + BREAKEVEN_PCT):
                new_trail = pos.entry_price
                pos.breakeven_moved = True
                print(f"\n[{_ts()}]   Stop moved to breakeven: ${new_trail:,.2f}")
            # Trail from peak
            trail_from_peak = pos.peak_price * (1 - TRAIL_DISTANCE_PCT)
            if trail_from_peak > new_trail:
                new_trail = trail_from_peak
        else:
            # Move to breakeven
            if not pos.breakeven_moved and btc_now <= pos.entry_price * (1 - BREAKEVEN_PCT):
                new_trail = pos.entry_price
                pos.breakeven_moved = True
                print(f"\n[{_ts()}]   Stop moved to breakeven: ${new_trail:,.2f}")
            # Trail from peak (for short, peak is lowest price seen)
            trail_from_peak = pos.peak_price * (1 + TRAIL_DISTANCE_PCT)
            if trail_from_peak < new_trail:
                new_trail = trail_from_peak

        pos.trail_stop_price = new_trail

        # Check stop triggers
        trailing_hit = (is_long and btc_now <= pos.trail_stop_price) or \
                       (not is_long and btc_now >= pos.trail_stop_price)
        hard_hit     = (is_long and btc_now <= pos.hard_stop_price) or \
                       (not is_long and btc_now >= pos.hard_stop_price)
        hour_end     = time_left <= CLOSE_BEFORE_END

        if hard_hit:
            self._close_position("stop_loss")
        elif trailing_hit:
            self._close_position("trailing_stop")
        elif hour_end:
            self._close_position("hour_end")

    def _close_position(self, reason: str):
        pos     = self.open_position
        btc_now = self.price_feed.current_price or pos.entry_price

        if self.mode == "live":
            try:
                hl = self._get_hl()
                # Cancel exchange stop if we have one
                if pos.exchange_oid:
                    hl.cancel_order(SYMBOL, pos.exchange_oid)
                fill_price = hl.market_close(SYMBOL, pos.size_btc)
                exit_price = fill_price or btc_now
            except Exception as e:
                print(f"[{_ts()}]   {red(f'CLOSE FAILED: {e}')}")
                exit_price = btc_now
        else:
            exit_price = btc_now

        # P&L calculation: (exit - entry) / entry * notional * direction
        is_long  = pos.direction == "Long"
        pnl_pct  = (exit_price - pos.entry_price) / pos.entry_price
        pnl_usd  = pnl_pct * pos.notional_usd * (1 if is_long else -1)

        self.equity += pnl_usd
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        dd = self.peak_equity - self.equity
        if dd > self.max_drawdown:
            self.max_drawdown = dd

        trade = Trade(
            opened_at=pos.entered_at,
            resolved_at=time.time(),
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            btc_at_open=pos.btc_at_open,
            size_usd=pos.size_usd,
            notional_usd=pos.notional_usd,
            pnl_usd=pnl_usd,
            exit_reason=reason,
            mode=self.mode,
            leverage=LEVERAGE,
            move_pct=pos.move_pct,
        )
        self.completed_trades.append(trade)

        if self._use_db:
            save_trade(trade)
            update_bot_state(self.equity, self.peak_equity, self.max_drawdown, self.mode)

        result_str = green(f"+${pnl_usd:.2f}") if pnl_usd >= 0 else red(f"-${abs(pnl_usd):.2f}")
        move = (exit_price - pos.entry_price) / pos.entry_price * 100
        print(f"\n[{_ts()}] {'-'*58}")
        print(f"[{_ts()}] CLOSED [{reason}]  {pos.direction}  "
              f"${pos.entry_price:,.2f} → ${exit_price:,.2f}  ({move:+.3f}%)")
        print(f"[{_ts()}]   PnL={result_str}  Equity={_eq(self.equity)}")

        self.open_position = None
        self._save_state()

    def _print_status(self, window_start: int, secs_into: float):
        if not self.price_feed.has_data:
            return

        price    = self.price_feed.current_price
        btc_open = self._window_opens.get(window_start, price)
        move     = (price - btc_open) / btc_open * 100 if btc_open else 0
        n        = len(self.completed_trades)
        wins     = sum(1 for t in self.completed_trades if t.pnl_usd > 0)
        wr       = wins / n if n else 0
        remaining = max(0, WINDOW_SECS - secs_into)
        mins, secs = divmod(int(remaining), 60)
        traded   = "IN TRADE" if self.open_position else (
                   "TRADED"   if window_start in self._traded_windows else "watching")

        pos_info = ""
        if self.open_position:
            pos      = self.open_position
            unreal   = (price - pos.entry_price) / pos.entry_price * pos.notional_usd
            unreal  *= 1 if pos.direction == "Long" else -1
            pos_info = (f"  |  {pos.direction} ${pos.entry_price:,.0f} "
                        f"stop=${pos.trail_stop_price:,.0f} {_eq(unreal)}")

        print(
            f"\r[{_ts()}]  BTC ${price:,.2f} ({move:+.3f}%)"
            f"  |  Hour: {mins}m{secs:02d}s [{traded}]"
            f"  |  Trades: {n} (W:{wins})"
            f"  |  WR: {wr:.0%}"
            f"  |  {_eq(self.equity)}"
            f"{pos_info}     ",
            end="", flush=True
        )

    def _save_state(self):
        state = {
            "updated_at": time.time(),
            "mode":       self.mode,
            "equity":     self.equity,
            "trades":     [asdict(t) for t in self.completed_trades],
        }
        try:
            os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
            with open(TRADES_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.warning("save_state_failed", error=str(e))

    def _print_summary(self):
        trades = self.completed_trades
        n      = len(trades)
        print("\n")
        print(bold("=" * 65))
        print(bold(f"  HYPERLIQUID TRADING SUMMARY  [{self.mode.upper()}]"))
        print(bold("=" * 65))
        print(f"  Symbol:    BTC-PERP  |  Leverage: {LEVERAGE}x")
        print(f"  Total trades: {n}")
        if n == 0:
            print(bold("=" * 65))
            return
        wins   = sum(1 for t in trades if t.pnl_usd > 0)
        losses = n - wins
        by_reason = {}
        for t in trades:
            by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
        print(f"  Wins / Losses:  {wins} / {losses}  ({wins/n:.1%} WR)")
        print(f"  Total PnL:      {_eq(self.equity)}")
        print(f"  Avg PnL/trade:  {_eq(self.equity / n)}")
        print(f"  Max drawdown:   ${self.max_drawdown:.2f}")
        print(f"  Exit reasons:   {by_reason}")
        print()
        for t in trades[-10:]:
            dt  = datetime.fromtimestamp(t.opened_at).strftime("%H:%M")
            sym = green("W") if t.pnl_usd > 0 else red("L")
            print(f"    {dt}  {t.direction:5s}  "
                  f"${t.entry_price:,.0f}→${t.exit_price:,.0f}  "
                  f"[{t.exit_reason[:8]}]  {sym}  {_eq(t.pnl_usd)}")
        print(bold("=" * 65))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Hyperliquid BTC futures momentum bot")
    p.add_argument("--mode",   choices=["paper", "live"], default="paper")
    p.add_argument("--size",   type=float, default=50.0,
                   help="Margin per trade in USD (notional = size * leverage)")
    p.add_argument("--check",  type=int,   default=30,
                   help="Check interval in seconds (default 30)")
    p.add_argument("--symbol", default=SYMBOL, help="Coin to trade (default BTC)")
    args = p.parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING)

    config = Config()

    if args.mode == "live":
        if not os.getenv("HYPERLIQUID_API_KEY"):
            print("ERROR: HYPERLIQUID_API_KEY not set in .env")
            sys.exit(1)
        if not os.getenv("HYPERLIQUID_WALLET_ADDRESS"):
            print("ERROR: HYPERLIQUID_WALLET_ADDRESS not set in .env")
            sys.exit(1)
        notional = args.size * LEVERAGE
        print(bold(red("\n  *** LIVE MONEY MODE — HYPERLIQUID ***")))
        print(bold(red(f"  Margin: ${args.size}  |  Notional: ${notional:.0f}  |  Leverage: {LEVERAGE}x")))
        print(bold(red("  Real orders will be placed on Hyperliquid!\n")))

    trader = HyperliquidTrader(
        config=config,
        mode=args.mode,
        trade_size_usd=args.size,
        check_interval=args.check,
    )

    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
