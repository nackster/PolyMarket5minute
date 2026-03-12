"""Advanced signal toolkit for short-term BTC prediction.

This module provides institutional-grade microstructure signals
designed for 5-minute binary prediction markets. Each signal class
is independent and can be combined for higher-confidence trades.

Key concepts:
- CVD (Cumulative Volume Delta): Tracks net aggressive buying vs selling.
  Aggressive = market orders hitting the bid/ask. Divergence between
  CVD and price reveals "hidden" directional pressure.
- Liquidity sweeps: When price runs through a dense cluster of resting
  orders (stop-losses, liquidations), then reverses. The sweep exhausts
  directional fuel.
- Order book imbalance: Ratio of bid vs ask depth. Heavy imbalance
  predicts short-term price direction.
- Tape speed: Trades per second. Sudden acceleration signals incoming
  volatility or a large player entering.
- VWAP deviation: Price stretched far from volume-weighted average
  tends to revert on short timeframes.
"""

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import structlog

from src.price_feed import PriceFeed, Tick

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Cumulative Volume Delta (CVD)
# ---------------------------------------------------------------------------

class CVDSignal(Enum):
    BULLISH_DIVERGENCE = "bullish_divergence"  # Price falling but CVD rising
    BEARISH_DIVERGENCE = "bearish_divergence"  # Price rising but CVD falling
    CONFIRMING_BULL = "confirming_bull"         # Both price and CVD rising
    CONFIRMING_BEAR = "confirming_bear"         # Both price and CVD falling
    NEUTRAL = "neutral"


@dataclass
class CVDReading:
    signal: CVDSignal
    cvd_value: float          # Raw CVD value
    cvd_change: float         # CVD change over window
    price_change: float       # Price change over window
    strength: float           # 0-1 signal strength


class CVDAnalyzer:
    """Tracks cumulative volume delta from trade flow.

    CVD = sum of (volume * direction) where:
    - Buyer-initiated trades (lifting the ask) are positive
    - Seller-initiated trades (hitting the bid) are negative

    Binance provides the 'is_buyer_maker' flag:
    - is_buyer_maker=True means the buyer placed a limit order,
      so the SELLER is the aggressor (sell pressure)
    - is_buyer_maker=False means the BUYER is the aggressor (buy pressure)
    """

    def __init__(self, max_history: int = 600):
        self.max_history = max_history
        self._cvd_points: deque[tuple[float, float]] = deque()  # (timestamp, cumulative_cvd)
        self._running_cvd: float = 0.0
        self._current_time: float = 0.0  # updated per-tick for backtest compatibility

    def _now(self) -> float:
        return self._current_time if self._current_time > 0 else time.time()

    def update(self, tick: Tick):
        """Process a new tick and update CVD."""
        self._current_time = tick.timestamp
        # Aggressive buyer = not buyer_maker
        if tick.is_buyer_maker:
            delta = -tick.volume  # Seller aggressor
        else:
            delta = tick.volume   # Buyer aggressor

        self._running_cvd += delta
        self._cvd_points.append((tick.timestamp, self._running_cvd))
        self._prune()

    def analyze(self, price_feed: PriceFeed, window_secs: float = 60) -> CVDReading:
        """Analyze CVD vs price over a time window to detect divergences."""
        if len(self._cvd_points) < 10:
            return CVDReading(CVDSignal.NEUTRAL, 0, 0, 0, 0)

        cutoff = self._now() - window_secs
        window_points = [(t, v) for t, v in self._cvd_points if t >= cutoff]

        if len(window_points) < 5:
            return CVDReading(CVDSignal.NEUTRAL, 0, 0, 0, 0)

        cvd_start = window_points[0][1]
        cvd_end = window_points[-1][1]
        cvd_change = cvd_end - cvd_start

        price_change_pct = price_feed.get_price_change_pct(window_secs)
        if price_change_pct is None:
            return CVDReading(CVDSignal.NEUTRAL, cvd_end, cvd_change, 0, 0)

        # Normalize CVD change for strength calculation
        avg_volume = price_feed.get_volume_since(window_secs) / max(1, len(window_points))
        cvd_normalized = cvd_change / max(0.001, avg_volume * len(window_points))

        # Detect divergence
        price_up = price_change_pct > 0.0001
        price_down = price_change_pct < -0.0001
        cvd_up = cvd_change > 0
        cvd_down = cvd_change < 0

        if price_down and cvd_up:
            signal = CVDSignal.BULLISH_DIVERGENCE
            strength = min(1.0, abs(cvd_normalized) * 3)
        elif price_up and cvd_down:
            signal = CVDSignal.BEARISH_DIVERGENCE
            strength = min(1.0, abs(cvd_normalized) * 3)
        elif price_up and cvd_up:
            signal = CVDSignal.CONFIRMING_BULL
            strength = min(1.0, abs(cvd_normalized) * 2)
        elif price_down and cvd_down:
            signal = CVDSignal.CONFIRMING_BEAR
            strength = min(1.0, abs(cvd_normalized) * 2)
        else:
            signal = CVDSignal.NEUTRAL
            strength = 0.0

        return CVDReading(
            signal=signal,
            cvd_value=cvd_end,
            cvd_change=cvd_change,
            price_change=price_change_pct,
            strength=strength,
        )

    def _prune(self):
        cutoff = self._now() - self.max_history
        while self._cvd_points and self._cvd_points[0][0] < cutoff:
            self._cvd_points.popleft()


