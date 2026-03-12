"""Backtesting engine for 5-minute BTC binary prediction strategies.

Simulates Polymarket-style binary markets using historical Binance data.
Every 5 minutes, it creates a synthetic market: "Will BTC be above $X
at time T+5min?" where X is the current price (or an offset).

The backtester replays historical ticks through the price feed and
signal analyzers, then checks if the strategy's prediction was correct.
"""

import math
import time as _time
from collections import deque
from dataclasses import dataclass, field

import structlog

from src.config import Config, StrategyConfig, TradingConfig
from src.price_feed import PriceFeed, Tick
from src.polymarket_client import Market
from src.risk_manager import RiskManager
from src.signals import (
    CVDAnalyzer,
    LiquiditySweepDetector,
    OrderBookAnalyzer,
    TapeSpeedAnalyzer,
    VWAPAnalyzer,
    SignalAggregator,
    BookImbalance,
    ExchangeSpread,
)
from src.strategies.base import BaseStrategy, Signal, SignalDirection
from src.strategies.momentum import MomentumStrategy
from src.strategies.mean_reversion import MeanReversionStrategy
from src.strategies.volatility import VolatilityStrategy
from src.strategies.composite import CompositeStrategy

log = structlog.get_logger()


@dataclass
class BacktestTrade:
    """Record of a single backtest trade."""
    timestamp: float
    strategy: str
    direction: str       # "YES" or "NO"
    strike_price: float
    entry_price: float   # Simulated market price we paid
    btc_price_at_entry: float
    btc_price_at_expiry: float
    size: float
    edge: float
    confidence: float
    won: bool
    pnl: float
    reason: str


