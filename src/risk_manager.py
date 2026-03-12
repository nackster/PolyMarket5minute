"""Risk management: position sizing, exposure limits, and trade filtering."""

import time
import structlog
from dataclasses import dataclass, field

from src.config import TradingConfig
from src.polymarket_client import Market, Position
from src.strategies.base import Signal, SignalDirection

log = structlog.get_logger()


@dataclass
class TradeDecision:
    """Final decision on whether and how to trade."""
    should_trade: bool
    signal: Signal | None
    size: float  # USDC amount
    token_id: str  # which token to buy
    side: str  # "BUY" or "SELL"
    reason: str


class RiskManager:
    """Controls position sizing and enforces risk limits."""

    def __init__(self, config: TradingConfig):
        self.config = config
        self.open_positions: list[Position] = []
        self.trade_history: list[dict] = []
        self._daily_pnl: float = 0.0
        self._last_trade_time: float = 0.0

    def evaluate_trade(self, signal: Signal, market: Market) -> TradeDecision:
        """Decide whether to take a trade and how to size it.

        Args:
            signal: The trading signal from a strategy
            market: The market to trade

        Returns:
            A TradeDecision with the final verdict.
        """
        # Check: minimum edge threshold
        if signal.edge < self.config.min_edge_threshold:
            return TradeDecision(
                should_trade=False, signal=signal, size=0, token_id="",
                side="", reason=f"Edge {signal.edge:.4f} below threshold {self.config.min_edge_threshold}",
            )

        # Check: max open positions
        if len(self.open_positions) >= self.config.max_open_positions:
            return TradeDecision(
                should_trade=False, signal=signal, size=0, token_id="",
                side="", reason=f"Max positions reached ({self.config.max_open_positions})",
            )

        # Check: don't trade the same market twice
        for pos in self.open_positions:
            if pos.market.condition_id == market.condition_id:
                return TradeDecision(
                    should_trade=False, signal=signal, size=0, token_id="",
                    side="", reason="Already have position in this market",
                )

        # Check: cooldown between trades (minimum 10 seconds)
        if time.time() - self._last_trade_time < 10:
            return TradeDecision(
                should_trade=False, signal=signal, size=0, token_id="",
                side="", reason="Trade cooldown active",
            )

        # Check: market has enough time left (at least 30 seconds)
        if market.seconds_until_resolution < 30:
            return TradeDecision(
                should_trade=False, signal=signal, size=0, token_id="",
                side="", reason="Market too close to resolution",
            )

        # Position sizing: Kelly criterion (conservative half-Kelly)
        size = self._calculate_position_size(signal)

        # Determine which token to buy
        if signal.direction == SignalDirection.YES:
            token_id = market.token_id_yes
        else:
            token_id = market.token_id_no

        return TradeDecision(
            should_trade=True,
            signal=signal,
            size=size,
            token_id=token_id,
            side="BUY",
            reason=f"Taking {signal.direction.value} with {signal.edge:.4f} edge, size=${size:.2f}",
        )

    def _calculate_position_size(self, signal: Signal) -> float:
        """Calculate position size using half-Kelly criterion.

        Kelly fraction = edge / odds
        We use half-Kelly for safety.
        """
        # For binary markets, the "odds" are determined by the price we pay
        # If we buy YES at 0.60, our payout odds are (1/0.60 - 1) = 0.667
        # Kelly = (p * b - q) / b where p=our_prob, b=payout_odds, q=1-p
        p = signal.confidence
        q = 1 - p

        # The price we'd pay is approximately (1 - edge) for the market price
        price = p - signal.edge  # approximate market price
        price = max(0.05, min(0.95, price))

        b = (1 / price) - 1  # payout odds
        if b <= 0:
            return self.config.max_position_size * 0.1  # minimum size

        kelly = (p * b - q) / b
        half_kelly = kelly / 2

        # Clamp to max position size
        size = max(1.0, min(self.config.max_position_size, self.config.max_position_size * half_kelly))

        return round(size, 2)

    def record_trade(self, decision: TradeDecision, market: Market):
        """Record that a trade was executed."""
        self._last_trade_time = time.time()
        position = Position(
            market=market,
            side=decision.signal.direction.value,
            size=decision.size,
            entry_price=market.implied_prob_yes if decision.signal.direction == SignalDirection.YES else market.implied_prob_no,
            timestamp=time.time(),
        )
        self.open_positions.append(position)
        self.trade_history.append({
            "timestamp": time.time(),
            "market": market.condition_id,
            "side": decision.signal.direction.value,
            "size": decision.size,
            "strategy": decision.signal.strategy_name,
            "edge": decision.signal.edge,
        })
        log.info(
            "trade_recorded",
            market=market.condition_id,
            side=decision.signal.direction.value,
            size=decision.size,
            strategy=decision.signal.strategy_name,
        )

    def resolve_position(self, condition_id: str, pnl: float):
        """Remove a position after market resolution."""
        self.open_positions = [
            p for p in self.open_positions if p.market.condition_id != condition_id
        ]
        self._daily_pnl += pnl
        log.info("position_resolved", condition_id=condition_id, pnl=pnl, daily_pnl=self._daily_pnl)

    @property
    def total_exposure(self) -> float:
        """Total USDC currently at risk."""
        return sum(p.size for p in self.open_positions)
