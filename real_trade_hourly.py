"""Real-money trader for Polymarket hourly BTC Up/Down markets.

Trades the bitcoin-up-or-down-{month}-{day}-{year}-{hour}-et markets.

Market structure:
  - Every hour, a market opens: "Will BTC go Up or Down this hour?"
  - "Up" wins if BTC closing price >= opening price for the 1-hour window
  - Resolution via Chainlink BTC/USD data stream
  - Markets pre-created 48 hours ahead
  - AMM + CLOB liquidity: $10k-$1M per market (vs $142k for 5-min)

Edge:
  - Enter at 1-5 minutes into the hour when BTC has moved 0.2%+
  - Early entry catches market before AMM reprices (~0.50 at open, moves fast)
  - Backtest: 5-min entry, 0.2% move -> 80% WR; 0.3% move -> 89% WR
  - Much deeper liquidity than 5-min markets = reliable fills

Usage:
    python real_trade_hourly.py                      # paper mode (default)
    python real_trade_hourly.py --mode live          # real money
    python real_trade_hourly.py --mode live --size 50
"""

import argparse
import asyncio
import json
import math
import os
import signal
import time
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta

import requests
import structlog

from src.config import Config
from src.price_feed import PriceFeed
from db import init_db, save_trade, update_bot_state

log = structlog.get_logger()

TRADES_FILE = "trades/real_trades_hourly.json"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Polymarket uses EST (UTC-5) for slug naming — no DST adjustment
ET_OFFSET_HOURS = -5

# ── ANSI helpers ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def green(s):  return f"{GREEN}{s}{RESET}"
def red(s):    return f"{RED}{s}{RESET}"
def yellow(s): return f"{YELLOW}{s}{RESET}"
def bold(s):   return f"{BOLD}{s}{RESET}"
def _ts():     return datetime.now().strftime("%H:%M:%S")
def _eq(v):    return green(f"+${v:.2f}") if v >= 0 else red(f"-${abs(v):.2f}")

MONTH_NAMES = ['', 'january', 'february', 'march', 'april', 'may', 'june',
               'july', 'august', 'september', 'october', 'november', 'december']


# ── Slug generator ────────────────────────────────────────────────────────────

