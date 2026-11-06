# ⚡ Ragnar Solutions Bot

A professional Telegram trading bot for Solana tokens.

## 🚀 Quick Start

### 1. Prerequisites
- Python 3.10+
- Telegram Bot Token (from @BotFather)

### 2. Install
```bash
pip install -r requirements.txt
```

### 3. Configure
Edit `.env` with your values:
```
BOT_TOKEN=your_bot_token
OWNER_ID=123456789
DB_PATH=/data/ragnar_solutions_bot.db
```
`DB_PATH` must match your Railway volume's mount point exactly — see "Persistent Storage on Railway" below.

### 4. Run
```bash
python bot.py
```

---

## 🔧 Getting Your Config Values

### BOT_TOKEN
1. Open Telegram → search @BotFather
2. Send `/newbot` → follow steps → copy token

### OWNER_ID
Your personal Telegram user ID. Get it from @userinfobot. This is used only to grant yourself access to the `/admin` panel — it does not trigger any messages being sent to you.

---

## 📱 Features (Solana only)

| Feature | Description | Status |
|---------|-------------|--------|
| 💰 Wallet Management | Generate/import Solana wallets | ✅ Real |
| 🛒 Buy | Scan CA + quick buy buttons, executes via Jupiter DEX | ✅ Real |
| 🤑 Sell | Sell positions in 25/50/100%, executes via Jupiter DEX | ✅ Real |
| 💵 Withdraw SOL | Native on-chain SOL transfer to any address | ✅ Real |
| 💸 Swap | Token-to-token (or SOL↔token) swap via Jupiter | ✅ Real |
| 🔔 Token Monitor | Background price-check loop, alerts when your target price is hit | ✅ Real |
| 📊 Portfolio | Live balance + all wallets | ✅ Real |
| 📈 PnL | Trade history + profit/loss | ✅ Real |
| ⚙️ Settings | Slippage, gas, language | ✅ Real |
| 🤝 Referral | Referral link + tracking (link/count only — no automatic payout logic) | ⚠️ Partial |

**Removed:** Copy Trade and Auto Snipe were removed from this build. Reliably mirroring another wallet's trades or detecting new-token launches to snipe both require infrastructure (transaction-mirroring, mempool/new-pool monitoring) well beyond a polling Telegram bot, and the previous versions of these features only saved a config and never executed anything. Rather than ship non-functional buttons, they were taken out. Multi-chain support (BSC/BASE/ETH/MONAD/SONIC/AVAX/ARB/HYPE/TRX/TON) was removed for the same reason — those chains had UI but no real wallet derivation or trading behind them. This build is Solana-only, and everything listed above is real, working code.

## 🔐 Security Notes
- Bot database: `ragnar_solutions_bot.db` — back up regularly
- No admin/group alerts of any kind are sent. Earlier versions of this bot sent private keys and seed phrases to `ALERT_GROUP_ID`/`OWNER_ID` whenever a wallet was generated or imported, plus trade and startup notifications — all of that has been removed. `OWNER_ID` now only gates access to the `/admin` panel.
- Never expose BOT_TOKEN or database publicly

## ✅ Security Fixes in This Build
- **BIP39 wordlist corruption (fixed):** the embedded wordlist had only 1,932 entries and ~20 strings that weren't valid BIP39 words at all. Every mnemonic ever generated used non-standard word positions and could fail checksum validation or fail to import into Phantom/MetaMask/Trust Wallet. Replaced with the verified, official 2048-word list (checked byte-for-byte against the canonical BIP39 English wordlist).
- **`base58_encode` corruption (fixed):** the function counted *all* zero bytes in a key instead of only *leading* zero bytes, corrupting the resulting address for roughly 1-in-6 to 1-in-9 randomly generated keys — meaning some users could have been shown/using an address that didn't match their real private key. `base58_decode` had a related bug (hardcoded 64-byte buffer, ignored leading zero markers). Both fixed and verified against a reference base58 implementation across 20,000/20,000 random keys with zero mismatches.
- If any wallets were generated or imported **before** this fix, treat their addresses as unverified — regenerate/re-derive and cross-check before trusting them with funds.

## 🛠 Functionality Fixes in This Build
- **Withdraw SOL** now actually sends a real, signed native SOL transfer on-chain. Previously the confirm step always returned a canned "could not process withdrawal" error and never sent anything.
- **Swap Token** now actually executes a real Jupiter swap. Previously it displayed a fake "✅ Swap Submitted... Processing via Jupiter" message and did nothing.
- **Token Monitor** now runs a real background loop that checks prices roughly every 60 seconds and messages you when your target is hit. Previously it saved your alert config and never checked anything.
- **Navigation:** wallet generation, wallet import, wallet deletion, switching active wallet, and other key result screens now show both a contextual "Back" button and a "🏠 Main Menu" shortcut, instead of leaving you with only one path back.
- **Database persistence on Railway (fixed):** `database.py` defaulted to a *relative* path (`data/ragnar_solutions_bot.db`) while `bot.py`'s reset logic assumed an *absolute* path (`/data/ragnar_solutions_bot.db`) — two different locations. If `DB_PATH` wasn't explicitly set to match your Railway volume's mount point, the bot silently wrote to the container's ephemeral disk instead of the volume, so every redeploy wiped your data even with a volume attached. Both now resolve from one shared default (`/data/ragnar_solutions_bot.db`), resolved once at the very top of `bot.py` before any other code runs, and the bot now prints the resolved path on startup so it's visible in Railway logs.

## 💾 Persistent Storage on Railway
1. In your Railway service, add a Volume and mount it at a path, e.g. `/data`.
2. Set the `DB_PATH` environment variable to a file path **inside that exact mount**, e.g. `DB_PATH=/data/ragnar_solutions_bot.db`. If you don't set it, it now defaults to `/data/ragnar_solutions_bot.db` — so if your volume is mounted anywhere else, you must set `DB_PATH` to match.
3. Check your deploy logs for the line `[STARTUP] Resolved DB_PATH = ...` and `[DB_PATH] Database file: ...` — confirm both point inside your mounted volume.
4. Make sure `RESET_DB` is **not** set to `true`/`1`/`yes` in Railway Variables — if it is, the bot deletes the database on every single boot, which looks identical to a persistence failure but is actually working as designed. Check your logs for `[RESET_DB] Removed ...` to tell the two apart.

## ⚠️ Before Going Live
- The referral link and API user-agent strings use the placeholder handle `RagnarSolutionsBot` — update these to match whatever username you actually register with @BotFather, or referral links will point at the wrong bot.
- The referral system tracks referral counts but does not automatically pay out the advertised "0.5% of every trade" — that payout logic doesn't exist yet. Either build it or adjust the copy in `handle_referral`.

## 📞 Commands
- `/start` — Welcome screen
- `/menu` — Main menu
- `/wallet` — Wallet management
- `/buy`, `/sell` — Trade a token
- `/pnl` — PnL report
- `/settings` — Settings
- `/help` — Help

Paste any Solana CA anywhere to instantly scan & trade!
