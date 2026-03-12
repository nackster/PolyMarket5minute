"""Central configuration loaded from environment variables."""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class PolymarketConfig:
    api_key: str = os.getenv("POLYMARKET_API_KEY", "")
    private_key: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    proxy_address: str = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
    api_url: str = os.getenv("POLYMARKET_API_URL", "https://clob.polymarket.com")


@dataclass
class BinanceConfig:
    ws_url: str = os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws/btcusdt@trade")


@dataclass
class TradingConfig:
    max_position_size: float = float(os.getenv("MAX_POSITION_SIZE", "10.0"))
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    min_edge_threshold: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.02"))
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "0.15"))


@dataclass
class StrategyConfig:
    momentum_lookback_secs: int = int(os.getenv("MOMENTUM_LOOKBACK_SECS", "120"))
    rsi_period: int = int(os.getenv("RSI_PERIOD", "14"))
    bb_std_dev: float = float(os.getenv("BB_STD_DEV", "2.0"))


@dataclass
class Config:
    polymarket: PolymarketConfig = None
    binance: BinanceConfig = None
    trading: TradingConfig = None
    strategy: StrategyConfig = None
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "logs/bot.log")

    def __post_init__(self):
        self.polymarket = self.polymarket or PolymarketConfig()
        self.binance = self.binance or BinanceConfig()
        self.trading = self.trading or TradingConfig()
        self.strategy = self.strategy or StrategyConfig()

    def validate(self) -> list[str]:
        """Return a list of validation errors, empty if config is valid."""
        errors = []
        if not self.polymarket.api_key:
            errors.append("POLYMARKET_API_KEY is required")
        if not self.polymarket.private_key:
            errors.append("POLYMARKET_PRIVATE_KEY is required")
        if self.trading.max_position_size <= 0:
            errors.append("MAX_POSITION_SIZE must be positive")
        if self.trading.min_edge_threshold < 0 or self.trading.min_edge_threshold > 1:
            errors.append("MIN_EDGE_THRESHOLD must be between 0 and 1")
        return errors