# ---------------------------------------------------------------------------
# Liquidity Sweep Detector
# ---------------------------------------------------------------------------

@dataclass
class LiquiditySweep:
    """Detected liquidity sweep event."""
    timestamp: float
    direction: str          # "up" (swept asks/stops above) or "down" (swept bids/stops below)
    sweep_price: float      # The extreme price reached during the sweep
    recovery_price: float   # Where price settled after the sweep
    sweep_size_pct: float   # How far price moved during the sweep (%)
    recovery_pct: float     # How much of the sweep was recovered (%)
    is_reversal: bool       # True if price reversed past the pre-sweep level


class LiquiditySweepDetector:
    """Detects liquidity sweeps / stop hunts.

    A liquidity sweep occurs when:
    1. Price makes a sharp move in one direction (the "sweep")
    2. Quickly reverses back (the "recovery")

    This pattern indicates resting orders (stops/liquidations) were hit,
    and the move has exhausted its fuel. The reversal is often tradeable.

    Detection method:
    - Track rolling highs/lows over short windows
    - When price spikes beyond a threshold then retraces >50%, flag it
    """

    def __init__(self, spike_threshold_pct: float = 0.0015, recovery_threshold: float = 0.5):
        self.spike_threshold = spike_threshold_pct  # Min spike size to be a "sweep"
        self.recovery_threshold = recovery_threshold  # Min % retracement to confirm
        self.recent_sweeps: deque[LiquiditySweep] = deque(maxlen=50)
        self._price_window: deque[tuple[float, float]] = deque()  # (timestamp, price)
        self._window_secs = 15  # Look for sweeps in 15-second windows
        self._current_time: float = 0.0

    def _now(self) -> float:
        return self._current_time if self._current_time > 0 else time.time()

    def update(self, tick: Tick):
        """Process a new tick."""
        self._current_time = tick.timestamp
        self._price_window.append((tick.timestamp, tick.price))
        self._prune_window()
        self._detect_sweep(tick)

    def get_recent_sweeps(self, seconds_ago: float = 120) -> list[LiquiditySweep]:
        """Get sweeps detected in the last N seconds."""
        cutoff = self._now() - seconds_ago
        return [s for s in self.recent_sweeps if s.timestamp >= cutoff]

    def has_recent_sweep(self, direction: str, seconds_ago: float = 60) -> bool:
        """Check if there's been a sweep in the given direction recently."""
        return any(
            s.direction == direction
            for s in self.get_recent_sweeps(seconds_ago)
        )

    def _detect_sweep(self, current_tick: Tick):
        if len(self._price_window) < 20:
            return

        prices = [p for _, p in self._price_window]
        timestamps = [t for t, _ in self._price_window]

        current = current_tick.price
        window_high = max(prices)
        window_low = min(prices)
        window_start = prices[0]

        if window_start == 0:
            return

        # Check for upward sweep (price spiked up then came back)
        spike_up_pct = (window_high - window_start) / window_start
        if spike_up_pct > self.spike_threshold:
            recovery_from_high = (window_high - current) / (window_high - window_start) if window_high != window_start else 0
            if recovery_from_high > self.recovery_threshold:
                sweep = LiquiditySweep(
                    timestamp=current_tick.timestamp,
                    direction="up",
                    sweep_price=window_high,
                    recovery_price=current,
                    sweep_size_pct=spike_up_pct,
                    recovery_pct=recovery_from_high,
                    is_reversal=current < window_start,
                )
                # Don't duplicate if we already detected this sweep
                if not self.recent_sweeps or self.recent_sweeps[-1].timestamp < current_tick.timestamp - 5:
                    self.recent_sweeps.append(sweep)
                    log.debug("liquidity_sweep_detected", direction="up", size=f"{spike_up_pct:.4%}")

        # Check for downward sweep (price spiked down then came back)
        spike_down_pct = (window_start - window_low) / window_start
        if spike_down_pct > self.spike_threshold:
            recovery_from_low = (current - window_low) / (window_start - window_low) if window_start != window_low else 0
            if recovery_from_low > self.recovery_threshold:
                sweep = LiquiditySweep(
                    timestamp=current_tick.timestamp,
                    direction="down",
                    sweep_price=window_low,
                    recovery_price=current,
                    sweep_size_pct=spike_down_pct,
                    recovery_pct=recovery_from_low,
                    is_reversal=current > window_start,
                )
                if not self.recent_sweeps or self.recent_sweeps[-1].timestamp < current_tick.timestamp - 5:
                    self.recent_sweeps.append(sweep)
                    log.debug("liquidity_sweep_detected", direction="down", size=f"{spike_down_pct:.4%}")

    def _prune_window(self):
        cutoff = self._now() - self._window_secs
        while self._price_window and self._price_window[0][0] < cutoff:
            self._price_window.popleft()


