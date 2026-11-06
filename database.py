"""SQLite database layer for Ragnar Solutions Bot."""
import sqlite3, json, os, threading
from datetime import datetime

# Must match bot.py's resolution exactly — this MUST be an absolute path
# matching your Railway volume's mount point, or the DB will silently live
# on the container's ephemeral disk and be wiped on every redeploy.
DB_PATH = os.environ.get("DB_PATH", "/data/ragnar_solutions_bot.db")

try:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
except OSError as e:
    # Can't create/access the configured path (e.g. running locally without
    # a mounted volume at /data, or without permission to create it).
    # Fall back to a local folder next to the code rather than crashing —
    # but this fallback is NOT persistent on Railway, so make it loud.
    _fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ragnar_solutions_bot.db")
    print(f"[DB_PATH WARNING] Could not use '{DB_PATH}' ({e}). "
          f"Falling back to '{_fallback}' — this fallback is NOT persistent on Railway. "
          f"Set DB_PATH to match your mounted volume path.")
    DB_PATH = _fallback
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

print(f"[DB_PATH] Database file: {DB_PATH}")
_lock = threading.Lock()

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with _lock:
        conn = get_conn()
        c = conn.cursor()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            language    TEXT DEFAULT 'en',
            started_at  TEXT,
            settings    TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS wallets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            chain       TEXT,
            label       TEXT,
            address     TEXT,
            privkey_b58 TEXT,
            privkey_hex TEXT,
            mnemonic    TEXT,
            deriv_index INTEGER DEFAULT 0,
            created_at  TEXT,
            is_active   INTEGER DEFAULT 0,
            UNIQUE(user_id, chain, address)
        );

        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            chain       TEXT,
            token_ca    TEXT,
            token_name  TEXT,
            action      TEXT,
            amount_sol  REAL,
            price_usd   REAL,
            pnl_usd     REAL DEFAULT 0,
            tx_hash     TEXT,
            created_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            chain       TEXT,
            token_ca    TEXT,
            token_name  TEXT,
            order_type  TEXT,
            trigger_price REAL,
            amount_sol  REAL,
            status      TEXT DEFAULT 'active',
            created_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS monitors (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            token_ca    TEXT,
            token_name  TEXT,
            alert_type  TEXT,
            threshold   REAL,
            active      INTEGER DEFAULT 1,
            created_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS snipe_configs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            token_ca    TEXT,
            amount_sol  REAL,
            slippage    REAL DEFAULT 15,
            active      INTEGER DEFAULT 1,
            created_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS referrals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            created_at  TEXT
        );
        """)
        conn.commit()
        for _sql in [
            "ALTER TABLE users ADD COLUMN custom_balance REAL DEFAULT NULL",
            "ALTER TABLE wallets ADD COLUMN is_active INTEGER DEFAULT 0",
        ]:
            try: conn.execute(_sql); conn.commit()
            except: pass
        conn.close()

# ── User helpers ──────────────────────────────────────────────────────────────
def upsert_user(user_id, username, first_name):
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO users(user_id,username,first_name,started_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name
        """, (user_id, username or '', first_name or 'User', datetime.utcnow().isoformat()))
        conn.commit(); conn.close()

def get_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def set_user_language(user_id, lang):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE users SET language=? WHERE user_id=?", (lang, user_id))
        conn.commit(); conn.close()

def get_user_settings(user_id):
    u = get_user(user_id)
    if not u: return {}
    try: return json.loads(u.get('settings','{}'))
    except: return {}

def save_user_settings(user_id, settings):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE users SET settings=? WHERE user_id=?", (json.dumps(settings), user_id))
        conn.commit(); conn.close()