def make_hourly_slug(utc_ts: int) -> tuple[str, int, int]:
    """
    Given a UTC unix timestamp, return (slug, window_start_utc, window_end_utc)
    for the hourly market currently active.

    Polymarket names markets after their ENDING hour in EST (UTC-5, no DST).
    e.g. 08:30 UTC = 03:30 EST -> current window: 03:00-04:00 EST -> "4am ET"
    """
    # Convert to EST
    est_ts = utc_ts + ET_OFFSET_HOURS * 3600
    # Floor to current EST hour
    est_hour_start = (est_ts // 3600) * 3600
    # Market ends at the next EST hour (this is the slug's named hour)
    est_hour_end = est_hour_start + 3600

    # Convert back to UTC for window boundaries
    window_start_utc = est_hour_start - ET_OFFSET_HOURS * 3600
    window_end_utc   = est_hour_end   - ET_OFFSET_HOURS * 3600

    # Format the slug using the END hour in EST
    # Treat est_hour_end as a naive UTC datetime (it's actually EST unix time)
    dt_end = datetime.utcfromtimestamp(est_hour_end)

    hour24 = dt_end.hour  # 0-23
    if hour24 == 0:
        hour_str = "12am"
    elif hour24 < 12:
        hour_str = f"{hour24}am"
    elif hour24 == 12:
        hour_str = "12pm"
    else:
        hour_str = f"{hour24 - 12}pm"

    slug = (f"bitcoin-up-or-down-"
            f"{MONTH_NAMES[dt_end.month]}-{dt_end.day}-{dt_end.year}-"
            f"{hour_str}-et")

    return slug, window_start_utc, window_end_utc


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class MarketInfo:
    slug: str
    condition_id: str
    question: str
    window_start: int       # UTC unix timestamp (start of hour)
    window_end: int         # UTC unix timestamp (end of hour)
    token_id_up: str
    token_id_down: str
    opening_price: float    # BTC price at window start (from Binance)
    price_up: float         # current AMM/CLOB price for Up
    price_down: float       # current AMM/CLOB price for Down


@dataclass
class Trade:
    opened_at: float
    resolved_at: float
    direction: str
    entry_price: float
    btc_at_entry: float
    btc_at_open: float
    btc_at_close: float
    size: float
    edge: float
    won: bool
    pnl: float
    mode: str
    market_slug: str
    reason: str


@dataclass
class OpenPos:
    market: MarketInfo
    direction: str
    entry_price: float
    btc_at_entry: float
    size: float
    edge: float
    reason: str
    entered_at: float


# ── Market discovery ─────────────────────────────────────────────────────────

def discover_hourly_market(utc_ts: int) -> MarketInfo | None:
    """Fetch the hourly market info for the given UTC timestamp."""
    slug, window_start, window_end = make_hourly_slug(utc_ts)
    try:
        resp = requests.get(
            f"{GAMMA_API}/events",
            params={"slug": slug},
            timeout=10,
        )
        if not resp.ok or not resp.json():
            log.debug("hourly_market_not_found", slug=slug)
            return None

        evt = resp.json()[0]
        m = evt["markets"][0]

        token_ids = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
        outcomes  = json.loads(m["outcomes"])      if isinstance(m["outcomes"], str)      else m["outcomes"]
        prices    = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])

        up_idx   = outcomes.index("Up")   if "Up"   in outcomes else 0
        down_idx = outcomes.index("Down") if "Down" in outcomes else 1

        return MarketInfo(
            slug=slug,
            condition_id=m["conditionId"],
            question=m["question"],
            window_start=window_start,
            window_end=window_end,
            token_id_up=token_ids[up_idx],
            token_id_down=token_ids[down_idx],
            opening_price=0.0,
            price_up=float(prices[up_idx])   if prices else 0.505,
            price_down=float(prices[down_idx]) if prices else 0.495,
        )
    except Exception as e:
        log.warning("hourly_market_discovery_failed", slug=slug, error=str(e))
        return None


def get_market_prices(market: MarketInfo) -> tuple[float, float]:
    """
    Get the best available price for Up and Down tokens.
    Tries CLOB book first; falls back to Gamma API outcomePrices.
    Returns (price_up, price_down).
    """
    def clob_ask(token_id: str) -> float | None:
        try:
            resp = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
            if resp.ok:
                asks = resp.json().get("asks", [])
                if asks:
                    best = min(float(a["price"]) for a in asks)
                    if best < 0.95:  # ignore degenerate 0.99 asks (no real quotes)
                        return best
        except Exception:
            pass
        return None

    up_ask   = clob_ask(market.token_id_up)
    down_ask = clob_ask(market.token_id_down)

    # Fall back to Gamma outcomePrices if CLOB has no real quotes
    if up_ask is None or down_ask is None:
        try:
            resp = requests.get(f"{GAMMA_API}/events", params={"slug": market.slug}, timeout=5)
            if resp.ok and resp.json():
                m = resp.json()[0]["markets"][0]
                prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
                outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                if prices:
                    up_idx   = outcomes.index("Up")   if "Up"   in outcomes else 0
                    down_idx = outcomes.index("Down") if "Down" in outcomes else 1
                    up_ask   = float(prices[up_idx])
                    down_ask = float(prices[down_idx])
        except Exception:
            pass

    return (up_ask or 0.505), (down_ask or 0.495)


# ── Strategy ─────────────────────────────────────────────────────────────────

# Tuned from hourly backtest (30 days, Binance 1-min data):
#   5-min entry, 0.2% move -> 80% WR (156 trades/month, ~5/day)
#   5-min entry, 0.3% move -> 89% WR (65 trades/month, ~2/day)
# We use 0.2% for more frequency, accept 80% WR at <0.60 entry price

