"""Backtest runner: fetch data and run strategy simulations.

Usage:
    python backtest.py                    # Default: 24h, all strategies
    python backtest.py --hours 72         # 3 days of data
    python backtest.py --hours 168        # 1 week
    python backtest.py --edge 0.03        # Higher edge threshold
    python backtest.py --strike-offset 0.001  # Slightly OTM markets
"""

import argparse
import os
import sys

import structlog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import Config
from src.data_fetcher import fetch_kline_history, klines_to_trades
from src.backtester import Backtester, BacktestResult


def setup_logging():
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def run_parameter_sweep(trades: list[dict], config: Config, args=None) -> dict[str, BacktestResult]:
    """Run backtests with different parameter combinations to find optimal settings."""
    results = {}

    # Sweep edge thresholds
    for edge in [0.01, 0.02, 0.03, 0.05]:
        config.trading.min_edge_threshold = edge
        bt = Backtester(config=config, simulated_spread=0.03)
        result = bt.run(trades)
        key = f"edge={edge}"
        results[key] = result
        print(f"\n--- {key} ---")
        print(f"  Trades: {result.total_trades}  Win rate: {result.win_rate:.1%}  PnL: ${result.total_pnl:.2f}")

    # Sweep market intervals
    config.trading.min_edge_threshold = 0.02  # Reset
    for interval in [180, 300, 600]:
        bt = Backtester(config=config, market_interval_secs=interval, simulated_spread=0.03)
        result = bt.run(trades)
        key = f"interval={interval}s"
        results[key] = result
        print(f"\n--- {key} ---")
        print(f"  Trades: {result.total_trades}  Win rate: {result.win_rate:.1%}  PnL: ${result.total_pnl:.2f}")

    # Sweep strike offsets (ATM vs OTM)
    for offset in [0.0, 0.001, -0.001, 0.002]:
        bt = Backtester(config=config, strike_offset_pct=offset, simulated_spread=0.03)
        result = bt.run(trades)
        key = f"offset={offset:+.3f}"
        results[key] = result
        print(f"\n--- {key} ---")
        print(f"  Trades: {result.total_trades}  Win rate: {result.win_rate:.1%}  PnL: ${result.total_pnl:.2f}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Backtest BTC 5-min prediction strategies")
    parser.add_argument("--hours", type=int, default=24, help="Hours of historical data to fetch")
    parser.add_argument("--edge", type=float, default=0.02, help="Minimum edge threshold")
    parser.add_argument("--strike-offset", type=float, default=0.0, help="Strike offset percentage")
    parser.add_argument("--spread", type=float, default=0.03, help="Simulated market spread")
    parser.add_argument("--interval", type=int, default=300, help="Market interval in seconds")
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--no-cache", action="store_true", help="Don't use cached data")
    args = parser.parse_args()

    setup_logging()
    log = structlog.get_logger()

    print("=" * 60)
    print("  Bitcoin 5-Min Polymarket Strategy Backtester")
    print("=" * 60)
    print()

    # Fetch historical data (klines = 2 API calls vs 220+ for raw trades)
    print(f"Fetching {args.hours}h of BTC 1-min klines from Binance...")
    klines = fetch_kline_history(hours=args.hours, interval="1m", cache=not args.no_cache)
    trades = klines_to_trades(klines)
    print(f"Loaded {len(klines):,} candles -> {len(trades):,} synthetic ticks")

    if not trades:
        print("ERROR: No trade data available. Check your internet connection.")
        sys.exit(1)

    # Show data range
    from datetime import datetime
    start = datetime.fromtimestamp(trades[0]["timestamp"])
    end = datetime.fromtimestamp(trades[-1]["timestamp"])
    print(f"Data range: {start} -> {end}")
    print(f"Price range: ${min(t['price'] for t in trades):,.2f} - ${max(t['price'] for t in trades):,.2f}")
    print()

    config = Config()
    config.trading.min_edge_threshold = args.edge

    if args.sweep:
        print("Running parameter sweep...")
        results = run_parameter_sweep(trades, config)
        print("\n\n" + "=" * 60)
        print("  PARAMETER SWEEP SUMMARY")
        print("=" * 60)
        best_key = max(results, key=lambda k: results[k].total_pnl)
        print(f"\n  Best config: {best_key}")
        print(results[best_key].summary())
    else:
        # Single backtest
        bt = Backtester(
            config=config,
            market_interval_secs=args.interval,
            strike_offset_pct=args.strike_offset,
            simulated_spread=args.spread,
        )
        result = bt.run(trades)
        print(result.summary())

        # Show recent trades
        if result.trades:
            print("\nLast 10 trades:")
            for t in result.trades[-10:]:
                icon = "W" if t.won else "L"
                print(
                    f"  [{icon}] {t.strategy:18s} {t.direction:3s}  "
                    f"strike=${t.strike_price:>10,.2f}  "
                    f"entry={t.entry_price:.3f}  "
                    f"edge={t.edge:.4f}  "
                    f"pnl=${t.pnl:>+7.2f}"
                )


if __name__ == "__main__":
    main()
