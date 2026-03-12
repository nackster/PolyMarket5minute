"""Paper trading engine for 5-minute BTC binary prediction markets.

Connects to Binance WebSocket for real-time BTC prices, creates synthetic
5-minute binary markets every 5 minutes, evaluates all strategies, and
tracks paper P&L — exactly what the live bot would do, but without
placing real orders.

Usage:
    python paper_trade.py                  # default settings
    python paper_trade.py --edge 0.03      # higher edge filter
    python paper_trade.py --size 25        # $25 per trade
    python paper_trade.py --interval 300   # market every 5 min (default)
"""

import argparse
import asyncio
import json
import math
import os
import signal
import time
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime

TRADES_FILE = "paper_trades.json"   # written after every resolved trade

import structlog

from src.config import Config, BinanceConfig
from src.polymarket_client import Market
from src.price_feed import PriceFeed, Tick
from src.signals import (
    CVDAnalyzer,
    LiquiditySweepDetector,
    TapeSpeedAnalyzer,
    VWAPAnalyzer,
    SignalAggregator,
)
from src.strategies.base import SignalDirection
from src.strategies.momentum import MomentumStrategy
from src.strategies.mean_reversion import MeanReversionStrategy
from src.strategies.volatility import VolatilityStrategy
from src.strategies.strike_arb import StrikeArbStrategy

log = structlog.get_logger()


# ── ANSI colour helpers ───────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def green(s):  return f"{GREEN}{s}{RESET}"
def red(s):    return f"{RED}{s}{RESET}"
def yellow(s): return f"{YELLOW}{s}{RESET}"
def cyan(s):   return f"{CYAN}{s}{RESET}"
def bold(s):   return f"{BOLD}{s}{RESET}"


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class PaperTrade:
    """A completed paper trade."""
    opened_at: float          # unix when we entered
    resolved_at: float        # unix when market expired
    strategy: str
    direction: str            # "YES" or "NO"
    strike: float
    entry_price: float        # probability we paid (e.g. 0.62)
    btc_at_entry: float
    btc_at_expiry: float
    size: float               # USDC
    edge: float
    confidence: float
    won: bool
    pnl: float
    reason: str


@dataclass
class OpenPosition:
    """A live market position waiting to resolve."""
    market_id: str
    opened_at: float
    resolves_at: float        # unix expiry time
    strategy: str
    direction: str
    strike: float
    entry_price: float
    btc_at_entry: float
    size: float
    edge: float
    reason: str


# ── Fair value model (matches backtester) ────────────────────────────────────

def estimate_fair_value(current_price: float, strike: float, secs_to_expiry: float) -> float:
    """Gaussian fair probability of BTC > strike at expiry."""
    if current_price == 0 or secs_to_expiry <= 0:
        return 0.5
    vol_per_sec = 0.001 / math.sqrt(300)
    scaled_vol = vol_per_sec * math.sqrt(secs_to_expiry)
    distance = (current_price - strike) / current_price
    if scaled_vol > 0:
        z = distance / scaled_vol
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return 1.0 if current_price > strike else 0.0


# ── Paper trading engine ──────────────────────────────────────────────────────

