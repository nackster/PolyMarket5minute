"""
Congressional Stock Trade Backtester
Fetches House disclosure data and backtests a strategy of copying purchases.

Usage:
    python backtest_congress.py                    # 2020-2024, 60-day hold
    python backtest_congress.py --days 90          # 90-day hold period
    python backtest_congress.py --start 2019-01-01 --end 2024-12-31
    python backtest_congress.py --min-amount 50000 # only trades >$50k
    python backtest_congress.py --top-traders      # show top 10 traders and picks
"""

import requests
import yfinance as yf
import json
import os
import time
import argparse
import statistics
from datetime import datetime, timedelta
from collections import defaultdict

# ── Constants ──────────────────────────────────────────────────────────────────
# Data sources tried in order (first success wins)
HOUSE_DATA_SOURCES = [
    # S3 bucket backing housestockwatcher.com — resolves even if domain DNS fails
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
    # Original API
    "https://housestockwatcher.com/api",
]
SENATE_DATA_SOURCES = [
    # S3 bucket backing senatestockwatcher.com
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions_for_senators.json",
    "https://senatestockwatcher.com/api",
]

CACHE_FILE = "trades/congress_prices.json"
# Cached bulk download — avoids re-downloading the full 50MB dataset every run
QUIVER_BULK_CACHE = "trades/quiver_congress_bulk.json"
# Local CSV fallback: place a CSV with columns date,ticker,representative,type,amount
# at this path and it will be used if all remote sources fail.
LOCAL_CSV_FALLBACK = "trades/congress_trades.csv"
# Quiver Quant API base
QUIVER_API_BASE = "https://api.quiverquant.com/beta"

AMOUNT_RANGES = [
    ("$1,001 - $15,000",        8000),
    ("$15,001 - $50,000",       32500),
    ("$50,001 - $100,000",      75000),
    ("$100,001 - $250,000",     175000),
    ("$250,001 - $500,000",     375000),
    ("$500,001 - $1,000,000",   750000),
    ("$1,000,001 - $5,000,000", 3000000),
    ("$5,000,001 - $25,000,000",15000000),
]

# ── Amount parsing ──────────────────────────────────────────────────────────────

def parse_amount(amount_str):
    """Return midpoint dollar value for congressional disclosure amount range."""
    if not amount_str:
        return 0
    s = amount_str.strip()
    for label, mid in AMOUNT_RANGES:
        if label.lower() in s.lower():
            return mid
    # Fallback: try to extract any number
    digits = "".join(c for c in s if c.isdigit() or c == ".")
    try:
        return float(digits)
    except ValueError:
        return 0

# ── Cache helpers ───────────────────────────────────────────────────────────────

def load_price_cache():
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_price_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)

# ── Date helpers ────────────────────────────────────────────────────────────────

def next_trading_day(date_str, prices_dict):
    """Given a date string YYYY-MM-DD, return the nearest date with price data."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(10):
        candidate = (dt + timedelta(days=i)).strftime("%Y-%m-%d")
        if candidate in prices_dict:
            return candidate
    return None

def date_plus_days(date_str, n):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return (dt + timedelta(days=n)).strftime("%Y-%m-%d")

# ── Price fetching ──────────────────────────────────────────────────────────────

def fetch_prices_batch(tickers, start_date, end_date, cache, batch_size=200):
    """
    Download prices for many tickers at once using yfinance batch mode.
    Much faster than one-by-one: a single API call for up to 200 tickers.
    Returns {ticker: {date_str: close_price}}.
    Silences per-ticker warnings; only prints summary.
    """
    import io, contextlib

    end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    end_fetch = end_dt.strftime("%Y-%m-%d")
    cache_key_prefix = f"{start_date}|{end_fetch}"

    # Split into already-cached and need-to-fetch
    result = {}
    to_fetch = []
    for tk in tickers:
        ck = f"{tk}|{cache_key_prefix}"
        if ck in cache:
            result[tk] = cache[ck]
        else:
            to_fetch.append(tk)

    if not to_fetch:
        return result

    # Download in batches, silencing yfinance noise
    for i in range(0, len(to_fetch), batch_size):
        batch = to_fetch[i:i + batch_size]
        print(f"  Downloading batch {i//batch_size + 1}/{(len(to_fetch)-1)//batch_size + 1} "
              f"({len(batch)} tickers)...", end=" ", flush=True)
        try:
            # Suppress yfinance stderr warnings
            with contextlib.redirect_stderr(io.StringIO()):
                df = yf.download(
                    batch, start=start_date, end=end_fetch,
                    progress=False, auto_adjust=True,
                )
            if df is None or df.empty:
                print("empty (rate limited?)")
                for tk in batch:
                    cache[f"{tk}|{cache_key_prefix}"] = {}
                    result[tk] = {}
                continue

            ok = 0
            for tk in batch:
                prices = {}
                try:
                    # Multi-ticker: columns are ('Close', 'AAPL')
                    # Single ticker: columns are ('Close', 'AAPL') too (yfinance wraps it)
                    if ("Close", tk) in df.columns:
                        close_col = df[("Close", tk)]
                    elif len(batch) == 1 and "Close" in df.columns:
                        close_col = df["Close"]
                    else:
                        raise KeyError(tk)

                    for idx, val in close_col.items():
                        if val != val:  # NaN
                            continue
                        d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                        try:
                            prices[d] = float(val)
                        except Exception:
                            pass
                    if prices:
                        ok += 1
                except Exception:
                    pass
                result[tk] = prices
                cache[f"{tk}|{cache_key_prefix}"] = prices
            print(f"{ok}/{len(batch)} tickers OK")

        except Exception as e:
            print(f"batch error: {e}")
            for tk in batch:
                result[tk] = {}
                cache[f"{tk}|{cache_key_prefix}"] = {}

    return result


def fetch_price_series(ticker, start_date, end_date, cache):
    """Single-ticker price fetch — used only when batch is not applicable."""
    result = fetch_prices_batch([ticker], start_date, end_date, cache)
    return result.get(ticker, {})

def get_price_on_or_after(prices_dict, date_str, max_days=5):
    """Get the price on date_str or up to max_days later (for weekends/holidays)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(max_days + 1):
        d = (dt + timedelta(days=i)).strftime("%Y-%m-%d")
        if d in prices_dict:
            return prices_dict[d], d
    return None, None

