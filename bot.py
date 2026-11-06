#!/usr/bin/env python3
"""Ragnar Solutions Bot — Full Telegram Bot"""
import os, sys, json, time, logging, threading
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

# ── DB_PATH: single source of truth, resolved once, before anything else ──
# This MUST be an absolute path matching your Railway volume's mount point
# exactly, or your database will silently live on the container's ephemeral
# disk and be wiped on every redeploy even with a volume attached.
# Set DB_PATH in Railway Variables to match your volume mount (e.g. /data
# mounted -> DB_PATH=/data/ragnar_solutions_bot.db).
os.environ.setdefault("DB_PATH", "/data/ragnar_solutions_bot.db")
DB_PATH = os.environ["DB_PATH"]
print(f"[STARTUP] Resolved DB_PATH = {DB_PATH}  "
      f"(must match your Railway volume mount path exactly, or data will not persist across deploys)")

# Per-user locks to prevent duplicate wallet generation from racing/duplicate updates
_gen_wallet_locks = {}

# ── Optional DB reset via env var ───────────────────────────────────────────
# Set RESET_DB=true in Railway Variables to wipe the database on next boot.
# IMPORTANT: remove or set back to "false" after the reset deploy completes.
if os.getenv("RESET_DB", "").lower() in ("1", "true", "yes"):
    _log_path = os.path.join(os.path.dirname(__file__), "ragnar_solutions_bot.log")
    for _p in (DB_PATH, _log_path):
        try:
            os.remove(_p)
            print(f"[RESET_DB] Removed {_p}")
        except FileNotFoundError:
            print(f"[RESET_DB] {_p} did not exist, nothing to remove")
        except Exception as _e:
            print(f"[RESET_DB] Failed to remove {_p}: {_e}")

from crypto_utils import generate_mnemonic, derive_solana_keypair
from database import (
    init_db, upsert_user, get_user, set_user_language, get_user_settings, save_user_settings,
    save_wallet, get_wallets, count_wallets, delete_wallet,
    get_active_wallet, set_active_wallet,
    set_custom_balance, get_custom_balance,
    save_trade, get_trades,
    save_order, get_orders, cancel_order,
    save_monitor, get_monitors, get_all_active_monitors, deactivate_monitor,
    save_referral, count_referrals,
    get_spl_tokens,
    init_admin_table, add_admin, remove_admin, get_admins, is_admin,
    get_all_users, get_bot_stats, save_broadcast
)
from solana_api import (
    get_sol_balance, get_sol_price_usd, get_token_info,
    is_solana_address, format_number, rug_pull_check
)
from jupiter_swap import buy_token, sell_token, get_token_balance, send_sol, swap_tokens, SOL_MINT

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
OWNER_ID         = int(os.getenv("OWNER_ID", "0"))  # still used for admin-panel access
MAX_FREE_WALLETS = 999999  # Unlimited wallets
MIN_FUND_SOL     = 0.001

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(
            stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
        ),
        logging.FileHandler("ragnar_solutions_bot.log", encoding='utf-8')
    ]
)
log = logging.getLogger("RagnarSolutionsBot")

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

# ── Telegram API ──────────────────────────────────────────────────────────────
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg(method: str, **kwargs) -> dict:
    url  = f"{BASE_URL}/{method}"
    data = json.dumps(kwargs).encode()
    req  = urllib.request.Request(url, data=data,
           headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error(f"TG API error {method}: {e}")
        return {}

def send(chat_id, text, reply_markup=None, parse_mode="HTML", **kw):
    p = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup: p["reply_markup"] = json.dumps(reply_markup)
    p.update(kw)
    return tg("sendMessage", **p)

def edit(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    p = {"chat_id": chat_id, "message_id": message_id,
         "text": text, "parse_mode": parse_mode}
    if reply_markup: p["reply_markup"] = json.dumps(reply_markup)
    result = tg("editMessageText", **p)
    if not result.get("ok"):
        # Fall back to sending a new message if edit fails
        send(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    return result

def answer_cb(callback_query_id, text="", show_alert=False):
    tg("answerCallbackQuery", callback_query_id=callback_query_id,
       text=text, show_alert=show_alert)

def kb(*rows) -> dict:
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row]
        for row in rows
    ]}

def delete_msg(chat_id, message_id):
    try: tg("deleteMessage", chat_id=chat_id, message_id=message_id)
    except: pass

# ── State machine ─────────────────────────────────────────────────────────────
_states: dict = {}
_state_lock = threading.Lock()

def get_state(uid):
    with _state_lock: return _states.get(uid, {}).copy()

def set_state(uid, state, **data):
    with _state_lock: _states[uid] = {"state": state, "data": data}

def clear_state(uid):
    with _state_lock: _states.pop(uid, None)

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu_kb():
    return kb(
        [("💰 Wallet Management", "wallet_menu")],
        [("🛒 Buy", "buy_menu"),  ("🤑 Sell", "sell_menu")],
        [("💵 Withdraw SOL", "withdraw"), ("💸 Swap Token", "swap")],
        [("📊 My Portfolio", "portfolio"), ("📈 Generate PnL", "pnl")],
        [("🔔 Token Monitor", "monitor")],
        [("⚙️ Settings", "settings")],
        [("🤝 Referral", "referral")],
    )

def wallet_menu_kb():
    return kb(
        [("⚡ Generate Wallet", "gen_wallet"), ("📥 Import Wallet", "import_wallet")],
        [("🔀 Select Active Wallet", "select_wallet")],
        [("🗑 Delete Wallet", "delete_wallet_menu")],
        [("ℹ️ Help", "wallet_help"), ("↩️ Return", "main_menu")],
    )

def settings_kb():
    return kb(
        [("↩️ Return", "main_menu")],
        [("🌐 Language", "language_menu"), ("🔔 Alerts", "alerts_settings")],
        [("⛽ Gas Settings", "gas_settings"), ("📊 Slippage", "slippage_settings")],
        [("🔐 Export Keys", "export_keys"), ("🗑 Reset Data", "reset_confirm")],
    )

# ── Wallet limit helpers ───────────────────────────────────────────────────────
def user_has_funded_wallet(uid) -> bool:
    for w in get_wallets(uid, "SOL"):
        try:
            if get_sol_balance(w["address"]) >= MIN_FUND_SOL:
                return True
        except: pass
    return False

def check_wallet_limit(uid) -> tuple:
    """Always allow wallet generation — unlimited wallets."""
    count = count_wallets(uid, "SOL")
    return True, count

def send_wallet_limit_msg(cid, uid, msg_id, current_count):
    wallets = get_wallets(uid, "SOL")
    addr_lines = "".join(
        f"\n  Wallet {i+1}: <code>{w['address']}</code>"
        for i, w in enumerate(wallets)
    )
    text = (
        f"⚠️ <b>Wallet Generation Limit Reached</b>\n\n"
        f"Ragnar Solutions Bot detected that you have generated wallets without funding. "
        f"Can't generate more wallets.\n\n"
        f"To generate more wallets, please fund at least one of your existing wallets with SOL.\n\n"
        f"<b>Current wallets generated: {current_count}/{MAX_FREE_WALLETS} (free limit)</b>\n\n"
        f"💡 <b>How to fund a wallet:</b>\n"
        f"1. Send SOL to one of your existing wallet addresses{addr_lines}\n"
        f"2. Minimum: {MIN_FUND_SOL} SOL\n"
        f"3. Once funded, you can generate unlimited wallets\n\n"
        f"Click 'Check Funding Status' after sending SOL."
    )
    edit(cid, msg_id, text, reply_markup=kb(
        [("💰 View My Wallets",      "view_my_wallets")],
        [("🔄 Check Funding Status", "check_funding")],
        [("↩️ Back",                 "wallet_menu")],
    ))

# ── Welcome ───────────────────────────────────────────────────────────────────
def send_welcome(chat_id, user_id, first_name):
    """Skip welcome screen — go straight to main menu."""
    send_main_menu(chat_id, user_id)

def send_main_menu(chat_id, user_id, edit_msg_id=None):
    user  = get_user(user_id)
    fname = (user or {}).get("first_name", "Trader")
    wallets  = get_wallets(user_id, "SOL")
    active_w = get_active_wallet(user_id, "SOL")
    sol_bal  = 0.0
    sol_addr = "No wallet — tap Wallet Management"
    if active_w:
        sol_addr = active_w["address"]
        try: sol_bal = get_sol_balance(sol_addr)
        except: pass
    custom_bal  = get_custom_balance(user_id)
    display_bal = custom_bal if custom_bal is not None else sol_bal
    sol_price   = 0.0
    try: sol_price = get_sol_price_usd()
    except: pass
    balance_tag = "🎁 " if custom_bal is not None else ""
    text = (
        f"👋 <b>Welcome {fname} to Ragnar Solutions Bot!</b>\n\n"
        f"💰 <b>Total Balance:</b> "
        f"{'No wallet connected' if not wallets else f'{balance_tag}{display_bal:.4f} SOL'}\n"
        f"👛 <b>Wallet:</b> <i>"
        f"{'No wallet — tap Wallet Management' if not wallets else sol_addr[:20]+'...'}</i>\n\n"
        f"📝 <i>Paste any Solana token address for quick actions!</i>"
    )
    if edit_msg_id:
        edit(chat_id, edit_msg_id, text, reply_markup=main_menu_kb())
    else:
        send(chat_id, text, reply_markup=main_menu_kb())

