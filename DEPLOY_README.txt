CEM PROFIT — Deployment package
================================

Files
-----
1. cem_profit_bot.py  - aiohttp Telegram bot + API backend
2. index.html         - English Telegram Mini App frontend
3. logo.svg           - SVG brand/favicon asset
4. requirements.txt   - Python dependency list

Deployment
----------
1. Upload all four files into the same application directory on your host.
2. Install dependencies with: pip install -r requirements.txt
3. Set the BOT_TOKEN environment variable to a NEW token from BotFather.
4. Confirm the ADMIN_CHAT_ID_RAW, DEPOSIT_WALLET and USDT_CONTRACT values in
   cem_profit_bot.py are correct for your deployment.
5. On Render, the app reads RENDER_EXTERNAL_URL automatically. The Mini App
   must be served over HTTPS from the same origin as the API.
6. Set the start command to: python cem_profit_bot.py
7. Deploy and open the bot's Mini App from Telegram.

Important behavior
------------------
- Deposit credit is checked server-side. Minimum deposit: 0.10 USDT on BNB
  Smart Chain (BEP-20), after the required confirmations.
- Referral attribution uses the Telegram Mini App start_param. A referred user
  becomes successful after their first eligible plan purchase. The referrer
  receives a one-time 0.05 USDT commission.
- Commission balance can be used for plan purchases or withdrawals. Minimum
  withdrawal is 0.30 USDT.
- Plans run for 10 days. Users can claim available verified profit once per
  24-hour interval.
- Profit is NOT generated automatically at a fixed percentage. The admin-only
  settlement panel appears on the Plans tab for the Telegram account matching
  ADMIN_CHAT_ID_RAW. Record only realized profit verified from actual trading.
  The user can then claim it, subject to the 24-hour interval. Use a unique
  period key for each plan, for example 2026-11-17. Duplicate plan/period
  settlements are rejected.

Security
--------
The bot token was previously exposed in chat. Revoke it with BotFather and
set the replacement as the BOT_TOKEN environment variable before deployment.
Do not publish the Python file or commit it to a public repository.
Use persistent storage for the SQLite database where your host supports it.