# ── Data fetching ───────────────────────────────────────────────────────────────

def _try_fetch_json(urls, label):
    """Try each URL in order, return parsed list of trade dicts, or None."""
    for url in urls:
        try:
            print(f"  Trying {url[:70]}...")
            resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "data" in data:
                records = data["data"]
            elif isinstance(data, list):
                records = data
            else:
                print(f"  [warn] Unexpected format from {url}: {type(data)}")
                continue
            print(f"  Got {len(records)} {label} disclosures from {url[:50]}")
            return records
        except Exception as e:
            print(f"  [warn] {url[:60]} failed: {e}")
    return None


def _try_load_local_csv():
    """Load congress_trades.csv if it exists. Returns list of dicts or None."""
    if not os.path.exists(LOCAL_CSV_FALLBACK):
        return None
    import csv
    print(f"  Loading local CSV: {LOCAL_CSV_FALLBACK}")
    trades = []
    with open(LOCAL_CSV_FALLBACK, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(dict(row))
    print(f"  Loaded {len(trades)} records from local CSV.")
    return trades


def _normalize_quiver_record(t):
    """
    Normalize a single Quiver Quant bulk record to the internal schema.
    Bulk schema: Ticker, Name, Filed, Traded, Transaction, Trade_Size_USD,
                 Party, Chamber, District, State, Comments
    """
    filed  = str(t.get("Filed") or t.get("ReportDate") or "")[:10]
    traded = str(t.get("Traded") or t.get("TransactionDate") or filed)[:10]
    amount = t.get("Trade_Size_USD") or t.get("Range") or t.get("Amount") or ""
    # Convert numeric amount to range string if needed
    try:
        amt_val = float(amount)
        for label, mid in AMOUNT_RANGES:
            lo, hi = mid * 0.5, mid * 1.5
            if lo <= amt_val <= hi:
                amount = label
                break
    except (ValueError, TypeError):
        pass
    return {
        "disclosure_date": filed,
        "transaction_date": traded,
        "representative": str(t.get("Name") or t.get("Representative") or "Unknown"),
        "ticker": str(t.get("Ticker") or "").strip().upper(),
        "type": str(t.get("Transaction") or ""),
        "amount": str(amount),
        "asset_description": str(t.get("Description") or t.get("Comments") or ""),
        "district": str(t.get("District") or ""),
        "party": str(t.get("Party") or ""),
        "chamber": str(t.get("Chamber") or ""),
        "ticker_type": str(t.get("TickerType") or ""),
    }


def fetch_quiver_trades(api_key):
    """
    Fetch ALL historical congressional trades from Quiver Quant.
    - First checks for a local bulk cache at trades/quiver_congress_bulk.json
    - If not cached, downloads the full bulk dataset (~50MB, 111k records, 2014-present)
      and saves it locally for future runs.
    Returns list of normalized trade dicts.
    """
    # ── Load from local cache if it exists ──────────────────────────────────────
    if os.path.exists(QUIVER_BULK_CACHE):
        print(f"  Loading congressional data from local cache ({QUIVER_BULK_CACHE})...")
        try:
            with open(QUIVER_BULK_CACHE, "r") as f:
                raw = json.load(f)
            print(f"  Loaded {len(raw)} records from cache.")
            return [_normalize_quiver_record(t) for t in raw]
        except Exception as e:
            print(f"  [warn] Cache load failed ({e}), re-downloading...")

    if not api_key:
        return None

    # ── Download bulk dataset ────────────────────────────────────────────────────
    print("Downloading full Quiver Quant congressional dataset (~50MB, one-time)...")
    headers_req = {
        "Authorization": f"Token {api_key}",
        "Accept": "application/json",
    }
    try:
        r = requests.get(
            f"{QUIVER_API_BASE}/bulk/congresstrading",
            headers=headers_req,
            timeout=300,
            stream=True,
        )
        if r.status_code == 401:
            print("  [error] Invalid API key — check quiverquant.com")
            return None
        r.raise_for_status()

        chunks, total = [], 0
        for chunk in r.iter_content(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            print(f"  {total // 1024}KB downloaded...", end="\r")
        print(f"\n  Download complete: {total // 1024}KB")

        raw = json.loads(b"".join(chunks))
        print(f"  Records: {len(raw)}")

        # Save to cache
        os.makedirs(os.path.dirname(QUIVER_BULK_CACHE), exist_ok=True)
        with open(QUIVER_BULK_CACHE, "w") as f:
            json.dump(raw, f)
        print(f"  Cached to {QUIVER_BULK_CACHE} (future runs will load instantly).")

        return [_normalize_quiver_record(t) for t in raw]

    except Exception as e:
        err = str(e)
        if "401" in err or "403" in err:
            print(f"  [error] API key rejected.")
        else:
            print(f"  [warn] Quiver Quant download failed: {e}")
        return None


def fetch_house_trades(quiver_key=None):
    """
    Fetch congressional stock disclosures. Tries sources in order:
      1. Local Quiver bulk cache (if already downloaded)
      2. Quiver Quant API download (if --quiver-key provided)
      3. House S3 bucket / housestockwatcher.com
      4. Senate S3 bucket / senatestockwatcher.com
      5. Local CSV at trades/congress_trades.csv
    """
    all_trades = []

    # Quiver Quant — checks local cache first, then downloads if key provided
    qv = fetch_quiver_trades(quiver_key)
    if qv:
        return qv

    # House data
    print("Fetching House disclosure data...")
    house = _try_fetch_json(HOUSE_DATA_SOURCES, "House")
    if house:
        all_trades.extend(house)

    # Senate data (bonus)
    print("Fetching Senate disclosure data...")
    senate = _try_fetch_json(SENATE_DATA_SOURCES, "Senate")
    if senate:
        for t in senate:
            if "senator" in t and "representative" not in t:
                t["representative"] = t["senator"]
            if "asset_type" in t and "asset_description" not in t:
                t["asset_description"] = t.get("asset_type", "")
        all_trades.extend(senate)

    if all_trades:
        print(f"  Total disclosures fetched: {len(all_trades)}")
        return all_trades

    # Local CSV last resort
    local = _try_load_local_csv()
    if local:
        return local

    raise RuntimeError(
        "\n\nCould not fetch congressional trade data from any source.\n"
        "\nFix options:\n"
        "  1. Get a Quiver Quant API key:\n"
        "       https://www.quiverquant.com/\n"
        "     Then run:\n"
        "       python backtest_congress.py --quiver-key YOUR_KEY\n"
        "     Or set env var:  QUIVER_API_KEY=YOUR_KEY\n"
        "\n"
        "  2. Test the full pipeline with synthetic data (no key needed):\n"
        "       python backtest_congress.py --demo\n"
        "\n"
        "  3. Manually download a CSV and save to:\n"
        f"       {LOCAL_CSV_FALLBACK}\n"
        "     Required columns: date, ticker, representative, type, amount\n"
    )

def is_valid_ticker(ticker, ticker_type=None):
    """Return True if ticker looks like a real US stock symbol."""
    if not ticker:
        return False
    t = ticker.strip().upper()
    if not t:
        return False
    # Quiver Quant includes a TickerType field: 'ST' = stock, others = bonds/funds/etc.
    if ticker_type and str(ticker_type).upper() not in ("ST", "ETF", ""):
        return False
    # Bond CUSIPs start with digits
    if t[0].isdigit():
        return False
    # Options/derivatives: spaces, slashes, or trailing letters on numbers
    if " " in t or "/" in t:
        return False
    # Real US stock tickers: 1-5 letters, optionally dot+1-2 letters (BRK.B, BF.A)
    # Allow up to 6 chars for edge cases
    if len(t) > 6:
        return False
    # Must be letters (plus optional single dot and letter suffix)
    import re
    if not re.match(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$', t):
        return False
    # Exclude obvious non-tickers
    if t in ("NA", "N/A", "NONE", "NULL", "TEST", "BELGIUM"):
        return False
    return True

def filter_purchases(trades, start_date, end_date, min_amount,
                     use_transaction_date=False, follow_reps=None, dedupe=True):
    """Filter to purchase transactions in date range above min_amount.

    Args:
        use_transaction_date: If True, use actual transaction date as entry signal
                              (30-45 days earlier than disclosure). This tests
                              whether the edge comes from insider timing vs. public filing.
        follow_reps: If set, a list/set of rep name substrings to follow exclusively.
        dedupe: If True (default), remove duplicate rep+ticker+date entries.
    """
    purchases = []
    seen = set()  # for deduplication
    follow_reps_lower = [r.lower() for r in (follow_reps or [])]

    for t in trades:
        # Normalise type field
        tx_type = (t.get("type") or t.get("transaction_type") or "").lower()
        if "purchase" not in tx_type and "buy" not in tx_type:
            continue

        # Rep filter
        rep = (t.get("representative") or t.get("name") or "Unknown").strip()
        if follow_reps_lower:
            rep_lower = rep.lower()
            if not any(f in rep_lower for f in follow_reps_lower):
                continue

        # Date selection
        disc  = (t.get("disclosure_date")  or "")[:10]
        txn   = (t.get("transaction_date") or disc)[:10]

        # Signal date: which date we use to enter the trade
        signal_date = txn if use_transaction_date else disc
        if not signal_date:
            continue
        try:
            d = datetime.strptime(signal_date, "%Y-%m-%d")
        except ValueError:
            continue
        if d < datetime.strptime(start_date, "%Y-%m-%d"):
            continue
        if d > datetime.strptime(end_date, "%Y-%m-%d"):
            continue

        ticker = (t.get("ticker") or "").strip().upper()
        ticker_type = t.get("ticker_type") or t.get("TickerType") or ""
        if not is_valid_ticker(ticker, ticker_type):
            continue

        amount_mid = parse_amount(t.get("amount") or "")
        if amount_mid < min_amount:
            continue

        # Deduplication: same rep + ticker + transaction date = one trade
        if dedupe:
            dedup_key = (rep, ticker, txn or disc)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

        purchases.append({
            "date": signal_date,
            "transaction_date": txn or disc,
            "ticker": ticker,
            "amount": amount_mid,
            "rep": rep,
            "description": t.get("asset_description") or "",
            "district": t.get("district") or "",
            "chamber": t.get("chamber") or "",
        })
    return purchases


def apply_cluster_filter(purchases, min_cluster, window_days=30):
    """
    Only keep purchases where at least `min_cluster` *unique* politicians
    bought the same ticker within `window_days` of each other.

    This is the strongest signal in congressional trading research:
    when multiple politicians independently buy the same stock, it suggests
    shared (non-public) information — and the effect is dramatically larger.

    Args:
        purchases: list of purchase dicts (must be sorted or will be sorted here)
        min_cluster: minimum number of unique reps that must buy (e.g. 3)
        window_days: look-back window in calendar days (default 30)

    Returns:
        Filtered list of purchases that belong to a cluster.
    """
    if min_cluster < 2:
        return purchases

    # Group by ticker → sorted list of (date, rep, idx)
    by_ticker = defaultdict(list)
    for i, p in enumerate(purchases):
        try:
            dt = datetime.strptime(p["date"], "%Y-%m-%d")
        except ValueError:
            continue
        by_ticker[p["ticker"]].append((dt, p["rep"], i))

    keep_indices = set()

    for ticker, entries in by_ticker.items():
        entries.sort(key=lambda x: x[0])  # sort by date
        n = len(entries)
        for j in range(n):
            anchor_dt = entries[j][0]
            window_end = anchor_dt + timedelta(days=window_days)
            # Collect all unique reps buying within window
            cluster_reps = set()
            cluster_idxs = []
            for k in range(j, n):
                if entries[k][0] > window_end:
                    break
                cluster_reps.add(entries[k][1])
                cluster_idxs.append(entries[k][2])
            if len(cluster_reps) >= min_cluster:
                keep_indices.update(cluster_idxs)

    filtered = [p for i, p in enumerate(purchases) if i in keep_indices]
    return filtered

# ── Core backtest ───────────────────────────────────────────────────────────────

def _compute_spy_trend(spy_prices, ma_days=200):
    """
    Given a dict {date_str: close_price} for SPY, return a set of date strings
    where SPY closed above its `ma_days`-day moving average (bull regime).
    Also returns a dict {date_str: ma_value} for inspection.
    """
    if not spy_prices:
        return set(), {}

    sorted_dates = sorted(spy_prices.keys())
    closes = [spy_prices[d] for d in sorted_dates]
    n = len(closes)

    bull_dates = set()
    ma_values = {}

    for i in range(n):
        if i < ma_days - 1:
            continue  # not enough history
        window = closes[i - ma_days + 1 : i + 1]
        ma = sum(window) / ma_days
        d = sorted_dates[i]
        ma_values[d] = ma
        if closes[i] > ma:
            bull_dates.add(d)

    return bull_dates, ma_values


def _nearest_trend_date(date_str, bull_dates, spy_dates_sorted, window=5):
    """Find the nearest available SPY date to check trend status."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(window):
        candidate = (dt + timedelta(days=i)).strftime("%Y-%m-%d")
        if candidate in bull_dates or candidate in set(spy_dates_sorted):
            # Check if it's in our spy dates
            if candidate in set(spy_dates_sorted):
                return candidate, candidate in bull_dates
    return None, False


def run_backtest(purchases, hold_days, cache, verbose=False, trend_filter=False,
                 trend_ma_days=200):
    """
    For each purchase, fetch entry and exit prices and SPY prices.
    Returns list of result dicts.

    Args:
        trend_filter: If True, skip trades entered when SPY < trend_ma_days-day MA.
                      This avoids trading in bear markets/corrections.
        trend_ma_days: Number of days for the trend MA (default: 200).
    """
    tickers_needed = set(t["ticker"] for t in purchases) | {"SPY"}
    dates = sorted(set(t["date"] for t in purchases))
    if not dates:
        return []

    global_start = dates[0]
    global_end = date_plus_days(dates[-1], hold_days + 15)  # buffer for holidays

    # For trend filter, fetch SPY going back extra days to compute MA
    spy_fetch_start = global_start
    if trend_filter:
        spy_fetch_start = date_plus_days(global_start, -(trend_ma_days + 30))

    print(f"  Pre-fetching prices for {len(tickers_needed)} tickers "
          f"({global_start} to {global_end})...")

    # Batch download — much faster than one-by-one
    ticker_prices = fetch_prices_batch(
        sorted(tickers_needed), global_start, global_end, cache
    )

    # Fetch extended SPY for trend MA if needed
    if trend_filter:
        spy_extended = fetch_prices_batch(
            ["SPY"], spy_fetch_start, global_end, cache
        )
        spy_prices_ext = spy_extended.get("SPY", {})
        # Merge with any SPY from main batch
        spy_prices_ext.update(ticker_prices.get("SPY", {}))
        bull_dates, _ = _compute_spy_trend(spy_prices_ext, trend_ma_days)
        spy_dates_sorted = sorted(spy_prices_ext.keys())
    else:
        bull_dates = None
        spy_dates_sorted = None

    save_price_cache(cache)
    print(f"  Price fetch complete.")

    spy_prices = ticker_prices.get("SPY", {})
    results = []
    trend_filtered_count = 0

    for trade in purchases:
        tk = trade["ticker"]
        entry_date = trade["date"]
        exit_date = date_plus_days(entry_date, hold_days)

        # Trend filter: skip if SPY is in downtrend on entry date
        if trend_filter and bull_dates is not None:
            _, in_bull = _nearest_trend_date(entry_date, bull_dates, spy_dates_sorted)
            if not in_bull:
                trend_filtered_count += 1
                continue

        prices = ticker_prices.get(tk, {})
        if not prices:
            continue

        entry_price, actual_entry = get_price_on_or_after(prices, entry_date)
        if entry_price is None:
            continue

        exit_price, actual_exit = get_price_on_or_after(prices, exit_date)
        if exit_price is None:
            continue

        spy_entry, _ = get_price_on_or_after(spy_prices, entry_date)
        spy_exit, _ = get_price_on_or_after(spy_prices, exit_date)
        if spy_entry is None or spy_exit is None:
            spy_return = None
        else:
            spy_return = (spy_exit - spy_entry) / spy_entry

        stock_return = (exit_price - entry_price) / entry_price
        alpha = stock_return - spy_return if spy_return is not None else None

        results.append({
            "date": entry_date,
            "ticker": tk,
            "rep": trade["rep"],
            "amount": trade["amount"],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "actual_entry": actual_entry,
            "actual_exit": actual_exit,
            "stock_return": stock_return,
            "spy_return": spy_return,
            "alpha": alpha,
            "description": trade["description"],
        })

    if trend_filter and trend_filtered_count:
        print(f"  Trend filter: skipped {trend_filtered_count} trades "
              f"(SPY below {trend_ma_days}d MA on entry date)")
    return results

# ── Display helpers ─────────────────────────────────────────────────────────────

def pct(v):
    if v is None:
        return "  N/A  "
    return f"{v*100:+7.2f}%"

def bar(val, max_val, width=30):
    """ASCII bar proportional to val/max_val."""
    if max_val == 0:
        return " " * width
    filled = int(round(abs(val) / max_val * width))
    filled = min(filled, width)
    char = "#" if val >= 0 else "-"
    return char * filled + " " * (width - filled)

def print_separator(char="-", width=72):
    print(char * width)

def print_header(title, width=72):
    print_separator("=", width)
    pad = (width - len(title) - 2) // 2
    print("=" + " " * pad + title + " " * (width - pad - len(title) - 2) + "=")
    print_separator("=", width)

# ── Analysis functions ──────────────────────────────────────────────────────────

def analyze_by_holding_period(all_results, holding_periods):
    print_header("ALPHA VS SPY BY HOLDING PERIOD")
    print(f"  {'Hold':>6}  {'Trades':>7}  {'Win%':>6}  {'Avg Return':>11}  {'Avg SPY':>9}  {'Avg Alpha':>10}")
    print_separator()
    for hp in holding_periods:
        results = all_results.get(hp, [])
        if not results:
            print(f"  {hp:>5}d   no data")
            continue
        valid = [r for r in results if r["alpha"] is not None]
        if not valid:
            continue
        returns = [r["stock_return"] for r in valid]
        spy_returns = [r["spy_return"] for r in valid]
        alphas = [r["alpha"] for r in valid]
        wins = sum(1 for r in returns if r > 0)
        win_rate = wins / len(returns) * 100
        avg_ret = statistics.mean(returns)
        avg_spy = statistics.mean(spy_returns)
        avg_alpha = statistics.mean(alphas)
        print(f"  {hp:>5}d   {len(results):>7}  {win_rate:>5.1f}%  "
              f"{avg_ret*100:>+10.2f}%  {avg_spy*100:>+8.2f}%  {avg_alpha*100:>+9.2f}%")
    print()

def top_politicians(results, min_trades=10, top_n=20):
    print_header(f"TOP {top_n} POLITICIANS BY TOTAL RETURN (min {min_trades} trades)")
    by_rep = defaultdict(list)
    for r in results:
        by_rep[r["rep"]].append(r["stock_return"])

    rep_stats = []
    for rep, rets in by_rep.items():
        if len(rets) < min_trades:
            continue
        avg = statistics.mean(rets)
        wins = sum(1 for r in rets if r > 0)
        rep_stats.append((rep, len(rets), avg, wins / len(rets)))

    rep_stats.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Politician':<35} {'Trades':>6}  {'Win%':>6}  {'Avg Return':>11}")
    print_separator()
    for rep, n, avg, wr in rep_stats[:top_n]:
        name = rep[:34]
        print(f"  {name:<35} {n:>6}  {wr*100:>5.1f}%  {avg*100:>+10.2f}%")
    if not rep_stats:
        print("  No politicians with enough trades.")
    print()

def top_tickers(results, top_n=20):
    print_header(f"TOP {top_n} MOST PURCHASED TICKERS + AVG RETURN")
    by_ticker = defaultdict(list)
    for r in results:
        by_ticker[r["ticker"]].append(r["stock_return"])

    ticker_stats = []
    for tk, rets in by_ticker.items():
        avg = statistics.mean(rets)
        wins = sum(1 for r in rets if r > 0)
        ticker_stats.append((tk, len(rets), avg, wins / len(rets)))

    ticker_stats.sort(key=lambda x: x[1], reverse=True)
    print(f"  {'Ticker':<10} {'Count':>6}  {'Win%':>6}  {'Avg Return':>11}")
    print_separator()
    for tk, n, avg, wr in ticker_stats[:top_n]:
        print(f"  {tk:<10} {n:>6}  {wr*100:>5.1f}%  {avg*100:>+10.2f}%")
    print()

def monthly_volume_chart(results, width=40):
    print_header("MONTHLY PURCHASE VOLUME (number of trades)")
    monthly = defaultdict(int)
    for r in results:
        ym = r["date"][:7]
        monthly[ym] += 1
    if not monthly:
        print("  No data.")
        return
    months = sorted(monthly.keys())
    max_count = max(monthly.values())
    for ym in months:
        count = monthly[ym]
        b = bar(count, max_count, width)
        print(f"  {ym}  {b}  {count:>4}")
    print()

def return_distribution(results, bins=10):
    print_header("DISTRIBUTION OF STOCK RETURNS")
    rets = [r["stock_return"] * 100 for r in results]
    if not rets:
        print("  No data.")
        return
    min_r = min(rets)
    max_r = max(rets)
    if min_r == max_r:
        print(f"  All returns equal: {min_r:.2f}%")
        return
    step = (max_r - min_r) / bins
    counts = [0] * bins
    for v in rets:
        idx = min(int((v - min_r) / step), bins - 1)
        counts[idx] += 1
    max_count = max(counts)
    print(f"  {'Range':>22}  {'Count':>6}  Bar")
    print_separator()
    for i in range(bins):
        lo = min_r + i * step
        hi = lo + step
        b = bar(counts[i], max_count, 30)
        print(f"  {lo:>+9.1f}% to {hi:>+7.1f}%  {counts[i]:>6}  {b}")
    print()

def best_worst_trades(results, n=10):
    print_header(f"BEST {n} INDIVIDUAL TRADES")
    sorted_results = sorted(results, key=lambda x: x["stock_return"], reverse=True)
    print(f"  {'Date':<12} {'Ticker':<8} {'Rep':<30} {'Return':>9}  {'Alpha':>9}")
    print_separator()
    for r in sorted_results[:n]:
        name = r["rep"][:29]
        print(f"  {r['date']:<12} {r['ticker']:<8} {name:<30} "
              f"{r['stock_return']*100:>+8.2f}%  {pct(r['alpha'])}")
    print()
    print_header(f"WORST {n} INDIVIDUAL TRADES")
    print(f"  {'Date':<12} {'Ticker':<8} {'Rep':<30} {'Return':>9}  {'Alpha':>9}")
    print_separator()
    for r in sorted_results[-n:]:
        name = r["rep"][:29]
        print(f"  {r['date']:<12} {r['ticker']:<8} {name:<30} "
              f"{r['stock_return']*100:>+8.2f}%  {pct(r['alpha'])}")
    print()

def portfolio_simulation(results, initial=10000):
    """
    Simple simulation: start with $10k. On each trade signal, allocate an equal
    fraction based on how many signals are active that month. Show monthly equity.
    Uses a fixed equal-weight per signal model.
    """
    print_header("$10,000 PORTFOLIO SIMULATION (equal weight per signal)")

    if not results:
        print("  No trades to simulate.")
        return

    # Sort by entry date
    sorted_r = sorted(results, key=lambda x: x["date"])

    # For simplicity: track capital, execute each trade with 1/N of current capital
    # where N = number of active concurrent trades at time of entry.
    # We approximate by assuming at most 20 concurrent positions.
    MAX_CONCURRENT = 20
    equity = initial
    monthly_equity = {}

    # Process trades chronologically
    active_trades = []  # (exit_date, pnl_factor)

    for r in sorted_r:
        entry = r["actual_entry"] or r["date"]
        exit_ = r["actual_exit"]
        if not exit_:
            continue

        # Close any trades that have exited (return principal + gain/loss)
        still_active = []
        for (ed, pnl_factor, alloc) in active_trades:
            if ed <= entry:
                equity += alloc * (1 + pnl_factor)
            else:
                still_active.append((ed, pnl_factor, alloc))
        active_trades = still_active

        if len(active_trades) >= MAX_CONCURRENT:
            continue  # Skip if too many open positions

        slots = MAX_CONCURRENT - len(active_trades)
        alloc = equity / slots if slots > 0 else 0
        alloc = min(alloc, equity * 0.10)  # cap at 10% per trade

        pnl_factor = r["stock_return"]  # net P&L as fraction
        equity -= alloc
        active_trades.append((exit_, pnl_factor, alloc))

        ym = entry[:7]
        monthly_equity[ym] = equity + sum(a for (_, _, a) in active_trades)

    # Close remaining trades
    for (ed, pnl_factor, alloc) in active_trades:
        equity += alloc * (1 + pnl_factor)

    # Build monthly equity curve from snapshots
    if not monthly_equity:
        print("  No monthly data.")
        return

    months = sorted(monthly_equity.keys())
    max_eq = max(monthly_equity.values())
    min_eq = min(monthly_equity.values())
    span = max_eq - min_eq if max_eq != min_eq else 1

    print(f"  Initial capital:  ${initial:>10,.2f}")
    print(f"  Final equity:     ${equity:>10,.2f}")
    total_ret = (equity - initial) / initial * 100
    print(f"  Total return:     {total_ret:>+10.2f}%")
    print()
    print(f"  {'Month':<10}  {'Equity':>12}  Curve")
    print_separator()
    for ym in months:
        eq = monthly_equity[ym]
        frac = (eq - min_eq) / span
        b = "#" * int(frac * 35)
        print(f"  {ym:<10}  ${eq:>11,.2f}  {b}")
    print()

def top_traders_detail(results, top_n=10):
    print_header(f"TOP {top_n} TRADERS WITH THEIR BEST PICKS")
    by_rep = defaultdict(list)
    for r in results:
        by_rep[r["rep"]].append(r)

    rep_avg = []
    for rep, trades in by_rep.items():
        if len(trades) < 3:
            continue
        avg = statistics.mean(t["stock_return"] for t in trades)
        rep_avg.append((rep, avg, trades))

    rep_avg.sort(key=lambda x: x[1], reverse=True)

    for rep, avg_ret, trades in rep_avg[:top_n]:
        print(f"\n  {rep}  (avg return: {avg_ret*100:+.2f}%, {len(trades)} trades)")
        print(f"  {'Date':<12} {'Ticker':<8} {'Description':<30} {'Return':>9}")
        print("  " + "-" * 60)
        best_picks = sorted(trades, key=lambda x: x["stock_return"], reverse=True)[:5]
        for t in best_picks:
            desc = (t["description"] or "")[:29]
            print(f"  {t['date']:<12} {t['ticker']:<8} {desc:<30} "
                  f"{t['stock_return']*100:>+8.2f}%")
    print()

# ── Demo data generator ─────────────────────────────────────────────────────────

def _generate_demo_trades(start_date, end_date):
    """
    Generate realistic-looking synthetic congressional trade disclosures for demo/testing.
    Uses well-known tickers that yfinance can fetch historical data for.
    """
    import random
    random.seed(42)

    politicians = [
        "Nancy Pelosi", "Austin Scott", "Michael McCaul", "Ro Khanna",
        "Josh Gottheimer", "David Rouzer", "Thomas Massie", "Dan Crenshaw",
        "Dean Phillips", "Virginia Foxx", "Greg Gianforte", "Susie Lee",
        "Markwayne Mullin", "Tom Reed", "Chip Roy", "Katherine Clark",
    ]
    tickers = [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AMD",
        "CRM", "NFLX", "INTC", "QCOM", "AVGO", "NOW", "SNOW", "PLTR",
        "SPY", "QQQ", "COST", "WMT", "JPM", "BAC", "GS", "V", "MA",
        "LMT", "RTX", "NOC", "GD", "BA", "XOM", "CVX", "SLB",
    ]
    amounts = [
        "$1,001 - $15,000", "$15,001 - $50,000",
        "$50,001 - $100,000", "$100,001 - $250,000",
        "$250,001 - $500,000",
    ]

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    total_days = (end_dt - start_dt).days

    trades = []
    # ~8 purchases disclosed per week on average
    num_trades = total_days // 7 * 8
    for _ in range(num_trades):
        offset = random.randint(0, total_days)
        trade_date = (start_dt + timedelta(days=offset)).strftime("%Y-%m-%d")
        # Disclosure 30-45 days after transaction
        disc_offset = random.randint(30, 45)
        disc_dt = start_dt + timedelta(days=offset + disc_offset)
        if disc_dt > end_dt:
            continue
        disc_date = disc_dt.strftime("%Y-%m-%d")

        trades.append({
            "disclosure_date": disc_date,
            "transaction_date": trade_date,
            "representative": random.choice(politicians),
            "ticker": random.choice(tickers),
            "type": "Purchase",
            "amount": random.choice(amounts),
            "asset_description": "Common Stock",
            "district": f"XX-{random.randint(1,20):02d}",
        })

    print(f"  [demo] Generated {len(trades)} synthetic trade disclosures.")
    return trades


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Congressional stock trade backtester")
    parser.add_argument("--start", default="2020-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2024-12-31", help="End date YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=60, help="Primary holding period in days")
    parser.add_argument("--min-amount", type=float, default=0,
                        help="Minimum disclosed transaction midpoint ($)")
    parser.add_argument("--top-traders", action="store_true",
                        help="Show top 10 traders and their best picks")
    parser.add_argument("--demo", action="store_true",
                        help="Use synthetic demo data (no network needed)")
    parser.add_argument("--quiver-key", default=os.environ.get("QUIVER_API_KEY", ""),
                        metavar="KEY",
                        help="Quiver Quant API key (free at quiverquant.com). "
                             "Can also set QUIVER_API_KEY env var.")

    # ── Strategy improvement flags ──────────────────────────────────────────
    parser.add_argument("--cluster", type=int, default=0, metavar="N",
                        help="Min # unique politicians buying same ticker within "
                             "--cluster-window days. E.g. --cluster 3 for cluster trades only.")
    parser.add_argument("--cluster-window", type=int, default=30, metavar="DAYS",
                        help="Window in days for cluster detection (default: 30)")
    parser.add_argument("--follow-reps", default="", metavar="NAMES",
                        help="Comma-separated rep name substrings to follow exclusively. "
                             "E.g. --follow-reps 'McCaul,Pelosi,Gianforte'")
    parser.add_argument("--use-transaction-date", action="store_true",
                        help="Enter on actual transaction date (30-45 days before disclosure). "
                             "Tests whether edge comes from timing vs. public filing event.")
    parser.add_argument("--no-dedupe", action="store_true",
                        help="Don't deduplicate trades by rep+ticker+date "
                             "(by default duplicates are removed)")
    parser.add_argument("--chamber", default="", choices=["", "house", "senate"],
                        help="Filter by chamber: house, senate, or all (default: all)")
    parser.add_argument("--trend-filter", action="store_true",
                        help="Only trade when SPY is above its 200-day moving average. "
                             "Avoids bear markets / corrections (2020 crash, 2022 bear).")
    parser.add_argument("--trend-ma", type=int, default=200, metavar="DAYS",
                        help="MA period for trend filter (default: 200)")

    args = parser.parse_args()

    holding_periods = sorted(set([30, 60, 90, 180, args.days]))

    follow_reps = [r.strip() for r in args.follow_reps.split(",") if r.strip()] \
                  if args.follow_reps else []

    print()
    print_header("CONGRESSIONAL STOCK TRADE BACKTESTER")
    print(f"  Date range:      {args.start} to {args.end}")
    print(f"  Min amount:      ${args.min_amount:,.0f}")
    print(f"  Primary hold:    {args.days} days")
    print(f"  Hold periods:    {holding_periods}")
    if args.cluster:
        print(f"  Cluster filter:  {args.cluster}+ politicians within {args.cluster_window} days")
    if follow_reps:
        print(f"  Follow reps:     {', '.join(follow_reps)}")
    if args.use_transaction_date:
        print(f"  Entry date:      TRANSACTION DATE (not disclosure — 30-45 days earlier)")
    else:
        print(f"  Entry date:      Disclosure date (public filing)")
    if args.chamber:
        print(f"  Chamber filter:  {args.chamber.upper()}")
    print(f"  Deduplication:   {'OFF' if args.no_dedupe else 'ON (rep+ticker+date)'}")
    print()

    # 1. Fetch data
    if args.demo:
        raw_trades = _generate_demo_trades(args.start, args.end)
    else:
        raw_trades = fetch_house_trades(quiver_key=args.quiver_key)

    # Chamber filter (Quiver bulk data has Chamber field)
    if args.chamber:
        ch = args.chamber.lower()
        raw_trades = [t for t in raw_trades
                      if ch in (t.get("chamber") or "").lower()]
        print(f"  After chamber filter ({args.chamber}): {len(raw_trades)} raw records.")

    # 2. Filter
    purchases = filter_purchases(
        raw_trades, args.start, args.end, args.min_amount,
        use_transaction_date=args.use_transaction_date,
        follow_reps=follow_reps if follow_reps else None,
        dedupe=not args.no_dedupe,
    )
    print(f"  After base filter: {len(purchases)} purchase disclosures.")

    # 2b. Cluster filter (optional)
    if args.cluster >= 2:
        before = len(purchases)
        purchases = apply_cluster_filter(purchases, args.cluster, args.cluster_window)
        print(f"  After cluster filter ({args.cluster}+ reps / {args.cluster_window}d): "
              f"{len(purchases)} trades (removed {before - len(purchases)})")

    if not purchases:
        print("  No trades match filters. Exiting.")
        return

    # 3. Load cache
    print(f"  Loading price cache from {CACHE_FILE}...")
    cache = load_price_cache()
    cached_count = len(cache)
    print(f"  Cache has {cached_count} entries.")

    if args.trend_filter:
        print(f"  Trend filter:    SPY > {args.trend_ma}d MA (bear market avoidance)")

    # 4. Run backtest for all holding periods
    all_results = {}
    for hp in holding_periods:
        print(f"\nRunning backtest for {hp}-day holding period...")
        results = run_backtest(purchases, hp, cache, verbose=False,
                               trend_filter=args.trend_filter,
                               trend_ma_days=args.trend_ma)
        all_results[hp] = results
        valid = [r for r in results if r["alpha"] is not None]
        print(f"  {len(valid)} trades completed with valid prices out of {len(purchases)} signals.")

    # Save cache after all fetches
    save_price_cache(cache)
    print(f"  Cache updated: {len(cache)} entries (was {cached_count}).")

    # 5. Use primary holding period results for most analyses
    primary = all_results.get(args.days, [])

    print()
    print_header("SUMMARY")
    print(f"  Trades analysed (primary {args.days}d): {len(primary)}")
    if primary:
        rets = [r["stock_return"] for r in primary]
        alphas = [r["alpha"] for r in primary if r["alpha"] is not None]
        wins = sum(1 for r in rets if r > 0)
        print(f"  Win rate:        {wins/len(rets)*100:.1f}%")
        print(f"  Avg return:      {statistics.mean(rets)*100:+.2f}%")
        if alphas:
            print(f"  Avg alpha:       {statistics.mean(alphas)*100:+.2f}%")
        if len(rets) > 1:
            print(f"  Median return:   {statistics.median(rets)*100:+.2f}%")
            print(f"  Stdev return:    {statistics.stdev(rets)*100:.2f}%")
            best = max(primary, key=lambda x: x["stock_return"])
            worst = min(primary, key=lambda x: x["stock_return"])
            print(f"  Best trade:      {best['ticker']} on {best['date']} "
                  f"({best['stock_return']*100:+.2f}%)")
            print(f"  Worst trade:     {worst['ticker']} on {worst['date']} "
                  f"({worst['stock_return']*100:+.2f}%)")
    print()

    # 6. Analysis sections
    analyze_by_holding_period(all_results, holding_periods)
    top_politicians(primary, min_trades=10, top_n=20)
    top_tickers(primary, top_n=20)
    monthly_volume_chart(primary)
    return_distribution(primary)
    best_worst_trades(primary, n=10)
    portfolio_simulation(primary)

    if args.top_traders:
        top_traders_detail(primary, top_n=10)


if __name__ == "__main__":
    main()