# ── Admin helpers ─────────────────────────────────────────────────────────────
def admin_menu_kb():
    return kb(
        [("📢 Broadcast",              "admin_broadcast")],
        [("✉️ Message User",           "admin_msg_user")],
        [("📋 List Admins",            "admin_list")],
        [("➕ Add Admin",              "admin_add")],
        [("➖ Remove Admin",           "admin_remove")],
        [("📊 Stats",                  "admin_stats")],
        [("💰 Set Balance",            "admin_set_balance")],
        [("🏦 Wallet Stats",           "admin_at_wallets")],
        [("↩️ Back to Menu",           "main_menu")],
    )

def handle_admin_menu(cid, uid, msg_id=None):
    text = "🔧 <b>Admin Menu</b>\n\nSelect an option:"
    if msg_id:
        edit(cid, msg_id, text, reply_markup=admin_menu_kb())
    else:
        send(cid, text, reply_markup=admin_menu_kb())

def handle_admin_stats(cid, uid, msg_id):
    s = get_bot_stats()
    admins = get_admins()
    text = (
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Users: <b>{s['total_users']}</b>\n"
        f"💳 Total Wallets: <b>{s['total_wallets']}</b>\n"
        f"📈 Total Trades: <b>{s['total_trades']}</b>\n"
        f"🟢 Total Buys: <b>{s['total_buy']}</b>\n"
        f"🔴 Total Sells: <b>{s['total_sell']}</b>\n"
        f"💰 Total SOL Traded: <b>{s['total_sol']:.4f} SOL</b>\n"
        f"🤖 Active Snipes: <b>{s['pending_snipes']}</b>\n"
        f"🛡️ Admins: <b>{len(admins) + 1}</b> (including owner)"
    )
    edit(cid, msg_id, text, reply_markup=kb([("🔄 Refresh","admin_stats"),("↩️ Back","admin_menu")]))

def handle_admin_list(cid, uid, msg_id):
    admins = get_admins()
    lines  = ["📋 <b>Admin List</b>\n"]
    lines.append(f"👑 Owner: <code>{OWNER_ID}</code>")
    if admins:
        for a in admins:
            lines.append(f"🛡️ @{a.get('username','?')} — <code>{a['user_id']}</code>")
    else:
        lines.append("No additional admins added yet.")
    edit(cid, msg_id, "\n".join(lines),
         reply_markup=kb([("➕ Add Admin","admin_add"),("↩️ Back","admin_menu")]))

def do_broadcast(admin_id, message_text):
    """Send message to all bot users."""
    users     = get_all_users()
    sent      = 0
    failed    = 0
    for u in users:
        try:
            result = send(u["user_id"],
                          f"📢 <b>Message from Ragnar Solutions Bot</b>\n\n{message_text}")
            if result.get("ok"):
                sent += 1
            else:
                failed += 1
        except:
            failed += 1
        time.sleep(0.05)  # Rate limit
    save_broadcast(admin_id, message_text, sent)
    return sent, failed

# ── Handlers ──────────────────────────────────────────────────────────────────
def handle_start(msg):
    uid   = msg["from"]["id"]
    cid   = msg["chat"]["id"]
    name  = msg["from"].get("first_name", "User")
    uname = msg["from"].get("username", "")
    upsert_user(uid, uname, name)
    # Admin gets admin menu, users get main menu directly
    if is_admin(uid, OWNER_ID):
        handle_admin_menu(cid, uid)
    else:
        send_main_menu(cid, uid)

def handle_wallet_menu(cid, uid, msg_id=None):
    wallets = get_wallets(uid)
    note = "ℹ️ Wallet not found. Please import or generate." if not wallets else \
           f"✅ You have {len(wallets)} wallet(s) set up."
    text = f"{note}\n\nManage your wallets below:"
    if msg_id:
        edit(cid, msg_id, text, reply_markup=wallet_menu_kb())
    else:
        send(cid, text, reply_markup=wallet_menu_kb())

