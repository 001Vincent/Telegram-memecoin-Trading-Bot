"""
Jupiter DEX swap execution for Ragnar Solutions Bot.
Uses Jupiter v6 Quote API + Solana RPC to build and send real transactions.
Requires: cryptography (already installed)
"""
import json, urllib.request, struct, base64, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from crypto_utils import derive_solana_keypair, mnemonic_to_seed, _derive_slip10_ed25519, base58_encode, base58_decode

JUPITER_QUOTE  = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP   = "https://quote-api.jup.ag/v6/swap"
SOL_RPC        = "https://api.mainnet-beta.solana.com"
SOL_MINT       = "So11111111111111111111111111111111111111112"
LAMPORTS       = 1_000_000_000

def _http_post(url, payload, timeout=20):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data,
           headers={"Content-Type":"application/json","User-Agent":"RagnarSolutionsBot/1.0"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def _http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent":"RagnarSolutionsBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def _rpc(method, params, timeout=20):
    return _http_post(SOL_RPC, {"jsonrpc":"2.0","id":1,"method":method,"params":params}, timeout)

def get_token_balance(address: str, mint: str) -> float:
    """Get SPL token balance for a wallet."""
    if mint == SOL_MINT:
        res = _rpc("getBalance", [address, {"commitment":"confirmed"}])
        if "result" in res:
            return res["result"]["value"] / LAMPORTS
        return 0.0
    res = _rpc("getTokenAccountsByOwner", [
        address,
        {"mint": mint},
        {"encoding": "jsonParsed", "commitment": "confirmed"}
    ])
    try:
        accounts = res["result"]["value"]
        if not accounts:
            return 0.0
        amt = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
        return float(amt["uiAmount"] or 0)
    except:
        return 0.0

def _sign_transaction(tx_bytes: bytes, privkey_bytes: bytes) -> bytes:
    """Sign a Solana transaction with Ed25519 private key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    privkey = Ed25519PrivateKey.from_private_bytes(privkey_bytes)
    # Solana tx: first 64 bytes = signature placeholder, then message
    # Find the message start (after signature count + signatures)
    sig_count = tx_bytes[0]
    msg_start = 1 + sig_count * 64
    message   = tx_bytes[msg_start:]
    signature = privkey.sign(message)
    # Replace first signature
    result = bytearray(tx_bytes)
    result[1:65] = signature
    return bytes(result)

def get_latest_blockhash() -> str:
    res = _rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
    try:
        return res["result"]["value"]["blockhash"]
    except Exception:
        return None

def get_token_decimals(mint: str) -> int:
    if mint == SOL_MINT:
        return 9
    res = _rpc("getTokenSupply", [mint])
    try:
        return res["result"]["value"]["decimals"]
    except Exception:
        return 6

def _encode_shortvec(n: int) -> bytes:
    """Solana 'compact-u16' shortvec length encoding."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)

def _confirm_tx(tx_hash: str, tries: int = 15, delay: int = 2):
    import time
    for _ in range(tries):
        time.sleep(delay)
        conf = _rpc("getSignatureStatuses", [[tx_hash], {"searchTransactionHistory": True}])
        try:
            status = conf["result"]["value"][0]
            if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
                if status.get("err"):
                    return {"confirmed": True, "err": status["err"]}
                return {"confirmed": True, "err": None}
        except Exception:
            pass
    return {"confirmed": False, "err": "timeout"}

def send_sol(mnemonic: str, wallet_index: int, to_address: str, amount_sol: float) -> dict:
    """
    Build, sign, and broadcast a real native SOL transfer (System Program
    Transfer instruction) — no third-party swap API involved.
    Returns {"success": bool, "tx_hash": str, "error": str}
    """
    try:
        from_address, _, privkey_hex = derive_solana_keypair(mnemonic, wallet_index)
        privkey_bytes = bytes.fromhex(privkey_hex)

        try:
            from_pk = base58_decode(from_address, length=32)
            to_pk = base58_decode(to_address, length=32)
        except Exception as e:
            return {"success": False, "error": f"Invalid address: {e}"}

        lamports = int(round(amount_sol * LAMPORTS))
        if lamports <= 0:
            return {"success": False, "error": "Amount too small"}

        # Leave a small buffer for the network fee (base fee is 5000 lamports
        # per signature; we don't attach compute-budget instructions here).
        bal_res = _rpc("getBalance", [from_address, {"commitment": "confirmed"}])
        try:
            bal_lamports = bal_res["result"]["value"]
        except Exception:
            return {"success": False, "error": "Could not fetch balance"}
        if lamports + 5000 > bal_lamports:
            return {"success": False, "error": "Amount + network fee exceeds balance"}

        blockhash = get_latest_blockhash()
        if not blockhash:
            return {"success": False, "error": "Could not fetch recent blockhash"}
        recent_blockhash = base58_decode(blockhash, length=32)

        system_program = base58_decode(base58_encode(b'\x00' * 32), length=32)  # all-zero 32 bytes

        header = bytes([1, 0, 1])  # 1 required sig, 0 readonly-signed, 1 readonly-unsigned (system program)
        account_keys = from_pk + to_pk + system_program
        ix_data = struct.pack('<I', 2) + struct.pack('<Q', lamports)  # System::Transfer
        instruction = bytes([2]) + _encode_shortvec(2) + bytes([0, 1]) + _encode_shortvec(len(ix_data)) + ix_data
        instructions_section = _encode_shortvec(1) + instruction

        message = header + _encode_shortvec(3) + account_keys + recent_blockhash + instructions_section

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        privkey = Ed25519PrivateKey.from_private_bytes(privkey_bytes)
        signature = privkey.sign(message)

        tx_bytes = _encode_shortvec(1) + signature + message
        tx_b64 = base64.b64encode(tx_bytes).decode()

        send_res = _rpc("sendTransaction", [
            tx_b64,
            {"encoding": "base64", "skipPreflight": False,
             "preflightCommitment": "confirmed", "maxRetries": 3}
        ])
        if "error" in send_res or not send_res.get("result"):
            return {"success": False, "error": f"Send failed: {send_res.get('error', send_res)}"}

        tx_hash = send_res["result"]
        conf = _confirm_tx(tx_hash)
        if not conf["confirmed"]:
            return {"success": False, "error": "Confirmation timeout", "tx_hash": tx_hash}
        if conf["err"]:
            return {"success": False, "error": f"TX failed on-chain: {conf['err']}", "tx_hash": tx_hash}

        return {"success": True, "tx_hash": tx_hash, "amount_sol": amount_sol, "to_address": to_address}

    except Exception as e:
        return {"success": False, "error": str(e)}

def swap_tokens(mnemonic: str, wallet_index: int, from_mint: str, to_mint: str,
                 amount_ui: float, slippage_bps: int = 150) -> dict:
    """
    Generic token-to-token (or SOL<->token) swap via Jupiter, for the Swap Token feature.
    amount_ui is in human units of from_mint (e.g. 0.5 SOL, or 100 of a token).
    """
    try:
        address, _, privkey_hex = derive_solana_keypair(mnemonic, wallet_index)
        privkey_bytes = bytes.fromhex(privkey_hex)

        decimals = get_token_decimals(from_mint)
        amount_raw = int(round(amount_ui * (10 ** decimals)))
        if amount_raw <= 0:
            return {"success": False, "error": "Amount too small"}

        if from_mint == SOL_MINT:
            bal_res = _rpc("getBalance", [address, {"commitment": "confirmed"}])
            try:
                bal_lamports = bal_res["result"]["value"]
            except Exception:
                bal_lamports = 0
            if amount_raw + 5000 > bal_lamports:
                return {"success": False, "error": "Amount + network fee exceeds SOL balance"}
        else:
            bal = get_token_balance(address, from_mint)
            if amount_ui > bal:
                return {"success": False, "error": f"Insufficient token balance (have {bal})"}

        quote_url = (
            f"{JUPITER_QUOTE}"
            f"?inputMint={from_mint}"
            f"&outputMint={to_mint}"
            f"&amount={amount_raw}"
            f"&slippageBps={slippage_bps}"
            f"&onlyDirectRoutes=false"
        )
        quote = _http_get(quote_url)
        if "error" in quote or not quote.get("outAmount"):
            return {"success": False, "error": f"Quote failed: {quote.get('error','No route found')}"}

        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": address,
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": 50000,
            "dynamicComputeUnitLimit": True,
        }
        swap_res = _http_post(JUPITER_SWAP, swap_payload)
        if "error" in swap_res or not swap_res.get("swapTransaction"):
            return {"success": False, "error": f"Swap build failed: {swap_res.get('error','Unknown')}"}

        tx_bytes = base64.b64decode(swap_res["swapTransaction"])
        signed_tx = _sign_transaction(tx_bytes, privkey_bytes)
        tx_b64 = base64.b64encode(signed_tx).decode()

        send_res = _rpc("sendTransaction", [
            tx_b64,
            {"encoding": "base64", "skipPreflight": False,
             "preflightCommitment": "confirmed", "maxRetries": 3}
        ])
        if "error" in send_res or not send_res.get("result"):
            return {"success": False, "error": f"Send failed: {send_res.get('error', send_res)}"}

        tx_hash = send_res["result"]
        conf = _confirm_tx(tx_hash)
        if not conf["confirmed"]:
            return {"success": False, "error": "Confirmation timeout", "tx_hash": tx_hash}
        if conf["err"]:
            return {"success": False, "error": f"TX failed on-chain: {conf['err']}", "tx_hash": tx_hash}

        out_decimals = get_token_decimals(to_mint)
        out_ui = int(quote.get("outAmount", 0)) / (10 ** out_decimals)
        return {"success": True, "tx_hash": tx_hash, "in_amount": amount_ui, "out_amount": out_ui}

    except Exception as e:
        return {"success": False, "error": str(e)}

