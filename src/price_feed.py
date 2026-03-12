"""Real-time BTC price feed via WebSocket with multi-source fallback.

Tries in order: Binance global → Binance.US → Kraken → REST polling fallback.
"""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
import structlog
import websockets

from src.config import BinanceConfig

log = structlog.get_logger()

# WebSocket sources to try in order (Binance global blocks US IPs with HTTP 451)
WS_SOURCES = [
    {
        "name": "Binance",
        "url": "wss://stream.binance.com:9443/ws/btcusdt@trade",
        "parser": "binance",
    },
    {
        "name": "Binance.US",
        "url": "wss://stream.binance.us:9443/ws/btcusd@trade",
        "parser": "binance",
    },
    {
        "name": "Kraken",
        "url": "wss://ws.kraken.com",
        "parser": "kraken",
        "subscribe": json.dumps({
            "event": "subscribe",
            "pair": ["XBT/USD"],
            "subscription": {"name": "trade"},
        }),
    },
]


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
        """Start the price feed, trying multiple WebSocket sources."""
        self._running = True

        while self._running:
            # Try each WS source in order
            connected = False
            for source in WS_SOURCES:
                if not self._running:
                    return
                try:
                    log.info("price_feed_trying", source=source["name"],
                             url=source["url"])
                    async with websockets.connect(
                        source["url"], ping_interval=20, ping_timeout=10
                    ) as ws:
                        self._ws = ws
                        self._active_source = source

                        # Send subscribe message if needed (e.g., Kraken)
                        if "subscribe" in source:
                            await ws.send(source["subscribe"])

                        log.info("price_feed_connected", source=source["name"])
                        connected = True
                        async for message in ws:
                            if not self._running:
                                return
                            self._process_message(message, source["parser"])
                except websockets.ConnectionClosed:
                    log.warning("price_feed_disconnected",
                                source=source["name"])
                except Exception as e:
                    err = str(e)
                    log.warning("price_feed_source_failed",
                                source=source["name"], error=err)
                    # If HTTP 451 (geo-blocked), skip to next source immediately
                    if "451" in err:
                        continue
                    if connected:
                        break  # Was working, try reconnecting to same source

            # If all WS sources failed, fall back to REST polling
            if not connected and self._running:
                log.info("price_feed_rest_fallback",
                         msg="All WebSocket sources failed, using REST polling")
                await self._rest_poll_loop()

            if self._running:
                await asyncio.sleep(2)

    async def _rest_poll_loop(self):
        """Fallback: poll Kraken REST API for BTC price every 2 seconds."""
        import aiohttp
        log.info("price_feed_rest_polling", source="Kraken REST")
        try:
            async with aiohttp.ClientSession() as session:
                while self._running:
                    try:
                        async with session.get(
                            "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                result = data.get("result", {})
                                pair = result.get("XXBTZUSD", result.get("XBTUSD", {}))
                                if pair:
                                    price = float(pair["c"][0])  # last trade price
                                    vol = float(pair["c"][1])    # last trade volume
                                    tick = Tick(
                                        price=price,
                                        volume=vol,
                                        timestamp=time.time(),
                                    )
                                    self._current_price = tick.price
                                    self.ticks.append(tick)
                                    self._prune_old_ticks()
                                    for cb in self._callbacks:
                                        try:
                                            cb(tick)
                                        except Exception:
                                            pass
                    except Exception as e:
                        log.warning("rest_poll_error", error=str(e))
                    await asyncio.sleep(2)
        except Exception as e:
            log.error("rest_poll_session_error", error=str(e))

    def stop(self):
        """Stop the price feed."""
        self._running = False
        log.info("price_feed_stopped")

    def _process_message(self, raw: str, parser: str = "binance"):
        """Process a WebSocket message based on the source parser."""
        try:
            data = json.loads(raw)

            if parser == "binance":
                tick = Tick(
                    price=float(data["p"]),
                    volume=float(data["q"]),
                    timestamp=data["T"] / 1000.0,
                    is_buyer_maker=data.get("m", False),
                )
            elif parser == "kraken":
                # Kraken trade messages: [channelID, [[price, vol, time, side, type, misc], ...], "trade", "XBT/USD"]
                if not isinstance(data, list) or len(data) < 3:
                    return
                if data[-2] != "trade":
                    return  # skip non-trade messages (heartbeats, status, etc.)
                trades = data[1]
                if not trades:
                    return
                # Process the latest trade
                t = trades[-1]
                tick = Tick(
                    price=float(t[0]),
                    volume=float(t[1]),
                    timestamp=float(t[2]),
                    is_buyer_maker=(t[3] == "s"),  # "s" = sell, "b" = buy
                )
            else:
                return

            self._current_price = tick.price
            self.ticks.append(tick)
            self._prune_old_ticks()

            for cb in self._callbacks:
                try:
                    cb(tick)
                except Exception as e:
                    log.error("tick_callback_error", error=str(e))

        except (KeyError, ValueError, json.JSONDecodeError, IndexError):
            pass  # skip unparseable messages silently

    def _prune_old_ticks(self):
        """Remove ticks older than max_history_secs."""
        cutoff = self._now() - self.max_history_secs
        while self.ticks and self.ticks[0].timestamp < cutoff:
            self.ticks.popleft()
