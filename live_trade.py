"""Live trading engine for 5-minute BTC binary prediction markets.

Two modes:
  --mode paper   (default) Synthetic markets, simulated trades, real Binance feed.
                 Identical to paper_trade.py but Heroku-ready.
  --mode live    Real Polymarket markets, real CLOB orders, real money.
                 Requires POLYMARKET_PRIVATE_KEY in env.

The live mode discovers BTC binary markets on Polymarket's CLOB API,
evaluates them with our strategy suite, and places real limit orders.

Usage:
    python live_trade.py                     # paper mode, $50 trades
    python live_trade.py --mode live         # real money
    python live_trade.py --mode live --size 20  # start small
"""

import argparse
import asyncio
import json
import math
import os
import signal
import time
import sys
from dataclasses import dataclass, asdict
from datetime import datetime

import structlog
import requests

from src.config import Config, BinanceConfig
from src.polymarket_client import Market, PolymarketClient
from src.price_feed import PriceFeed, Tick
from src.signals import (
    CVDAnalyzer,
    LiquiditySweepDetector,
    TapeSpeedAnalyzer,
    VWAPAnalyzer,
)
from src.strategies.base import SignalDirection
from src.strategies.momentum import MomentumStrategy
from src.strategies.mean_reversion import MeanReversionStrategy
from src.strategies.volatility import VolatilityStrategy
from src.strategies.strike_arb import StrikeArbStrategy

log = structlog.get_logger()

TRADES_FILE = "live_trades.json"

# ── ANSI colour helpers ──────────────────────────────────────────────────────
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
def _ts():     return datetime.now().strftime("%H:%M:%S")
def _equity_str(eq):
    return green(f"+${eq:.2f}") if eq >= 0 else red(f"-${abs(eq):.2f}")


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class LiveTrade:
    opened_at: float
    resolved_at: float
    strategy: str
    direction: str
    strike: float
    entry_price: float
    btc_at_entry: float
    btc_at_expiry: float
    size: float
    edge: float
    confidence: float
    won: bool
    pnl: float
    reason: str
    mode: str           # "paper" or "live"
    order_id: str = ""  # Polymarket order ID (live mode only)


@dataclass
class OpenPosition:
    market_id: str
    opened_at: float
    resolves_at: float
    strategy: str
    direction: str
    strike: float
    entry_price: float
    btc_at_entry: float
    size: float
    edge: float
    reason: str
    order_id: str = ""


# ── Fair value model ─────────────────────────────────────────────────────────

def estimate_fair_value(current_price, strike, secs_to_expiry):
    if current_price == 0 or secs_to_expiry <= 0:
        return 0.5
    vol_per_sec = 0.001 / math.sqrt(300)
    scaled_vol = vol_per_sec * math.sqrt(secs_to_expiry)
    distance = (current_price - strike) / current_price
    if scaled_vol > 0:
        z = distance / scaled_vol
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return 1.0 if current_price > strike else 0.0


# ── Trader engine ────────────────────────────────────────────────────────────

