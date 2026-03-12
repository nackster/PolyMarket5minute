"""Scan Polymarket for active BTC binary prediction markets.

Usage:
    python scan_markets.py                # list all BTC markets
    python scan_markets.py --verbose      # show full details + order books
"""

import argparse
import json
import sys
import requests
from datetime import datetime, timezone


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"


def fetch_btc_markets(verbose=False):
    """Search Polymarket Gamma API for BTC-related binary markets."""
    print("Scanning Polymarket for BTC binary markets...\n")

    # Gamma API lets us search by keyword
    all_markets = []
    for keyword in ["Bitcoin", "BTC"]:
        url = f"{GAMMA_API}/markets"
        params = {
            "closed": "false",
            "limit": 100,
            "order": "end_date_iso",
            "ascending": "true",
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            markets = resp.json()
            all_markets.extend(markets)
        except Exception as e:
            print(f"  Error fetching from Gamma API ({keyword}): {e}")

    # Deduplicate by condition_id
    seen = set()
    unique = []
    for m in all_markets:
        cid = m.get("condition_id", "")
        if cid and cid not in seen:
            seen.add(cid)
            unique.append(m)

    # Filter for BTC-related
    btc_markets = []
    for m in unique:
        q = (m.get("question", "") + " " + m.get("description", "")).lower()
        if "bitcoin" in q or "btc" in q:
            btc_markets.append(m)

    if not btc_markets:
        print("No active BTC markets found on Polymarket.")
        print("\nTrying CLOB API directly...")
        try:
            resp = requests.get(f"{CLOB_API}/markets", timeout=15)
            resp.raise_for_status()
            clob_markets = resp.json()
            if isinstance(clob_markets, dict):
                clob_markets = clob_markets.get("data", [])
            for m in clob_markets:
                q = (m.get("question", "") + " " + m.get("description", "")).lower()
                if "bitcoin" in q or "btc" in q:
                    btc_markets.append(m)
        except Exception as e:
            print(f"  Error fetching from CLOB API: {e}")

    if not btc_markets:
        print("\nNo BTC markets found on either API.")
        print("This could mean:")
        print("  1. Polymarket doesn't currently have BTC binary markets")
        print("  2. Markets use different keywords (try browsing polymarket.com)")
        print("  3. API access is restricted")
        return []

    # Sort by end date
    btc_markets.sort(key=lambda m: m.get("end_date_iso", "9999"))

    print(f"Found {len(btc_markets)} BTC markets:\n")
    print("=" * 80)

    for i, m in enumerate(btc_markets, 1):
        question = m.get("question", "N/A")
        end_date = m.get("end_date_iso", "N/A")
        condition_id = m.get("condition_id", "N/A")
        tokens = m.get("tokens", [])

        # Parse end date for human display
        try:
            if end_date and end_date != "N/A":
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                time_left = end_dt - datetime.now(timezone.utc)
                time_str = f"{end_dt.strftime('%Y-%m-%d %H:%M UTC')} ({time_left})"
            else:
                time_str = "N/A"
        except Exception:
            time_str = end_date

        print(f"\n  [{i}] {question}")
        print(f"      Condition ID: {condition_id[:40]}...")
        print(f"      Ends: {time_str}")

        if tokens:
            for t in tokens:
                outcome = t.get("outcome", "?")
                price = t.get("price", "?")
                token_id = t.get("token_id", "?")
                print(f"      {outcome}: price={price}  token_id={token_id[:30]}...")

        # Check for 5-minute or short-duration indicators
        q_lower = question.lower()
        is_short = any(k in q_lower for k in ["5 min", "5-min", "5min", "minute", "hour"])
        if is_short:
            print(f"      ** SHORT-DURATION MARKET DETECTED **")

        if verbose:
            # Fetch order book for first token
            if tokens:
                tid = tokens[0].get("token_id", "")
                if tid:
                    try:
                        book_resp = requests.get(
                            f"{CLOB_API}/book",
                            params={"token_id": tid},
                            timeout=10,
                        )
                        if book_resp.ok:
                            book = book_resp.json()
                            bids = book.get("bids", [])[:3]
                            asks = book.get("asks", [])[:3]
                            print(f"      Order book (top 3):")
                            print(f"        Bids: {bids}")
                            print(f"        Asks: {asks}")
                    except Exception as e:
                        print(f"      (order book fetch failed: {e})")

    print("\n" + "=" * 80)

    # Summary of market durations
    short_markets = [m for m in btc_markets if any(
        k in m.get("question", "").lower()
        for k in ["5 min", "5-min", "5min", "minute", "hour", "hourly"]
    )]
    daily_markets = [m for m in btc_markets if any(
        k in m.get("question", "").lower()
        for k in ["today", "daily", "day", "tonight", "tomorrow", "week"]
    )]

    print(f"\nSummary:")
    print(f"  Total BTC markets: {len(btc_markets)}")
    print(f"  Short-duration (min/hour): {len(short_markets)}")
    print(f"  Daily/Weekly: {len(daily_markets)}")
    print(f"  Other: {len(btc_markets) - len(short_markets) - len(daily_markets)}")

    if not short_markets:
        print("\n  NOTE: No 5-minute markets found.")
        print("  Polymarket may only offer daily/weekly BTC markets.")
        print("  Our strategy can be adapted for longer timeframes,")
        print("  or we continue paper trading with synthetic 5-min markets.")

    return btc_markets


def main():
    parser = argparse.ArgumentParser(description="Scan Polymarket for BTC markets")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show order books")
    args = parser.parse_args()
    fetch_btc_markets(verbose=args.verbose)


if __name__ == "__main__":
    main()
