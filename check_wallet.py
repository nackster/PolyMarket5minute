"""Check on-chain balances: MATIC, native USDC, and USDC.e (bridged)."""
import os
import requests
from dotenv import load_dotenv
from eth_account import Account

load_dotenv()

pk = os.getenv("POLYMARKET_PRIVATE_KEY", "")
acct = Account.from_key(pk)
addr = acct.address
print(f"Wallet address: {addr}")

RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://polygon-rpc.com",
]

BALANCE_OF = "0x70a08231" + "000000000000000000000000" + addr[2:].lower()
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

def rpc_call(payload):
    for rpc in RPCS:
        try:
            r = requests.post(rpc, json=payload, timeout=10)
            data = r.json()
            if "result" in data:
                return data["result"]
            print(f"  RPC {rpc} returned error: {data.get('error', data)}")
        except Exception as e:
            print(f"  RPC {rpc} failed: {e}")
    return None

print("\nChecking on-chain balances...")

# MATIC
result = rpc_call({"jsonrpc":"2.0","id":1,"method":"eth_getBalance","params":[addr,"latest"]})
matic = int(result, 16) / 1e18 if result else -1

# USDC.e
result = rpc_call({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":USDC_E,"data":BALANCE_OF},"latest"]})
usdc_e = int(result, 16) / 1e6 if result else -1

# Native USDC
result = rpc_call({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":USDC_NATIVE,"data":BALANCE_OF},"latest"]})
usdc_native = int(result, 16) / 1e6 if result else -1

print(f"\nMATIC:       {matic:.6f}")
print(f"USDC.e:      {usdc_e:.6f}  (bridged - Polymarket uses this)")
print(f"USDC native: {usdc_native:.6f}")

if usdc_native > 0 and usdc_e == 0:
    print("\nYou have native USDC but Polymarket needs USDC.e (bridged)!")
    print("Swap native USDC -> USDC.e on QuickSwap or 1inch")
if matic < 0.01:
    print("\nYou need MATIC for gas! Send at least 0.1 MATIC to this wallet.")
if usdc_e > 0 and matic >= 0.01:
    print("\nWallet looks good! USDC.e + MATIC available.")

print("\n--- CLOB API balance ---")
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

for sig_type, name in [(0, "EOA"), (1, "POLY_PROXY")]:
    kwargs = dict(key=pk, chain_id=137, signature_type=sig_type)
    proxy = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
    if sig_type == 1 and proxy:
        kwargs["funder"] = proxy
    client = ClobClient("https://clob.polymarket.com", **kwargs)
    try:
        creds = client.derive_api_key(nonce=0)
        client.set_api_creds(creds)
    except:
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
        except Exception as e:
            print(f"  {name}: creds failed - {e}")
            continue
    try:
        bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        print(f"  {name}: {bal}")
    except Exception as e:
        print(f"  {name}: failed - {e}")