def buy_token(mnemonic: str, token_ca: str, amount_sol: float,
              slippage_bps: int = 150, wallet_index: int = 0) -> dict:
    """
    Execute a real buy on Jupiter DEX.
    Returns {"success": bool, "tx_hash": str, "error": str}
    """
    try:
        address, _, privkey_hex = derive_solana_keypair(mnemonic, wallet_index)
        privkey_bytes = bytes.fromhex(privkey_hex)
        amount_lamports = int(amount_sol * LAMPORTS)

        # 1. Get quote
        quote_url = (
            f"{JUPITER_QUOTE}"
            f"?inputMint={SOL_MINT}"
            f"&outputMint={token_ca}"
            f"&amount={amount_lamports}"
            f"&slippageBps={slippage_bps}"
            f"&onlyDirectRoutes=false"
        )
        quote = _http_get(quote_url)
        if "error" in quote or not quote.get("outAmount"):
            return {"success": False, "error": f"Quote failed: {quote.get('error','No route found')}"}

        # 2. Get swap transaction
        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": address,
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": 50000,
            "dynamicComputeUnitLimit": True,
        }
        swap_res = _http_post(JUPITER_SWAP, swap_payload)
        if "error" in swap_res or not swap_res.get("swapTransaction"):
            return {"success": False, "error": f"Swap build failed: {swap_res.get('error','Unknown')}"}

        # 3. Decode and sign transaction
        tx_bytes = base64.b64decode(swap_res["swapTransaction"])
        signed_tx = _sign_transaction(tx_bytes, privkey_bytes)
        tx_b64    = base64.b64encode(signed_tx).decode()

        # 4. Send transaction
        send_res = _rpc("sendTransaction", [
            tx_b64,
            {"encoding": "base64", "skipPreflight": False,
             "preflightCommitment": "confirmed", "maxRetries": 3}
        ])
        if "error" in send_res or not send_res.get("result"):
            err = send_res.get("error", send_res)
            return {"success": False, "error": f"Send failed: {err}"}

        tx_hash = send_res["result"]

        # 5. Confirm
        for _ in range(15):
            import time; time.sleep(2)
            conf = _rpc("getSignatureStatuses", [[tx_hash], {"searchTransactionHistory": True}])
            try:
                status = conf["result"]["value"][0]
                if status and status.get("confirmationStatus") in ("confirmed","finalized"):
                    if status.get("err"):
                        return {"success": False, "error": f"TX failed on-chain: {status['err']}",
                                "tx_hash": tx_hash}
                    out_amount = int(quote.get("outAmount", 0))
                    out_ui     = out_amount / (10 ** int(quote.get("outputMint", {}).get("decimals", 6)
                                                         if isinstance(quote.get("outputMint"), dict) else 6))
                    return {"success": True, "tx_hash": tx_hash,
                            "out_amount": out_ui, "in_amount": amount_sol}
            except:
                pass
        return {"success": False, "error": "Confirmation timeout", "tx_hash": tx_hash}

    except Exception as e:
        return {"success": False, "error": str(e)}

