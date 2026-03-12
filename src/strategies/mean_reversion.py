"""Mean reversion strategy: fades overextended moves.

Edge thesis: After a sharp, rapid BTC move (especially on low volume),
price tends to revert. If the market is pricing in continuation of the
move, we can profit by fading it.
"""

import structlog
from src.config import StrategyConfig
from src.polymarket_client import Market
from src.price_feed import PriceFeed
from src.strategies.base import BaseStrategy, Signal, SignalDirection

log = structlog.get_logger()


class MeanReversionStrategy(BaseStrategy):

    def __init__(self, config: StrategyConfig):
        self.config = config

    @property
    def name(self) -> str:
        return "mean_reversion"

    def evaluate(self, market: Market, price_feed: PriceFeed) -> Signal | None:
        if not price_feed.has_data:
            return None

        current_price = price_feed.current_price
        strike = market.strike_price
        if strike == 0 or current_price == 0:
            return None

        # Check for overextension: compare short-term vs medium-term movement
        short_change = price_feed.get_price_change_pct(30)   # last 30s
        medium_change = price_feed.get_price_change_pct(120)  # last 2min

        if short_change is None or medium_change is None:
            return None

        volatility = price_feed.get_volatility(120)
        if volatility is None or volatility == 0:
            return None

        # Z-score: how many standard deviations is the recent move?
        z_score = short_change / volatility if volatility > 0 else 0

        # We want overextended moves (1.5 < |z| < 4.0).
        # |z| > 4 means BTC is trending hard — mean reversion fails badly in trends.
        # Live data confirms: |z| 5-10 wins only 10% of the time.
        if abs(z_score) < 1.5:
            return None  # Move isn't extreme enough
        if abs(z_score) > 4.0:
            return None  # Trending market — don't fight the trend

        # Trend regime filter: if BTC has moved strongly over 5 minutes, it's
        # trending — mean reversion is a loser in trending markets. Skip.
        trend_5min = price_feed.get_price_change_pct(300)
        if trend_5min is not None and abs(trend_5min) > 0.0015:
            return None  # >0.15% trend in 5 min = trending, not ranging

        # Volume check: mean reversion is stronger on LOW volume spikes
        buy_sell_ratio = price_feed.get_buy_sell_ratio(30)
        volume_30s = price_feed.get_volume_since(30)
        volume_120s = price_feed.get_volume_since(120)

        # Volume spike ratio (is the last 30s volume abnormally high/low?)
        vol_ratio = (volume_30s * 4) / volume_120s if volume_120s > 0 else 1.0

        # Low volume spike = better mean reversion signal
        reversion_strength = abs(z_score) / 3.0  # Normalize
        if vol_ratio < 1.5:
            reversion_strength *= 1.2  # Low volume = stronger reversion

        # Distance to strike
        distance_pct = (current_price - strike) / strike

        # If price spiked UP and is now above strike, reversion favors NO
        # If price spiked DOWN and is now below strike, reversion favors YES
        if short_change > 0 and z_score > 2.0:
            # Price spiked up -> expect reversion down
            if distance_pct > 0:
                # Above strike but expecting to fall: could cross below
                our_prob_yes = 0.50 - (reversion_strength * 0.15)
            else:
                # Below strike and spiked up: less likely to reach strike
                our_prob_yes = 0.35
        elif short_change < 0 and z_score < -2.0:
            # Price spiked down -> expect reversion up
            if distance_pct < 0:
                # Below strike but expecting to bounce: could cross above
                our_prob_yes = 0.50 + (reversion_strength * 0.15)
            else:
                # Above strike and spiked down: might still stay above
                our_prob_yes = 0.65
        else:
            return None

        our_prob_yes = max(0.10, min(0.90, our_prob_yes))

        # Calculate edge
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
            f"z_score={z_score:.2f}, short_move={short_change:+.4%}, "
            f"vol_ratio={vol_ratio:.2f}, reversion_strength={reversion_strength:.2f}"
        )

        log.debug("mean_reversion_signal", direction=direction.value, edge=edge, reason=reason)

        return Signal(
            direction=direction,
            confidence=confidence,
            edge=edge,
            strategy_name=self.name,
            reason=reason,
        )
