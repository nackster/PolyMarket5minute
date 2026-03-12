"""Main bot orchestrator: ties together price feed, strategies, and execution."""

import asyncio
import time
import structlog

from src.config import Config
from src.polymarket_client import PolymarketClient, Market
from src.price_feed import PriceFeed, Tick
from src.risk_manager import RiskManager
from src.signals import (
    CVDAnalyzer,
    LiquiditySweepDetector,
    TapeSpeedAnalyzer,
    VWAPAnalyzer,
    MultiExchangeAnalyzer,
)
from src.strategies.base import BaseStrategy, Signal, SignalDirection
from src.strategies.momentum import MomentumStrategy
from src.strategies.mean_reversion import MeanReversionStrategy
from src.strategies.volatility import VolatilityStrategy
from src.strategies.composite import CompositeStrategy

log = structlog.get_logger()


class TradingBot:
    """Main trading bot that orchestrates all components."""

    def __init__(self, config: Config):
        self.config = config
        self.polymarket = PolymarketClient(config.polymarket)
        self.price_feed = PriceFeed(config.binance)
        self.risk_manager = RiskManager(config.trading)

        # Advanced signal analyzers
        self.cvd = CVDAnalyzer()
        self.sweep_detector = LiquiditySweepDetector()
        self.tape_analyzer = TapeSpeedAnalyzer()
        self.vwap_analyzer = VWAPAnalyzer(std_devs=config.strategy.bb_std_dev)
        self.exchange_analyzer = MultiExchangeAnalyzer()

        # Register tick callbacks for signal analyzers
        self.price_feed.on_tick(self._on_tick)

        self.strategies: list[BaseStrategy] = self._init_strategies()
        self._running = False
        self._scan_interval = 15  # seconds between market scans

    def _on_tick(self, tick: Tick):
        """Feed each tick to all signal analyzers."""
        self.cvd.update(tick)
        self.sweep_detector.update(tick)
        self.tape_analyzer.update(tick)

    def _init_strategies(self) -> list[BaseStrategy]:
        """Initialize all trading strategies."""
        return [
            MomentumStrategy(self.config.strategy),
            MeanReversionStrategy(self.config.strategy),
            VolatilityStrategy(self.config.strategy),
            CompositeStrategy(
                self.config.strategy,
                cvd=self.cvd,
                sweep_detector=self.sweep_detector,
                tape_analyzer=self.tape_analyzer,
                vwap_analyzer=self.vwap_analyzer,
                exchange_analyzer=self.exchange_analyzer,
            ),
        ]

    async def start(self):
        """Start the trading bot."""
        log.info("bot_starting", strategies=[s.name for s in self.strategies])
        self._running = True

        # Start the price feed and trading loop concurrently
        await asyncio.gather(
            self.price_feed.start(),
            self._trading_loop(),
        )

    async def stop(self):
        """Gracefully stop the bot."""
        log.info("bot_stopping")
        self._running = False
        self.price_feed.stop()

    async def _trading_loop(self):
        """Main trading loop: scan markets, evaluate strategies, execute trades."""
        # Wait for price feed to have data
        log.info("waiting_for_price_data")
        while self._running and not self.price_feed.has_data:
            await asyncio.sleep(1)

        log.info("price_data_ready", price=self.price_feed.current_price)

        while self._running:
            try:
                await self._scan_and_trade()
            except Exception as e:
                log.error("trading_loop_error", error=str(e))

            await asyncio.sleep(self._scan_interval)

    async def _scan_and_trade(self):
        """Single iteration: fetch markets, run strategies, execute trades."""
        # Fetch active 5-minute BTC markets
        markets = self.polymarket.get_active_btc_5min_markets()
        if not markets:
            log.debug("no_active_markets")
            return

        log.info(
            "scanning_markets",
            count=len(markets),
            btc_price=self.price_feed.current_price,
            open_positions=len(self.risk_manager.open_positions),
        )

        for market in markets:
            # Skip markets that are about to resolve
            if market.seconds_until_resolution < 30:
                continue

            # Run all strategies and collect signals
            signals = self._evaluate_strategies(market)
            if not signals:
                continue

            # Pick the best signal (highest edge)
            best_signal = max(signals, key=lambda s: s.edge)

            log.info(
                "best_signal",
                market=market.question[:80],
                strategy=best_signal.strategy_name,
                direction=best_signal.direction.value,
                edge=f"{best_signal.edge:.4f}",
                confidence=f"{best_signal.confidence:.3f}",
            )

            # Run through risk management
            decision = self.risk_manager.evaluate_trade(best_signal, market)

            if not decision.should_trade:
                log.debug("trade_rejected", reason=decision.reason)
                continue

            # Execute the trade
            log.info(
                "executing_trade",
                market=market.question[:80],
                side=decision.signal.direction.value,
                size=decision.size,
                token_id=decision.token_id[:16] + "...",
            )

            result = self.polymarket.place_limit_order(
                token_id=decision.token_id,
                side=decision.side,
                price=self._calculate_entry_price(best_signal, market),
                size=decision.size,
            )

            if result:
                self.risk_manager.record_trade(decision, market)
                log.info("trade_executed", result=result)
            else:
                log.warning("trade_execution_failed")

    def _evaluate_strategies(self, market: Market) -> list[Signal]:
        """Run all strategies on a market and collect non-None signals."""
        signals = []
        for strategy in self.strategies:
            try:
                signal = strategy.evaluate(market, self.price_feed)
                if signal and signal.direction != SignalDirection.HOLD:
                    signals.append(signal)
            except Exception as e:
                log.error("strategy_error", strategy=strategy.name, error=str(e))
        return signals

    def _calculate_entry_price(self, signal: Signal, market: Market) -> float:
        """Calculate the limit order price for entry.

        We try to get a slightly better price than the current market
        to improve our edge.
        """
        if signal.direction == SignalDirection.YES:
            # Buying YES: bid slightly below market
            market_price = market.implied_prob_yes
            entry = market_price - 0.01  # 1 cent better
        else:
            # Buying NO: bid slightly below market
            market_price = market.implied_prob_no
            entry = market_price - 0.01

        # Clamp to valid range
        return max(0.01, min(0.99, round(entry, 2)))
