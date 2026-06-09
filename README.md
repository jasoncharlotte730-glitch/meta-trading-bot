# Meta Trading Bot

This is a Telegram trading bot for Solana tokens.

## Deploy on Render

Render must run this project as a **Background Worker**, not a Web Service.

### Required files
- `bot.py`
- `requirements.txt`
- `render.yaml`

### Render service setup

1. Connect your GitHub repository to Render.
2. Create a new service and choose **Background Worker**.
3. Use branch `main`.
4. Set the build command:
   ```bash
   pip install -r requirements.txt
   ```
5. Set the start command:
   ```bash
   python bot.py
   ```
6. Add environment variables in Render:
   - `BOT_TOKEN` with your Telegram bot token
   - `ADMIN_ID` with your Telegram user ID (optional)

### Important notes

- Do not commit `bot_master.key` or any private keys to GitHub.
- `bot.py` now requires `BOT_TOKEN` from the environment.
- If you see a Telegram `Conflict: terminated by other getUpdates request` error, another bot instance is still using the same token. Stop it or revoke the token in BotFather.
- If Render still says "No open ports detected", the service is configured as a web service instead of a worker.
