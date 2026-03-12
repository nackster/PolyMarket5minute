"""Momentum strategy: trades in the direction of recent price movement.

Edge thesis: When BTC has strong short-term momentum (measured over 60-120s),
it's more likely to continue in that direction over the next 5 minutes than
the market price implies. This is especially true for strong moves with
high volume confirmation.
"""

import structlog
from src.config import StrategyConfig
from src.polymarket_client import Market
from src.price_feed import PriceFeed
from src.strategies.base import BaseStrategy, Signal, SignalDirection

log = structlog.get_logger()


class MomentumStrategy(BaseStrategy):

    def __init__(self, config: StrategyConfig):
        self.config = config

    @property
    def name(self) -> str:
        return "momentum"

    def evaluate(self, market: Market, price_feed: PriceFeed) -> Signal | None:
        if not price_feed.has_data:
            return None

        lookback = self.config.momentum_lookback_secs
        current_price = price_feed.current_price
        strike = market.strike_price

        if strike == 0 or current_price == 0:
            return None

        # Core momentum signals
        price_change_pct = price_feed.get_price_change_pct(lookback)
        if price_change_pct is None:
            return None

        # Medium-term trend filter (5-minute window): avoid fighting strong trends.
        # If BTC has moved strongly in one direction, bias probability accordingly.
        trend_change = price_feed.get_price_change_pct(300)  # 5-min trend
        trend_bias = 0.0
        if trend_change is not None:
            if trend_change > 0.003:     # +0.3% in 5min = strong uptrend
                trend_bias = +0.08
            elif trend_change < -0.003:  # -0.3% in 5min = strong downtrend
                trend_bias = -0.08
            elif trend_change > 0.001:
                trend_bias = +0.03
            elif trend_change < -0.001:
                trend_bias = -0.03

        buy_sell_ratio = price_feed.get_buy_sell_ratio(lookback)
        vwap = price_feed.get_vwap(lookback)

        # Distance from current price to strike as a percentage
        distance_pct = (current_price - strike) / strike

        # Base probability estimate: how likely is BTC to be above strike?
        # Start with distance-based estimate
        if abs(distance_pct) > 0.005:
            # Far from strike - strong directional bias
            base_prob = 0.85 if distance_pct > 0 else 0.15
        elif abs(distance_pct) > 0.002:
            # Moderate distance
            base_prob = 0.70 if distance_pct > 0 else 0.30
        else:
            # Very close to strike - momentum matters most here
            base_prob = 0.50

        # Minimum momentum threshold: very small moves are noise, not signal.
        # 0.03% over 120s = ~$20 on $70k BTC — below this is random drift.
        MIN_MOMENTUM = 0.0003  # 0.03%
        if abs(price_change_pct) < MIN_MOMENTUM:
            return None

        # Momentum adjustment: shift probability based on price movement
        momentum_shift = min(0.15, max(-0.15, price_change_pct * 100))

        # Volume confirmation: stronger signal when buy/sell is lopsided
        volume_multiplier = 1.0
        if buy_sell_ratio is not None:
            if buy_sell_ratio > 1.5:
                volume_multiplier = 1.3  # Strong buying
            elif buy_sell_ratio < 0.67:
                volume_multiplier = 1.3  # Strong selling (boost magnitude)
                momentum_shift *= -1 if momentum_shift > 0 else 1  # Align with selling

        # BSR conflict filter: only block on extreme volume disagreement.
        # BSR > 8 = 8x more buying than selling; if our signal is bearish, skip.
        # BSR < 0.15 = 6x more selling than buying; if our signal is bullish, skip.
        if buy_sell_ratio is not None:
            if buy_sell_ratio > 8.0 and price_change_pct < 0:
                return None  # Extreme buying vs bearish momentum — skip
            if buy_sell_ratio < 0.15 and price_change_pct > 0:
                return None  # Extreme selling vs bullish momentum — skip

        # VWAP confirmation: if price is above VWAP, bullish signal
        vwap_shift = 0.0
        if vwap is not None and vwap > 0:
            vwap_distance = (current_price - vwap) / vwap
            vwap_shift = min(0.05, max(-0.05, vwap_distance * 50))

        # Final probability estimate (trend bias aligns us with medium-term direction)
        our_prob_yes = base_prob + (momentum_shift * volume_multiplier) + vwap_shift + trend_bias
        our_prob_yes = max(0.05, min(0.95, our_prob_yes))  # Clamp

        # Calculate edge against the market
        market_prob_yes = market.implied_prob_yes
        market_prob_no = market.implied_prob_no

        edge_yes = our_prob_yes - market_prob_yes
        edge_no = (1 - our_prob_yes) - market_prob_no

        # Pick the side with the bigger edge
        if edge_yes > edge_no and edge_yes > 0:
            direction = SignalDirection.YES
            confidence = our_prob_yes
            edge = edge_yes
        elif edge_no > 0:
            direction = SignalDirection.NO
            confidence = 1 - our_prob_yes
            edge = edge_no
        else:
            return None  # No edge

        reason = (
            f"momentum={price_change_pct:+.4%} over {lookback}s, "
            f"distance_to_strike={distance_pct:+.4%}, "
            f"buy_sell_ratio={buy_sell_ratio:.2f}, "
            f"our_prob_yes={our_prob_yes:.3f} vs market={market_prob_yes:.3f}"
        )

        log.debug("momentum_signal", direction=direction.value, edge=edge, reason=reason)

        return Signal(
            direction=direction,
            confidence=confidence,
            edge=edge,
            strategy_name=self.name,
            reason=reason,
        )