# ---------------------------------------------------------------------------
# Order Book Imbalance (Binance depth)
# ---------------------------------------------------------------------------

@dataclass
class BookImbalance:
    """Snapshot of order book imbalance."""
    bid_depth: float        # Total bid volume within N levels
    ask_depth: float        # Total ask volume within N levels
    imbalance_ratio: float  # bid_depth / ask_depth (>1 = bid heavy)
    signal: str             # "strong_bid", "strong_ask", "neutral"
    strength: float         # 0-1


class OrderBookAnalyzer:
    """Analyzes Binance BTC order book for imbalance signals.

    Heavy bid side = buying support = bullish short-term
    Heavy ask side = selling pressure = bearish short-term

    We track the imbalance over time to detect shifts.
    """

    def __init__(self):
        self._history: deque[tuple[float, float]] = deque(maxlen=120)  # (timestamp, ratio)
        self._current_time: float = 0.0

    def _now(self) -> float:
        return self._current_time if self._current_time > 0 else time.time()

    def analyze_depth(self, bids: list[list], asks: list[list], levels: int = 10, timestamp: float = 0) -> BookImbalance:
        """Analyze order book depth.

        Args:
            bids: [[price, qty], ...] sorted descending
            asks: [[price, qty], ...] sorted ascending
            levels: Number of levels to analyze
            timestamp: Current time (use simulated time in backtesting)
        """
        bid_depth = sum(float(b[1]) for b in bids[:levels]) if bids else 0
        ask_depth = sum(float(a[1]) for a in asks[:levels]) if asks else 0

        if ask_depth == 0:
            ratio = float("inf") if bid_depth > 0 else 1.0
        else:
            ratio = bid_depth / ask_depth

        if timestamp > 0:
            self._current_time = timestamp
        self._history.append((self._now(), ratio))

        if ratio > 2.0:
            signal = "strong_bid"
            strength = min(1.0, (ratio - 1) / 3)
        elif ratio < 0.5:
            signal = "strong_ask"
            strength = min(1.0, (1 / ratio - 1) / 3)
        elif ratio > 1.3:
            signal = "mild_bid"
            strength = (ratio - 1) / 2
        elif ratio < 0.77:
            signal = "mild_ask"
            strength = (1 / ratio - 1) / 2
        else:
            signal = "neutral"
            strength = 0.0

        return BookImbalance(
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            imbalance_ratio=ratio,
            signal=signal,
            strength=strength,
        )

    def get_imbalance_trend(self, seconds: float = 30) -> float:
        """Get the trend of imbalance over time. Positive = increasingly bid-heavy."""
        cutoff = self._now() - seconds
        points = [(t, r) for t, r in self._history if t >= cutoff]
        if len(points) < 3:
            return 0.0
        # Simple linear regression slope
        n = len(points)
        sum_x = sum(i for i in range(n))
        sum_y = sum(r for _, r in points)
        sum_xy = sum(i * r for i, (_, r) in enumerate(points))
        sum_x2 = sum(i * i for i in range(n))
        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return 0.0
        return (n * sum_xy - sum_x * sum_y) / denom