class PaperTrader:
    """
    Live paper trading loop.

    Every `market_interval` seconds:
      1. Snap BTC price, round to nearest $100 for strike
      2. Build a synthetic Market with fair-value pricing
      3. Run all strategies, pick the highest-edge signal
      4. If edge > threshold: open a paper position

    Each open position resolves after `market_interval` seconds:
      5. Compare BTC price at expiry to strike -> won/lost
      6. Compute PnL, log the result, update running totals
    """

    def __init__(
        self,
        config: Config,
        market_interval: int = 300,
        simulated_spread: float = 0.03,
        trade_size: float = 10.0,
    ):
        self.config = config
        self.market_interval = market_interval
        self.simulated_spread = simulated_spread
        self.trade_size = trade_size

        # Price feed and signal analyzers
        self.price_feed = PriceFeed(config.binance, max_history_secs=900)
        self.cvd = CVDAnalyzer()
        self.sweep_detector = LiquiditySweepDetector()
        self.tape_analyzer = TapeSpeedAnalyzer()
        self.vwap_analyzer = VWAPAnalyzer(std_devs=config.strategy.bb_std_dev)

        # Strategies — StrikeArbStrategy fires every interval as the reliable baseline;
        # momentum / MR / volatility can override it when they see a higher edge.
        self.strategies = [
            StrikeArbStrategy(config.strategy),
            MomentumStrategy(config.strategy),
            MeanReversionStrategy(config.strategy),
            VolatilityStrategy(config.strategy),
        ]

        # State
        self.open_positions: list[OpenPosition] = []
        self.completed_trades: list[PaperTrade] = []
        self.equity = 0.0
        self.peak_equity = 0.0
        self.max_drawdown = 0.0
        self._market_counter = 0

        # Register callbacks so all analyzers get every tick
        self.price_feed.on_tick(self.cvd.update)
        self.price_feed.on_tick(self.sweep_detector.update)
        self.price_feed.on_tick(self.tape_analyzer.update)

    def _request_shutdown(self):
        """Signal handler for graceful shutdown (SIGTERM on Heroku)."""
        self._shutdown = True

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        """Start the paper trading engine."""
        print(bold("\n" + "=" * 65))
        print(bold("  PAPER TRADING ENGINE  —  5-min BTC Binary Markets"))
        print(bold("=" * 65))
        print(f"  Interval: {self.market_interval}s  |  Size: ${self.trade_size}  "
              f"|  Min edge: {self.config.trading.min_edge_threshold:.1%}")
        print(f"  Warmup: 120s of price data before first market")
        print(bold("=" * 65) + "\n")

        # Start price feed in background
        feed_task = asyncio.create_task(self.price_feed.start())

        # Wait for warmup
        warmup_secs = 120
        print(f"[{_ts()}] Connecting to Binance WebSocket...")
        await asyncio.sleep(5)   # brief wait for first ticks to arrive

        print(f"[{_ts()}] Warming up ({warmup_secs}s of price data needed)...")
        warmup_start = time.time()
        while time.time() - warmup_start < warmup_secs:
            if self.price_feed.has_data:
                elapsed = time.time() - warmup_start
                remaining = warmup_secs - elapsed
                price = self.price_feed.current_price
                print(f"\r[{_ts()}]  BTC ${price:,.2f}  |  warmup {remaining:.0f}s remaining   ",
                      end="", flush=True)
            await asyncio.sleep(5)
        print()

        # Calculate next market boundary (align to wall-clock 5-min boundaries)
        next_market = _next_boundary(self.market_interval)
        secs_until = next_market - time.time()
        print(f"[{_ts()}] Warmup complete. First market in {secs_until:.0f}s  "
              f"(at {datetime.fromtimestamp(next_market).strftime('%H:%M:%S')})\n")

        # Graceful shutdown on SIGTERM (Heroku sends this) or KeyboardInterrupt
        self._shutdown = False
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                pass  # Windows — handled via KeyboardInterrupt

        # Main event loop
        try:
            while not self._shutdown:
                now = time.time()

                # Resolve any expired positions
                await self._resolve_expired(now)

                # Open a new market if it's time
                if now >= next_market:
                    await self._open_market(now)
                    next_market += self.market_interval

                # Print live status every 10 seconds
                self._print_status(next_market)

                await asyncio.sleep(10)

        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            print(f"\n[{_ts()}] Stopping paper trader...")
            self.price_feed.stop()
            feed_task.cancel()
            self._save_state()
            self._print_final_summary()

    # ── Market creation ───────────────────────────────────────────────────────

    async def _open_market(self, now: float):
        """Create a new synthetic market and evaluate strategies."""
        if not self.price_feed.has_data:
            return

        self._market_counter += 1
        current_price = self.price_feed.current_price

        # Round to nearest $100 strike
        strike = round(current_price / 100.0) * 100.0
        expiry = now + self.market_interval

        # Simulate market prices using fair-value model + spread
        fair_value = estimate_fair_value(current_price, strike, self.market_interval)
        sim_yes_price = max(0.05, min(0.95, fair_value + self.simulated_spread / 2))
        sim_no_price  = max(0.05, min(0.95, 1.0 - fair_value + self.simulated_spread / 2))

        market = Market(
            condition_id=f"paper_{int(now)}",
            question=f"Will Bitcoin be above ${strike:,.0f} at {datetime.fromtimestamp(expiry).strftime('%H:%M')}?",
            token_id_yes=f"yes_{int(now)}",
            token_id_no=f"no_{int(now)}",
            outcome_yes_price=sim_yes_price,
            outcome_no_price=sim_no_price,
            end_time=expiry,
            created_at=now,
            strike_price=strike,
        )

        # Evaluate all strategies.
        # Only filter: minimum edge must cover the simulated spread + a small buffer.
        # StrikeArbStrategy fires every interval; others override when they see higher edge.
        MIN_EDGE_LIVE = max(self.config.trading.min_edge_threshold, 0.04)

        best_signal = None
        skipped = []
        for strategy in self.strategies:
            try:
                signal = strategy.evaluate(market, self.price_feed)
                if not signal or signal.direction == SignalDirection.HOLD:
                    continue
                if signal.edge < MIN_EDGE_LIVE:
                    skipped.append(f"{strategy.name}(edge={signal.edge:.3f}<min)")
                    continue
                if best_signal is None or signal.edge > best_signal.edge:
                    best_signal = signal
            except Exception as e:
                log.debug("strategy_error", strategy=strategy.name, error=str(e))

        dist_pct = (current_price - strike) / strike * 100
        print(f"\n[{_ts()}] {'-'*58}")
        print(f"[{_ts()}] Market #{self._market_counter}: {market.question}")
        print(f"[{_ts()}]   BTC=${current_price:,.2f}  Strike=${strike:,.0f}  "
              f"Dist={dist_pct:+.2f}%  Fair={fair_value:.3f}")
        print(f"[{_ts()}]   Market prices: YES={sim_yes_price:.3f}  NO={sim_no_price:.3f}")

        if best_signal:
            direction_str = best_signal.direction.value
            entry_price = sim_yes_price if best_signal.direction == SignalDirection.YES else sim_no_price
            colour = green if direction_str == "YES" else red

            pos = OpenPosition(
                market_id=market.condition_id,
                opened_at=now,
                resolves_at=expiry,
                strategy=best_signal.strategy_name,
                direction=direction_str,
                strike=strike,
                entry_price=entry_price,
                btc_at_entry=current_price,
                size=self.trade_size,
                edge=best_signal.edge,
                reason=best_signal.reason,
            )
            self.open_positions.append(pos)

            print(f"[{_ts()}]   {colour(bold(f'>> ENTER {direction_str}'))}  "
                  f"strategy={best_signal.strategy_name}  "
                  f"entry={entry_price:.3f}  "
                  f"edge={best_signal.edge:+.3f}  "
                  f"conf={best_signal.confidence:.3f}")
            print(f"[{_ts()}]   Reason: {best_signal.reason[:80]}")
        else:
            skip_str = f"  filtered: {', '.join(skipped)}" if skipped else ""
            print(f"[{_ts()}]   {yellow('>> NO TRADE')}{skip_str}")

    # ── Position resolution ───────────────────────────────────────────────────

    async def _resolve_expired(self, now: float):
        """Check for expired positions and settle them."""
        still_open = []
        resolved_any = False
        for pos in self.open_positions:
            if now < pos.resolves_at:
                still_open.append(pos)
                continue

            # Settle this position
            if not self.price_feed.has_data:
                still_open.append(pos)
                continue

            btc_at_expiry = self.price_feed.current_price
            won = (
                (pos.direction == "YES" and btc_at_expiry > pos.strike)
                or (pos.direction == "NO"  and btc_at_expiry <= pos.strike)
            )

            if won:
                pnl = (1.0 - pos.entry_price) * pos.size
            else:
                pnl = -pos.entry_price * pos.size

            self.equity += pnl
            if self.equity > self.peak_equity:
                self.peak_equity = self.equity
            dd = self.peak_equity - self.equity
            if dd > self.max_drawdown:
                self.max_drawdown = dd

            trade = PaperTrade(
                opened_at=pos.opened_at,
                resolved_at=now,
                strategy=pos.strategy,
                direction=pos.direction,
                strike=pos.strike,
                entry_price=pos.entry_price,
                btc_at_entry=pos.btc_at_entry,
                btc_at_expiry=btc_at_expiry,
                size=pos.size,
                edge=pos.edge,
                confidence=pos.entry_price,
                won=won,
                pnl=pnl,
                reason=pos.reason,
            )
            self.completed_trades.append(trade)
            resolved_any = True

            result_str = green("WON") if won else red("LOST")
            print(f"\n[{_ts()}] {'-'*58}")
            print(f"[{_ts()}] RESOLVED: {pos.strategy.upper()} {pos.direction}  "
                  f"Strike=${pos.strike:,.0f}")
            print(f"[{_ts()}]   Entry BTC=${pos.btc_at_entry:,.2f}  ->  "
                  f"Expiry BTC=${btc_at_expiry:,.2f}")
            print(f"[{_ts()}]   {result_str}  PnL={green(f'+${pnl:.2f}') if pnl > 0 else red(f'-${abs(pnl):.2f}')}"
                  f"  Running equity: {_equity_str(self.equity)}")

        self.open_positions = still_open
        if resolved_any:
            self._save_state()

    def _save_state(self):
        """Persist completed trades + equity to disk for external inspection."""
        state = {
            "updated_at": time.time(),
            "equity": self.equity,
            "peak_equity": self.peak_equity,
            "max_drawdown": self.max_drawdown,
            "open_positions": [
                {
                    "market_id": p.market_id,
                    "opened_at": p.opened_at,
                    "resolves_at": p.resolves_at,
                    "strategy": p.strategy,
                    "direction": p.direction,
                    "strike": p.strike,
                    "entry_price": p.entry_price,
                    "btc_at_entry": p.btc_at_entry,
                    "size": p.size,
                    "edge": p.edge,
                }
                for p in self.open_positions
            ],
            "trades": [asdict(t) for t in self.completed_trades],
        }
        try:
            with open(TRADES_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.warning("save_state_failed", error=str(e))

    # ── Status display ────────────────────────────────────────────────────────

    def _print_status(self, next_market: float):
        """Print a compact live-status line."""
        if not self.price_feed.has_data:
            return

        price = self.price_feed.current_price
        secs_until = max(0, next_market - time.time())
        n_trades = len(self.completed_trades)
        wins = sum(1 for t in self.completed_trades if t.won)
        win_rate = wins / n_trades if n_trades else 0.0
        open_count = len(self.open_positions)

        # Compact one-liner with running stats
        print(
            f"\r[{_ts()}]  BTC ${price:,.2f}"
            f"  |  Next market: {secs_until:.0f}s"
            f"  |  Open: {open_count}"
            f"  |  Trades: {n_trades} (W:{wins} L:{n_trades-wins})"
            f"  |  WR: {win_rate:.0%}"
            f"  |  Equity: {_equity_str(self.equity)}"
            f"     ",
            end="", flush=True
        )

    # ── Final summary ─────────────────────────────────────────────────────────

    def _print_final_summary(self):
        """Print full performance summary on exit."""
        trades = self.completed_trades
        n = len(trades)
        print("\n")
        print(bold("=" * 65))
        print(bold("  PAPER TRADING SUMMARY"))
        print(bold("=" * 65))
        print(f"  Total trades:    {n}")

        if n == 0:
            print("  (no trades completed yet)")
            print(bold("=" * 65))
            return

        wins = sum(1 for t in trades if t.won)
        losses = n - wins
        win_rate = wins / n
        total_pnl = self.equity

        gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        pnls = [t.pnl for t in trades]
        sharpe = 0.0
        if len(pnls) > 1:
            mean = sum(pnls) / len(pnls)
            var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
            std = math.sqrt(var) if var > 0 else 0.001
            sharpe = (mean / std) * math.sqrt(105120)

        avg_edge = sum(t.edge for t in trades) / n

        print(f"  Wins / Losses:   {wins} / {losses}")
        print(f"  Win rate:        {win_rate:.1%}")
        print(f"  Total PnL:       {_equity_str(total_pnl)}")
        print(f"  Avg PnL/trade:   {_equity_str(total_pnl / n)}")
        print(f"  Avg edge:        {avg_edge:+.4f}")
        print(f"  Profit factor:   {pf:.2f}")
        print(f"  Sharpe (ann):    {sharpe:.2f}")
        print(f"  Max drawdown:    ${self.max_drawdown:.2f}")
        print()

        # Strategy breakdown
        strats: dict[str, dict] = {}
        for t in trades:
            s = strats.setdefault(t.strategy, {"n": 0, "wins": 0, "pnl": 0.0})
            s["n"] += 1
            s["wins"] += int(t.won)
            s["pnl"] += t.pnl
        print("  Strategy Breakdown:")
        for name, s in strats.items():
            wr = s["wins"] / s["n"] if s["n"] else 0
            print(f"    {name:20s}  n={s['n']:3d}  wr={wr:.0%}  pnl={_equity_str(s['pnl'])}")

        print()
        print("  Recent trades:")
        for t in trades[-10:]:
            dt = datetime.fromtimestamp(t.opened_at).strftime("%H:%M")
            symbol = "W" if t.won else "L"
            pnl_str = green(f"+${t.pnl:.2f}") if t.pnl > 0 else red(f"-${abs(t.pnl):.2f}")
            print(f"    {dt}  {t.strategy:16s}  {t.direction:3s}  "
                  f"${t.strike:,.0f}  {symbol}  {pnl_str}")

        print(bold("=" * 65))


# ── Utilities ─────────────────────────────────────────────────────────────────

def _ts() -> str:
    """Compact HH:MM:SS timestamp."""
    return datetime.now().strftime("%H:%M:%S")


def _equity_str(equity: float) -> str:
    """Coloured equity string."""
    if equity >= 0:
        return green(f"+${equity:.2f}")
    return red(f"-${abs(equity):.2f}")


def _next_boundary(interval_secs: int) -> float:
    """Next wall-clock time that's a multiple of interval_secs."""
    now = time.time()
    return math.ceil(now / interval_secs) * interval_secs


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Paper trade 5-min BTC binary markets")
    p.add_argument("--edge",     type=float, default=0.02,  help="Min edge threshold (default 0.02)")
    p.add_argument("--size",     type=float, default=10.0,  help="Trade size in USDC (default $10)")
    p.add_argument("--spread",   type=float, default=0.03,  help="Simulated market spread (default 0.03)")
    p.add_argument("--interval", type=int,   default=300,   help="Market interval seconds (default 300)")
    return p.parse_args()


def main():
    args = parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING)  # silence noisy libs

    config = Config()
    config.trading.min_edge_threshold = args.edge

    trader = PaperTrader(
        config=config,
        market_interval=args.interval,
        simulated_spread=args.spread,
        trade_size=args.size,
    )

    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
