"""Composite strategy: combines all advanced signals for highest edge.

This is the "full stack" strategy that uses every signal source:
CVD divergence, liquidity sweeps, order book imbalance, tape speed,
VWAP deviation, and multi-exchange spread.

It produces higher-confidence signals by requiring multiple confirming
signals and down-weighting when signals conflict.

This is where the real edge lives.
"""

import structlog
from src.config import StrategyConfig
from src.polymarket_client import Market
from src.price_feed import PriceFeed
from src.signals import (
    CVDAnalyzer,
    LiquiditySweepDetector,
    TapeSpeedAnalyzer,
    VWAPAnalyzer,
    SignalAggregator,
    BookImbalance,
    ExchangeSpread,
    MultiExchangeAnalyzer,
)
from src.strategies.base import BaseStrategy, Signal, SignalDirection

log = structlog.get_logger()


class CompositeStrategy(BaseStrategy):
    """Combines all signal sources into a single high-confidence strategy."""

    def __init__(
        self,
        config: StrategyConfig,
        cvd: CVDAnalyzer,
        sweep_detector: LiquiditySweepDetector,
        tape_analyzer: TapeSpeedAnalyzer,
        vwap_analyzer: VWAPAnalyzer,
        exchange_analyzer: MultiExchangeAnalyzer = None,
    ):
        self.config = config
        self.cvd = cvd
        self.sweep_detector = sweep_detector
        self.tape_analyzer = tape_analyzer
        self.vwap_analyzer = vwap_analyzer
        self.exchange_analyzer = exchange_analyzer
        self.aggregator = SignalAggregator()

    @property
    def name(self) -> str:
        return "composite"

    def evaluate(self, market: Market, price_feed: PriceFeed) -> Signal | None:
        if not price_feed.has_data:
            return None

        current_price = price_feed.current_price
        strike = market.strike_price
        if strike == 0 or current_price == 0:
            return None

        # Gather all signals
        cvd_reading = self.cvd.analyze(price_feed, window_secs=60)
        sweeps = self.sweep_detector.get_recent_sweeps(seconds_ago=120)
        tape = self.tape_analyzer.analyze()
        vwap = self.vwap_analyzer.analyze(price_feed, window_secs=120)

        # Use neutral defaults for order book and exchange spread
        # (these require separate data feeds in live mode)
        book = BookImbalance(0, 0, 1.0, "neutral", 0)
        spread = ExchangeSpread(current_price, current_price, 0, "neutral", 0)

        if self.exchange_analyzer:
            spread = self.exchange_analyzer.analyze()

        # Get composite signal
        composite = self.aggregator.aggregate(cvd_reading, sweeps, book, tape, vwap, spread)

        if composite.direction == "neutral":
            return None

        # Map composite direction to market-relative direction
        distance_pct = (current_price - strike) / strike

        # Base probability from distance to strike
        if abs(distance_pct) > 0.005:
            base_prob = 0.85 if distance_pct > 0 else 0.15
        elif abs(distance_pct) > 0.002:
            base_prob = 0.70 if distance_pct > 0 else 0.30
        else:
            base_prob = 0.50

        # 5-minute trend filter: align with medium-term direction
        trend_change = price_feed.get_price_change_pct(300)
        trend_bias = 0.0
        if trend_change is not None:
            if trend_change > 0.003:
                trend_bias = +0.07
            elif trend_change < -0.003:
                trend_bias = -0.07
            elif trend_change > 0.001:
                trend_bias = +0.03
            elif trend_change < -0.001:
                trend_bias = -0.03

        # Adjust based on composite signal
        adjustment = composite.confidence * 0.20  # Max 20% shift
        if composite.direction == "bullish":
            our_prob_yes = base_prob + adjustment + trend_bias
        else:
            our_prob_yes = base_prob - adjustment + trend_bias

        # Extra boost when tape is accelerating (conviction amplifier)
        tape_data = composite.components.get("tape_speed", {})
        if tape_data.get("accelerating", False):
            # Amplify the signal slightly when market is active
            deviation_from_50 = our_prob_yes - 0.5
            our_prob_yes = 0.5 + deviation_from_50 * 1.1

        # Penalty for conflicting signals (reduce confidence)
        if composite.conflicting_signals >= 2:
            our_prob_yes = 0.5 + (our_prob_yes - 0.5) * 0.7

        our_prob_yes = max(0.05, min(0.95, our_prob_yes))

        # Calculate edge
        market_prob_yes = market.implied_prob_yes
        market_prob_no = market.implied_prob_no
        edge_yes = our_prob_yes - market_prob_yes
        edge_no = (1 - our_prob_yes) - market_prob_no

        if edge_yes > edge_no and edge_yes > 0:
            direction = SignalDirection.YES
            confidence = our_prob_yes
            edge = edge_yes
        elif edge_no > 0:
            direction = SignalDirection.NO
            confidence = 1 - our_prob_yes
            edge = edge_no
        else:
            return None

        components_str = ", ".join(
            f"{k}={v}" for k, v in composite.components.items()
        )
        reason = (
            f"composite[{composite.direction}] conf={composite.confidence:.3f}, "
            f"driver={composite.primary_driver}, conflicts={composite.conflicting_signals}, "
            f"our_prob_yes={our_prob_yes:.3f} vs market={market_prob_yes:.3f}"
        )

        log.debug("composite_signal", direction=direction.value, edge=edge)

        return Signal(
            direction=direction,
            confidence=confidence,
            edge=edge,
            strategy_name=self.name,
            reason=reason,
        )