# ---------------------------------------------------------------------------
# Tape Speed (Trades per second)
# ---------------------------------------------------------------------------

@dataclass
class TapeReading:
    """Current tape speed analysis."""
    trades_per_second: float
    avg_trades_per_second: float  # Rolling average
    speed_ratio: float            # current / average (>1 = accelerating)
    is_accelerating: bool         # Significant acceleration detected
    acceleration_factor: float    # How much faster than normal


class TapeSpeedAnalyzer:
    """Monitors trade frequency to detect activity surges.

    A sudden increase in trades/second often precedes large moves.
    This is the "tape speeding up" that floor traders watch for.
    """

    def __init__(self, window_secs: int = 60):
        self.window_secs = window_secs
        self._trade_times: deque[float] = deque()
        self._acceleration_threshold = 2.0  # 2x normal = accelerating
        self._current_time: float = 0.0

    def _now(self) -> float:
        return self._current_time if self._current_time > 0 else time.time()

    def update(self, tick: Tick):
        self._current_time = tick.timestamp
        self._trade_times.append(tick.timestamp)
        self._prune()

    def analyze(self) -> TapeReading:
        if len(self._trade_times) < 10:
            return TapeReading(0, 0, 1.0, False, 1.0)

        now = self._now()

        # Current speed: trades in the last 5 seconds
        recent_cutoff = now - 5
        recent_trades = sum(1 for t in self._trade_times if t >= recent_cutoff)
        current_tps = recent_trades / 5.0

        # Average speed: trades over the full window
        total_trades = len(self._trade_times)
        elapsed = now - self._trade_times[0]
        avg_tps = total_trades / max(1, elapsed)

        speed_ratio = current_tps / max(0.1, avg_tps)
        is_accelerating = speed_ratio > self._acceleration_threshold

        return TapeReading(
            trades_per_second=current_tps,
            avg_trades_per_second=avg_tps,
            speed_ratio=speed_ratio,
            is_accelerating=is_accelerating,
            acceleration_factor=speed_ratio,
        )

    def _prune(self):
        cutoff = self._now() - self.window_secs
        while self._trade_times and self._trade_times[0] < cutoff:
            self._trade_times.popleft()


# ---------------------------------------------------------------------------
# VWAP Deviation Bands
# ---------------------------------------------------------------------------

@dataclass
class VWAPReading:
    """VWAP analysis result."""
    vwap: float
    upper_band: float       # VWAP + N std devs
    lower_band: float       # VWAP - N std devs
    deviation: float        # Current price distance from VWAP in std devs
    signal: str             # "overbought", "oversold", "neutral"
    strength: float         # 0-1


class VWAPAnalyzer:
    """VWAP with standard deviation bands for mean-reversion signals.

    Price stretched >2 std devs from VWAP on short timeframes tends to
    revert. This is one of the most reliable short-term signals.
    """

    def __init__(self, std_devs: float = 2.0):
        self.std_devs = std_devs

    def analyze(self, price_feed: PriceFeed, window_secs: float = 120) -> VWAPReading:
        ticks = price_feed.get_prices_since(window_secs)

        if len(ticks) < 20:
            return VWAPReading(0, 0, 0, 0, "neutral", 0)

        # Calculate VWAP
        total_value = sum(t.price * t.volume for t in ticks)
        total_volume = sum(t.volume for t in ticks)

        if total_volume == 0:
            return VWAPReading(0, 0, 0, 0, "neutral", 0)

        vwap = total_value / total_volume

        # Calculate standard deviation of price around VWAP
        sq_diff_sum = sum(t.volume * (t.price - vwap) ** 2 for t in ticks)
        variance = sq_diff_sum / total_volume
        std = math.sqrt(variance) if variance > 0 else 0

        upper = vwap + self.std_devs * std
        lower = vwap - self.std_devs * std

        current = price_feed.current_price
        deviation = (current - vwap) / std if std > 0 else 0

        if deviation > self.std_devs:
            signal = "overbought"
            strength = min(1.0, (deviation - self.std_devs) / self.std_devs)
        elif deviation < -self.std_devs:
            signal = "oversold"
            strength = min(1.0, (abs(deviation) - self.std_devs) / self.std_devs)
        else:
            signal = "neutral"
            strength = 0.0

        return VWAPReading(
            vwap=vwap,
            upper_band=upper,
            lower_band=lower,
            deviation=deviation,
            signal=signal,
            strength=strength,
        )


