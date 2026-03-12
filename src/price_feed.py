"""Real-time BTC price feed via Binance WebSocket."""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
import structlog
import websockets

from src.config import BinanceConfig

log = structlog.get_logger()


@dataclass
class Tick:
    """A single trade/price tick."""
    price: float
    volume: float
    timestamp: float  # unix time in seconds
    is_buyer_maker: bool = False


class PriceFeed:
    """Maintains a rolling window of BTC price ticks from Binance."""

    def __init__(self, config: BinanceConfig, max_history_secs: int = 600):
        self.config = config
        self.max_history_secs = max_history_secs
        self.ticks: deque[Tick] = deque()
        self._current_price: float = 0.0
        self._current_time: float = 0.0  # updated per-tick; 0 = use real time
        self._ws = None
        self._running = False
        self._callbacks: list = []

    @property
    def current_price(self) -> float:
        return self._current_price

    @property
    def has_data(self) -> bool:
        return len(self.ticks) > 0

    def on_tick(self, callback):
        """Register a callback for new ticks. callback(tick: Tick)"""
        self._callbacks.append(callback)

    def _now(self) -> float:
        """Current time: uses simulated tick time during backtesting, real time live."""
        return self._current_time if self._current_time > 0 else time.time()

    def get_prices_since(self, seconds_ago: float) -> list[Tick]:
        """Get all ticks from the last N seconds."""
        cutoff = self._now() - seconds_ago
        return [t for t in self.ticks if t.timestamp >= cutoff]

    def get_price_change(self, seconds_ago: float) -> float | None:
        """Get price change over the last N seconds. Returns None if insufficient data."""
        ticks = self.get_prices_since(seconds_ago)
        if len(ticks) < 2:
            return None
        return ticks[-1].price - ticks[0].price

    def get_price_change_pct(self, seconds_ago: float) -> float | None:
        """Get percentage price change over the last N seconds."""
        ticks = self.get_prices_since(seconds_ago)
        if len(ticks) < 2 or ticks[0].price == 0:
            return None
        return (ticks[-1].price - ticks[0].price) / ticks[0].price

    def get_volume_since(self, seconds_ago: float) -> float:
        """Get total volume over the last N seconds."""
        ticks = self.get_prices_since(seconds_ago)
        return sum(t.volume for t in ticks)

    def get_buy_sell_ratio(self, seconds_ago: float) -> float | None:
        """Get buy/sell volume ratio over the last N seconds.

        Returns ratio > 1 means more buying, < 1 means more selling.
        """
        ticks = self.get_prices_since(seconds_ago)
        if not ticks:
            return None
        buy_vol = sum(t.volume for t in ticks if not t.is_buyer_maker)
        sell_vol = sum(t.volume for t in ticks if t.is_buyer_maker)
        if sell_vol == 0:
            return float("inf") if buy_vol > 0 else 1.0
        return buy_vol / sell_vol

    def get_vwap(self, seconds_ago: float) -> float | None:
        """Get volume-weighted average price over the last N seconds."""
        ticks = self.get_prices_since(seconds_ago)
        if not ticks:
            return None
        total_value = sum(t.price * t.volume for t in ticks)
        total_volume = sum(t.volume for t in ticks)
        if total_volume == 0:
            return ticks[-1].price
        return total_value / total_volume

    def get_volatility(self, seconds_ago: float) -> float | None:
        """Get price volatility (std dev of returns) over the last N seconds."""
        ticks = self.get_prices_since(seconds_ago)
        if len(ticks) < 4:
            return None
        # Sample every ~1 second for return calculation
        prices = [t.price for t in ticks[::max(1, len(ticks) // 60)]]
        if len(prices) < 3:
            return None
        returns = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        return variance ** 0.5

    async def start(self):
        """Start the WebSocket price feed."""
        self._running = True
        log.info("price_feed_starting", url=self.config.ws_url)

        while self._running:
            try:
                async with websockets.connect(self.config.ws_url) as ws:
                    self._ws = ws
                    log.info("price_feed_connected")
                    async for message in ws:
                        if not self._running:
                            break
                        self._process_message(message)
            except websockets.ConnectionClosed:
                log.warning("price_feed_disconnected, reconnecting...")
                await asyncio.sleep(1)
            except Exception as e:
                log.error("price_feed_error", error=str(e))
                await asyncio.sleep(3)

    def stop(self):
        """Stop the price feed."""
        self._running = False
        log.info("price_feed_stopped")

    def _process_message(self, raw: str):
        """Process a Binance trade message."""
        try:
            data = json.loads(raw)
            tick = Tick(
                price=float(data["p"]),
                volume=float(data["q"]),
                timestamp=data["T"] / 1000.0,  # Binance sends ms
                is_buyer_maker=data.get("m", False),
            )
            self._current_price = tick.price
            self.ticks.append(tick)
            self._prune_old_ticks()

            for cb in self._callbacks:
                try:
                    cb(tick)
                except Exception as e:
                    log.error("tick_callback_error", error=str(e))

        except (KeyError, ValueError, json.JSONDecodeError) as e:
            log.warning("price_feed_parse_error", error=str(e))

    def _prune_old_ticks(self):
        """Remove ticks older than max_history_secs."""
        cutoff = self._now() - self.max_history_secs
        while self.ticks and self.ticks[0].timestamp < cutoff:
            self.ticks.popleft()
