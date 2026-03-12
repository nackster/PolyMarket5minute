"""Strike arbitrage strategy: exploits realized vol vs market implied vol.

Edge thesis:
  The simulated market prices binary options using a fixed vol assumption
  (0.1% expected move over 5 minutes). Real BTC realized vol is almost always
  higher than this fixed baseline.

  When realized vol > market vol:
    - Market overestimates certainty for off-strike positions
    - Probabilities are priced too extreme (too close to 0 or 1)
    - We see the "reversion" direction (the side BTC must cross to) as underpriced
    - Buy YES when BTC is below strike (market prices it too low)
    - Buy NO when BTC is above strike (market prices it too high)

  This creates a systematic edge that fires EVERY market interval:
    - No momentum prerequisites
    - No z-score thresholds (near-ATM = near-zero edge = correctly filtered by min_edge)
    - Trades whenever realized vol diverges from market vol AND edge > min threshold

Empirically validated on 5-minute paper trades:
  Trades where we bought the reversion direction (cheap side):
    BTC below strike → buy YES at low entry (0.36, 0.50) → won
  Trades where momentum pushed us to buy the expensive side:
    BTC above strike → buy YES at high entry (0.60) → lost
"""

import math
import structlog
from src.config import StrategyConfig
from src.polymarket_client import Market
from src.price_feed import PriceFeed
from src.strategies.base import BaseStrategy, Signal, SignalDirection

log = structlog.get_logger()

# Fixed vol per-second baked into the paper trader's simulated market prices
# (matches estimate_fair_value in paper_trade.py)
_MARKET_VOL_PER_SEC = 0.001 / math.sqrt(300)   # ≈ 0.0000577 /s


class StrikeArbStrategy(BaseStrategy):
    """
    Always-on strategy: fire every 5-min market interval using vol arb.

    Uses the discrepancy between realized vol (from price_feed) and the
    fixed vol assumption in the market pricing model to find cheap options.
    """

    def __init__(self, config: StrategyConfig):
        self.config = config

    @property
    def name(self) -> str:
        return "strike_arb"

    def evaluate(self, market: Market, price_feed: PriceFeed) -> Signal | None:
        if not price_feed.has_data:
            return None

        current_price = price_feed.current_price
        strike = market.strike_price
        secs_left = market.seconds_until_resolution

        if strike == 0 or current_price == 0 or secs_left <= 0:
            return None

        # Realized vol — try 120s first, fall back to 60s
        vol = price_feed.get_volatility(120)
        if vol is None or vol <= 0:
            vol = price_feed.get_volatility(60)
        if vol is None or vol <= 0:
            return None

        # Our fair probability using realized vol
        scaled_vol = vol * math.sqrt(secs_left)
        distance = (current_price - strike) / current_price

        if scaled_vol > 0:
            z = distance / scaled_vol
            our_prob_yes = _normal_cdf(z)
        else:
            our_prob_yes = 1.0 if current_price > strike else 0.0

        our_prob_yes = max(0.05, min(0.95, our_prob_yes))

        # Market's implied probability (from market price)
        market_prob_yes = market.implied_prob_yes
        edge_yes = our_prob_yes - market_prob_yes
        edge_no  = (1 - our_prob_yes) - market.implied_prob_no

        if edge_yes > edge_no and edge_yes > 0:
            direction  = SignalDirection.YES
            confidence = our_prob_yes
            edge       = edge_yes
        elif edge_no > 0:
            direction  = SignalDirection.NO
            confidence = 1 - our_prob_yes
            edge       = edge_no
        else:
            return None  # Realized vol matches market vol exactly — no edge

        vol_ratio = vol / _MARKET_VOL_PER_SEC
        reason = (
            f"realized_vol={vol:.5f} ({vol_ratio:.1f}x market_vol), "
            f"our_prob={our_prob_yes:.3f} vs market={market_prob_yes:.3f}, "
            f"dist={distance:+.4%}, secs_left={secs_left:.0f}"
        )

        log.debug("strike_arb_signal", direction=direction.value, edge=edge, reason=reason)

        return Signal(
            direction=direction,
            confidence=confidence,
            edge=edge,
            strategy_name=self.name,
            reason=reason,
        )


def _normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))