# ---------------------------------------------------------------------------
# Multi-Exchange Spread (Coinbase vs Binance lead/lag)
# ---------------------------------------------------------------------------

@dataclass
class ExchangeSpread:
    """Cross-exchange price spread analysis."""
    binance_price: float
    reference_price: float    # e.g., Coinbase
    spread_bps: float         # Spread in basis points
    direction: str            # "binance_leads", "reference_leads", "neutral"
    strength: float


class MultiExchangeAnalyzer:
    """Detects lead/lag relationships between exchanges.

    Coinbase often leads Binance for USD-denominated pairs.
    When Coinbase price moves first, Binance tends to follow.
    This gives a 100-500ms edge for predicting direction.
    """

    def __init__(self):
        self._reference_prices: deque[tuple[float, float]] = deque(maxlen=300)
        self._binance_prices: deque[tuple[float, float]] = deque(maxlen=300)

    def update_reference(self, price: float, timestamp: float):
        """Update the reference exchange price (e.g., Coinbase)."""
        self._reference_prices.append((timestamp, price))

    def update_binance(self, price: float, timestamp: float):
        """Update the Binance price."""
        self._binance_prices.append((timestamp, price))

    def analyze(self) -> ExchangeSpread:
        if not self._binance_prices or not self._reference_prices:
            return ExchangeSpread(0, 0, 0, "neutral", 0)

        b_price = self._binance_prices[-1][1]
        r_price = self._reference_prices[-1][1]

        if b_price == 0 or r_price == 0:
            return ExchangeSpread(b_price, r_price, 0, "neutral", 0)

        spread_bps = ((r_price - b_price) / b_price) * 10000

        if spread_bps > 2:
            direction = "reference_leads"  # Reference is higher -> Binance should go up
            strength = min(1.0, abs(spread_bps) / 10)
        elif spread_bps < -2:
            direction = "binance_leads"  # Binance is higher -> might come back down
            strength = min(1.0, abs(spread_bps) / 10)
        else:
            direction = "neutral"
            strength = 0.0

        return ExchangeSpread(
            binance_price=b_price,
            reference_price=r_price,
            spread_bps=spread_bps,
            direction=direction,
            strength=strength,
        )


# ---------------------------------------------------------------------------
# Composite Signal Aggregator
# ---------------------------------------------------------------------------

@dataclass
class CompositeSignal:
    """Aggregated signal from all analyzers."""
    direction: str              # "bullish", "bearish", "neutral"
    confidence: float           # 0-1
    components: dict            # Individual signal details
    primary_driver: str         # Which signal is the main driver
    conflicting_signals: int    # How many signals disagree


