"""Base strategy interface and signal types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from src.polymarket_client import Market
from src.price_feed import PriceFeed


class SignalDirection(Enum):
    YES = "YES"   # BTC will be ABOVE the strike
    NO = "NO"     # BTC will be BELOW the strike
    HOLD = "HOLD" # No trade


@dataclass
class Signal:
    """A trading signal produced by a strategy."""
    direction: SignalDirection
    confidence: float      # 0.0 - 1.0, our estimated probability
    edge: float            # confidence - market_price (the theoretical edge)
    strategy_name: str
    reason: str            # human-readable explanation


class BaseStrategy(ABC):
    """Interface for all trading strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this strategy."""
        ...

    @abstractmethod
    def evaluate(self, market: Market, price_feed: PriceFeed) -> Signal | None:
        """Evaluate a market and return a signal, or None if no opinion.

        Args:
            market: The Polymarket 5-min BTC market to evaluate
            price_feed: Real-time BTC price data

        Returns:
            A Signal if the strategy has a trade idea, None otherwise.
        """
        ...