@dataclass
class BacktestResult:
    """Complete backtest results."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    avg_edge: float = 0.0
    avg_pnl_per_trade: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)
    strategy_breakdown: dict = field(default_factory=dict)
    equity_curve: list[float] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "=" * 60,
            "  BACKTEST RESULTS",
            "=" * 60,
            f"  Total trades:      {self.total_trades}",
            f"  Win rate:          {self.win_rate:.1%}",
            f"  Total PnL:         ${self.total_pnl:.2f}",
            f"  Avg PnL/trade:     ${self.avg_pnl_per_trade:.2f}",
            f"  Avg edge:          {self.avg_edge:.4f}",
            f"  Profit factor:     {self.profit_factor:.2f}",
            f"  Sharpe ratio:      {self.sharpe_ratio:.2f}",
            f"  Max drawdown:      ${self.max_drawdown:.2f}",
            "",
            "  Strategy Breakdown:",
        ]
        for name, stats in self.strategy_breakdown.items():
            lines.append(
                f"    {name:20s}  trades={stats['trades']:4d}  "
                f"win_rate={stats['win_rate']:.1%}  "
                f"pnl=${stats['pnl']:.2f}"
            )
        lines.append("=" * 60)
        return "\n".join(lines)


class SimulatedPriceFeed(PriceFeed):
    """PriceFeed that accepts replayed historical ticks instead of WebSocket."""

    def __init__(self, max_history_secs: int = 600):
        from src.config import BinanceConfig
        super().__init__(BinanceConfig(ws_url=""), max_history_secs)

    def inject_tick(self, tick: Tick):
        """Manually inject a tick (used during backtesting)."""
        self._current_price = tick.price
        self._current_time = tick.timestamp  # keep time window relative to sim time
        self.ticks.append(tick)
        self._prune_old_ticks()
        for cb in self._callbacks:
            try:
                cb(tick)
            except Exception:
                pass


class Backtester:
    """Replays historical data and simulates 5-minute binary markets."""

    def __init__(
        self,
        config: Config = None,
        market_interval_secs: int = 300,  # New market every 5 minutes
        strike_offset_pct: float = 0.0,   # 0 = at-the-money, +/- for offset
        simulated_spread: float = 0.03,   # Simulated bid-ask spread (market inefficiency)
    ):
        self.config = config or Config()
        self.market_interval = market_interval_secs
        self.strike_offset_pct = strike_offset_pct
        self.simulated_spread = simulated_spread

        # Components
        self.price_feed = SimulatedPriceFeed()
        self.cvd = CVDAnalyzer()
        self.sweep_detector = LiquiditySweepDetector()
        self.tape_analyzer = TapeSpeedAnalyzer()
        self.vwap_analyzer = VWAPAnalyzer(std_devs=self.config.strategy.bb_std_dev)
        self.signal_aggregator = SignalAggregator()

        self.strategies: list[BaseStrategy] = [
            MomentumStrategy(self.config.strategy),
            MeanReversionStrategy(self.config.strategy),
            VolatilityStrategy(self.config.strategy),
            # CompositeStrategy: disabled until CVD/sweep signals are validated
            # on real Polymarket data. Synthetic OHLC ticks produce noisy CVD.
            # CompositeStrategy(
            #     self.config.strategy,
            #     cvd=self.cvd,
            #     sweep_detector=self.sweep_detector,
            #     tape_analyzer=self.tape_analyzer,
            #     vwap_analyzer=self.vwap_analyzer,
            # ),
        ]

    def run(self, trades: list[dict], warmup_secs: int = 120) -> BacktestResult:
        """Run backtest on historical trade data.

        Args:
            trades: List of trade dicts from data_fetcher with keys:
                    price, qty, timestamp, is_buyer_maker
            warmup_secs: Seconds of data to feed before starting to trade.

        Returns:
            BacktestResult with full performance analysis.
        """
        if not trades:
            log.error("no_trade_data")
            return BacktestResult()

        log.info("backtest_starting", trades=len(trades), warmup_secs=warmup_secs)

        result = BacktestResult()
        equity = 0.0
        peak_equity = 0.0
        max_dd = 0.0

        start_time = trades[0]["timestamp"]
        warmup_end = start_time + warmup_secs
        next_market_time = warmup_end + self.market_interval

        # Build index of prices by timestamp for quick expiry lookup
        price_index = self._build_price_index(trades)

        # Track per-strategy stats
        strat_stats = {}
        for s in self.strategies:
            strat_stats[s.name] = {"trades": 0, "wins": 0, "pnl": 0.0}

        tick_count = 0
        for trade_data in trades:
            tick = Tick(
                price=trade_data["price"],
                volume=trade_data["qty"],
                timestamp=trade_data["timestamp"],
                is_buyer_maker=trade_data["is_buyer_maker"],
            )

            # Feed tick to all analyzers
            self.price_feed.inject_tick(tick)
            self.cvd.update(tick)
            self.sweep_detector.update(tick)
            self.tape_analyzer.update(tick)
            tick_count += 1

            # Skip warmup period
            if tick.timestamp < warmup_end:
                continue

            # Create a new market every N seconds
            if tick.timestamp >= next_market_time:
                current_price = tick.price
                # Round to nearest $100 increment (like real Polymarket strikes)
                # then apply optional offset. This creates a realistic mix of
                # ITM, ATM, and OTM markets rather than always exactly ATM.
                strike_granularity = 100.0
                rounded_strike = round(current_price / strike_granularity) * strike_granularity
                strike = rounded_strike * (1 + self.strike_offset_pct)
                expiry_time = tick.timestamp + self.market_interval

                # Look up actual BTC price at expiry
                expiry_price = self._get_price_at_time(price_index, expiry_time)
                if expiry_price is None:
                    next_market_time += self.market_interval
                    continue

                # Simulate market prices (add some noise/spread)
                actual_prob_yes = 1.0 if expiry_price > strike else 0.0
                # Simulated market price: fair value with some spread
                fair_value = self._estimate_fair_value(current_price, strike, expiry_time - tick.timestamp)
                sim_yes_price = max(0.05, min(0.95, fair_value + self.simulated_spread / 2))
                sim_no_price = max(0.05, min(0.95, 1 - fair_value + self.simulated_spread / 2))

                market = Market(
                    condition_id=f"sim_{int(tick.timestamp)}",
                    question=f"Will Bitcoin be above ${strike:,.2f}?",
                    token_id_yes=f"yes_{int(tick.timestamp)}",
                    token_id_no=f"no_{int(tick.timestamp)}",
                    outcome_yes_price=sim_yes_price,
                    outcome_no_price=sim_no_price,
                    end_time=expiry_time,
                    created_at=tick.timestamp,  # Fix: use simulated time so seconds_until_resolution is correct
                    strike_price=strike,
                )

                # Evaluate all strategies.
                # Filters (backed by live trade data analysis):
                #   • edge 2%-30%: >30% usually = trending-market mean-reversion trap
                #   • entry price ≤ 0.60: paying >60c requires near-certainty we don't have
                MAX_EDGE        = 0.30
                MAX_ENTRY_PRICE = 0.60

                best_signal = None
                for strategy in self.strategies:
                    try:
                        signal = strategy.evaluate(market, self.price_feed)
                        if not signal or signal.direction == SignalDirection.HOLD:
                            continue
                        entry = sim_yes_price if signal.direction == SignalDirection.YES else sim_no_price
                        if signal.edge < self.config.trading.min_edge_threshold:
                            continue
                        if signal.edge > MAX_EDGE:
                            continue
                        if entry > MAX_ENTRY_PRICE:
                            continue
                        if best_signal is None or signal.edge > best_signal.edge:
                            best_signal = signal
                    except Exception:
                        continue

                if best_signal:
                    # Simulate trade execution
                    won = (
                        (best_signal.direction == SignalDirection.YES and expiry_price > strike)
                        or (best_signal.direction == SignalDirection.NO and expiry_price <= strike)
                    )

                    entry_price = sim_yes_price if best_signal.direction == SignalDirection.YES else sim_no_price
                    size = min(self.config.trading.max_position_size, 10.0)

                    # PnL: if won, we get (1 - entry_price) * size; if lost, we lose entry_price * size
                    if won:
                        pnl = (1 - entry_price) * size
                    else:
                        pnl = -entry_price * size

                    bt_trade = BacktestTrade(
                        timestamp=tick.timestamp,
                        strategy=best_signal.strategy_name,
                        direction=best_signal.direction.value,
                        strike_price=strike,
                        entry_price=entry_price,
                        btc_price_at_entry=current_price,
                        btc_price_at_expiry=expiry_price,
                        size=size,
                        edge=best_signal.edge,
                        confidence=best_signal.confidence,
                        won=won,
                        pnl=pnl,
                        reason=best_signal.reason,
                    )

                    result.trades.append(bt_trade)
                    equity += pnl
                    result.equity_curve.append(equity)

                    # Track peaks and drawdowns
                    if equity > peak_equity:
                        peak_equity = equity
                    dd = peak_equity - equity
                    if dd > max_dd:
                        max_dd = dd

                    # Per-strategy tracking
                    strat_stats[best_signal.strategy_name]["trades"] += 1
                    strat_stats[best_signal.strategy_name]["pnl"] += pnl
                    if won:
                        strat_stats[best_signal.strategy_name]["wins"] += 1

                next_market_time += self.market_interval

        # Compile results
        result.total_trades = len(result.trades)
        result.winning_trades = sum(1 for t in result.trades if t.won)
        result.losing_trades = result.total_trades - result.winning_trades
        result.total_pnl = equity
        result.max_drawdown = max_dd

        if result.total_trades > 0:
            result.win_rate = result.winning_trades / result.total_trades
            result.avg_edge = sum(t.edge for t in result.trades) / result.total_trades
            result.avg_pnl_per_trade = equity / result.total_trades

            # Profit factor
            gross_profit = sum(t.pnl for t in result.trades if t.pnl > 0)
            gross_loss = abs(sum(t.pnl for t in result.trades if t.pnl < 0))
            result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

            # Sharpe ratio (annualized, assuming 5-min intervals)
            pnls = [t.pnl for t in result.trades]
            if len(pnls) > 1:
                mean_pnl = sum(pnls) / len(pnls)
                var_pnl = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
                std_pnl = math.sqrt(var_pnl) if var_pnl > 0 else 0.001
                # ~105,120 five-minute intervals per year
                result.sharpe_ratio = (mean_pnl / std_pnl) * math.sqrt(105120)

        # Strategy breakdown
        for name, stats in strat_stats.items():
            result.strategy_breakdown[name] = {
                "trades": stats["trades"],
                "wins": stats["wins"],
                "win_rate": stats["wins"] / stats["trades"] if stats["trades"] > 0 else 0,
                "pnl": stats["pnl"],
            }

        log.info("backtest_complete", total_trades=result.total_trades, pnl=result.total_pnl)
        return result

    def _build_price_index(self, trades: list[dict]) -> list[tuple[float, float]]:
        """Build a sorted (timestamp, price) index for quick lookup."""
        return [(t["timestamp"], t["price"]) for t in trades]

    def _get_price_at_time(
        self, price_index: list[tuple[float, float]], target_time: float
    ) -> float | None:
        """Binary search for the price closest to target_time."""
        if not price_index:
            return None

        lo, hi = 0, len(price_index) - 1

        # If target is beyond our data, return None
        if target_time > price_index[-1][0]:
            return None

        while lo < hi:
            mid = (lo + hi) // 2
            if price_index[mid][0] < target_time:
                lo = mid + 1
            else:
                hi = mid

        return price_index[lo][1]

    def _estimate_fair_value(
        self, current_price: float, strike: float, secs_to_expiry: float
    ) -> float:
        """Estimate the 'fair' probability of YES using a simple model.

        This simulates what a reasonably efficient market would price,
        so we can test if our strategies find edge above this.
        """
        if current_price == 0 or secs_to_expiry <= 0:
            return 0.5

        # Use ~0.1% per 5 minutes as baseline BTC volatility
        vol_per_sec = 0.001 / math.sqrt(300)
        scaled_vol = vol_per_sec * math.sqrt(secs_to_expiry)

        distance = (current_price - strike) / current_price
        if scaled_vol > 0:
            z = distance / scaled_vol
            return 0.5 * (1 + math.erf(z / math.sqrt(2)))
        else:
            return 1.0 if current_price > strike else 0.0
