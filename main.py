"""Entry point for the Bitcoin 5-Minute Polymarket Trading Bot."""

import asyncio
import signal
import sys
import os
import structlog

# Ensure src is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import Config
from src.bot import TradingBot


def setup_logging(log_level: str, log_file: str):
    """Configure structured logging."""
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def run_bot():
    """Initialize and run the trading bot."""
    config = Config()

    setup_logging(config.log_level, config.log_file)
    log = structlog.get_logger()

    # Validate configuration
    errors = config.validate()
    if errors:
        for err in errors:
            log.error("config_error", message=err)
        log.error("fix_config", hint="Copy .env.example to .env and fill in your credentials")
        sys.exit(1)

    log.info(
        "config_loaded",
        max_position=config.trading.max_position_size,
        max_positions=config.trading.max_open_positions,
        min_edge=config.trading.min_edge_threshold,
        strategies=["momentum", "mean_reversion", "volatility"],
    )

    bot = TradingBot(config)

    # Graceful shutdown on Ctrl+C
    loop = asyncio.get_event_loop()

    def shutdown_handler():
        log.info("shutdown_signal_received")
        asyncio.ensure_future(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await bot.start()
    except KeyboardInterrupt:
        log.info("keyboard_interrupt")
        await bot.stop()


def main():
    print("=" * 60)
    print("  Bitcoin 5-Minute Polymarket Trading Bot")
    print("=" * 60)
    print()
    print("Strategies: momentum | mean_reversion | volatility")
    print("Press Ctrl+C to stop")
    print()

    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
