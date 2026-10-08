# Soto Telegram Mini App

Soto is a Python standard-library app with Telegram Mini App authentication, SQLite-backed play-coin accounts, server-settled games, and persistent invite rooms.

## Run locally

Use Python 3.9 or newer. Create a bot with [@BotFather](https://t.me/BotFather). You can set these variables in PowerShell (keep the token private):

```powershell
$env:TELEGRAM_BOT_TOKEN = "your-bot-token"
$env:TELEGRAM_BOT_USERNAME = "your_bot_username"
python server.py
```

If `TELEGRAM_BOT_TOKEN` is not set, the app reads a raw token from the local `token.txt` file. Keep that file private; Git ignores it. If the file is missing or empty, running `python server.py` in an interactive terminal prompts for the token without echoing it. For deployment, set it as a private environment variable instead.

The server listens on port 8000 by default. The app requires Telegram's signed `initData`, so opening it directly in a regular browser does not authenticate a player. The backend checks that signature before every API request and rejects data older than 24 hours. First-time players choose a display name, which is saved to their Telegram account. Once deployed at a public HTTPS address, players using Telegram from different countries can join the same poker room.

Blackjack is a stateful single-player hand with Hit and Stand actions. If the app is closed mid-hand, reopening Blackjack resumes the active hand without charging the wager twice. Poker rooms support Texas Hold'em for 2–8 Telegram users: create a room, share its invite link or 8-character code, and have the host deal once everyone has joined. Each player starts with 500 free table chips; blinds are 5/10. Check, call, fold, and raise actions are validated and settled by the server. Poker table chips do not change the account's play-coin balance. Reopen a room from “Your rooms” to resume play.

## Configure Telegram

Deploy the app at a public HTTPS address, then configure that address as the bot's Mini App URL in BotFather. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME` as private hosting environment variables. Set `PORT` if the host assigns a port. Keep the SQLite database on persistent storage and run one app instance; SQLite is not configured for multi-instance hosting.

### Free Render deployment

The included `render.yaml` prepares a free Render web service. Push this project to a private GitHub repository, then create a Blueprint in the Render dashboard from that repository and enter `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME` when prompted. Never commit `token.txt` or `soto.sqlite3`; they are excluded by `.gitignore`. Render assigns a stable `https://<service-name>.onrender.com` address; set that URL as the Mini App URL in BotFather. The free service can sleep while idle and its local SQLite database is temporary, so it is suitable for trying the app but poker rooms and balances may be lost when the service restarts. For saved data and always-on availability, use persistent storage on a paid plan or a database service.

Room invite links use Telegram's `startapp=room_CODE` parameter. Friends in any country can join by opening the link in Telegram or entering the 8-character code before the host starts the table. Share the deployed app's invite link; a local `localhost` address is only reachable from your own device. The table synchronizes turn state by polling the server. Bot commands and friend lists are not implemented.

All coins are virtual. Do not use this prototype for deposits, cash prizes, or payouts.

## Test

```powershell
python -m unittest -v
```