MIN_SECS_INTO  = 60     # enter at 1+ minutes into the hour
MAX_SECS_INTO  = 300    # don't enter after 5 minutes (before market reprices)
MIN_MOVE_PCT   = 0.002  # 0.2% minimum BTC move from hourly open
MAX_ENTRY      = 0.70   # cap entry — above 0.70 the payoff asymmetry hurts
MIN_ENTRY      = 0.40   # don't buy cheap contrarian tokens
MIN_EDGE       = 0.05   # 5% minimum edge (higher bar than 5-min due to longer hold)
SLIPPAGE       = 0.03   # 3 cents above AMM price to ensure fill
WINDOW_SECS    = 3600


def evaluate_hourly(
    btc_now: float,
    btc_opening: float,
    market: MarketInfo,
    secs_into_window: float,
) -> tuple[str, float, float, str] | None:
    """
    Decide whether to buy Up or Down for the hourly market.
    Returns (direction, entry_price, edge, reason) or None.
    """
    if btc_opening <= 0 or btc_now <= 0:
        return None

    if secs_into_window < MIN_SECS_INTO or secs_into_window > MAX_SECS_INTO:
        return None

    move_pct = (btc_now - btc_opening) / btc_opening
    if abs(move_pct) < MIN_MOVE_PCT:
        return None

    direction = "Up" if move_pct > 0 else "Down"

    # Estimate probability using normal CDF on z-score of move
    # Hourly BTC vol ~0.3-0.5%, so 0.3% move at 5 min is already ~0.6-1 sigma
    # Use 30-min realized vol scaled to remaining time
    remaining_secs = max(1, WINDOW_SECS - secs_into_window)
    # BTC hourly vol ~= 0.004 (0.4%) per hour, so per second = 0.004/sqrt(3600)
    hourly_vol_per_sec = 0.004 / math.sqrt(3600)
    scaled_vol = hourly_vol_per_sec * math.sqrt(remaining_secs)
    z = abs(move_pct) / max(scaled_vol, 0.0001)
    prob = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    prob = max(0.50, min(0.90, prob))  # cap between 50-90%

    # Get current market price (AMM or CLOB)
    price_up, price_down = get_market_prices(market)

    if direction == "Up":
        ask = price_up
        prob_win = prob
    else:
        ask = price_down
        prob_win = 1.0 - prob

    # Bail early if market has already repriced above our cap
    if ask >= MAX_ENTRY:
        return None

    entry_price = min(ask + SLIPPAGE, MAX_ENTRY)
    edge = prob_win - ask  # edge vs actual market price, not vs our limit

    if entry_price < MIN_ENTRY or entry_price > MAX_ENTRY:
        return None
    if edge < MIN_EDGE:
        return None

    reason = (
        f"btc_move={move_pct:+.4%} from hourly open, "
        f"prob={prob_win:.3f}, ask={ask:.3f}, entry={entry_price:.3f}, "
        f"edge={edge:.3f}, secs_in={secs_into_window:.0f}"
    )

    return direction, entry_price, edge, reason


# ── Trader ───────────────────────────────────────────────────────────────────

