"""Real-money trader for Polymarket 5-minute BTC Up/Down markets.

Trades the btc-updown-5m-{timestamp} markets on Polymarket using real
Binance price feed to identify when the market is mispriced.

Market structure:
  - Every 5 minutes, a new market opens: "Will BTC go Up or Down?"
  - "Up" wins if BTC closing price >= opening price for the 5-min window
  - "Down" wins if BTC closing price < opening price
  - Resolution via Chainlink BTC/USD data stream
  - Markets pre-created ~8 hours ahead

Edge:
  - Markets are always-ATM (opening price = strike), priced near 50/50
  - Thin volume (~$300-1500/market) means prices can be stale
  - We monitor real-time BTC via Binance, enter when price has moved
    significantly from the window's opening price but market hasn't repriced
  - Enter 60-180s into the window (enough signal, enough time to resolve)

Usage:
    python real_trade.py                      # paper mode (default)
    python real_trade.py --mode live          # real money
    python real_trade.py --mode live --size 50
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
from datetime import datetime

import requests
import structlog

from src.config import Config
from src.price_feed import PriceFeed
from db import init_db, save_trade, update_bot_state

log = structlog.get_logger()

TRADES_FILE = "real_trades.json"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

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


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class MarketInfo:
    """A Polymarket 5-min BTC Up/Down market."""
    slug: str
    condition_id: str
    question: str
    window_start: int       # unix timestamp
    window_end: int
    token_id_up: str
    token_id_down: str
    opening_price: float    # BTC price at window start (from Binance)
    price_up: float         # current market price for Up
    price_down: float       # current market price for Down


@dataclass
class Trade:
    opened_at: float
    resolved_at: float
    direction: str          # "Up" or "Down"
    entry_price: float
    btc_at_entry: float
    btc_at_open: float      # BTC at window start
    btc_at_close: float     # BTC at window end
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

def discover_market(window_start_ts: int) -> MarketInfo | None:
    """Fetch market info from Polymarket Gamma API for a given 5-min window."""
    slug = f"btc-updown-5m-{window_start_ts}"
    try:
        resp = requests.get(
            f"{GAMMA_API}/events",
            params={"slug": slug},
            timeout=10,
        )
        if not resp.ok or not resp.json():
            return None

        evt = resp.json()[0]
        m = evt["markets"][0]

        # Parse token IDs and outcomes
        token_ids = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
        outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
        prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])

        # Map Up/Down to token IDs
        up_idx = outcomes.index("Up") if "Up" in outcomes else 0
        down_idx = outcomes.index("Down") if "Down" in outcomes else 1

        return MarketInfo(
            slug=slug,
            condition_id=m["conditionId"],
            question=m["question"],
            window_start=window_start_ts,
            window_end=window_start_ts + 300,
            token_id_up=token_ids[up_idx],
            token_id_down=token_ids[down_idx],
            opening_price=0.0,  # filled by trader from Binance
            price_up=float(prices[up_idx]) if prices else 0.505,
            price_down=float(prices[down_idx]) if prices else 0.495,
        )
    except Exception as e:
        log.warning("market_discovery_failed", slug=slug, error=str(e))
        return None


def get_best_prices(token_id: str) -> tuple[float, float, float]:
    """Get best bid, best ask, and ask size for a token. Returns (bid, ask, ask_size)."""
    try:
        resp = requests.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=5,
        )
        if resp.ok:
            book = resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = max((float(b["price"]) for b in bids), default=0.0)
            best_ask_entry = min(asks, key=lambda a: float(a["price"]), default=None)
            if best_ask_entry:
                return best_bid, float(best_ask_entry["price"]), float(best_ask_entry["size"])
            return best_bid, 1.0, 0.0
    except Exception:
        pass
    return 0.0, 1.0, 0.0


# ── Strategy ─────────────────────────────────────────────────────────────────

def evaluate_updown(
    btc_now: float,
    btc_opening: float,
    market: MarketInfo,
    secs_into_window: float,
    realized_vol: float | None,
) -> tuple[str, float, float, str] | None:
    """
    Decide whether to buy Up or Down.

    Returns (direction, entry_price, edge, reason) or None.

    Strategy:
      1. Calculate how far BTC has moved from opening price
      2. Estimate probability of Up based on current distance + realized vol
      3. Compare to market prices (best ask)
      4. Buy if edge > threshold
    """
    if btc_opening <= 0 or btc_now <= 0:
        return None

    # How far BTC has moved from opening (positive = above opening = favors Up)
    move_pct = (btc_now - btc_opening) / btc_opening

    # Time remaining in window
    secs_remaining = max(1, 300 - secs_into_window)

    # Use realized vol to estimate probability of Up at close
    # If BTC is currently above opening, it needs to STAY above (or go higher)
    # If BTC is currently below opening, it needs to CROSS back above
    if realized_vol and realized_vol > 0:
        # Scale vol to remaining time
        scaled_vol = realized_vol * math.sqrt(secs_remaining)
        if scaled_vol > 0:
            # z = how many vol-units above/below opening
            z = move_pct / scaled_vol
            prob_up = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        else:
            prob_up = 1.0 if move_pct > 0 else 0.0
    else:
        # Fallback: simple momentum estimate
        # BTC above opening → >50% chance of Up, scaled by how far
        # Typical 5-min BTC move is ~0.05-0.1%, so 0.1% is significant
        prob_up = 0.5 + (move_pct / 0.002)  # ~0.002 = 2x typical move
        prob_up = max(0.05, min(0.95, prob_up))

    # Cap probability estimate at 0.85 — our model is overconfident at extremes
    # (55-trade data: we claimed 95% probability but only hit ~57% on those trades)
    prob_up = max(0.15, min(0.85, prob_up))
    prob_down = 1.0 - prob_up

    # Get real market prices (best ask = what we'd pay to buy)
    bid_up, ask_up, size_up = get_best_prices(market.token_id_up)
    bid_down, ask_down, size_down = get_best_prices(market.token_id_down)

    # Edge = our probability - price we'd pay
    edge_up = prob_up - ask_up
    edge_down = prob_down - ask_down

    # Pick best direction
    if edge_up > edge_down and edge_up > 0:
        direction = "Up"
        entry_price = ask_up
        edge = edge_up
        available = size_up
    elif edge_down > 0:
        direction = "Down"
        entry_price = ask_down
        edge = edge_down
        available = size_down
    else:
        return None

    # Minimum thresholds — tuned from 65-trade paper run analysis:
    #   Entry >= 0.70: lost money (terrible payoff asymmetry, win $10 lose $40)
    #   Entry 0.40-0.65: THE SWEET SPOT — 70% WR, +$7.67/trade
    #   Entry < 0.40: contrarian bets, 0-30% WR, almost always lose
    # ONLY trade the sweet spot.
    MIN_EDGE = 0.03          # 3% minimum edge (covers spread + buffer)
    MIN_ENTRY = 0.35         # Don't buy cheap contrarian bets — they lose
    MAX_ENTRY = 0.65         # Don't pay too much — loss asymmetry kills us
    MIN_SECS_INTO = 90       # wait 90s into window (more signal, less noise)
    MAX_SECS_INTO = 240      # don't enter in last 60s (not enough time)
    MIN_MOVE_PCT = 0.0005    # BTC must move at least 0.05% — filters tiny moves where Binance/Chainlink diverge
    SLIPPAGE = 0.02          # Pay up to 2 cents above ask for immediate fill

    # Add slippage buffer so order crosses the spread and gets matched immediately
    entry_price = min(entry_price + SLIPPAGE, MAX_ENTRY)
    edge = edge - SLIPPAGE   # Recalculate edge after slippage

    if edge < MIN_EDGE:
        return None
    if entry_price < MIN_ENTRY or entry_price > MAX_ENTRY:
        return None
    if secs_into_window < MIN_SECS_INTO:
        return None
    if secs_into_window > MAX_SECS_INTO:
        return None
    if abs(move_pct) < MIN_MOVE_PCT:
        return None

    reason = (
        f"btc_move={move_pct:+.4%} from open, "
        f"prob_{direction.lower()}={prob_up if direction=='Up' else prob_down:.3f}, "
        f"ask={entry_price:.3f}, edge={edge:.3f}, "
        f"vol={'%.5f' % realized_vol if realized_vol else 'N/A'}, "
        f"secs_in={secs_into_window:.0f}, avail=${available:.0f}"
    )

    return direction, entry_price, edge, reason


# ── Trader ───────────────────────────────────────────────────────────────────

class RealTrader:
    def __init__(self, config, mode="paper", trade_size=50.0,
                 check_interval=15):
        self.config = config
        self.mode = mode
        self.trade_size = trade_size
        self.check_interval = check_interval  # how often to check within a window
        self._shutdown = False

        # Price feed
        self.price_feed = PriceFeed(config.binance, max_history_secs=900)

        # Database
        self._use_db = init_db()
        if self._use_db:
            print(f"[{_ts()}] Database connected (PostgreSQL)")
        else:
            print(f"[{_ts()}] No DATABASE_URL — using local JSON file")

        # State
        self.open_positions: list[OpenPos] = []
        self.completed_trades: list[Trade] = []
        self.equity = 0.0
        self.peak_equity = 0.0
        self.max_drawdown = 0.0

        # Track opening prices per window
        self._window_opens: dict[int, float] = {}  # window_start -> btc_price
        self._window_markets: dict[int, MarketInfo] = {}
        self._traded_windows: set[int] = set()  # windows we've already traded
        self._window_attempts: dict[int, int] = {}  # failed attempts per window

        # Redemption queue — batch redeem every 10 minutes
        self._pending_redeems: list[str] = []  # condition_ids to redeem
        self._last_redeem_time: float = 0.0
        self._redeem_interval: float = 600.0  # 10 minutes


    def _request_shutdown(self):
        self._shutdown = True


    async def run(self):
        mode_str = red(bold("LIVE MONEY")) if self.mode == "live" else green("PAPER")
        print(bold("\n" + "=" * 65))
        print(bold(f"  REAL MARKET TRADER  [{mode_str}{BOLD}]"))
        print(bold(f"  Polymarket btc-updown-5m markets"))
        print(bold("=" * 65))
        print(f"  Mode: {self.mode.upper()}  |  Size: ${self.trade_size}")
        print(f"  Check interval: {self.check_interval}s within each window")
        if self.mode == "live":
            print(yellow("  WARNING: Real orders will be placed on Polymarket!"))
        print(bold("=" * 65) + "\n")

        # Start price feed
        feed_task = asyncio.create_task(self.price_feed.start())

        # Warmup
        print(f"[{_ts()}] Connecting to Binance WebSocket...")
        await asyncio.sleep(5)
        warmup_secs = 60
        print(f"[{_ts()}] Warming up ({warmup_secs}s)...")
        warmup_start = time.time()
        while time.time() - warmup_start < warmup_secs:
            if self.price_feed.has_data:
                remaining = warmup_secs - (time.time() - warmup_start)
                price = self.price_feed.current_price
                print(f"\r[{_ts()}]  BTC ${price:,.2f}  |  warmup {remaining:.0f}s   ",
                      end="", flush=True)
            await asyncio.sleep(5)
        print()

        # Pre-fetch the next market
        now_ts = int(time.time())
        current_window = (now_ts // 300) * 300
        self._prefetch_market(current_window)
        next_window = current_window + 300
        self._prefetch_market(next_window)

        print(f"[{_ts()}] Ready. Current window: {datetime.fromtimestamp(current_window).strftime('%H:%M:%S')}")
        print(f"[{_ts()}] Monitoring for entry opportunities...\n")

        # Signal handling
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
                current_window = (now_ts // 300) * 300
                secs_into = now - current_window

                # Record opening price at window start
                if current_window not in self._window_opens and self.price_feed.has_data:
                    self._window_opens[current_window] = self.price_feed.current_price
                    self._prefetch_market(current_window)
                    # Also prefetch next window
                    self._prefetch_market(current_window + 300)
                    print(f"\n[{_ts()}] Window started: {datetime.fromtimestamp(current_window).strftime('%H:%M:%S')}"
                          f"  Opening BTC=${self._window_opens[current_window]:,.2f}")

                # Check for entry opportunity in current window (max 2 failed attempts)
                if (current_window not in self._traded_windows
                        and current_window in self._window_opens
                        and self._window_attempts.get(current_window, 0) < 2
                        and self.price_feed.has_data):
                    await self._check_entry(current_window, secs_into)

                # Resolve completed windows
                await self._resolve_windows(now)

                # Status
                self._print_status(current_window, secs_into)

                await asyncio.sleep(self.check_interval)

        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            print(f"\n[{_ts()}] Shutting down...")
            self.price_feed.stop()
            feed_task.cancel()
            self._save_state()
            self._print_summary()

    def _prefetch_market(self, window_start: int):
        """Fetch market info from Polymarket if not already cached."""
        if window_start in self._window_markets:
            return
        market = discover_market(window_start)
        if market:
            self._window_markets[window_start] = market
            log.debug("market_prefetched", slug=market.slug)

    async def _check_entry(self, window_start: int, secs_into: float):
        """Evaluate entry opportunity in the current window."""
        btc_now = self.price_feed.current_price
        btc_opening = self._window_opens.get(window_start, 0)
        market = self._window_markets.get(window_start)

        if not market or btc_opening <= 0:
            return

        # Update market opening price
        market.opening_price = btc_opening

        # Get realized vol
        vol = self.price_feed.get_volatility(120)

        result = evaluate_updown(btc_now, btc_opening, market, secs_into, vol)
        if not result:
            return

        direction, entry_price, edge, reason = result

        # Mark as traded (one trade per window)
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
        # In live mode, place real order — only track position if order succeeds
        if self.mode == "live":
            if not self._place_order(pos):
                # Order failed — don't track, count the attempt
                self._traded_windows.discard(pos.market.window_start)
                attempts = self._window_attempts.get(pos.market.window_start, 0) + 1
                self._window_attempts[pos.market.window_start] = attempts
                remaining = 2 - attempts
                if remaining > 0:
                    print(f"[{_ts()}]   Order failed — not tracked, will retry ({remaining} attempt(s) left)")
                else:
                    print(f"[{_ts()}]   Order failed — max retries reached, skipping window")
                return

        self.open_positions.append(pos)

    def _get_clob_client(self):
        """Lazily init and cache the CLOB client for live orders."""
        if not getattr(self, '_clob_client', None):
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            proxy_addr = self.config.polymarket.proxy_address

            # Try both POLY_PROXY and EOA with derive (not create) API creds
            for sig_type, sig_name in [(1, "POLY_PROXY"), (0, "EOA")]:
                kwargs = dict(
                    key=self.config.polymarket.private_key,
                    chain_id=137,
                    signature_type=sig_type,
                )
                if sig_type in (1, 2) and proxy_addr:
                    kwargs['funder'] = proxy_addr

                client = ClobClient(CLOB_API, **kwargs)
                addr = client.signer.address()
                funder = client.builder.funder
                print(f"[{_ts()}]   Trying {sig_name}: signer={addr}, funder={funder}")

                # Use derive_api_key (gets EXISTING creds) not create (makes NEW empty ones)
                try:
                    creds = client.derive_api_key(nonce=0)
                    if creds:
                        client.set_api_creds(creds)
                        print(f"[{_ts()}]   Derived API key: {creds.api_key[:16]}...")
                    else:
                        print(f"[{_ts()}]   derive failed, trying create...")
                        creds = client.create_or_derive_api_creds()
                        client.set_api_creds(creds)
                        print(f"[{_ts()}]   Created API key: {creds.api_key[:16]}...")
                except Exception:
                    creds = client.create_or_derive_api_creds()
                    client.set_api_creds(creds)
                    print(f"[{_ts()}]   Created API key: {creds.api_key[:16]}...")

                # Check balance
                try:
                    bal_info = client.get_balance_allowance(
                        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                    )
                    print(f"[{_ts()}]   Balance: {bal_info}")
                    balance = float(bal_info.get('balance', 0)) if isinstance(bal_info, dict) else 0
                    if balance > 0:
                        print(f"[{_ts()}]   FOUND BALANCE: ${balance/1e6:.2f} with {sig_name}")
                        self._clob_client = client
                        break
                except Exception as e:
                    print(f"[{_ts()}]   Balance check: {e}")

            if not getattr(self, '_clob_client', None):
                # Default to POLY_PROXY if no balance found
                print(f"[{_ts()}]   No balance found, using POLY_PROXY")
                self._clob_client = client  # use last tried client

            # Set USDC allowance
            try:
                self._clob_client.update_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                print(f"[{_ts()}]   Allowance set")
            except Exception as e:
                print(f"[{_ts()}]   Allowance note: {e}")
            print(f"[{_ts()}]   CLOB client initialized")
        return self._clob_client

    def _place_order(self, pos: OpenPos) -> bool:
        """Place a real limit order on Polymarket CLOB. Returns True if successful."""
        token_id = (pos.market.token_id_up if pos.direction == "Up"
                    else pos.market.token_id_down)
        # Round price to 2 decimal places (Polymarket requirement)
        price = round(pos.entry_price, 2)
        size = pos.size

        print(f"[{_ts()}]   {red(bold('PLACING REAL ORDER'))}: "
              f"BUY {pos.direction} @ {price:.2f} x ${size:.2f}")

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            client = self._get_clob_client()
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )
            # Use FOK (Fill or Kill) — if not immediately matched, auto-cancels
            # create_and_post_order ignores order_type, so split the calls manually
            signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order, orderType=OrderType.FOK)
            print(f"[{_ts()}]   Order response: {resp}")
            status = resp.get('status', '') if isinstance(resp, dict) else ''
            success = resp.get('success', False) if isinstance(resp, dict) else False
            if not success or status != 'matched':
                # 'live' = resting limit order, not yet filled — no tokens received
                # Cancel immediately to unlock the USDC collateral
                order_id = resp.get('orderID', '') if isinstance(resp, dict) else ''
                if order_id and status == 'live':
                    try:
                        client.cancel(order_id)
                        print(f"[{_ts()}]   {red(f'ORDER NOT FILLED: status=live — cancelled to unlock funds')}")
                    except Exception as ce:
                        print(f"[{_ts()}]   {red(f'ORDER NOT FILLED: status=live — cancel failed: {ce}')}")
                else:
                    print(f"[{_ts()}]   {red(f'ORDER NOT FILLED: status={status} — not tracking')}")
                log.warning("order_not_filled", status=status, response=resp)
                return False
            log.info("order_placed", direction=pos.direction, price=price,
                     size=size, status=status, response=resp)
            return True
        except Exception as e:
            print(f"[{_ts()}]   {red(f'ORDER FAILED: {e}')}")
            log.error("order_failed", error=str(e))
            return False

    async def _resolve_windows(self, now: float):
        """Resolve positions whose windows have ended."""
        still_open = []
        for pos in self.open_positions:
            if now < pos.market.window_end:
                still_open.append(pos)
                continue

            if not self.price_feed.has_data:
                still_open.append(pos)
                continue

            btc_close = self.price_feed.current_price
            btc_open = pos.market.opening_price

            # Resolution: Up wins if close >= open
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

            # Save to database
            if self._use_db:
                save_trade(trade)
                update_bot_state(self.equity, self.peak_equity,
                                 self.max_drawdown, self.mode)

            result_str = green("WON") if won else red("LOST")
            move = (btc_close - btc_open) / btc_open * 100
            print(f"\n[{_ts()}] {'-'*58}")
            print(f"[{_ts()}] RESOLVED: {pos.direction}  Result={actual_result}  "
                  f"BTC move={move:+.3f}%")
            print(f"[{_ts()}]   Open=${btc_open:,.2f} -> Close=${btc_close:,.2f}")
            print(f"[{_ts()}]   {result_str}  PnL={_eq(pnl)}  "
                  f"Equity={_eq(self.equity)}")

            # Queue conditional tokens for batch redemption
            if self.mode == "live":
                self._pending_redeems.append(pos.market.condition_id)

            self._save_state()

        self.open_positions = still_open

        # Batch redeem every 10 minutes — scan blockchain, not just pending queue
        if (self.mode == "live"
                and time.time() - self._last_redeem_time > self._redeem_interval):
            self._batch_redeem()

    def _batch_redeem(self):
        """Scan blockchain for unredeemed conditional tokens and redeem them."""
        self._last_redeem_time = time.time()
        self._pending_redeems.clear()
        print(f"\n[{_ts()}] Running batch redeem scan (last 4h of markets)...")
        try:
            import requests as _requests
            import json as _json
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider('https://polygon-bor-rpc.publicnode.com'))
            acct = w3.eth.account.from_key(self.config.polymarket.private_key)
            addr = acct.address

            CT = Web3.to_checksum_address('0x4D97DCd97eC945f40cF65F87097ACe5EA0476045')
            USDC_E = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
            ct_abi = [
                {'inputs':[{'name':'account','type':'address'},{'name':'id','type':'uint256'}],'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'type':'function'},
                {'inputs':[{'name':'','type':'bytes32'}],'name':'payoutDenominator','outputs':[{'name':'','type':'uint256'}],'type':'function'},
                {'inputs':[{'name':'','type':'bytes32'},{'name':'','type':'uint256'}],'name':'payoutNumerators','outputs':[{'name':'','type':'uint256'}],'type':'function'},
                {'inputs':[{'name':'collateralToken','type':'address'},{'name':'parentCollectionId','type':'bytes32'},{'name':'conditionId','type':'bytes32'},{'name':'indexSets','type':'uint256[]'}],'name':'redeemPositions','outputs':[],'type':'function'}
            ]
            ct = w3.eth.contract(address=CT, abi=ct_abi)

            # Scan last 4 hours of 5-min markets (48 markets, not 288)
            now = int(time.time())
            scan_from = now - 4 * 3600
            base_ts = (scan_from // 300) * 300
            to_redeem = []
            markets_checked = 0

            for ts in range(base_ts, now, 300):
                # Skip if window ended less than 10 min ago (oracle delay)
                if now - (ts + 300) < 600:
                    continue
                try:
                    r = _requests.get(
                        f'https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{ts}',
                        timeout=5
                    )
                    events = r.json()
                    if not events:
                        continue
                    markets_checked += 1
                    for m in events[0].get('markets', []):
                        cid = m.get('conditionId', '')
                        if not cid:
                            continue
                        token_ids = _json.loads(m.get('clobTokenIds', '[]'))
                        for outcome_idx, tid in enumerate(token_ids):
                            bal = ct.functions.balanceOf(addr, int(tid)).call()
                            if bal > 0:
                                slug = f"btc-updown-5m-{ts}"
                                cid_bytes = bytes.fromhex(cid[2:] if cid.startswith('0x') else cid)
                                # Check oracle resolved
                                payout_denom = ct.functions.payoutDenominator(cid_bytes).call()
                                if payout_denom == 0:
                                    print(f"[{_ts()}]   Skipping {slug}: oracle not resolved yet (will retry)")
                                else:
                                    # Check our specific outcome is the winning side
                                    payout_num = ct.functions.payoutNumerators(cid_bytes, outcome_idx).call()
                                    if payout_num == 0:
                                        print(f"[{_ts()}]   Skipping {slug}: our tokens lost (oracle resolved against us)")
                                    else:
                                        # indexSet: outcome 0 → 1 (binary 01), outcome 1 → 2 (binary 10)
                                        index_set = 1 << outcome_idx
                                        print(f"[{_ts()}]   Found tokens to redeem: {slug} (bal={bal}, outcome_idx={outcome_idx}, indexSet={index_set}, payout_denom={payout_denom}, payout_num={payout_num})")
                                        to_redeem.append((ts, cid, index_set))
                                break
                except Exception as e:
                    log.debug("redeem_scan_error", ts=ts, error=str(e))
                    continue

            print(f"[{_ts()}] Scan done: {markets_checked} markets checked, {len(to_redeem)} to redeem")
            if not to_redeem:
                return

            # Check wallet balance before redeeming
            usdc_abi = [{'inputs':[{'name':'account','type':'address'}],'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'type':'function'}]
            usdc = w3.eth.contract(address=USDC_E, abi=usdc_abi)
            bal_before = usdc.functions.balanceOf(addr).call() / 1e6
            print(f"\n[{_ts()}] Redeeming {len(to_redeem)} positions...")
            print(f"[{_ts()}] Wallet BEFORE redeem: ${bal_before:.2f} USDC.e")
            log.info("wallet_before_redeem", balance_usd=round(bal_before, 2))

            gas_price = int(w3.eth.gas_price * 1.5)
            nonce = w3.eth.get_transaction_count(addr)
            redeemed = 0

            for i, (ts, condition_id, index_set) in enumerate(to_redeem):
                try:
                    tx = ct.functions.redeemPositions(
                        USDC_E,
                        b'\x00' * 32,
                        bytes.fromhex(condition_id[2:] if condition_id.startswith('0x') else condition_id),
                        [index_set]
                    ).build_transaction({
                        'from': addr, 'nonce': nonce + i, 'gas': 200000,
                        'gasPrice': gas_price, 'chainId': 137,
                    })
                    signed = acct.sign_transaction(tx)
                    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                    if receipt.status == 1:
                        redeemed += 1
                        log.info("redeem_tx", slug=f"btc-updown-5m-{ts}", tx=tx_hash.hex()[:16])
                    else:
                        log.warning("redeem_tx_failed", slug=f"btc-updown-5m-{ts}")
                except Exception as e:
                    log.warning("redeem_tx_error", slug=f"btc-updown-5m-{ts}", error=str(e))

            bal_after = usdc.functions.balanceOf(addr).call() / 1e6
            gained = bal_after - bal_before
            print(f"[{_ts()}] Redeemed {redeemed}/{len(to_redeem)} positions")
            print(f"[{_ts()}] Wallet AFTER redeem:  ${bal_after:.2f} USDC.e  (received +${gained:.2f})")
            log.info("batch_redeem", count=redeemed, total=len(to_redeem),
                     wallet_before=round(bal_before, 2), wallet_after=round(bal_after, 2),
                     gained=round(gained, 2))

        except Exception as e:
            print(f"[{_ts()}] Batch redeem error: {e}")
            log.warning("batch_redeem_failed", error=str(e))

    def _print_status(self, window_start, secs_into):
        if not self.price_feed.has_data:
            return
        price = self.price_feed.current_price
        btc_open = self._window_opens.get(window_start, price)
        move = (price - btc_open) / btc_open * 100 if btc_open else 0
        n = len(self.completed_trades)
        wins = sum(1 for t in self.completed_trades if t.won)
        wr = wins / n if n else 0
        traded = "TRADED" if window_start in self._traded_windows else "watching"
        remaining = max(0, 300 - secs_into)

        print(
            f"\r[{_ts()}]  BTC ${price:,.2f} ({move:+.3f}%)"
            f"  |  Window: {remaining:.0f}s left [{traded}]"
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
            "peak_equity": self.peak_equity,
            "max_drawdown": self.max_drawdown,
            "trades": [asdict(t) for t in self.completed_trades],
        }
        # Remove MarketInfo from trades (not serializable as-is)
        for t in state["trades"]:
            t.pop("market", None)
        try:
            with open(TRADES_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.warning("save_state_failed", error=str(e))

    def _print_summary(self):
        trades = self.completed_trades
        n = len(trades)
        print("\n")
        print(bold("=" * 65))
        print(bold(f"  TRADING SUMMARY  [{self.mode.upper()}]"))
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
            dt = datetime.fromtimestamp(t.opened_at).strftime("%H:%M")
            sym = green("W") if t.won else red("L")
            print(f"    {dt}  {t.direction:4s}  entry={t.entry_price:.3f}  "
                  f"edge={t.edge:+.3f}  {sym}  {_eq(t.pnl)}")
        print(bold("=" * 65))


# ── Entry point ──────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Trade Polymarket BTC Up/Down 5-min markets")
    p.add_argument("--mode", choices=["paper", "live"], default="paper",
                   help="Trading mode (default: paper)")
    p.add_argument("--size", type=float, default=50.0,
                   help="Trade size in USDC (default $50)")
    p.add_argument("--check", type=int, default=15,
                   help="Check interval in seconds within each window (default 15)")
    return p.parse_args()


def main():
    args = parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING)

    config = Config()

    if args.mode == "live":
        if not config.polymarket.private_key:
            print("ERROR: POLYMARKET_PRIVATE_KEY required for live mode")
            print("Set it in .env file")
            sys.exit(1)
        print(bold(red("\n  *** LIVE MONEY MODE ***")))
        print(bold(red(f"  Trade size: ${args.size}")))
        print(bold(red("  Real orders will be placed on Polymarket!\n")))

    trader = RealTrader(
        config=config,
        mode=args.mode,
        trade_size=args.size,
        check_interval=args.check,
    )

    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