class SignalAggregator:
    """Combines all signal sources into a single composite signal.

    Uses a weighted voting system where each signal contributes
    based on its historical reliability and current strength.
    """

    # Signal weights (tunable based on backtesting)
    WEIGHTS = {
        "cvd": 0.25,
        "liquidity_sweep": 0.20,
        "book_imbalance": 0.15,
        "tape_speed": 0.10,
        "vwap": 0.20,
        "exchange_spread": 0.10,
    }

    def aggregate(
        self,
        cvd: CVDReading,
        sweeps: list[LiquiditySweep],
        book: BookImbalance,
        tape: TapeReading,
        vwap: VWAPReading,
        spread: ExchangeSpread,
    ) -> CompositeSignal:
        """Aggregate all signals into a directional composite."""
        votes = {}  # signal_name -> (direction_score, weight)
        components = {}

        # CVD signal
        cvd_score = 0
        if cvd.signal == CVDSignal.BULLISH_DIVERGENCE:
            cvd_score = cvd.strength
        elif cvd.signal == CVDSignal.BEARISH_DIVERGENCE:
            cvd_score = -cvd.strength
        elif cvd.signal == CVDSignal.CONFIRMING_BULL:
            cvd_score = cvd.strength * 0.7
        elif cvd.signal == CVDSignal.CONFIRMING_BEAR:
            cvd_score = -cvd.strength * 0.7
        votes["cvd"] = cvd_score
        components["cvd"] = {"signal": cvd.signal.value, "strength": cvd.strength}

        # Liquidity sweep signal (recent sweeps)
        sweep_score = 0
        if sweeps:
            latest = sweeps[-1]
            # Sweep UP then reverse = bearish; sweep DOWN then reverse = bullish
            if latest.direction == "up" and latest.is_reversal:
                sweep_score = -latest.recovery_pct
            elif latest.direction == "down" and latest.is_reversal:
                sweep_score = latest.recovery_pct
        votes["liquidity_sweep"] = sweep_score
        components["liquidity_sweep"] = {"count": len(sweeps), "score": sweep_score}

        # Book imbalance
        book_score = 0
        if book.signal == "strong_bid":
            book_score = book.strength
        elif book.signal == "strong_ask":
            book_score = -book.strength
        elif book.signal == "mild_bid":
            book_score = book.strength * 0.5
        elif book.signal == "mild_ask":
            book_score = -book.strength * 0.5
        votes["book_imbalance"] = book_score
        components["book_imbalance"] = {"signal": book.signal, "ratio": book.imbalance_ratio}

        # Tape speed (amplifier, not directional by itself)
        tape_multiplier = 1.0
        if tape.is_accelerating:
            tape_multiplier = min(1.5, 1.0 + (tape.acceleration_factor - 2) * 0.25)
        components["tape_speed"] = {
            "tps": tape.trades_per_second,
            "accelerating": tape.is_accelerating,
            "multiplier": tape_multiplier,
        }

        # VWAP deviation (mean reversion)
        vwap_score = 0
        if vwap.signal == "overbought":
            vwap_score = -vwap.strength  # Overbought = expect reversion down
        elif vwap.signal == "oversold":
            vwap_score = vwap.strength   # Oversold = expect reversion up
        votes["vwap"] = vwap_score
        components["vwap"] = {"signal": vwap.signal, "deviation": vwap.deviation}

        # Exchange spread
        spread_score = 0
        if spread.direction == "reference_leads":
            spread_score = spread.strength   # Reference higher = Binance should rise
        elif spread.direction == "binance_leads":
            spread_score = -spread.strength  # Binance higher = might fall
        votes["exchange_spread"] = spread_score
        components["exchange_spread"] = {"direction": spread.direction, "bps": spread.spread_bps}

        # Weighted aggregate
        weighted_sum = 0
        total_weight = 0
        conflicting = 0
        primary_driver = "none"
        max_contribution = 0

        for name, score in votes.items():
            weight = self.WEIGHTS.get(name, 0.1)
            contribution = score * weight
            weighted_sum += contribution
            total_weight += weight

            if abs(contribution) > abs(max_contribution):
                max_contribution = contribution
                primary_driver = name

        # Count conflicts
        positive = sum(1 for s in votes.values() if s > 0.1)
        negative = sum(1 for s in votes.values() if s < -0.1)
        conflicting = min(positive, negative)

        # Apply tape speed multiplier
        weighted_sum *= tape_multiplier

        # Normalize to -1..1 range
        if total_weight > 0:
            normalized = weighted_sum / total_weight
        else:
            normalized = 0

        # Convert to direction and confidence
        if normalized > 0.05:
            direction = "bullish"
            confidence = min(0.95, abs(normalized))
        elif normalized < -0.05:
            direction = "bearish"
            confidence = min(0.95, abs(normalized))
        else:
            direction = "neutral"
            confidence = 0.0

        # Reduce confidence when signals conflict
        if conflicting >= 2:
            confidence *= 0.6

        return CompositeSignal(
            direction=direction,
            confidence=confidence,
            components=components,
            primary_driver=primary_driver,
            conflicting_signals=conflicting,
        )