def handle_gen_wallet(cid, uid, msg_id):
    lock = _gen_wallet_locks.setdefault(uid, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        can_gen, current_count = check_wallet_limit(uid)
        if not can_gen:
            send_wallet_limit_msg(cid, uid, msg_id, current_count)
            return

        # Each user always gets a fresh unique mnemonic — never share master seed
        mnemonic  = generate_mnemonic()
        is_master = False
        index     = current_count  # 0 for first wallet, 1 for second, etc.

        address, privkey_b58, privkey_hex = derive_solana_keypair(mnemonic, index)
        save_wallet(uid, "SOL", address, privkey_b58, privkey_hex, mnemonic, index, f"Wallet {index+1}")
        new_count  = current_count + 1
        remaining  = max(0, MAX_FREE_WALLETS - new_count)
        limit_note = (
            f"\n\n⚠️ <b>Free limit:</b> {new_count}/{MAX_FREE_WALLETS} wallets. "
            f"{'Fund a wallet to generate more.' if remaining==0 else f'{remaining} free slot(s) left.'}"
        )
        edit(cid, msg_id,
             f"✅ <b>SOL Wallet #{index+1} Generated!</b>\n\n"
             f"📍 <b>Address:</b>\n<code>{address}</code>\n\n"
             f"🔑 <b>Private Key (Base58):</b>\n<code>{privkey_b58}</code>\n\n"
             f"{'🌱 <b>Seed Phrase:</b>' + chr(10) + '<code>' + mnemonic + '</code>' + chr(10) if not is_master else ''}"
             f"⚠️ <b>Save your keys securely. Never share them.</b>"
             f"{limit_note}",
             reply_markup=kb(
                 [("↩️ Back to Wallet Menu", "wallet_menu")],
                 [("🏠 Main Menu", "main_menu")],
             ))
    finally:
        lock.release()

def handle_import_wallet(cid, uid, msg_id):
    set_state(uid, "awaiting_import_key")
    edit(cid, msg_id,
         "📥 <b>Import Wallet</b>\n\n"
         "Send your <b>12 or 24-word seed phrase</b>.\n\n"
         "⚠️ Only use this in a private chat with the bot.",
         reply_markup=kb([("❌ Cancel", "wallet_menu")]))

def handle_delete_wallet_menu(cid, uid, msg_id=None):
    wallets = get_wallets(uid)
    if not wallets:
        send(cid, "No wallets to delete.",
             reply_markup=kb([("↩️ Back", "wallet_menu")])); return
    final_kb = {
        "inline_keyboard":
            [[{"text": f"🗑 {w.get('label','Wallet')} — {w.get('address','')[:10]}...",
               "callback_data": f"confirm_del_{w['id']}"}]
             for w in wallets]
            + [[{"text": "↩️ Back", "callback_data": "wallet_menu"}]]
    }
    send(cid, "🗑 <b>Delete Wallet</b>\n\nSelect the wallet to delete:\n⚠️ This cannot be undone!",
         reply_markup=final_kb)

def handle_buy_menu(cid, uid, msg_id):
    wallets = get_wallets(uid, "SOL")
    if not wallets:
        edit(cid, msg_id,
             "⚠️ No SOL wallet found. Please generate or import a wallet first.",
             reply_markup=kb([("💰 Wallet Management","wallet_menu"),("↩️ Back","main_menu")])); return
    set_state(uid, "awaiting_buy_ca")
    edit(cid, msg_id,
         "🛒 <b>Buy Token</b>\n\nPaste the token <b>Contract Address (CA)</b>:",
         reply_markup=kb([("❌ Cancel","main_menu")]))

def handle_sell_menu(cid, uid, msg_id):
    trades = get_trades(uid, 20)
    if not [t for t in trades if t["action"]=="buy"]:
        edit(cid, msg_id, "📭 No open positions found. Buy a token first.",
             reply_markup=kb([("🛒 Buy","buy_menu"),("↩️ Back","main_menu")])); return
    set_state(uid, "awaiting_sell_ca")
    edit(cid, msg_id, "🤑 <b>Sell Token</b>\n\nPaste the token CA to sell:",
         reply_markup=kb([("❌ Cancel","main_menu")]))

def handle_token_scan(cid, uid, ca: str):
    loading = send(cid, "🔍 Scanning token, please wait...")
    info    = get_token_info(ca)
    delete_msg(cid, loading["result"]["message_id"])
    if not info:
        send(cid,
             f"❌ Token not found on DexScreener.\n\n<code>{ca}</code>\n\n"
             "Make sure it's a valid Solana token CA.",
             reply_markup=kb([("🔄 Try Again","buy_menu"),("↩️ Menu","main_menu")])); return
    rug        = rug_pull_check(info)
    risk_emoji = {"LOW":"🟢","MEDIUM":"🟡","HIGH":"🔴"}.get(rug["risk"],"⚪")
    c5  = f"{'📈' if info['price_change_5m']  >=0 else '📉'} {info['price_change_5m']:+.1f}%"
    c1h = f"{'📈' if info['price_change_1h']  >=0 else '📉'} {info['price_change_1h']:+.1f}%"
    c24 = f"{'📈' if info['price_change_24h'] >=0 else '📉'} {info['price_change_24h']:+.1f}%"
    text = (
        f"⚡ <b>{info['name']} (${info['symbol']})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💲 <b>Price:</b> ${info['price_usd']:.8f}\n"
        f"💧 <b>Liquidity:</b> ${format_number(info['liquidity_usd'])}\n"
        f"📊 <b>Market Cap:</b> ${format_number(info['market_cap'])}\n"
        f"📈 <b>Volume 24h:</b> ${format_number(info['volume_24h'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"5m: {c5}  |  1h: {c1h}  |  24h: {c24}\n"
        f"🔄 Buys/Sells 24h: {info['buys_24h']}/{info['sells_24h']}\n"
        f"🏦 DEX: {info['dex'].upper()}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{risk_emoji} <b>Rug Risk:</b> {rug['risk']}\n"
        + "\n".join(rug["flags"]) + "\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 CA: <code>{ca}</code>"
    )
    send(cid, text, reply_markup=kb(
        [("🟢 BUY 0.1 SOL",f"quickbuy_0.1_{ca}"),("🟢 BUY 0.5 SOL",f"quickbuy_0.5_{ca}")],
        [("🟢 BUY 1 SOL",  f"quickbuy_1_{ca}"),  ("💬 Custom Buy", f"custombuy_{ca}")],
        [("🔴 SELL 25%",   f"quicksell_25_{ca}"), ("🔴 SELL 50%",  f"quicksell_50_{ca}")],
        [("🔴 SELL 100%",  f"quicksell_100_{ca}"),("🔔 Set Alert", f"alert_{ca}")],
        [("🔄 Refresh",    f"scan_{ca}")],
        [("↩️ Back to Menu","main_menu")],
    ))

def execute_buy(cid, uid, ca: str, amount_sol: float, token_name: str):
    w = get_active_wallet(uid, "SOL")
    if not w:
        send(cid, "❌ No SOL wallet. Generate or import one first.",
             reply_markup=kb([("💰 Wallets","wallet_menu")],[("🏠 Main Menu","main_menu")])); return
    settings = get_user_settings(uid)
    slippage = int(settings.get("slippage",15) * 100)
    bal = get_sol_balance(w["address"])
    if bal < amount_sol + 0.005:
        send(cid,
             f"❌ <b>Insufficient balance</b>\n\n"
             f"Need {amount_sol+0.005:.4f} SOL but have {bal:.4f} SOL.",
             reply_markup=kb([("↩️ Menu","main_menu")])); return
    prog = send(cid,
                f"⏳ <b>Executing buy...</b>\n"
                f"🪙 Token: <b>{token_name}</b>\n"
                f"💰 Amount: <b>{amount_sol} SOL</b>\n"
                f"⚡ Routing via Jupiter DEX...")
    result = buy_token(w["mnemonic"], ca, amount_sol, slippage_bps=slippage, wallet_index=w["deriv_index"])
    delete_msg(cid, prog["result"]["message_id"])
    if result["success"]:
        tx  = result["tx_hash"]
        out = result.get("out_amount","?")
        save_trade(uid, "SOL", ca, token_name, "buy", amount_sol, 0, tx)
        send(cid,
             f"✅ <b>Buy Successful!</b>\n\n"
             f"🪙 Token: <b>{token_name}</b>\n"
             f"💰 Spent: <b>{amount_sol} SOL</b>\n"
             f"📦 Received: <b>{out}</b> tokens\n"
             f"🔗 TX: <a href='https://solscan.io/tx/{tx}'>{tx[:20]}...</a>",
             reply_markup=kb(
                 [("📊 Portfolio","portfolio"),("🔴 Sell Now",f"quicksell_100_{ca}")],
                 [("↩️ Menu","main_menu")]
             ))
    else:
        err = result.get("error","Unknown")
        tx  = result.get("tx_hash","")
        send(cid,
             f"❌ <b>Buy Failed</b>\n\nError: {err}\n"
             f"{'🔗 TX: <code>'+tx+'</code>' if tx else ''}\n\nTry adjusting slippage in ⚙️ Settings.",
             reply_markup=kb(
                 [("⚙️ Settings","settings"),("🔄 Retry",f"quickbuy_{amount_sol}_{ca}")],
                 [("↩️ Menu","main_menu")]
             ))

def execute_sell(cid, uid, ca: str, sell_pct: float, token_name: str):
    w = get_active_wallet(uid, "SOL")
    if not w:
        send(cid, "❌ No SOL wallet. Generate or import one first.",
             reply_markup=kb([("💰 Wallets","wallet_menu")],[("🏠 Main Menu","main_menu")])); return
    settings = get_user_settings(uid)
    slippage = int(settings.get("slippage",15) * 100)
    prog = send(cid,
                f"⏳ <b>Executing sell...</b>\n"
                f"🪙 Token: <b>{token_name}</b>\n"
                f"📤 Selling: <b>{int(sell_pct*100)}%</b>\n"
                f"⚡ Routing via Jupiter DEX...")
    result = sell_token(w["mnemonic"], ca, sell_pct, slippage_bps=slippage, wallet_index=w["deriv_index"])
    delete_msg(cid, prog["result"]["message_id"])
    if result["success"]:
        tx  = result["tx_hash"]
        sol = result.get("sol_received",0)
        save_trade(uid, "SOL", ca, token_name, "sell", sol, 0, tx)
        send(cid,
             f"✅ <b>Sell Successful!</b>\n\n"
             f"🪙 Token: <b>{token_name}</b>\n"
             f"📤 Sold: <b>{int(sell_pct*100)}%</b>\n"
             f"💰 Received: <b>{sol:.4f} SOL</b>\n"
             f"🔗 TX: <a href='https://solscan.io/tx/{tx}'>{tx[:20]}...</a>",
             reply_markup=kb([("📈 PnL","pnl"),("↩️ Menu","main_menu")]))
    else:
        err = result.get("error","Unknown")
        send(cid,
             f"❌ <b>Sell Failed</b>\n\nError: {err}\n\nTry adjusting slippage in ⚙️ Settings.",
             reply_markup=kb([("⚙️ Settings","settings"),("↩️ Menu","main_menu")]))

def execute_swap(cid, uid, from_mint: str, to_mint: str, amount: float):
    w = get_active_wallet(uid, "SOL")
    if not w:
        send(cid, "❌ No SOL wallet. Generate or import one first.",
             reply_markup=kb([("💰 Wallets","wallet_menu")],[("🏠 Main Menu","main_menu")])); return
    settings = get_user_settings(uid)
    slippage = int(settings.get("slippage",15) * 100)
    prog = send(cid, "⏳ <b>Executing swap...</b>\n⚡ Routing via Jupiter DEX...")
    result = swap_tokens(w["mnemonic"], w["deriv_index"], from_mint, to_mint, amount, slippage_bps=slippage)
    delete_msg(cid, prog["result"]["message_id"])
    if result["success"]:
        tx = result["tx_hash"]
        save_trade(uid, "SOL", to_mint if from_mint == SOL_MINT else from_mint,
                    "SOL" if to_mint == SOL_MINT else to_mint, "swap", amount, 0, tx)
        send(cid,
             f"✅ <b>Swap Successful!</b>\n\n"
             f"📤 Sent: <b>{result['in_amount']}</b>\n"
             f"📦 Received: <b>{result['out_amount']}</b>\n"
             f"🔗 TX: <a href='https://solscan.io/tx/{tx}'>{tx[:20]}...</a>",
             reply_markup=kb([("📊 Portfolio","portfolio"),("↩️ Menu","main_menu")]))
    else:
        err = result.get("error","Unknown")
        send(cid,
             f"❌ <b>Swap Failed</b>\n\nError: {err}\n\nTry adjusting slippage in ⚙️ Settings.",
             reply_markup=kb([("⚙️ Settings","settings"),("↩️ Menu","main_menu")]))

def execute_withdrawal(cid, uid, msg_id, to_address: str, amount: float, wallet_label: str):
    w = get_active_wallet(uid, "SOL")
    if not w:
        edit(cid, msg_id, "❌ No SOL wallet found.",
             reply_markup=kb([("💰 Wallets","wallet_menu")],[("🏠 Main Menu","main_menu")])); return
    edit(cid, msg_id, f"⏳ <b>Sending {amount:.4f} SOL...</b>\n\nBroadcasting to the network...")
    result = send_sol(w["mnemonic"], w["deriv_index"], to_address, amount)
    if result["success"]:
        tx = result["tx_hash"]
        save_trade(uid, "SOL", to_address, "SOL", "withdraw", amount, 0, tx)
        send(cid,
             f"✅ <b>Withdrawal Successful!</b>\n\n"
             f"📍 <b>From:</b> {wallet_label}\n"
             f"📬 <b>To:</b> <code>{to_address}</code>\n"
             f"💰 <b>Amount:</b> {amount:.4f} SOL\n"
             f"🔗 TX: <a href='https://solscan.io/tx/{tx}'>{tx[:20]}...</a>",
             reply_markup=kb([("📊 Portfolio","portfolio"),("↩️ Menu","main_menu")]))
    else:
        err = result.get("error","Unknown")
        send(cid,
             f"❌ <b>Withdrawal Failed</b>\n\nError: {err}",
             reply_markup=kb([("🔄 Try Again","withdraw"),("↩️ Menu","main_menu")]))

# ── Portfolio ─────────────────────────────────────────────────────────────────
def handle_portfolio(cid, uid, msg_id):
    """Show wallet selector — user picks which wallet to inspect."""
    wallets = get_wallets(uid)
    if not wallets:
        edit(cid, msg_id,
             "📊 <b>My Portfolio</b>\n\nNo wallets found. Generate a wallet to get started!",
             reply_markup=kb([("💰 Wallet Management","wallet_menu"),("↩️ Back","main_menu")])); return

    sol_price = 0.0
    try: sol_price = get_sol_price_usd()
    except: pass

    total_usd = 0.0
    lines = ["📊 <b>My Portfolio</b>\n━━━━━━━━━━━━━━━━━━━━\n"]
    for w in wallets:
        if w["chain"] == "SOL":
            try: bal = get_sol_balance(w["address"])
            except: bal = 0.0
            usd = bal * sol_price
            total_usd += usd
            lines.append(
                f"🔗 <b>SOL</b> | {w['label']}\n"
                f"📍 {w['address'][:22]}...\n"
                f"💰 {bal:.4f} SOL ≈ ${usd:.2f}\n"
            )
    trades    = get_trades(uid)
    total_pnl = sum(t.get("pnl_usd",0) for t in trades)
    lines.append(
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>Total Value:</b> ${total_usd:.2f}\n"
        f"📈 <b>Total PnL:</b> {'🟢' if total_pnl>=0 else '🔴'} ${total_pnl:.2f}"
    )

    # Wallet picker buttons for assets view
    markup_rows = []
    for w in wallets:
        markup_rows.append([(
            f"Wallet {w['deriv_index']+1 if w.get('label','').startswith('Wallet') else w['label']} ({w['address'][:8]}...)",
            f"portfolio_assets_{w['id']}"
        )])
    markup_rows.append([("🔄 Refresh","portfolio"),("↩️ Back","main_menu")])
    edit(cid, msg_id, "\n".join(lines), reply_markup={"inline_keyboard": markup_rows})

def handle_portfolio_assets(cid, uid, msg_id, wallet_id: int):
    """Scan and display all assets for a specific wallet."""
    wallets = get_wallets(uid)
    wallet  = next((w for w in wallets if w["id"] == wallet_id), None)
    if not wallet:
        answer_cb("","Wallet not found",True); return

    address = wallet["address"]
    label   = wallet["label"]

    # Send scanning message
    edit(cid, msg_id,
         f"🔄 Scanning assets for {label}...\n"
         f"💰 Fetching SOL balance...\n"
         f"✅ Checking bot-tracked tokens\n"
         f"⏳ Fetching wallet tokens...")

    # SOL balance
    try: sol_bal = get_sol_balance(address)
    except: sol_bal = 0.0

    sol_price = 0.0
    try: sol_price = get_sol_price_usd()
    except: pass

    # Bot-tracked trades for this user
    trades     = get_trades(uid)
    bought_cas = list({t["token_ca"] for t in trades if t["action"]=="buy"})

    # SPL tokens from chain
    spl_tokens = get_spl_tokens(address)

    total_usd = sol_bal * sol_price

    if not spl_tokens:
        text = (
            f"💼 <b>Wallet Assets: {label}</b>\n\n"
            f"📍 <b>Address:</b> {address}\n"
            f"💰 <b>SOL Balance:</b> {sol_bal:.4f} SOL\n"
            f"📊 <b>Total Tokens Found:</b> 0\n\n"
            f"🗂 No SPL tokens found in this wallet."
        )
    else:
        lines = [
            f"💼 <b>Wallet Assets: {label}</b>\n\n"
            f"📍 <b>Address:</b> {address}\n"
            f"💰 <b>SOL Balance:</b> {sol_bal:.4f} SOL\n"
            f"📊 <b>Total Tokens Found:</b> {len(spl_tokens)}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        ]
        for tok in spl_tokens[:10]:
            mint = tok["mint"]
            bal  = tok["balance"]
            # Try to get price from DexScreener
            try:
                info  = get_token_info(mint)
                name  = info.get("symbol","?") if info else "Unknown"
                price = info.get("price_usd",0) if info else 0
                usd   = bal * price
                total_usd += usd
                lines.append(f"🪙 <b>{name}</b>: {bal:.4f} ≈ ${usd:.2f}")
            except:
                lines.append(f"🪙 <code>{mint[:16]}...</code>: {bal:.4f}")
        lines.append(f"\n━━━━━━━━━━━━━━━━━━━━\n💼 <b>Wallet Value:</b> ≈ ${total_usd:.2f}")
        text = "\n".join(lines)

    edit(cid, msg_id, text, reply_markup=kb(
        [("🔄 Refresh", f"portfolio_assets_{wallet_id}"),
         ("↩️ Back",    "portfolio")],
        [("🏠 Main Menu", "main_menu")],
    ))

def handle_pnl(cid, uid, msg_id):
    trades = get_trades(uid, 30)
    if not trades:
        edit(cid, msg_id,
             "📈 <b>PnL Report</b>\n\nNo trades yet. Start trading to track your PnL!",
             reply_markup=kb([("↩️ Back","main_menu")])); return
    total_bought = sum(t["amount_sol"] for t in trades if t["action"]=="buy")
    total_sold   = sum(t["amount_sol"] for t in trades if t["action"]=="sell")
    pnl = total_sold - total_bought
    lines = ["📈 <b>PnL Report</b>\n━━━━━━━━━━━━━━━━━━━━\n"]
    for t in trades[:10]:
        emoji = "🟢" if t["action"]=="buy" else "🔴"
        lines.append(f"{emoji} {t['action'].upper()} {t.get('token_name','?')} | {t['amount_sol']:.3f} SOL")
    lines.append(
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Trades: {len(trades)}\n"
        f"📥 Bought: {total_bought:.3f} SOL\n"
        f"📤 Sold: {total_sold:.3f} SOL\n"
        f"{'🟢' if pnl>=0 else '🔴'} Net PnL: {pnl:+.4f} SOL"
    )
    edit(cid, msg_id, "\n".join(lines),
         reply_markup=kb([("🔄 Refresh","pnl"),("↩️ Back","main_menu")]))

def handle_active_orders(cid, uid, msg_id):
    orders = get_orders(uid)
    if not orders:
        edit(cid, msg_id, "🕐 <b>Active Orders</b>\n\nNo active limit orders.",
             reply_markup=kb([("↩️ Back","main_menu")])); return
    lines = ["🕐 <b>Active Orders</b>\n━━━━━━━━━━━━━━━━━━━━\n"]
    markup_rows = []
    for o in orders[:8]:
        emoji = "🟢" if o["order_type"]=="buy" else "🔴"
        lines.append(f"{emoji} {o['order_type'].upper()} {o.get('token_name','?')}\n"
                     f"   Trigger: ${o['trigger_price']:.6f} | {o['amount_sol']} SOL")
        markup_rows.append([(f"❌ Cancel #{o['id']}",f"cancel_order_{o['id']}")])
    markup_rows.append([("↩️ Back","main_menu")])
    edit(cid, msg_id, "\n".join(lines), reply_markup={"inline_keyboard": markup_rows})

def handle_monitor(cid, uid, msg_id):
    monitors = get_monitors(uid)
    if not monitors:
        set_state(uid, "awaiting_monitor_ca")
        edit(cid, msg_id,
             "🔔 <b>Token Monitor</b>\n\nPaste a token CA to set up a price alert:",
             reply_markup=kb([("❌ Cancel","main_menu")])); return
    lines = ["🔔 <b>Active Monitors</b>\n━━━━━━━━━━━━━━━━━━━━\n"]
    for m in monitors:
        lines.append(f"• {m.get('token_name','?')} — {m['alert_type']} @ ${m['threshold']:.6f}")
    edit(cid, msg_id, "\n".join(lines) + "\n\nAdd another?",
         reply_markup=kb([("➕ Add Monitor","add_monitor"),("↩️ Back","main_menu")]))

def handle_withdraw(cid, uid, msg_id):
    w = get_active_wallet(uid, "SOL")
    if not w:
        edit(cid, msg_id, "💵 No SOL wallet found.",
             reply_markup=kb([("💰 Wallets","wallet_menu"),("↩️ Back","main_menu")])); return
    try: real_bal = get_sol_balance(w["address"])
    except: real_bal = 0.0
    custom_bal  = get_custom_balance(uid)
    display_bal = custom_bal if custom_bal is not None else real_bal
    set_state(uid, "awaiting_withdraw_address",
              balance=display_bal, real_balance=real_bal,
              from_address=w["address"], wallet_label=w.get("label","Wallet"))
    edit(cid, msg_id,
         f"💵 <b>Withdraw SOL</b>\n\n"
         f"📍 <b>From:</b> {w.get('label','Wallet')}\n"
         f"💰 <b>Available:</b> {display_bal:.4f} SOL\n\n"
         f"Enter the <b>destination address</b>:",
         reply_markup=kb([("❌ Cancel","main_menu")]))

def handle_swap(cid, uid, msg_id):
    edit(cid, msg_id,
         "💸 <b>Swap Token</b>\n\n"
         "Format: <code>FROM_CA TO_CA AMOUNT</code>\n\n"
         "Use <code>SOL</code> for either side to mean native SOL, e.g.\n"
         "<code>SOL 7xKX...pump 0.5</code>",
         reply_markup=kb([("❌ Cancel","main_menu")]))
    set_state(uid, "awaiting_swap")

def handle_referral(cid, uid, msg_id):
    count    = count_referrals(uid)
    ref_link = f"https://t.me/RagnarSolutionsBot?start=ref_{uid}"
    edit(cid, msg_id,
         f"🤝 <b>Referral Program</b>\n\n"
         f"Earn 0.5% of every trade your referrals make!\n\n"
         f"🔗 Your Link:\n<code>{ref_link}</code>\n\n"
         f"👥 Total Referrals: <b>{count}</b>",
         reply_markup=kb([("↩️ Back","main_menu")]))


def handle_settings(cid, uid, msg_id):
    s = get_user_settings(uid)
    edit(cid, msg_id,
         f"⚙️ <b>Settings</b>\n\n"
         f"📊 Slippage: <b>{s.get('slippage',15)}%</b>\n"
         f"⛽ Gas: <b>{s.get('gas','normal').title()}</b>\n"
         f"🔔 Alerts: <b>{'On' if s.get('alerts',True) else 'Off'}</b>",
         reply_markup=settings_kb())

def handle_export_keys(cid, uid, msg_id):
    wallets = get_wallets(uid)
    if not wallets:
        edit(cid, msg_id, "No wallets to export.", reply_markup=kb([("↩️ Back","settings")])); return
    lines = ["🔐 <b>Your Wallet Keys</b>\n⚠️ Keep PRIVATE!\n"]
    for w in wallets:
        lines.append(f"\n⛓️ <b>{w['chain']}</b> — {w['label']}\n"
                     f"📍 <code>{w['address']}</code>\n"
                     f"🔑 <code>{w['privkey_b58']}</code>")
    edit(cid, msg_id, "\n".join(lines), reply_markup=kb([("↩️ Back","settings")]))

# ── Text message handler ──────────────────────────────────────────────────────
def handle_text(msg):
    uid   = msg["from"]["id"]
    cid   = msg["chat"]["id"]
    text  = msg.get("text","").strip()
    st    = get_state(uid)
    state = st.get("state","")
    data  = st.get("data",{})

    # /cancel command
    if text in ("/cancel","/start") and state:
        clear_state(uid)
        if text == "/cancel":
            send(cid, "❌ Cancelled.", reply_markup=kb([("🏠 Main Menu","show_main_menu")]))
            return

    # Paste CA anywhere to scan
    if is_solana_address(text) and state not in ("awaiting_import_key", "awaiting_withdraw_address"):
        clear_state(uid)
        handle_token_scan(cid, uid, text); return

    if state == "awaiting_buy_ca":
        clear_state(uid)
        if is_solana_address(text): handle_token_scan(cid, uid, text)
        else: send(cid,"❌ Invalid CA.",reply_markup=kb([("↩️ Menu","main_menu")]))

    elif state == "awaiting_sell_ca":
        clear_state(uid)
        if is_solana_address(text): handle_token_scan(cid, uid, text)
        else: send(cid,"❌ Invalid CA.",reply_markup=kb([("↩️ Menu","main_menu")]))

    elif state == "awaiting_buy_amount":
        try:
            amount = float(text)
            if amount <= 0 or amount > 100:
                send(cid, "❌ Amount must be between 0.001 and 100 SOL."); return
            ca, token = data.get("ca",""), data.get("token","Token")
            clear_state(uid)
            threading.Thread(target=execute_buy, args=(cid,uid,ca,amount,token), daemon=True).start()
        except: send(cid,"❌ Invalid amount. Enter a number like 0.5")

    elif state == "awaiting_withdraw_address":
        from solana_api import is_solana_address as _isa
        if not _isa(text.strip()):
            send(cid, "❌ Invalid Solana address. Please enter a valid address:",
                 reply_markup=kb([("❌ Cancel", "wallet_menu")])); return
        bal   = data.get("balance", 0)
        label = data.get("wallet_label", "Wallet")
        to    = text.strip()
        set_state(uid, "awaiting_withdraw_amount",
                  to_address=to, balance=bal,
                  real_balance=data.get("real_balance", bal),
                  from_address=data.get("from_address",""),
                  wallet_label=label)
        send(cid,
             f"💵 <b>Enter Amount to Withdraw</b>\n\n"
             f"📍 <b>From:</b> {label}\n"
             f"📬 <b>To:</b> <code>{to[:20]}...</code>\n"
             f"💰 <b>Available:</b> {bal:.4f} SOL\n\n"
             f"Enter amount of SOL to withdraw:",
             reply_markup=kb([("❌ Cancel", "main_menu")]))

    elif state == "awaiting_withdraw_amount":
        try:
            amount = float(text)
            bal    = data.get("balance", 0)
            to     = data.get("to_address", "")
            label  = data.get("wallet_label", "Wallet")
            if amount <= 0:
                send(cid, "❌ Amount must be greater than 0."); return
            if amount > bal:
                send(cid,
                     f"❌ Insufficient balance.\n\n"
                     f"Available: <b>{bal:.4f} SOL</b>\n"
                     f"Requested: <b>{amount:.4f} SOL</b>",
                     reply_markup=kb([("❌ Cancel", "main_menu")])); return
            total_deduct = amount
            remaining    = bal - total_deduct
            set_state(uid, "awaiting_withdraw_confirm",
                      to_address=to, amount=amount, balance=bal,
                      wallet_label=label)
            send(cid,
                 f"🗂 <b>Withdrawal Confirmation</b>\n\n"
                 f"📍 <b>From Wallet:</b> {label}\n"
                 f"📬 <b>To Address:</b> <code>{to[:20]}...</code>\n"
                 f"💰 <b>Amount:</b> {amount:.4f} SOL\n"
                 f"📊 <b>Total Deduction:</b> {total_deduct:.4f} SOL\n"
                 f"💵 <b>Remaining Balance:</b> {max(0, remaining):.4f} SOL\n\n"
                 f"Proceed with withdrawal?",
                 reply_markup=kb(
                     [("✅ Confirm Withdrawal", "confirm_withdrawal"),
                      ("❌ Cancel", "cancel_withdrawal")]
                 ))
        except: send(cid, "❌ Invalid amount. Enter a number like 0.5")

    elif state == "awaiting_monitor_ca":
        if is_solana_address(text):
            info  = get_token_info(text)
            tname = info.get("name","Unknown") if info else "Unknown"
            set_state(uid,"awaiting_monitor_threshold",ca=text,token_name=tname)
            send(cid,f"Token: <b>{tname}</b>\n\nEnter target price in USD (e.g. 0.001):")
        else: send(cid,"❌ Invalid CA.")

    elif state == "awaiting_monitor_threshold":
        try:
            threshold = float(text)
            ca    = data.get("ca","")
            tname = data.get("token_name","Unknown")
            save_monitor(uid,ca,tname,"price_above",threshold)
            clear_state(uid)
            send(cid,
                 f"🔔 Alert set for <b>{tname}</b> at ${threshold:.6f}",
                 reply_markup=kb([("↩️ Menu","main_menu")]))
        except: send(cid,"❌ Invalid threshold.")

    elif state == "awaiting_import_key":
        words = text.strip().split()
        clear_state(uid)
        if len(words) in (12,24):
            try:
                addr, priv, phex = derive_solana_keypair(text.strip(), 0)
                # Use imported label — count existing to name correctly
                idx   = count_wallets(uid,"SOL")
                label = f"Imported Wallet {idx+1}" if idx > 0 else "Imported Wallet"
                save_wallet(uid,"SOL",addr,priv,phex,text.strip(),idx,label)
                send(cid,
                     f"✅ <b>Wallet Imported!</b>\n\n"
                     f"📍 <code>{addr}</code>",
                     reply_markup=kb(
                         [("💰 Wallet Menu","wallet_menu")],
                         [("🏠 Main Menu","main_menu")],
                     ))
            except Exception as e:
                send(cid,f"❌ Import failed: {e}",reply_markup=kb([("↩️ Menu","main_menu")]))
        else:
            send(cid,"❌ Send a valid 12 or 24-word seed phrase.",
                 reply_markup=kb([("↩️ Menu","main_menu")]))

    elif state == "awaiting_swap":
        parts = text.split()
        clear_state(uid)
        if len(parts) == 3:
            from_raw, to_raw, amt_raw = parts
            from_mint = SOL_MINT if from_raw.upper() == "SOL" else from_raw
            to_mint   = SOL_MINT if to_raw.upper() == "SOL" else to_raw
            if not (from_mint == SOL_MINT or is_solana_address(from_mint)) or \
               not (to_mint == SOL_MINT or is_solana_address(to_mint)):
                send(cid, "❌ Invalid CA. Use a valid Solana token address or SOL.",
                     reply_markup=kb([("↩️ Menu", "main_menu")])); return
            try:
                amount = float(amt_raw)
                if amount <= 0:
                    raise ValueError()
            except ValueError:
                send(cid, "❌ Invalid amount.", reply_markup=kb([("↩️ Menu", "main_menu")])); return
            threading.Thread(target=execute_swap, args=(cid, uid, from_mint, to_mint, amount), daemon=True).start()
        else:
            send(cid,"❌ Format: FROM_CA TO_CA AMOUNT",reply_markup=kb([("↩️ Menu","main_menu")]))

    # ── Admin text states ──────────────────────────────────────
    elif state == "admin_awaiting_add_id":
        try:
            target_uid = int(text.strip())
            target_user = get_user(target_uid)
            if not target_user:
                send(cid, "❌ User not found. They must have used the bot first.",
                     reply_markup=kb([("↩️ Back","admin_menu")])); return
            if target_uid == OWNER_ID:
                send(cid, "ℹ️ That's the owner — already has full access.",
                     reply_markup=kb([("↩️ Back","admin_menu")])); return
            add_admin(target_uid, target_user.get("username",""), uid)
            clear_state(uid)
            send(cid,
                 f"✅ <b>Admin Added!</b>\n\n"
                 f"👤 {target_user.get('first_name','User')} (<code>{target_uid}</code>) is now an admin.",
                 reply_markup=kb([("📋 List Admins","admin_list"),("↩️ Admin Menu","admin_menu")]))
        except ValueError:
            send(cid, "❌ Invalid ID. Enter a numeric Telegram User ID:",
                 reply_markup=kb([("❌ Cancel","admin_menu")]))

    elif state == "admin_awaiting_broadcast":
        msg_text = text.strip()
        clear_state(uid)
        # Ask for confirmation
        set_state(uid, "admin_broadcast_confirm", msg_text=msg_text)
        users = get_all_users()
        send(cid,
             f"📢 <b>Confirm Broadcast</b>\n\n"
             f"Recipients: <b>{len(users)}</b> users\n\n"
             f"Message preview:\n<i>{msg_text[:200]}</i>\n\n"
             f"Send this to everyone?",
             reply_markup=kb(
                 [("✅ Yes, Send Now","admin_confirm_broadcast")],
                 [("❌ Cancel","admin_menu")]
             ))

    elif state == "admin_broadcast_confirm":
        # User typed something instead of clicking button — re-ask
        send(cid, "Please click ✅ Yes or ❌ Cancel.",
             reply_markup=kb([("✅ Send","admin_confirm_broadcast"),("❌ Cancel","admin_menu")]))

    elif state == "admin_awaiting_msg_uid":
        try:
            target_uid = int(text.strip())
            target_user = get_user(target_uid)
            if not target_user:
                send(cid, "❌ User not found.",
                     reply_markup=kb([("↩️ Back","admin_menu")])); return
            set_state(uid, "admin_awaiting_msg_content", target_uid=target_uid,
                      target_name=target_user.get("first_name","User"))
            send(cid,
                 f"✉️ Messaging: <b>{target_user.get('first_name','User')}</b>\n\n"
                 f"Type the message to send:")
        except ValueError:
            send(cid, "❌ Invalid ID.",reply_markup=kb([("↩️ Back","admin_menu")]))

    elif state == "admin_awaiting_msg_content":
        target_uid  = data.get("target_uid")
        target_name = data.get("target_name","User")
        msg_text    = text.strip()
        clear_state(uid)
        result = send(target_uid,
                      f"✉️ <b>Message from Ragnar Solutions Bot Admin</b>\n\n{msg_text}")
        if result.get("ok"):
            send(cid, f"✅ Message sent to <b>{target_name}</b>!",
                 reply_markup=kb([("↩️ Admin Menu","admin_menu")]))
        else:
            send(cid, f"❌ Failed to send message.",
                 reply_markup=kb([("↩️ Admin Menu","admin_menu")]))

    elif state == "admin_awaiting_balance_uid":
        try:
            target_uid  = int(text.strip())
            target_user = get_user(target_uid)
            if not target_user:
                send(cid,"❌ User not found.",reply_markup=kb([("↩️ Back","admin_menu")])); return
            wallets = get_wallets(target_uid, "SOL")
            lines   = [f"💰 <b>Set Custom Balance</b>\n",
                       f"👤 {target_user.get('first_name','?')} (<code>{target_uid}</code>)\n"]
            total = 0.0
            for w in wallets:
                try: bal = get_sol_balance(w["address"])
                except: bal = 0.0
                total += bal
                lines.append(f"• {w['label']}: {bal:.4f} SOL")
            custom = get_custom_balance(target_uid)
            lines.append(f"\n📡 On-chain Total: <b>{total:.4f} SOL</b>")
            if custom is not None:
                lines.append(f"🎁 Custom Balance: <b>{custom:.4f} SOL</b>")
            lines.append(f"\nEnter a custom SOL balance to display, or <b>0</b> to clear.")
            set_state(uid, "admin_awaiting_balance_amount",
                      target_uid=target_uid,
                      target_name=target_user.get("first_name","User"))
            send(cid, "\n".join(lines),
                 reply_markup=kb([("🗑 Clear Custom Balance", f"admin_clear_balance_{target_uid}"),
                                  ("❌ Cancel", "admin_menu")]))
        except ValueError:
            send(cid,"❌ Invalid ID.",reply_markup=kb([("↩️ Back","admin_menu")]))

    elif state == "admin_awaiting_balance_amount":
        try:
            amount     = float(text.strip())
            target_uid = get_state(uid).get("data",{}).get("target_uid")
            tname      = get_state(uid).get("data",{}).get("target_name","User")
            clear_state(uid)
            if amount <= 0:
                set_custom_balance(target_uid, None)
                send(cid,
                     f"✅ Custom balance cleared for <b>{tname}</b>\n"
                     f"They will now see their real on-chain balance.",
                     reply_markup=kb([("↩️ Admin Menu","admin_menu")]))
            else:
                set_custom_balance(target_uid, amount)
                send(cid,
                     f"✅ Custom balance set!\n\n"
                     f"👤 <b>{tname}</b> (<code>{target_uid}</code>)\n"
                     f"🎁 Display Balance: <b>{amount:.4f} SOL</b>",
                     reply_markup=kb([("↩️ Admin Menu","admin_menu")]))
        except ValueError:
            send(cid,"❌ Invalid amount.",reply_markup=kb([("❌ Cancel","admin_menu")]))

    else:
        send(cid,
             "⚡ Send /start to open the bot or paste a Solana token CA to scan!",
             reply_markup=kb([("🏠 Main Menu","show_main_menu")]))

# ── Callback handler ──────────────────────────────────────────────────────────
def handle_callback(cb):
    uid    = cb["from"]["id"]
    cid    = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    data   = cb["data"]
    answer_cb(cb["id"])

    if data == "show_main_menu":
        send_main_menu(cid, uid, edit_msg_id=msg_id)
    elif data == "main_menu":
        send_main_menu(cid, uid, edit_msg_id=msg_id)

    # ── Wallet ─────────────────────────────────────────────────
    elif data == "wallet_menu":
        handle_wallet_menu(cid, uid, msg_id)
    elif data == "gen_wallet":
        handle_gen_wallet(cid, uid, msg_id)
    elif data == "import_wallet":
        handle_import_wallet(cid, uid, msg_id)
    elif data == "delete_wallet_menu":
        handle_delete_wallet_menu(cid, uid, msg_id)

    elif data.startswith("confirm_del_"):
        wid     = int(data.replace("confirm_del_",""))
        wallets = get_wallets(uid)
        wallet  = next((w for w in wallets if w["id"]==wid), None)
        if wallet:
            send(cid,
                 f"⚠️ <b>Confirm Delete</b>\n\n"
                 f"Wallet: <b>{wallet['label']}</b>\n"
                 f"Address: <code>{wallet['address']}</code>\n\n"
                 f"⚠️ This cannot be undone!",
                 reply_markup=kb(
                     [("✅ Yes, Delete", f"do_del_{wid}")],
                     [("❌ Cancel",      "wallet_menu")]
                 ))
        else:
            send(cid, "❌ Wallet not found.",
                 reply_markup=kb([("↩️ Back", "wallet_menu")]))
            
    elif data.startswith("do_del_"):
        wid = int(data.replace("do_del_",""))
        delete_wallet(uid, wid)
        remaining = get_wallets(uid)
        send(cid,
             f"✅ <b>Wallet deleted successfully.</b>\n\n"
             f"Remaining wallets: <b>{len(remaining)}</b>",
             reply_markup=kb([("💰 Wallet Menu", "wallet_menu")],[("🏠 Main Menu","main_menu")]))
        delete_wallet(uid, wid)
        handle_wallet_menu(cid, uid, msg_id)

    elif data == "view_my_wallets":
        wallets = get_wallets(uid, "SOL")
        if not wallets:
            answer_cb(cb["id"],"No wallets yet!",True); return
        lines = ["💳 <b>Your SOL Wallets</b>\n"]
        for i,w in enumerate(wallets,1):
            try: bal = get_sol_balance(w["address"])
            except: bal = 0.0
            lines.append(f"\n<b>Wallet {i}</b>\n📍 <code>{w['address']}</code>\n💰 {bal:.4f} SOL")
        edit(cid, msg_id, "\n".join(lines), reply_markup=kb(
            [("↩️ Back","wallet_menu")],[("🏠 Main Menu","main_menu")]
        ))

    elif data == "check_funding":
        wallets = get_wallets(uid, "SOL")
        funded  = False
        lines   = ["🔄 <b>Checking Funding Status...</b>\n"]
        for w in wallets:
            try: bal = get_sol_balance(w["address"])
            except: bal = 0.0
            status = "✅ Funded!" if bal >= MIN_FUND_SOL else "❌ Not funded"
            if bal >= MIN_FUND_SOL: funded = True
            lines.append(f"• <code>{w['address'][:22]}...</code>\n  {bal:.4f} SOL — {status}")
        if funded:
            lines.append("\n\n✅ <b>Wallet funded! You can now generate unlimited wallets.</b>")
        else:
            lines.append(f"\n\n⚠️ No wallet funded yet. Minimum: {MIN_FUND_SOL} SOL")
        edit(cid, msg_id, "\n".join(lines), reply_markup=kb(
            [("⚡ Generate Wallet","gen_wallet")] if funded else [("🔄 Check Again","check_funding")],
            [("↩️ Back","wallet_menu")],
            [("🏠 Main Menu","main_menu")],
        ))

    elif data == "rearrange_wallets":
        wallets = get_wallets(uid)
        if not wallets:
            answer_cb(cb["id"],"No wallets yet!",True); return
        lines = ["🗂 <b>Your Wallets</b>\n"]
        for w in wallets:
            lines.append(f"• [{w['chain']}] {w['label']}: <code>{w['address'][:22]}...</code>")
        edit(cid, msg_id, "\n".join(lines), reply_markup=kb(
            [("↩️ Back","wallet_menu")],[("🏠 Main Menu","main_menu")]
        ))

    elif data == "select_wallet":
        wallets = get_wallets(uid, "SOL")
        if not wallets:
            answer_cb(cb["id"], "No wallets yet!", True); return
        active      = get_active_wallet(uid, "SOL")
        active_addr = active["address"] if active else None
        btn_rows    = []
        for i, w in enumerate(wallets):
            is_cur = w["address"] == active_addr
            label  = f"{'✅ ' if is_cur else ''}{w['label']} — {w['address'][:16]}..."
            btn_rows.append((label, f"swallet_{i}"))
        btn_rows.append(("↩️ Back", "wallet_menu"))
        inline_rows = [[{"text": label, "callback_data": cbdata}]
                       for label, cbdata in btn_rows]
        edit(cid, msg_id,
             "🔀 <b>Select Active Wallet</b>\n\n"
             "Choose which wallet to use for trading.\n"
             "✅ = currently active",
             reply_markup={"inline_keyboard": inline_rows})

    elif data.startswith("swallet_"):
        try:
            idx     = int(data.replace("swallet_", ""))
            wallets = get_wallets(uid, "SOL")
            if idx >= len(wallets):
                answer_cb(cb["id"], "Wallet not found!", True); return
            wallet  = wallets[idx]
            address = wallet["address"]
            set_active_wallet(uid, address, "SOL")
            answer_cb(cb["id"], "✅ Active wallet set!", False)
            edit(cid, msg_id,
                 f"✅ <b>Active Wallet Updated!</b>\n\n"
                 f"🏦 <b>{wallet['label']}</b>\n"
                 f"📍 <code>{address}</code>\n\n"
                 f"All trades will now use this wallet.",
                 reply_markup=kb(
                     [("🔀 Switch Again", "select_wallet")],
                     [("↩️ Wallet Menu", "wallet_menu")],
                     [("🏠 Main Menu", "main_menu")],
                 ))
        except Exception:
            answer_cb(cb["id"], "Error selecting wallet.", True)

    elif data == "wallet_help":
        edit(cid, msg_id,
             "ℹ️ <b>Wallet Help</b>\n\n"
             "• <b>Generate Wallet:</b> Creates a new SOL wallet — unlimited\n"
             "• <b>Import Wallet:</b> Add existing wallet via 12/24-word seed phrase\n"
             "• <b>Select Active Wallet:</b> Choose which wallet to use for trading\n"
             "• <b>Delete Wallet:</b> Remove a wallet from the bot (keys still exist on-chain)\n\n"
             "💡 The active wallet is used for all buys, sells, swaps, and withdrawals.",
             reply_markup=kb([("↩️ Back","wallet_menu")],[("🏠 Main Menu","main_menu")]))

    # ── Trading ────────────────────────────────────────────────
    elif data == "buy_menu":
        handle_buy_menu(cid, uid, msg_id)
    elif data == "sell_menu":
        handle_sell_menu(cid, uid, msg_id)

    elif data.startswith("quickbuy_"):
        parts  = data.split("_",2)
        amount = float(parts[1])
        ca     = parts[2]
        info   = get_token_info(ca)
        tname  = info.get("name","Token") if info else "Token"
        threading.Thread(target=execute_buy, args=(cid,uid,ca,amount,tname), daemon=True).start()
        edit(cid, msg_id,
             f"⏳ <b>Processing buy of {amount} SOL...</b>\n\nRouting via Jupiter DEX. Please wait.")

    elif data.startswith("custombuy_"):
        ca    = data.replace("custombuy_","")
        info  = get_token_info(ca)
        tname = info.get("name","Token") if info else "Token"
        set_state(uid,"awaiting_buy_amount",ca=ca,token=tname)
        edit(cid, msg_id,
             f"💬 <b>Custom Buy</b>\n\nToken: <b>{tname}</b>\n\nEnter SOL amount:",
             reply_markup=kb([("❌ Cancel","main_menu")]))

    elif data.startswith("quicksell_"):
        parts    = data.split("_",2)
        pct_str  = parts[1]
        ca       = parts[2]
        sell_pct = int(pct_str)/100
        info     = get_token_info(ca)
        tname    = info.get("name","Token") if info else "Token"
        threading.Thread(target=execute_sell, args=(cid,uid,ca,sell_pct,tname), daemon=True).start()
        edit(cid, msg_id,
             f"⏳ <b>Processing sell of {pct_str}%...</b>\n\nRouting via Jupiter DEX. Please wait.")

    elif data.startswith("scan_"):
        ca = data.replace("scan_","")
        delete_msg(cid, msg_id)
        handle_token_scan(cid, uid, ca)

    elif data.startswith("alert_"):
        ca = data.replace("alert_","")
        set_state(uid,"awaiting_monitor_threshold",ca=ca,token_name="Token")
        edit(cid, msg_id,
             f"🔔 <b>Set Price Alert</b>\n\nCA: <code>{ca}</code>\n\nEnter target price in USD:",
             reply_markup=kb([("❌ Cancel","main_menu")]))

    # ── Portfolio ──────────────────────────────────────────────
    elif data == "portfolio":
        handle_portfolio(cid, uid, msg_id)

    elif data.startswith("portfolio_assets_"):
        wallet_id = int(data.replace("portfolio_assets_",""))
        threading.Thread(
            target=handle_portfolio_assets, args=(cid,uid,msg_id,wallet_id), daemon=True
        ).start()

    elif data in ("pnl","positions"):
        handle_pnl(cid, uid, msg_id)

    elif data == "active_orders":
        handle_active_orders(cid, uid, msg_id)

    elif data.startswith("cancel_order_"):
        oid = int(data.replace("cancel_order_",""))
        cancel_order(uid, oid)
        handle_active_orders(cid, uid, msg_id)

    # ── Features ───────────────────────────────────────────────
    elif data == "monitor":
        handle_monitor(cid, uid, msg_id)

    elif data == "add_monitor":
        set_state(uid,"awaiting_monitor_ca")
        edit(cid, msg_id,"🔔 Paste token CA to monitor:",reply_markup=kb([("❌ Cancel","main_menu")]))

    elif data == "withdraw":
        handle_withdraw(cid, uid, msg_id)

    elif data == "confirm_withdrawal":
        st  = get_state(uid)
        d   = st.get("data", {}) if st else {}
        to      = d.get("to_address", "")
        amount  = d.get("amount", 0)
        label   = d.get("wallet_label", "Wallet")
        clear_state(uid)
        threading.Thread(target=execute_withdrawal, args=(cid, uid, msg_id, to, amount, label), daemon=True).start()

    elif data == "cancel_withdrawal":
        clear_state(uid)
        answer_cb(cb["id"], "Withdrawal cancelled.", False)
        send_main_menu(cid, uid)

    elif data == "swap":
        handle_swap(cid, uid, msg_id)

    elif data == "referral":
        handle_referral(cid, uid, msg_id)


    # ── Settings ───────────────────────────────────────────────
    elif data == "settings":
        handle_settings(cid, uid, msg_id)

    elif data == "language_menu":
        edit(cid, msg_id, "🌐 Please choose your preferred language:",
             reply_markup=kb(
                 [("↩️ Return","settings")],
                 [("🇺🇸 English","set_lang_en"),("🇨🇳 Chinese","set_lang_zh")],
             ))

    elif data == "set_lang_en":
        set_user_language(uid, "en"); handle_settings(cid, uid, msg_id)

    elif data == "set_lang_zh":
        set_user_language(uid, "zh"); handle_settings(cid, uid, msg_id)

    elif data == "export_keys":
        handle_export_keys(cid, uid, msg_id)

    elif data == "slippage_settings":
        s = get_user_settings(uid)
        edit(cid, msg_id,
             f"📊 <b>Slippage</b>\n\nCurrent: {s.get('slippage',15)}%\n\nSelect:",
             reply_markup=kb(
                 [("5%","set_slip_5"),("10%","set_slip_10"),("15%","set_slip_15")],
                 [("20%","set_slip_20"),("25%","set_slip_25"),("50%","set_slip_50")],
                 [("↩️ Back","settings")],
             ))

    elif data.startswith("set_slip_"):
        slip = int(data.replace("set_slip_",""))
        s = get_user_settings(uid); s["slippage"] = slip
        save_user_settings(uid, s); handle_settings(cid, uid, msg_id)

    elif data == "gas_settings":
        edit(cid, msg_id, "⛽ <b>Gas Settings</b>\n\nSelect speed:",
             reply_markup=kb(
                 [("🐢 Slow","set_gas_slow"),("🚶 Normal","set_gas_normal"),("🚀 Fast","set_gas_fast")],
                 [("↩️ Back","settings")],
             ))

    elif data.startswith("set_gas_"):
        gas = data.replace("set_gas_","")
        s = get_user_settings(uid); s["gas"] = gas
        save_user_settings(uid, s); handle_settings(cid, uid, msg_id)

    elif data == "alerts_settings":
        s = get_user_settings(uid); s["alerts"] = not s.get("alerts",True)
        save_user_settings(uid, s); handle_settings(cid, uid, msg_id)

    elif data == "reset_confirm":
        edit(cid, msg_id,
             "⚠️ <b>Reset All Data?</b>\n\nDeletes ALL wallets and trade history.",
             reply_markup=kb([("✅ Yes, Reset","reset_confirmed"),("❌ Cancel","settings")]))

    elif data == "reset_confirmed":
        import sqlite3
        from database import DB_PATH, _lock
        with _lock:
            conn = sqlite3.connect(DB_PATH)
            for tbl in ("wallets","trades","orders","monitors","snipe_configs"):
                conn.execute(f"DELETE FROM {tbl} WHERE user_id=?", (uid,))
            conn.commit(); conn.close()
        send_main_menu(cid, uid, edit_msg_id=msg_id)

    elif data == "signals":
        edit(cid, msg_id, "📡 <b>Signals</b>\n\nLive trading signals coming soon!",
             reply_markup=kb([("↩️ Back","main_menu")]))

    elif data == "bridge":
        edit(cid, msg_id, "🔀 <b>Bridge</b>\n\nCross-chain bridge coming soon.",
             reply_markup=kb([("↩️ Back","main_menu")]))

    elif data == "premium":
        edit(cid, msg_id,
             "⭐ <b>Ragnar Solutions Premium</b>\n\n"
             "• 🚀 Priority transactions\n• 📡 Live signals\n"
             "• 🤖 Advanced copy trade\n• ⛽ Gas subsidies\n\n💎 1 SOL/month",
             reply_markup=kb([("↩️ Back","main_menu")]))

    # ── Admin callbacks ────────────────────────────────────────
    elif data == "admin_menu":
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        handle_admin_menu(cid, uid, msg_id)

    elif data == "admin_stats":
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        handle_admin_stats(cid, uid, msg_id)

    elif data == "admin_list":
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        handle_admin_list(cid, uid, msg_id)

    elif data == "admin_add":
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        set_state(uid, "admin_awaiting_add_id")
        edit(cid, msg_id,
             "➕ <b>Add Admin</b>\n\nEnter the Telegram User ID of the new admin:\n\n"
             "<i>Example: 123456789</i>",
             reply_markup=kb([("❌ Cancel","admin_menu")]))

    elif data == "admin_remove":
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        admins = get_admins()
        if not admins:
            edit(cid, msg_id, "No admins to remove.",
                 reply_markup=kb([("↩️ Back","admin_menu")])); return
        rows = [[{"text": f"Remove @{a.get('username','?')}", "callback_data": f"admin_do_remove_{a['user_id']}"}] for a in admins]
        rows.append([{"text":"↩️ Back","callback_data":"admin_menu"}])
        edit(cid, msg_id, "➖ <b>Remove Admin</b>\n\nSelect admin to remove:",
             reply_markup={"inline_keyboard": rows})

    elif data.startswith("admin_do_remove_"):
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        target = int(data.replace("admin_do_remove_",""))
        if target == OWNER_ID:
            answer_cb(cb["id"],"Cannot remove the owner!",True); return
        remove_admin(target)
        handle_admin_list(cid, uid, msg_id)

    elif data == "admin_broadcast":
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        set_state(uid, "admin_awaiting_broadcast")
        edit(cid, msg_id,
             "📢 <b>Broadcast Message</b>\n\n"
             "Type the message to send to ALL users:\n\n"
             "⚠️ This will be sent to every user who has used the bot.",
             reply_markup=kb([("❌ Cancel","admin_menu")]))

    elif data == "admin_msg_user":
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        set_state(uid, "admin_awaiting_msg_uid")
        edit(cid, msg_id,
             "✉️ <b>Message User</b>\n\nEnter the Telegram User ID to message:",
             reply_markup=kb([("❌ Cancel","admin_menu")]))

    elif data == "admin_set_balance":
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        set_state(uid, "admin_awaiting_balance_uid")
        edit(cid, msg_id,
             "💰 <b>Set Custom Balance</b>\n\n"
             "Enter the Telegram <b>User ID</b> of the user:",
             reply_markup=kb([("❌ Cancel","admin_menu")]))

    elif data.startswith("admin_clear_balance_"):
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        target_uid = int(data.replace("admin_clear_balance_",""))
        set_custom_balance(target_uid, None)
        clear_state(uid)
        answer_cb(cb["id"], "✅ Custom balance cleared!", False)
        edit(cid, msg_id,
             "✅ <b>Custom balance cleared.</b>\n\nUser will now see their real on-chain balance.",
             reply_markup=kb([("↩️ Admin Menu","admin_menu")]))

    elif data == "admin_at_wallets":
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        stats = get_bot_stats()
        edit(cid, msg_id,
             f"🏦 <b>Wallet Stats</b>\n\n"
             f"Total wallets in system: <b>{stats['total_wallets']}</b>\n\n"
             f"Each wallet has its own independently generated BIP39 mnemonic "
             f"(not derived from a shared master seed).",
             reply_markup=kb([("↩️ Back","admin_menu")]))

    elif data == "admin_confirm_broadcast":
        if not is_admin(uid, OWNER_ID):
            answer_cb(cb["id"],"Access denied!",True); return
        st = get_state(uid)
        msg_text = st.get("data",{}).get("msg_text","")
        if not msg_text:
            edit(cid, msg_id, "No message to broadcast.",
                 reply_markup=kb([("↩️ Back","admin_menu")])); return
        clear_state(uid)
        edit(cid, msg_id, "📢 Broadcasting to all users... please wait.")
        def do_bc():
            sent, failed = do_broadcast(uid, msg_text)
            send(cid,
                 f"✅ <b>Broadcast Complete!</b>\n\n"
                 f"✅ Sent: {sent}\n❌ Failed: {failed}",
                 reply_markup=kb([("↩️ Admin Menu","admin_menu")]))
        threading.Thread(target=do_bc, daemon=True).start()

    else:
        answer_cb(cb["id"],"Coming soon!",True)

# ── Polling loop ──────────────────────────────────────────────────────────────
def get_updates(offset=None, timeout=30):
    params = {"timeout":timeout,"allowed_updates":["message","callback_query"]}
    if offset: params["offset"] = offset
    url = f"{BASE_URL}/getUpdates?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"RagnarSolutionsBot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout+5) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"getUpdates error: {e}"); return {}