def sell_token(mnemonic: str, token_ca: str, sell_pct: float,
               slippage_bps: int = 150, wallet_index: int = 0) -> dict:
    """
    Execute a real sell on Jupiter DEX.
    sell_pct: 0.25 = 25%, 0.5 = 50%, 1.0 = 100%
    """
    try:
        address, _, privkey_hex = derive_solana_keypair(mnemonic, wallet_index)
        privkey_bytes = bytes.fromhex(privkey_hex)

        # Get current token balance
        token_bal = get_token_balance(address, token_ca)
        if token_bal <= 0:
            return {"success": False, "error": "No token balance found"}

        # Get token decimals via RPC
        mint_info = _rpc("getTokenSupply", [token_ca])
        try:
            decimals = mint_info["result"]["value"]["decimals"]
        except:
            decimals = 6

        sell_amount = int(token_bal * sell_pct * (10 ** decimals))
        if sell_amount <= 0:
            return {"success": False, "error": "Sell amount too small"}

        # Quote
        quote_url = (
            f"{JUPITER_QUOTE}"
            f"?inputMint={token_ca}"
            f"&outputMint={SOL_MINT}"
            f"&amount={sell_amount}"
            f"&slippageBps={slippage_bps}"
        )
        quote = _http_get(quote_url)
        if "error" in quote or not quote.get("outAmount"):
            return {"success": False, "error": f"Quote failed: {quote.get('error','No route')}"}

        # Swap tx
        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": address,
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": 50000,
            "dynamicComputeUnitLimit": True,
        }
        swap_res = _http_post(JUPITER_SWAP, swap_payload)
        if "error" in swap_res or not swap_res.get("swapTransaction"):
            return {"success": False, "error": f"Swap build failed: {swap_res.get('error','')}"}

        tx_bytes  = base64.b64decode(swap_res["swapTransaction"])
        signed_tx = _sign_transaction(tx_bytes, privkey_bytes)
        tx_b64    = base64.b64encode(signed_tx).decode()

        send_res = _rpc("sendTransaction", [
            tx_b64,
            {"encoding": "base64", "skipPreflight": False,
             "preflightCommitment": "confirmed", "maxRetries": 3}
        ])
        if "error" in send_res or not send_res.get("result"):
            return {"success": False, "error": f"Send failed: {send_res.get('error','')}"}

        tx_hash   = send_res["result"]
        sol_out   = int(quote.get("outAmount", 0)) / LAMPORTS

        for _ in range(15):
            import time; time.sleep(2)
            conf = _rpc("getSignatureStatuses", [[tx_hash], {"searchTransactionHistory": True}])
            try:
                status = conf["result"]["value"][0]
                if status and status.get("confirmationStatus") in ("confirmed","finalized"):
                    if status.get("err"):
                        return {"success": False, "error": f"TX failed: {status['err']}",
                                "tx_hash": tx_hash}
                    return {"success": True, "tx_hash": tx_hash,
                            "sol_received": sol_out, "sold_pct": sell_pct * 100}
            except:
                pass
        return {"success": False, "error": "Confirmation timeout", "tx_hash": tx_hash}

    except Exception as e:
        return {"success": False, "error": str(e)}