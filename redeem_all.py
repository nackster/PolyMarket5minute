"""Scan and redeem all outstanding winning conditional tokens.

Scans the last 48 hours of both market types:
  - btc-updown-5m-{ts}                          (5-minute markets)
  - bitcoin-up-or-down-{month}-{day}-{year}-{hour}-et  (hourly markets)

For each market that has resolved, checks if we hold winning tokens
and redeems them back to USDC.e.

Usage:
    python redeem_all.py           # scan + redeem
    python redeem_all.py --dry-run # scan only, no transactions
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

PRIVATE_KEY   = os.getenv("POLYMARKET_PRIVATE_KEY", "")
GAMMA_API     = "https://gamma-api.polymarket.com"
RPC_URL       = "https://polygon-bor-rpc.publicnode.com"
CT_ADDRESS    = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E        = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
SCAN_HOURS    = 48   # how far back to scan

MONTH_NAMES = ['', 'january', 'february', 'march', 'april', 'may', 'june',
               'july', 'august', 'september', 'october', 'november', 'december']

CT_ABI = [
    {'inputs':[{'name':'account','type':'address'},{'name':'id','type':'uint256'}],
     'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'type':'function'},
    {'inputs':[{'name':'','type':'bytes32'}],
     'name':'payoutDenominator','outputs':[{'name':'','type':'uint256'}],'type':'function'},
    {'inputs':[{'name':'','type':'bytes32'},{'name':'','type':'uint256'}],
     'name':'payoutNumerators','outputs':[{'name':'','type':'uint256'}],'type':'function'},
    {'inputs':[{'name':'collateralToken','type':'address'},
               {'name':'parentCollectionId','type':'bytes32'},
               {'name':'conditionId','type':'bytes32'},
               {'name':'indexSets','type':'uint256[]'}],
     'name':'redeemPositions','outputs':[],'type':'function'},
]
USDC_ABI = [
    {'inputs':[{'name':'account','type':'address'}],
     'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'type':'function'},
]


# ── Slug generators ──────────────────────────────────────────────────────────

def slugs_5m(hours_back: int) -> list[tuple[str, int]]:
    """Return (slug, window_end_ts) for all 5-min windows in the last N hours."""
    now    = int(time.time())
    start  = now - hours_back * 3600
    base   = (start // 300) * 300
    result = []
    for ts in range(base, now, 300):
        end = ts + 300
        if now - end < 120:   # skip windows that just closed (oracle delay)
            continue
        result.append((f"btc-updown-5m-{ts}", end))
    return result


def slugs_hourly(hours_back: int) -> list[tuple[str, int]]:
    """Return (slug, window_end_utc) for all hourly windows in the last N hours."""
    ET_OFFSET = -5  # Polymarket uses EST (UTC-5), no DST
    now       = int(time.time())
    result    = []

    for h in range(1, hours_back + 1):
        scan_utc  = now - h * 3600
        est_ts    = scan_utc + ET_OFFSET * 3600
        est_start = (est_ts // 3600) * 3600
        est_end   = est_start + 3600
        window_end_utc = est_end - ET_OFFSET * 3600

        if now - window_end_utc < 1800:  # skip if closed less than 30 min ago
            continue

        dt      = datetime.utcfromtimestamp(est_end)
        h24     = dt.hour
        if h24 == 0:    hstr = "12am"
        elif h24 < 12:  hstr = f"{h24}am"
        elif h24 == 12: hstr = "12pm"
        else:           hstr = f"{h24 - 12}pm"

        slug = (f"bitcoin-up-or-down-"
                f"{MONTH_NAMES[dt.month]}-{dt.day}-{dt.year}-{hstr}-et")
        result.append((slug, window_end_utc))

    return result


# ── Market scanner ───────────────────────────────────────────────────────────

def fetch_market(slug: str) -> dict | None:
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=8)
        if r.ok and r.json():
            return r.json()[0]["markets"][0]
    except Exception:
        pass
    return None


def scan_for_redeemable(w3, ct, addr: str, slugs: list[tuple[str, int]]) -> list[dict]:
    """
    Walk through all slugs, check if we hold winning tokens.
    Returns list of dicts: {slug, condition_id, index_set, balance, token_id}
    """
    found = []
    total = len(slugs)

    for i, (slug, window_end) in enumerate(slugs):
        print(f"\r  Scanning {i+1}/{total} markets...", end="", flush=True)

        m = fetch_market(slug)
        if not m:
            continue

        cid = m.get("conditionId", "")
        if not cid:
            continue

        cid_bytes = bytes.fromhex(cid[2:] if cid.startswith("0x") else cid)

        # Skip if oracle hasn't resolved yet
        try:
            payout_denom = ct.functions.payoutDenominator(cid_bytes).call()
        except Exception:
            continue
        if payout_denom == 0:
            continue

        # Check each outcome token
        raw_ids = m.get("clobTokenIds", "[]")
        token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids

        for outcome_idx, tid in enumerate(token_ids):
            try:
                bal = ct.functions.balanceOf(addr, int(tid)).call()
            except Exception:
                continue
            if bal == 0:
                continue

            try:
                payout_num = ct.functions.payoutNumerators(cid_bytes, outcome_idx).call()
            except Exception:
                continue
            if payout_num == 0:
                print(f"\n  {slug}: holding LOSING tokens (outcome {outcome_idx}) — skipping")
                continue

            index_set   = 1 << outcome_idx
            usdc_value  = bal / 1e6
            print(f"\n  FOUND: {slug}")
            print(f"    outcome_idx={outcome_idx}  indexSet={index_set}  "
                  f"bal={bal}  (~${usdc_value:.2f} USDC.e)")
            found.append({
                "slug":         slug,
                "condition_id": cid,
                "index_set":    index_set,
                "balance":      bal,
                "usdc_value":   usdc_value,
                "token_id":     tid,
            })
            break  # only one winning side per market

    print()
    return found


# ── Redemption ────────────────────────────────────────────────────────────────

def redeem_all(w3, ct, usdc, acct, to_redeem: list[dict]) -> float:
    """Submit redeemPositions transactions. Returns total USDC.e received."""
    bal_before = usdc.functions.balanceOf(acct.address).call() / 1e6
    print(f"\nWallet BEFORE: ${bal_before:.4f} USDC.e")

    gas_price = int(w3.eth.gas_price * 1.5)
    nonce     = w3.eth.get_transaction_count(acct.address)
    redeemed  = 0

    for i, item in enumerate(to_redeem):
        cid_bytes = bytes.fromhex(
            item["condition_id"][2:] if item["condition_id"].startswith("0x")
            else item["condition_id"]
        )
        print(f"\n  [{i+1}/{len(to_redeem)}] Redeeming {item['slug']} "
              f"(~${item['usdc_value']:.2f})...")
        try:
            tx = ct.functions.redeemPositions(
                Web3.to_checksum_address(USDC_E),
                b'\x00' * 32,
                cid_bytes,
                [item["index_set"]],
            ).build_transaction({
                "from":     acct.address,
                "nonce":    nonce + i,
                "gas":      200_000,
                "gasPrice": gas_price,
                "chainId":  137,
            })
            signed  = acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"    TX sent: 0x{tx_hash.hex()[:20]}...")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
            if receipt.status == 1:
                print(f"    OK — confirmed in block {receipt.blockNumber}")
                redeemed += 1
            else:
                print(f"    FAILED — tx reverted")
        except Exception as e:
            print(f"    ERROR: {e}")

    bal_after = usdc.functions.balanceOf(acct.address).call() / 1e6
    gained    = bal_after - bal_before
    print(f"\nWallet AFTER:  ${bal_after:.4f} USDC.e")
    print(f"Received:      +${gained:.4f} USDC.e  ({redeemed}/{len(to_redeem)} redeemed)")
    return gained


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Redeem all winning Polymarket tokens")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan only, do not submit transactions")
    parser.add_argument("--hours", type=int, default=SCAN_HOURS,
                        help=f"Hours to scan back (default {SCAN_HOURS})")
    args = parser.parse_args()

    if not PRIVATE_KEY:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env")
        return

    print("=" * 60)
    print("  Polymarket Token Redemption Scanner")
    print("=" * 60)
    if args.dry_run:
        print("  [DRY RUN — no transactions will be sent]")
    print(f"  Scanning last {args.hours} hours\n")

    # Connect
    w3   = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("ERROR: Could not connect to Polygon RPC")
        return

    acct = w3.eth.account.from_key(PRIVATE_KEY)
    ct   = w3.eth.contract(address=Web3.to_checksum_address(CT_ADDRESS), abi=CT_ABI)
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E),     abi=USDC_ABI)

    print(f"Wallet:  {acct.address}")
    bal = usdc.functions.balanceOf(acct.address).call() / 1e6
    print(f"Balance: ${bal:.4f} USDC.e\n")

    # Collect slugs from both market types
    s5m     = slugs_5m(args.hours)
    shourly = slugs_hourly(args.hours)
    print(f"Scanning {len(s5m)} x 5-min markets + {len(shourly)} x hourly markets...")

    all_slugs = s5m + shourly

    to_redeem = scan_for_redeemable(w3, ct, acct.address, all_slugs)

    if not to_redeem:
        print("Nothing to redeem — wallet is clean.")
        return

    total_value = sum(r["usdc_value"] for r in to_redeem)
    print(f"\nFound {len(to_redeem)} position(s) to redeem (~${total_value:.2f} USDC.e total)")

    if args.dry_run:
        print("\n[DRY RUN] Would redeem:")
        for r in to_redeem:
            print(f"  {r['slug']}  ~${r['usdc_value']:.2f}")
        return

    redeem_all(w3, ct, usdc, acct, to_redeem)


if __name__ == "__main__":
    main()