def dispatch(update):
    try:
        if "message" in update:
            msg  = update["message"]
            txt  = msg.get("text","")
            uid  = msg["from"]["id"]
            upsert_user(uid, msg["from"].get("username",""), msg["from"].get("first_name","User"))
            cid_ = msg["chat"]["id"]
            if   txt.startswith("/start"):    handle_start(msg)
            elif txt.startswith("/admin"):
                if is_admin(uid, OWNER_ID):
                    handle_admin_menu(msg["chat"]["id"], uid)
                else:
                    send(msg["chat"]["id"], "❌ Access denied.")
            elif txt.startswith("/menu"):     send_main_menu(cid_, uid)
            elif txt.startswith("/wallet"):   handle_wallet_menu(cid_, uid)
            elif txt.startswith("/buy"):
                send(cid_, "🛒 Paste a token CA to buy:", reply_markup=kb([("❌ Cancel","main_menu")]))
                set_state(uid, "awaiting_buy_ca")
            elif txt.startswith("/sell"):
                send(cid_, "📤 Paste a token CA to sell:", reply_markup=kb([("❌ Cancel","main_menu")]))
                set_state(uid, "awaiting_sell_ca")
            elif txt.startswith("/pnl"):      handle_pnl(cid_, uid, None)
            elif txt.startswith("/settings"): handle_settings(cid_, uid, None)
            elif txt.startswith("/cancel"):   handle_text(msg)
            elif txt.startswith("/help"):
                send(msg["chat"]["id"],
                     "ℹ️ <b>Ragnar Solutions Bot</b>\n\n"
                     "/start — Welcome\n/menu — Main menu\n"
                     "/wallet — Wallet manager\n/buy — Buy a token\n/sell — Sell a token\n"
                     "/pnl — PnL report\n/settings — Settings\n"
                     "/cancel — Cancel current action\n\n"
                     "Paste any Solana CA to scan & trade!")
            else: handle_text(msg)
        elif "callback_query" in update:
            cb  = update["callback_query"]
            uid = cb["from"]["id"]
            upsert_user(uid, cb["from"].get("username",""), cb["from"].get("first_name","User"))
            handle_callback(cb)
    except Exception as e:
        log.exception(f"Dispatch error: {e}")

