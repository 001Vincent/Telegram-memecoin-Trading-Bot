"""
Solana RPC + token data utilities.
Uses public Solana mainnet RPC and Jupiter/DexScreener APIs.
"""
import json, urllib.request, urllib.error, urllib.parse, time

SOL_RPC = "https://api.mainnet-beta.solana.com"
JUPITER_PRICE_API = "https://price.jup.ag/v6/price"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

def _rpc_call(method: str, params: list, timeout=10):
    payload = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req = urllib.request.Request(SOL_RPC, data=payload,
                                 headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def _http_get(url: str, timeout=8) -> dict:
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"RagnarSolutionsBot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def get_sol_balance(address: str) -> float:
    """Return SOL balance for address."""
    res = _rpc_call("getBalance", [address, {"commitment":"confirmed"}])
    if "result" in res:
        return res["result"]["value"] / 1e9
    return 0.0

def get_sol_price_usd() -> float:
    """Get current SOL/USD price."""
    data = _http_get(f"{JUPITER_PRICE_API}?ids=So11111111111111111111111111111111111111112")
    try:
        return float(data["data"]["So11111111111111111111111111111111111111112"]["price"])
    except:
        # Fallback: DexScreener
        try:
            d = _http_get("https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112")
            return float(d["pairs"][0]["priceUsd"])
        except:
            return 0.0

def get_token_info(ca: str) -> dict:
    """Get token info from DexScreener."""
    data = _http_get(f"{DEXSCREENER_API}/{ca}")
    if "error" in data or not data.get("pairs"):
        return {}
    pairs = sorted(data["pairs"], key=lambda x: float(x.get("liquidity",{}).get("usd",0) or 0), reverse=True)
    p = pairs[0]
    return {
        "name": p.get("baseToken",{}).get("name","Unknown"),
        "symbol": p.get("baseToken",{}).get("symbol","???"),
        "price_usd": float(p.get("priceUsd") or 0),
        "price_native": float(p.get("priceNative") or 0),
        "liquidity_usd": float(p.get("liquidity",{}).get("usd") or 0),
        "volume_24h": float(p.get("volume",{}).get("h24") or 0),
        "market_cap": float(p.get("fdv") or 0),
        "price_change_5m": float(p.get("priceChange",{}).get("m5") or 0),
        "price_change_1h": float(p.get("priceChange",{}).get("h1") or 0),
        "price_change_24h": float(p.get("priceChange",{}).get("h24") or 0),
        "dex": p.get("dexId",""),
        "pair_address": p.get("pairAddress",""),
        "buys_24h": p.get("txns",{}).get("h24",{}).get("buys",0),
        "sells_24h": p.get("txns",{}).get("h24",{}).get("sells",0),
    }

def get_token_price(ca: str) -> float:
    data = get_token_info(ca)
    return data.get("price_usd", 0.0)

def is_solana_address(s: str) -> bool:
    """Basic check if string looks like a Solana address."""
    if not s: return False
    s = s.strip()
    return len(s) in range(32, 45) and all(
        c in '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz' for c in s
    )

def format_number(n: float) -> str:
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{n/1_000:.2f}K"
    return f"{n:.4f}"

def rug_pull_check(token_info: dict) -> dict:
    """Simple rug-pull risk assessment."""
    risk = "LOW"
    flags = []
    liq = token_info.get("liquidity_usd", 0)
    vol = token_info.get("volume_24h", 0)
    mc  = token_info.get("market_cap", 0)
    
    if liq < 5000:
        flags.append("⚠️ Very low liquidity")
        risk = "HIGH"
    elif liq < 20000:
        flags.append("⚠️ Low liquidity")
        risk = "MEDIUM"
    
    if mc > 0 and liq / max(mc, 1) < 0.02:
        flags.append("⚠️ Liquidity < 2% of market cap")
        if risk == "LOW": risk = "MEDIUM"
    
    if vol < 1000 and liq > 0:
        flags.append("⚠️ Very low 24h volume")
    
    sells = token_info.get("sells_24h", 0)
    buys  = token_info.get("buys_24h", 1)
    if sells > buys * 3:
        flags.append("⚠️ High sell pressure")
        risk = "HIGH"
    
    if not flags:
        flags.append("✅ No major red flags detected")
    
    return {"risk": risk, "flags": flags}