# ── Wallet helpers ────────────────────────────────────────────────────────────
def save_wallet(user_id, chain, address, privkey_b58, privkey_hex, mnemonic, index, label=None):
    with _lock:
        conn = get_conn()
        label = label or f"Wallet {index+1}"
        conn.execute("""
            INSERT OR IGNORE INTO wallets
            (user_id,chain,label,address,privkey_b58,privkey_hex,mnemonic,deriv_index,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (user_id, chain, label, address, privkey_b58, privkey_hex,
              mnemonic, index, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()

def get_wallets(user_id, chain=None):
    conn = get_conn()
    if chain:
        rows = conn.execute("SELECT * FROM wallets WHERE user_id=? AND chain=? ORDER BY deriv_index",
                            (user_id, chain)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM wallets WHERE user_id=? ORDER BY chain,deriv_index",
                            (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_wallet(user_id, chain="SOL"):
    with _lock:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM wallets WHERE user_id=? AND chain=? AND is_active=1 LIMIT 1",
                (user_id, chain)).fetchone()
            if row: return dict(row)
            row = conn.execute(
                "SELECT * FROM wallets WHERE user_id=? AND chain=? ORDER BY id LIMIT 1",
                (user_id, chain)).fetchone()
            return dict(row) if row else None
        finally: conn.close()

def set_active_wallet(user_id, address, chain="SOL"):
    with _lock:
        conn = get_conn()
        try:
            conn.execute("UPDATE wallets SET is_active=0 WHERE user_id=? AND chain=?", (user_id, chain))
            conn.execute("UPDATE wallets SET is_active=1 WHERE user_id=? AND chain=? AND address=?", (user_id, chain, address))
            conn.commit()
        finally: conn.close()

def get_custom_balance(user_id):
    with _lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT custom_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
            return row["custom_balance"] if row else None
        finally: conn.close()

def set_custom_balance(user_id, amount):
    with _lock:
        conn = get_conn()
        try:
            conn.execute("UPDATE users SET custom_balance=? WHERE user_id=?", (amount, user_id))
            conn.commit()
        finally: conn.close()

def count_wallets(user_id, chain):
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM wallets WHERE user_id=? AND chain=?",
                     (user_id, chain)).fetchone()[0]
    conn.close()
    return n

def delete_wallet(user_id, wallet_id):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM wallets WHERE id=? AND user_id=?", (wallet_id, user_id))
        conn.commit(); conn.close()

# ── Trade helpers ─────────────────────────────────────────────────────────────
def save_trade(user_id, chain, token_ca, token_name, action, amount_sol, price_usd=0, tx_hash=''):
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO trades(user_id,chain,token_ca,token_name,action,amount_sol,price_usd,tx_hash,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (user_id, chain, token_ca, token_name, action, amount_sol, price_usd,
              tx_hash, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()

def get_trades(user_id, limit=50):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM trades WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                        (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Order helpers ─────────────────────────────────────────────────────────────
def save_order(user_id, chain, token_ca, token_name, order_type, trigger_price, amount_sol):
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO orders(user_id,chain,token_ca,token_name,order_type,trigger_price,amount_sol,created_at)
            VALUES(?,?,?,?,?,?,?,?)
        """, (user_id, chain, token_ca, token_name, order_type, trigger_price,
              amount_sol, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()

def get_orders(user_id, status='active'):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM orders WHERE user_id=? AND status=? ORDER BY created_at DESC",
                        (user_id, status)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def cancel_order(user_id, order_id):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE orders SET status='cancelled' WHERE id=? AND user_id=?", (order_id, user_id))
        conn.commit(); conn.close()

# ── Monitor helpers ───────────────────────────────────────────────────────────
def save_monitor(user_id, token_ca, token_name, alert_type, threshold):
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO monitors(user_id,token_ca,token_name,alert_type,threshold,created_at)
            VALUES(?,?,?,?,?,?)
        """, (user_id, token_ca, token_name, alert_type, threshold, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()

def get_monitors(user_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM monitors WHERE user_id=? AND active=1", (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_active_monitors():
    """All active monitors across all users, used by the background price-check loop."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM monitors WHERE active=1").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def deactivate_monitor(monitor_id):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE monitors SET active=0 WHERE id=?", (monitor_id,))
        conn.commit(); conn.close()

# ── Snipe helpers ─────────────────────────────────────────────────────────────
def save_snipe(user_id, token_ca, amount_sol, slippage=15):
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO snipe_configs(user_id,token_ca,amount_sol,slippage,created_at)
            VALUES(?,?,?,?,?)
        """, (user_id, token_ca, amount_sol, slippage, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()

def get_snipes(user_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM snipe_configs WHERE user_id=? AND active=1", (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Referral helpers ──────────────────────────────────────────────────────────
def save_referral(referrer_id, referred_id):
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT OR IGNORE INTO referrals(referrer_id,referred_id,created_at)
            VALUES(?,?,?)
        """, (referrer_id, referred_id, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()

def count_referrals(user_id):
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (user_id,)).fetchone()[0]
    conn.close()
    return n

# ── Copy Trade helpers ────────────────────────────────────────────────────────
def save_copytrade(user_id, target_uid, amount_sol, slippage=15):
    with _lock:
        conn = get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS copytrades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                target_uid  INTEGER,
                amount_sol  REAL,
                slippage    REAL DEFAULT 15,
                active      INTEGER DEFAULT 1,
                created_at  TEXT,
                UNIQUE(user_id, target_uid)
            )
        """)
        conn.execute("""
            INSERT OR REPLACE INTO copytrades(user_id,target_uid,amount_sol,slippage,active,created_at)
            VALUES(?,?,?,?,1,?)
        """, (user_id, target_uid, amount_sol, slippage, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()

def get_copytrades(user_id):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM copytrades WHERE user_id=? AND active=1", (user_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except:
        conn.close()
        return []

def delete_copytrade(user_id, target_uid):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE copytrades SET active=0 WHERE user_id=? AND target_uid=?",
                     (user_id, target_uid))
        conn.commit(); conn.close()

def get_spl_tokens(address: str) -> list:
    """Fetch SPL token accounts for a Solana address via RPC."""
    import urllib.request, json
    SOL_RPC = "https://api.mainnet-beta.solana.com"
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [address, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                   {"encoding": "jsonParsed", "commitment": "confirmed"}]
    }).encode()
    req = urllib.request.Request(SOL_RPC, data=payload,
          headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        tokens = []
        for acct in data.get("result",{}).get("value",[]):
            info = acct["account"]["data"]["parsed"]["info"]
            amt  = info["tokenAmount"]
            ui   = float(amt.get("uiAmount") or 0)
            if ui > 0:
                tokens.append({
                    "mint":    info["mint"],
                    "balance": ui,
                    "decimals": amt["decimals"]
                })
        return tokens
    except:
        return []

# ── Admin helpers ─────────────────────────────────────────────────────────────
def init_admin_table():
    with _lock:
        conn = get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                added_by   INTEGER,
                added_at   TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id   INTEGER,
                message    TEXT,
                sent_count INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        conn.commit(); conn.close()

def add_admin(user_id, username, added_by):
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT OR IGNORE INTO admins(user_id,username,added_by,added_at)
            VALUES(?,?,?,?)
        """, (user_id, username, added_by, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()

def remove_admin(user_id):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        conn.commit(); conn.close()

def get_admins():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM admins").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def is_admin(user_id, owner_id):
    if user_id == owner_id: return True
    conn = get_conn()
    row  = conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None

def get_all_users():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY started_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_bot_stats():
    conn = get_conn()
    total_users  = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    total_wallets= conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    total_buy    = conn.execute("SELECT COUNT(*) FROM trades WHERE action='buy'").fetchone()[0]
    total_sell   = conn.execute("SELECT COUNT(*) FROM trades WHERE action='sell'").fetchone()[0]
    total_sol    = conn.execute("SELECT COALESCE(SUM(amount_sol),0) FROM trades WHERE action='buy'").fetchone()[0]
    pending_snipes = conn.execute("SELECT COUNT(*) FROM snipe_configs WHERE active=1").fetchone()[0]
    conn.close()
    return {
        "total_users":   total_users,
        "total_trades":  total_trades,
        "total_wallets": total_wallets,
        "total_buy":     total_buy,
        "total_sell":    total_sell,
        "total_sol":     total_sol,
        "pending_snipes": pending_snipes,
    }

def save_broadcast(admin_id, message, sent_count):
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO broadcasts(admin_id,message,sent_count,created_at)
            VALUES(?,?,?,?)
        """, (admin_id, message, sent_count, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()