"""Volatility strategy: exploits implied vs realized volatility mismatch.

Edge thesis: When the Polymarket price implies a certain probability of
BTC crossing the strike, we can compare this to our own probability
estimate based on realized volatility. If the market is over/under-pricing
the probability of a move, we trade the mispricing.
"""

import math
import structlog
from src.config import StrategyConfig
from src.polymarket_client import Market
from src.price_feed import PriceFeed
from src.strategies.base import BaseStrategy, Signal, SignalDirection

log = structlog.get_logger()


class VolatilityStrategy(BaseStrategy):

    def __init__(self, config: StrategyConfig):
        self.config = config

    @property
    def name(self) -> str:
        return "volatility"

    def evaluate(self, market: Market, price_feed: PriceFeed) -> Signal | None:
        if not price_feed.has_data:
            return None

        current_price = price_feed.current_price
        strike = market.strike_price
        secs_left = market.seconds_until_resolution

        if strike == 0 or current_price == 0 or secs_left <= 0:
            return None

        # Get realized volatility from recent data
        vol = price_feed.get_volatility(120)
        if vol is None or vol <= 0:
            return None

        # Scale volatility to the remaining time window
        # vol is per-sample (~1s intervals), scale to secs_left
        # Using sqrt(T) scaling for random walk
        scaled_vol = vol * math.sqrt(secs_left)

        # Calculate probability that price will be above strike at expiry
        # Using a simple normal distribution approximation
        distance = (current_price - strike) / current_price
        if scaled_vol > 0:
            z = distance / scaled_vol
            our_prob_yes = self._normal_cdf(z)
        else:
            our_prob_yes = 1.0 if current_price > strike else 0.0

        # Require a meaningful directional position (|z| > 0.5).
        # Live data: all 5 volatility trades had |z| < 0.4 and lost every one.
        # Near-zero z means BTC is exactly where vol model expects — no edge.
        if abs(z) < 0.5:
            return None

        our_prob_yes = max(0.05, min(0.95, our_prob_yes))

        # Compare to market implied probability
        market_prob_yes = market.implied_prob_yes
        edge_yes = our_prob_yes - market_prob_yes
        edge_no = (1 - our_prob_yes) - market.implied_prob_no

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

        reason = (
            f"realized_vol={vol:.6f}, scaled_vol={scaled_vol:.6f}, "
            f"z_score={z:.2f}, our_prob={our_prob_yes:.3f} vs market={market_prob_yes:.3f}, "
            f"secs_left={secs_left:.0f}"
        )

        log.debug("volatility_signal", direction=direction.value, edge=edge, reason=reason)

        return Signal(
            direction=direction,
            confidence=confidence,
            edge=edge,
            strategy_name=self.name,
            reason=reason,
        )

    @staticmethod
    def _normal_cdf(x: float) -> float:
        """Approximate the standard normal CDF using the error function."""
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))
