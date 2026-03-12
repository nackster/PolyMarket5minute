"""Polymarket CLOB API client for the 5-minute BTC prediction markets."""

import time
import structlog
from dataclasses import dataclass
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from src.config import PolymarketConfig

log = structlog.get_logger()


@dataclass
class Market:
    """Represents a Polymarket 5-min BTC prediction market."""
    condition_id: str
    question: str
    token_id_yes: str
    token_id_no: str
    outcome_yes_price: float
    outcome_no_price: float
    end_time: float  # unix timestamp when the market resolves
    strike_price: float  # the BTC price threshold
    created_at: float = 0.0  # set to simulated tick time in backtesting; 0 = use time.time()

    @property
    def seconds_until_resolution(self) -> float:
        ref = self.created_at if self.created_at > 0 else time.time()
        return max(0, self.end_time - ref)

    @property
    def implied_prob_yes(self) -> float:
        return self.outcome_yes_price

    @property
    def implied_prob_no(self) -> float:
        return self.outcome_no_price


@dataclass
class Position:
    """An open position on a market."""
    market: Market
    side: str  # "YES" or "NO"
    size: float  # USDC amount
    entry_price: float
    timestamp: float


class PolymarketClient:
    """Wrapper around the Polymarket CLOB API for BTC 5-min markets."""

    # Known search terms for finding 5-min BTC markets
    BTC_5MIN_KEYWORDS = ["Bitcoin", "BTC", "5 minute", "5-minute", "5min"]

    def __init__(self, config: PolymarketConfig):
        self.config = config
        self.client = None
        self.positions: list[Position] = []
        self._init_client()

    def _init_client(self):
        """Initialize the CLOB client with API credentials."""
        try:
            self.client = ClobClient(
                self.config.api_url,
                key=self.config.api_key,
                chain_id=137,  # Polygon mainnet
            )
            # If we have a private key, derive API creds
            if self.config.private_key:
                self.client.set_api_creds(
                    self.client.create_or_derive_api_creds()
                )
            log.info("polymarket_client_initialized", api_url=self.config.api_url)
        except Exception as e:
            log.error("polymarket_client_init_failed", error=str(e))
            raise

    def get_active_btc_5min_markets(self) -> list[Market]:
        """Fetch currently active 5-minute BTC prediction markets."""
        markets = []
        try:
            # Search for BTC 5-minute markets via the CLOB API
            response = self.client.get_markets()
            if not response:
                log.warning("no_markets_returned")
                return markets

            now = time.time()
            for m in response:
                question = m.get("question", "").lower()
                # Filter for BTC 5-minute markets
                is_btc = any(kw.lower() in question for kw in self.BTC_5MIN_KEYWORDS[:2])
                is_5min = any(kw.lower() in question for kw in self.BTC_5MIN_KEYWORDS[2:])

                if not (is_btc and is_5min):
                    continue

                # Only get markets that haven't resolved yet
                end_time = float(m.get("end_date_iso", 0)) if m.get("end_date_iso") else 0
                if end_time and end_time < now:
                    continue

                tokens = m.get("tokens", [])
                if len(tokens) < 2:
                    continue

                # Parse the strike price from the question
                strike = self._parse_strike_price(m.get("question", ""))

                market = Market(
                    condition_id=m.get("condition_id", ""),
                    question=m.get("question", ""),
                    token_id_yes=tokens[0].get("token_id", ""),
                    token_id_no=tokens[1].get("token_id", ""),
                    outcome_yes_price=float(tokens[0].get("price", 0.5)),
                    outcome_no_price=float(tokens[1].get("price", 0.5)),
                    end_time=end_time,
                    strike_price=strike,
                )
                markets.append(market)

            log.info("btc_5min_markets_found", count=len(markets))
        except Exception as e:
            log.error("fetch_markets_failed", error=str(e))

        return markets

    def get_market_orderbook(self, token_id: str) -> dict:
        """Fetch the order book for a specific token."""
        try:
            book = self.client.get_order_book(token_id)
            return {
                "bids": book.get("bids", []),
                "asks": book.get("asks", []),
            }
        except Exception as e:
            log.error("orderbook_fetch_failed", token_id=token_id, error=str(e))
            return {"bids": [], "asks": []}

    def place_market_order(self, token_id: str, side: str, size: float) -> dict | None:
        """Place a market order on a token.

        Args:
            token_id: The token to trade
            side: "BUY" or "SELL"
            size: Amount in USDC
        """
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=0.0,  # market order
                size=size,
                side=side,
            )
            resp = self.client.create_and_post_order(order_args)
            log.info("order_placed", token_id=token_id, side=side, size=size, response=resp)
            return resp
        except Exception as e:
            log.error("order_failed", token_id=token_id, side=side, size=size, error=str(e))
            return None

    def place_limit_order(
        self, token_id: str, side: str, price: float, size: float
    ) -> dict | None:
        """Place a limit order on a token.

        Args:
            token_id: The token to trade
            side: "BUY" or "SELL"
            price: Limit price (0.01 - 0.99)
            size: Amount in USDC
        """
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )
            resp = self.client.create_and_post_order(order_args)
            log.info(
                "limit_order_placed",
                token_id=token_id, side=side, price=price, size=size, response=resp,
            )
            return resp
        except Exception as e:
            log.error(
                "limit_order_failed",
                token_id=token_id, side=side, price=price, size=size, error=str(e),
            )
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            self.client.cancel(order_id)
            log.info("order_cancelled", order_id=order_id)
            return True
        except Exception as e:
            log.error("cancel_failed", order_id=order_id, error=str(e))
            return False

    def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        try:
            return self.client.get_orders() or []
        except Exception as e:
            log.error("get_orders_failed", error=str(e))
            return []

    @staticmethod
    def _parse_strike_price(question: str) -> float:
        """Extract the BTC strike price from a market question.

        Example: "Will Bitcoin be above $97,500.50 at 3:05 PM?" -> 97500.50
        """
        import re
        # Match dollar amounts like $97,500 or $97,500.50
        match = re.search(r"\$([0-9,]+(?:\.\d+)?)", question)
        if match:
            price_str = match.group(1).replace(",", "")
            try:
                return float(price_str)
            except ValueError:
                pass
        return 0.0