def run():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set! Edit your .env file."); sys.exit(1)
    init_db()
    init_admin_table()
    log.info("Database initialized")
    me = tg("getMe")
    if not me.get("ok"):
        log.error(f"Invalid bot token: {me}"); sys.exit(1)
    log.info(f"Bot started: {me['result']['first_name']}")

    # Register bot commands for native ≡ Menu button
    tg("setMyCommands", commands=[
        {"command": "start",    "description": "Open main menu"},
        {"command": "menu",     "description": "Main menu"},
        {"command": "wallet",   "description": "Wallet management"},
        {"command": "buy",      "description": "Buy a token"},
        {"command": "sell",     "description": "Sell a token"},
        {"command": "pnl",      "description": "PnL report"},
        {"command": "settings", "description": "Settings"},
        {"command": "cancel",   "description": "Cancel current action"},
    ])
    tg("setChatMenuButton", menu_button={"type": "commands"})
    log.info("Bot menu button configured.")

    log.info(f"Ragnar Solutions Bot online — @{me['result'].get('username','')} — {utcnow()} UTC")
    offset = None
    threading.Thread(target=check_monitors_loop, daemon=True).start()
    log.info("Token monitor loop started.")
    log.info("Polling started — bot is live!")
    while True:
        try:
            updates = get_updates(offset)
            if updates.get("ok") and updates.get("result"):
                for upd in updates["result"]:
                    offset = upd["update_id"] + 1
                    threading.Thread(target=dispatch, args=(upd,), daemon=True).start()
        except KeyboardInterrupt:
            log.info("Bot stopped."); break
        except Exception as e:
            log.error(f"Polling error: {e}"); time.sleep(3)