class HourlyTrader:
    def __init__(self, config, mode="paper", trade_size=50.0, check_interval=30):
        self.config = config
        self.mode = mode
        self.trade_size = trade_size
        self.check_interval = check_interval
        self._shutdown = False

        self.price_feed = PriceFeed(config.binance, max_history_secs=7200)

        self._use_db = init_db()
        if self._use_db:
            print(f"[{_ts()}] Database connected (PostgreSQL)")
        else:
            print(f"[{_ts()}] No DATABASE_URL — using local JSON file")

        self.open_positions: list[OpenPos] = []
        self.completed_trades: list[Trade] = []
        self.equity = 0.0
        self.peak_equity = 0.0
        self.max_drawdown = 0.0

        # Track one opening price per hour window
        self._window_opens: dict[int, float] = {}    # window_start_utc -> btc_price
        self._window_markets: dict[int, MarketInfo] = {}
        self._traded_windows: set[int] = set()
        self._window_attempts: dict[int, int] = {}

        # Redemption
        self._last_redeem_time: float = 0.0
        self._redeem_interval: float = 600.0

    def _request_shutdown(self):
        self._shutdown = True

    async def run(self):
        mode_str = red(bold("LIVE MONEY")) if self.mode == "live" else green("PAPER")
        print(bold("\n" + "=" * 65))
        print(bold(f"  HOURLY MARKET TRADER  [{mode_str}{BOLD}]"))
        print(bold(f"  Polymarket bitcoin-up-or-down hourly markets"))
        print(bold("=" * 65))
        print(f"  Mode: {self.mode.upper()}  |  Size: ${self.trade_size}")
        print(f"  Entry window: {MIN_SECS_INTO}-{MAX_SECS_INTO}s into each hour")
        print(f"  Min BTC move: {MIN_MOVE_PCT*100:.1f}%  |  Max entry: {MAX_ENTRY}")
        if self.mode == "live":
            print(yellow("  WARNING: Real orders will be placed on Polymarket!"))
        print(bold("=" * 65) + "\n")

        feed_task = asyncio.create_task(self.price_feed.start())

        print(f"[{_ts()}] Connecting to Binance WebSocket...")
        await asyncio.sleep(5)
        warmup_secs = 60
        print(f"[{_ts()}] Warming up ({warmup_secs}s to build price history)...")
        warmup_start = time.time()
        while time.time() - warmup_start < warmup_secs:
            if self.price_feed.has_data:
                remaining = warmup_secs - (time.time() - warmup_start)
                price = self.price_feed.current_price
                print(f"\r[{_ts()}]  BTC ${price:,.2f}  |  warmup {remaining:.0f}s   ",
                      end="", flush=True)
            await asyncio.sleep(5)
        print()

        # Pre-fetch current hour market
        now_ts = int(time.time())
        slug, w_start, w_end = make_hourly_slug(now_ts)
        print(f"[{_ts()}] Current hour market: {slug}")
        print(f"[{_ts()}]   Window: {datetime.utcfromtimestamp(w_start).strftime('%H:%M')} - "
              f"{datetime.utcfromtimestamp(w_end).strftime('%H:%M')} UTC")
        self._prefetch_market(now_ts)

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                pass

        try:
            while not self._shutdown:
                now = time.time()
                now_ts = int(now)

                _, window_start, window_end = make_hourly_slug(now_ts)
                secs_into = now - window_start

                # Record opening price at hour start
                if window_start not in self._window_opens and self.price_feed.has_data:
                    self._window_opens[window_start] = self.price_feed.current_price
                    self._prefetch_market(now_ts)
                    # Also prefetch next hour
                    self._prefetch_market(now_ts + 3600)
                    slug, _, _ = make_hourly_slug(now_ts)
                    print(f"\n[{_ts()}] Hour started: {slug}")
                    print(f"[{_ts()}]   BTC open = ${self._window_opens[window_start]:,.2f}")

                # Check for entry (only once per hour, max 2 tries)
                if (window_start not in self._traded_windows
                        and window_start in self._window_opens
                        and self._window_attempts.get(window_start, 0) < 2
                        and self.price_feed.has_data):
                    await self._check_entry(window_start, window_end, secs_into)

                # Resolve completed positions
                await self._resolve_windows(now)

                # Status line
                self._print_status(window_start, secs_into)

                await asyncio.sleep(self.check_interval)

        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            print(f"\n[{_ts()}] Shutting down...")
            self.price_feed.stop()
            feed_task.cancel()
            self._save_state()
            self._print_summary()

    def _prefetch_market(self, utc_ts: int):
        _, window_start, _ = make_hourly_slug(utc_ts)
        if window_start in self._window_markets:
            return
        market = discover_hourly_market(utc_ts)
        if market:
            self._window_markets[window_start] = market
            log.debug("hourly_market_prefetched", slug=market.slug)

    async def _check_entry(self, window_start: int, window_end: int, secs_into: float):
        btc_now = self.price_feed.current_price
        btc_opening = self._window_opens.get(window_start, 0)
        market = self._window_markets.get(window_start)

        if not market or btc_opening <= 0:
            return

        market.opening_price = btc_opening

        result = evaluate_hourly(btc_now, btc_opening, market, secs_into)
        if not result:
            return

        direction, entry_price, edge, reason = result

        self._traded_windows.add(window_start)

        colour = green if direction == "Up" else red
        print(f"\n[{_ts()}] {'-'*58}")
        print(f"[{_ts()}] {colour(bold(f'>> ENTER {direction.upper()}'))}  "
              f"entry={entry_price:.3f}  edge={edge:+.3f}")
        print(f"[{_ts()}]   {reason[:90]}")
        print(f"[{_ts()}]   Market: {market.slug}")

        pos = OpenPos(
            market=market,
            direction=direction,
            entry_price=entry_price,
            btc_at_entry=btc_now,
            size=self.trade_size,
            edge=edge,
            reason=reason,
            entered_at=time.time(),
        )

        if self.mode == "live":
            if not self._place_order(pos):
                self._traded_windows.discard(window_start)
                attempts = self._window_attempts.get(window_start, 0) + 1
                self._window_attempts[window_start] = attempts
                remaining = 2 - attempts
                if remaining > 0:
                    print(f"[{_ts()}]   Order failed — will retry ({remaining} attempt(s) left)")
                else:
                    print(f"[{_ts()}]   Order failed — max retries, skipping this hour")
                return

        self.open_positions.append(pos)

    def _get_clob_client(self):
        if not getattr(self, '_clob_client', None):
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            import httpx
            import py_clob_client.http_helpers.helpers as _clob_http
            _clob_http._http_client = httpx.Client(http2=True, timeout=5.0)

            proxy_addr = self.config.polymarket.proxy_address

            for sig_type, sig_name in [(1, "POLY_PROXY"), (0, "EOA")]:
                kwargs = dict(key=self.config.polymarket.private_key, chain_id=137, signature_type=sig_type)
                if sig_type in (1, 2) and proxy_addr:
                    kwargs['funder'] = proxy_addr

                client = ClobClient(CLOB_API, **kwargs)
                addr = client.signer.address()
                funder = client.builder.funder
                print(f"[{_ts()}]   Trying {sig_name}: signer={addr}, funder={funder}")

                try:
                    creds = client.derive_api_key(nonce=0)
                    if creds:
                        client.set_api_creds(creds)
                        print(f"[{_ts()}]   Derived API key: {creds.api_key[:16]}...")
                    else:
                        creds = client.create_or_derive_api_creds()
                        client.set_api_creds(creds)
                except Exception:
                    creds = client.create_or_derive_api_creds()
                    client.set_api_creds(creds)

                try:
                    bal_info = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                    balance = float(bal_info.get('balance', 0)) if isinstance(bal_info, dict) else 0
                    if balance > 0:
                        print(f"[{_ts()}]   Balance: ${balance/1e6:.2f} with {sig_name}")
                        self._clob_client = client
                        break
                except Exception as e:
                    print(f"[{_ts()}]   Balance check: {e}")

            if not getattr(self, '_clob_client', None):
                self._clob_client = client

            try:
                self._clob_client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                print(f"[{_ts()}]   Allowance set")
            except Exception as e:
                print(f"[{_ts()}]   Allowance note: {e}")
            print(f"[{_ts()}]   CLOB client initialized")
        return self._clob_client

    def _place_order(self, pos: OpenPos) -> bool:
        token_id = pos.market.token_id_up if pos.direction == "Up" else pos.market.token_id_down
        size = pos.size

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            client = self._get_clob_client()

            # Get fresh price — prefer CLOB ask, fall back to Gamma outcomePrices
            price_up, price_down = get_market_prices(pos.market)
            market_price = price_up if pos.direction == "Up" else price_down

            price = round(min(market_price + SLIPPAGE, MAX_ENTRY), 2)
            if price >= MAX_ENTRY:
                print(f"[{_ts()}]   Market price too high: {market_price:.2f} — skipping")
                return False

            print(f"[{_ts()}]   {red(bold('PLACING REAL ORDER'))}: "
                  f"BUY {pos.direction} @ {price:.2f} (mkt={market_price:.2f}+3c) x ${size:.2f}")

            order_args = OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
            signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order, orderType=OrderType.FOK)
            print(f"[{_ts()}]   Order response: {resp}")

            status  = resp.get('status', '')   if isinstance(resp, dict) else ''
            success = resp.get('success', False) if isinstance(resp, dict) else False

            if not success or status != 'matched':
                order_id = resp.get('orderID', '') if isinstance(resp, dict) else ''
                if order_id and status == 'live':
                    try:
                        client.cancel(order_id)
                        print(f"[{_ts()}]   {red('ORDER NOT FILLED: status=live — cancelled')}")
                    except Exception as ce:
                        print(f"[{_ts()}]   {red(f'ORDER NOT FILLED: cancel failed: {ce}')}")
                else:
                    print(f"[{_ts()}]   {red(f'ORDER NOT FILLED: status={status}')}")
                return False

            log.info("order_placed", direction=pos.direction, price=price, size=size)
            return True

        except Exception as e:
            print(f"[{_ts()}]   {red(f'ORDER FAILED: {e}')}")
            return False

    async def _resolve_windows(self, now: float):
        still_open = []
        for pos in self.open_positions:
            if now < pos.market.window_end:
                still_open.append(pos)
                continue
            if not self.price_feed.has_data:
                still_open.append(pos)
                continue

            btc_close = self.price_feed.current_price
            btc_open  = pos.market.opening_price

            actual_result = "Up" if btc_close >= btc_open else "Down"
            won = (pos.direction == actual_result)
            pnl = (1.0 - pos.entry_price) * pos.size if won else -pos.entry_price * pos.size

            self.equity += pnl
            if self.equity > self.peak_equity:
                self.peak_equity = self.equity
            dd = self.peak_equity - self.equity
            if dd > self.max_drawdown:
                self.max_drawdown = dd

            trade = Trade(
                opened_at=pos.entered_at,
                resolved_at=now,
                direction=pos.direction,
                entry_price=pos.entry_price,
                btc_at_entry=pos.btc_at_entry,
                btc_at_open=btc_open,
                btc_at_close=btc_close,
                size=pos.size,
                edge=pos.edge,
                won=won,
                pnl=pnl,
                mode=self.mode,
                market_slug=pos.market.slug,
                reason=pos.reason,
            )
            self.completed_trades.append(trade)

            if self._use_db:
                save_trade(trade)
                update_bot_state(self.equity, self.peak_equity, self.max_drawdown, self.mode)

            result_str = green("WON") if won else red("LOST")
            move = (btc_close - btc_open) / btc_open * 100
            print(f"\n[{_ts()}] {'-'*58}")
            print(f"[{_ts()}] RESOLVED: {pos.direction}  Result={actual_result}  BTC={move:+.3f}%")
            print(f"[{_ts()}]   Open=${btc_open:,.2f} -> Close=${btc_close:,.2f}")
            print(f"[{_ts()}]   {result_str}  PnL={_eq(pnl)}  Equity={_eq(self.equity)}")

            if self.mode == "live":
                # Queue for next batch redeem cycle
                pass

            self._save_state()

        self.open_positions = still_open

        # Batch redeem every 10 minutes
        if (self.mode == "live"
                and time.time() - self._last_redeem_time > self._redeem_interval):
            self._batch_redeem()

    def _batch_redeem(self):
        """Scan blockchain for unredeemed hourly market tokens and redeem them."""
        self._last_redeem_time = time.time()
        print(f"\n[{_ts()}] Running hourly batch redeem scan (last 6 hours)...")
        try:
            import requests as _req
            from web3 import Web3

            w3   = Web3(Web3.HTTPProvider('https://polygon-bor-rpc.publicnode.com'))
            acct = w3.eth.account.from_key(self.config.polymarket.private_key)
            addr = acct.address

            CT     = Web3.to_checksum_address('0x4D97DCd97eC945f40cF65F87097ACe5EA0476045')
            USDC_E = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
            ct_abi = [
                {'inputs':[{'name':'account','type':'address'},{'name':'id','type':'uint256'}],'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'type':'function'},
                {'inputs':[{'name':'','type':'bytes32'}],'name':'payoutDenominator','outputs':[{'name':'','type':'uint256'}],'type':'function'},
                {'inputs':[{'name':'','type':'bytes32'},{'name':'','type':'uint256'}],'name':'payoutNumerators','outputs':[{'name':'','type':'uint256'}],'type':'function'},
                {'inputs':[{'name':'collateralToken','type':'address'},{'name':'parentCollectionId','type':'bytes32'},{'name':'conditionId','type':'bytes32'},{'name':'indexSets','type':'uint256[]'}],'name':'redeemPositions','outputs':[],'type':'function'},
            ]
            ct = w3.eth.contract(address=CT, abi=ct_abi)

            # Scan last 6 hourly markets (allow 30 min after close for oracle)
            now_ts    = int(time.time())
            to_redeem = []

            for hours_ago in range(1, 7):
                scan_ts = now_ts - hours_ago * 3600
                _, w_start, w_end = make_hourly_slug(scan_ts)
                # Skip if window ended less than 30 min ago (oracle delay)
                if now_ts - w_end < 1800:
                    continue
                slug, _, _ = make_hourly_slug(scan_ts)
                try:
                    r = _req.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=5)
                    events = r.json()
                    if not events:
                        continue
                    for m in events[0].get("markets", []):
                        cid = m.get("conditionId", "")
                        if not cid:
                            continue
                        token_ids = json.loads(m.get("clobTokenIds", "[]"))
                        cid_bytes = bytes.fromhex(cid[2:] if cid.startswith('0x') else cid)
                        payout_denom = ct.functions.payoutDenominator(cid_bytes).call()
                        if payout_denom == 0:
                            continue  # not resolved yet
                        for outcome_idx, tid in enumerate(token_ids):
                            bal = ct.functions.balanceOf(addr, int(tid)).call()
                            if bal > 0:
                                payout_num = ct.functions.payoutNumerators(cid_bytes, outcome_idx).call()
                                if payout_num > 0:
                                    index_set = 1 << outcome_idx
                                    print(f"[{_ts()}]   Redeem candidate: {slug} (bal={bal}, indexSet={index_set})")
                                    to_redeem.append((slug, cid, index_set))
                                    break
                except Exception as e:
                    log.debug("redeem_scan_error", slug=slug, error=str(e))

            print(f"[{_ts()}] Scan done: {len(to_redeem)} to redeem")
            if not to_redeem:
                return

            usdc_abi = [{'inputs':[{'name':'account','type':'address'}],'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'type':'function'}]
            usdc      = w3.eth.contract(address=USDC_E, abi=usdc_abi)
            bal_before = usdc.functions.balanceOf(addr).call() / 1e6
            print(f"[{_ts()}] Wallet BEFORE: ${bal_before:.2f}")

            gas_price = int(w3.eth.gas_price * 1.5)
            nonce     = w3.eth.get_transaction_count(addr)
            redeemed  = 0

            for i, (slug, condition_id, index_set) in enumerate(to_redeem):
                try:
                    cid_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith('0x') else condition_id)
                    tx = ct.functions.redeemPositions(
                        USDC_E, b'\x00'*32, cid_bytes, [index_set]
                    ).build_transaction({
                        'from': addr, 'nonce': nonce + i, 'gas': 200000,
                        'gasPrice': gas_price, 'chainId': 137,
                    })
                    signed   = acct.sign_transaction(tx)
                    tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
                    receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                    if receipt.status == 1:
                        redeemed += 1
                        log.info("redeem_tx", slug=slug, tx=tx_hash.hex()[:16])
                except Exception as e:
                    log.warning("redeem_tx_error", slug=slug, error=str(e))

            bal_after = usdc.functions.balanceOf(addr).call() / 1e6
            gained    = bal_after - bal_before
            print(f"[{_ts()}] Redeemed {redeemed}/{len(to_redeem)}  Wallet: ${bal_after:.2f} (+${gained:.2f})")

        except Exception as e:
            print(f"[{_ts()}] Batch redeem error: {e}")

    def _print_status(self, window_start: int, secs_into: float):
        if not self.price_feed.has_data:
            return
        price    = self.price_feed.current_price
        btc_open = self._window_opens.get(window_start, price)
        move     = (price - btc_open) / btc_open * 100 if btc_open else 0
        n        = len(self.completed_trades)
        wins     = sum(1 for t in self.completed_trades if t.won)
        wr       = wins / n if n else 0
        traded   = "TRADED" if window_start in self._traded_windows else "watching"
        remaining = max(0, WINDOW_SECS - secs_into)
        mins, secs = divmod(int(remaining), 60)

        print(
            f"\r[{_ts()}]  BTC ${price:,.2f} ({move:+.3f}%)"
            f"  |  Hour: {mins}m{secs:02d}s left [{traded}]"
            f"  |  Trades: {n} (W:{wins} L:{n-wins})"
            f"  |  WR: {wr:.0%}"
            f"  |  {_eq(self.equity)}"
            f"     ",
            end="", flush=True
        )

    def _save_state(self):
        state = {
            "updated_at": time.time(),
            "mode": self.mode,
            "equity": self.equity,
            "trades": [asdict(t) for t in self.completed_trades],
        }
        for t in state["trades"]:
            t.pop("market", None)
        try:
            os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
            with open(TRADES_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.warning("save_state_failed", error=str(e))

    def _print_summary(self):
        trades = self.completed_trades
        n = len(trades)
        print("\n")
        print(bold("=" * 65))
        print(bold(f"  HOURLY TRADING SUMMARY  [{self.mode.upper()}]"))
        print(bold("=" * 65))
        print(f"  Total trades: {n}")
        if n == 0:
            print(bold("=" * 65))
            return
        wins = sum(1 for t in trades if t.won)
        print(f"  Wins / Losses:   {wins} / {n - wins}")
        print(f"  Win rate:        {wins/n:.1%}")
        print(f"  Total PnL:       {_eq(self.equity)}")
        print(f"  Avg PnL/trade:   {_eq(self.equity / n)}")
        print(f"  Max drawdown:    ${self.max_drawdown:.2f}")
        for t in trades[-10:]:
            dt  = datetime.fromtimestamp(t.opened_at).strftime("%H:%M")
            sym = green("W") if t.won else red("L")
            print(f"    {dt}  {t.direction:4s}  entry={t.entry_price:.3f}  "
                  f"edge={t.edge:+.3f}  {sym}  {_eq(t.pnl)}")
        print(bold("=" * 65))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Trade Polymarket BTC Up/Down hourly markets")
    p.add_argument("--mode",  choices=["paper", "live"], default="paper")
    p.add_argument("--size",  type=float, default=50.0, help="Trade size in USDC (default $50)")
    p.add_argument("--check", type=int,   default=30,   help="Check interval in seconds (default 30)")
    args = p.parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING)

    config = Config()

    if args.mode == "live":
        if not config.polymarket.private_key:
            print("ERROR: POLYMARKET_PRIVATE_KEY required for live mode")
            sys.exit(1)
        print(bold(red("\n  *** LIVE MONEY MODE ***")))
        print(bold(red(f"  Trade size: ${args.size}")))
        print(bold(red("  Real orders will be placed on Polymarket!\n")))

    trader = HourlyTrader(config=config, mode=args.mode, trade_size=args.size, check_interval=args.check)

    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # Quick slug test
    if "--test-slug" in sys.argv:
        import time as t
        now = int(t.time())
        for offset in range(0, 5):
            ts = now + offset * 3600
            slug, start, end = make_hourly_slug(ts)
            print(f"  UTC {datetime.utcfromtimestamp(ts).strftime('%H:%M')} -> {slug}")
            print(f"    window: {datetime.utcfromtimestamp(start).strftime('%H:%M')} - "
                  f"{datetime.utcfromtimestamp(end).strftime('%H:%M')} UTC")
        sys.exit(0)

    main()