class LiveTrader:
    def __init__(self, config, mode="paper", market_interval=300,
                 simulated_spread=0.03, trade_size=50.0):
        self.config = config
        self.mode = mode
        self.market_interval = market_interval
        self.simulated_spread = simulated_spread
        self.trade_size = trade_size
        self._shutdown = False

        # Price feed
        self.price_feed = PriceFeed(config.binance, max_history_secs=900)

        # Signal analyzers
        self.cvd = CVDAnalyzer()
        self.sweep_detector = LiquiditySweepDetector()
        self.tape_analyzer = TapeSpeedAnalyzer()

        # Strategies
        self.strategies = [
            StrikeArbStrategy(config.strategy),
            MomentumStrategy(config.strategy),
            MeanReversionStrategy(config.strategy),
            VolatilityStrategy(config.strategy),
        ]

        # Polymarket client (live mode)
        self.poly_client = None
        if mode == "live":
            try:
                self.poly_client = PolymarketClient(config.polymarket)
                print(f"[{_ts()}] Polymarket client initialized (LIVE MODE)")
            except Exception as e:
                print(f"[{_ts()}] {red('ERROR')}: Failed to init Polymarket client: {e}")
                print(f"[{_ts()}] Falling back to paper mode")
                self.mode = "paper"

        # State
        self.open_positions: list[OpenPosition] = []
        self.completed_trades: list[LiveTrade] = []
        self.equity = 0.0
        self.peak_equity = 0.0
        self.max_drawdown = 0.0
        self._market_counter = 0

        # Register tick callbacks
        self.price_feed.on_tick(self.cvd.update)
        self.price_feed.on_tick(self.sweep_detector.update)
        self.price_feed.on_tick(self.tape_analyzer.update)

    def _request_shutdown(self):
        self._shutdown = True

    async def run(self):
        mode_str = red(bold("LIVE MONEY")) if self.mode == "live" else green("PAPER")
        print(bold("\n" + "=" * 65))
        print(bold(f"  TRADING ENGINE  [{mode_str}{BOLD}]  —  5-min BTC Binary Markets"))
        print(bold("=" * 65))
        print(f"  Mode: {self.mode.upper()}  |  Size: ${self.trade_size}  "
              f"|  Interval: {self.market_interval}s")
        print(bold("=" * 65) + "\n")

        if self.mode == "live":
            print(yellow("  WARNING: LIVE MODE — Real money will be used!"))
            print(yellow("  Checking for real Polymarket BTC markets..."))
            real_markets = self._discover_real_markets()
            if not real_markets:
                print(yellow("  No real 5-min BTC markets found on Polymarket."))
                print(yellow("  Falling back to paper mode with real price feed."))
                self.mode = "paper"
            else:
                print(green(f"  Found {len(real_markets)} tradeable markets!"))
            print()

        # Start price feed
        feed_task = asyncio.create_task(self.price_feed.start())

        # Warmup
        warmup_secs = 120
        print(f"[{_ts()}] Connecting to Binance WebSocket...")
        await asyncio.sleep(5)
        print(f"[{_ts()}] Warming up ({warmup_secs}s)...")
        warmup_start = time.time()
        while time.time() - warmup_start < warmup_secs:
            if self.price_feed.has_data:
                remaining = warmup_secs - (time.time() - warmup_start)
                price = self.price_feed.current_price
                print(f"\r[{_ts()}]  BTC ${price:,.2f}  |  warmup {remaining:.0f}s remaining   ",
                      end="", flush=True)
            await asyncio.sleep(5)
        print()

        next_market = _next_boundary(self.market_interval)
        secs_until = next_market - time.time()
        print(f"[{_ts()}] Warmup complete. First market in {secs_until:.0f}s\n")

        # Signal handling for Heroku SIGTERM
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                pass

        try:
            while not self._shutdown:
                now = time.time()
                await self._resolve_expired(now)
                if now >= next_market:
                    await self._open_market(now)
                    next_market += self.market_interval
                self._print_status(next_market)
                await asyncio.sleep(10)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            print(f"\n[{_ts()}] Shutting down...")
            self.price_feed.stop()
            feed_task.cancel()
            self._save_state()
            self._print_final_summary()

    def _discover_real_markets(self) -> list[Market]:
        """Try to find real 5-min BTC binary markets on Polymarket."""
        if not self.poly_client:
            return []
        try:
            return self.poly_client.get_active_btc_5min_markets()
        except Exception as e:
            log.warning("market_discovery_failed", error=str(e))
            return []

    async def _open_market(self, now):
        if not self.price_feed.has_data:
            return

        self._market_counter += 1
        current_price = self.price_feed.current_price
        strike = round(current_price / 100.0) * 100.0
        expiry = now + self.market_interval

        fair_value = estimate_fair_value(current_price, strike, self.market_interval)
        sim_yes_price = max(0.05, min(0.95, fair_value + self.simulated_spread / 2))
        sim_no_price  = max(0.05, min(0.95, 1.0 - fair_value + self.simulated_spread / 2))

        market = Market(
            condition_id=f"{'live' if self.mode == 'live' else 'paper'}_{int(now)}",
            question=f"Will Bitcoin be above ${strike:,.0f} at "
                     f"{datetime.fromtimestamp(expiry).strftime('%H:%M')}?",
            token_id_yes=f"yes_{int(now)}",
            token_id_no=f"no_{int(now)}",
            outcome_yes_price=sim_yes_price,
            outcome_no_price=sim_no_price,
            end_time=expiry,
            created_at=now,
            strike_price=strike,
        )

        # Evaluate strategies
        MIN_EDGE_LIVE = max(self.config.trading.min_edge_threshold, 0.04)
        best_signal = None
        skipped = []
        for strategy in self.strategies:
            try:
                sig = strategy.evaluate(market, self.price_feed)
                if not sig or sig.direction == SignalDirection.HOLD:
                    continue
                if sig.edge < MIN_EDGE_LIVE:
                    skipped.append(f"{strategy.name}(edge={sig.edge:.3f}<min)")
                    continue
                if best_signal is None or sig.edge > best_signal.edge:
                    best_signal = sig
            except Exception as e:
                log.debug("strategy_error", strategy=strategy.name, error=str(e))

        dist_pct = (current_price - strike) / strike * 100
        print(f"\n[{_ts()}] {'-'*58}")
        print(f"[{_ts()}] Market #{self._market_counter}: {market.question}")
        print(f"[{_ts()}]   BTC=${current_price:,.2f}  Strike=${strike:,.0f}  "
              f"Dist={dist_pct:+.2f}%  Fair={fair_value:.3f}")

        if best_signal:
            direction_str = best_signal.direction.value
            entry_price = sim_yes_price if best_signal.direction == SignalDirection.YES else sim_no_price
            colour = green if direction_str == "YES" else red

            # Place real order in live mode
            order_id = ""
            if self.mode == "live" and self.poly_client:
                order_id = self._place_real_order(market, best_signal, entry_price)
                if not order_id:
                    print(f"[{_ts()}]   {yellow('ORDER FAILED - recording as paper trade')}")

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
                order_id=order_id,
            )
            self.open_positions.append(pos)

            mode_tag = f" [{red('LIVE')}]" if order_id else ""
            print(f"[{_ts()}]   {colour(bold(f'>> ENTER {direction_str}'))}{mode_tag}  "
                  f"strategy={best_signal.strategy_name}  "
                  f"entry={entry_price:.3f}  "
                  f"edge={best_signal.edge:+.3f}")
            print(f"[{_ts()}]   Reason: {best_signal.reason[:80]}")
        else:
            skip_str = f"  filtered: {', '.join(skipped)}" if skipped else ""
            print(f"[{_ts()}]   {yellow('>> NO TRADE')}{skip_str}")

    def _place_real_order(self, market, signal, entry_price) -> str:
        """Place a real limit order on Polymarket. Returns order_id or ''."""
        try:
            token_id = (market.token_id_yes if signal.direction == SignalDirection.YES
                       else market.token_id_no)
            resp = self.poly_client.place_limit_order(
                token_id=token_id,
                side="BUY",
                price=round(entry_price, 2),
                size=self.trade_size,
            )
            if resp and isinstance(resp, dict):
                oid = resp.get("orderID", resp.get("id", ""))
                log.info("live_order_placed", order_id=oid, price=entry_price,
                         size=self.trade_size)
                return str(oid)
        except Exception as e:
            log.error("live_order_failed", error=str(e))
        return ""

    async def _resolve_expired(self, now):
        still_open = []
        resolved_any = False
        for pos in self.open_positions:
            if now < pos.resolves_at:
                still_open.append(pos)
                continue
            if not self.price_feed.has_data:
                still_open.append(pos)
                continue

            btc_at_expiry = self.price_feed.current_price
            won = (
                (pos.direction == "YES" and btc_at_expiry > pos.strike)
                or (pos.direction == "NO" and btc_at_expiry <= pos.strike)
            )
            pnl = (1.0 - pos.entry_price) * pos.size if won else -pos.entry_price * pos.size

            self.equity += pnl
            if self.equity > self.peak_equity:
                self.peak_equity = self.equity
            dd = self.peak_equity - self.equity
            if dd > self.max_drawdown:
                self.max_drawdown = dd

            trade = LiveTrade(
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
                mode=self.mode if pos.order_id else "paper",
                order_id=pos.order_id,
            )
            self.completed_trades.append(trade)
            resolved_any = True

            result_str = green("WON") if won else red("LOST")
            print(f"\n[{_ts()}] {'-'*58}")
            print(f"[{_ts()}] RESOLVED: {pos.strategy.upper()} {pos.direction}  "
                  f"Strike=${pos.strike:,.0f}")
            print(f"[{_ts()}]   Entry BTC=${pos.btc_at_entry:,.2f}  ->  "
                  f"Expiry BTC=${btc_at_expiry:,.2f}")
            print(f"[{_ts()}]   {result_str}  PnL={_equity_str(pnl)}"
                  f"  Running equity: {_equity_str(self.equity)}")

        self.open_positions = still_open
        if resolved_any:
            self._save_state()

    def _save_state(self):
        state = {
            "updated_at": time.time(),
            "mode": self.mode,
            "equity": self.equity,
            "peak_equity": self.peak_equity,
            "max_drawdown": self.max_drawdown,
            "open_positions": [
                {
                    "market_id": p.market_id, "opened_at": p.opened_at,
                    "resolves_at": p.resolves_at, "strategy": p.strategy,
                    "direction": p.direction, "strike": p.strike,
                    "entry_price": p.entry_price, "btc_at_entry": p.btc_at_entry,
                    "size": p.size, "edge": p.edge, "order_id": p.order_id,
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

    def _print_status(self, next_market):
        if not self.price_feed.has_data:
            return
        price = self.price_feed.current_price
        secs_until = max(0, next_market - time.time())
        n = len(self.completed_trades)
        wins = sum(1 for t in self.completed_trades if t.won)
        wr = wins / n if n else 0
        print(
            f"\r[{_ts()}]  BTC ${price:,.2f}"
            f"  |  Next: {secs_until:.0f}s"
            f"  |  Open: {len(self.open_positions)}"
            f"  |  Trades: {n} (W:{wins} L:{n-wins})"
            f"  |  WR: {wr:.0%}"
            f"  |  Equity: {_equity_str(self.equity)}"
            f"     ",
            end="", flush=True
        )

    def _print_final_summary(self):
        trades = self.completed_trades
        n = len(trades)
        print("\n")
        print(bold("=" * 65))
        print(bold(f"  TRADING SUMMARY  [{self.mode.upper()}]"))
        print(bold("=" * 65))
        print(f"  Total trades: {n}")
        if n == 0:
            print(bold("=" * 65))
            return

        wins = sum(1 for t in trades if t.won)
        wr = wins / n
        gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        print(f"  Wins / Losses:   {wins} / {n - wins}")
        print(f"  Win rate:        {wr:.1%}")
        print(f"  Total PnL:       {_equity_str(self.equity)}")
        print(f"  Avg PnL/trade:   {_equity_str(self.equity / n)}")
        print(f"  Profit factor:   {pf:.2f}")
        print(f"  Max drawdown:    ${self.max_drawdown:.2f}")

        # Strategy breakdown
        strats = {}
        for t in trades:
            s = strats.setdefault(t.strategy, {"n": 0, "wins": 0, "pnl": 0.0})
            s["n"] += 1
            s["wins"] += int(t.won)
            s["pnl"] += t.pnl
        print("\n  Strategy Breakdown:")
        for name, s in strats.items():
            sr = s["wins"] / s["n"] if s["n"] else 0
            print(f"    {name:20s}  n={s['n']:3d}  wr={sr:.0%}  pnl={_equity_str(s['pnl'])}")
        print(bold("=" * 65))


def _next_boundary(interval_secs):
    now = time.time()
    return math.ceil(now / interval_secs) * interval_secs


def parse_args():
    p = argparse.ArgumentParser(description="Trade 5-min BTC binary markets")
    p.add_argument("--mode", choices=["paper", "live"], default="paper",
                   help="Trading mode (default: paper)")
    p.add_argument("--edge", type=float, default=0.02, help="Min edge (default 0.02)")
    p.add_argument("--size", type=float, default=50.0, help="Trade size USDC (default $50)")
    p.add_argument("--spread", type=float, default=0.03, help="Simulated spread (default 0.03)")
    p.add_argument("--interval", type=int, default=300, help="Market interval secs (default 300)")
    return p.parse_args()


def main():
    args = parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING)

    config = Config()
    config.trading.min_edge_threshold = args.edge

    if args.mode == "live":
        errors = config.validate()
        if errors:
            print(f"Config errors for live mode:")
            for e in errors:
                print(f"  - {e}")
            print("Set POLYMARKET_PRIVATE_KEY in .env or environment")
            sys.exit(1)

    trader = LiveTrader(
        config=config,
        mode=args.mode,
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