def check_monitors_loop(interval_seconds: int = 60):
    """Background loop: polls active price monitors and fires alerts when triggered."""
    while True:
        try:
            for m in get_all_active_monitors():
                try:
                    price = get_token_price(m["token_ca"])
                    if price <= 0:
                        continue
                    threshold = m["threshold"]
                    triggered = (
                        (m["alert_type"] == "price_above" and price >= threshold) or
                        (m["alert_type"] == "price_below" and price <= threshold)
                    )
                    if triggered:
                        deactivate_monitor(m["id"])
                        send(m["user_id"],
                             f"🔔 <b>Price Alert Triggered!</b>\n\n"
                             f"🪙 Token: <b>{m.get('token_name','Unknown')}</b>\n"
                             f"📋 CA: <code>{m['token_ca']}</code>\n"
                             f"🎯 Target: ${threshold:.6f}\n"
                             f"💵 Current: ${price:.6f}",
                             reply_markup=kb(
                                 [("👀 View Token", f"scan_{m['token_ca']}")],
                                 [("↩️ Menu","main_menu")],
                             ))
                except Exception as e:
                    log.error(f"Monitor check failed for id={m.get('id')}: {e}")
        except Exception as e:
            log.error(f"Monitor loop error: {e}")
        time.sleep(interval_seconds)

if __name__ == "__main__":
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=",1)
                    os.environ.setdefault(k.strip(), v.strip())
        BOT_TOKEN   = os.environ.get("BOT_TOKEN","")
        OWNER_ID    = int(os.environ.get("OWNER_ID","0"))
        BASE_URL    = f"https://api.telegram.org/bot{BOT_TOKEN}"
    run()