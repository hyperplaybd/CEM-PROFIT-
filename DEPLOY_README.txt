CEM PROFIT - PATCHED DEPLOYMENT

Files:
- cem_profit_bot_referral_plans_fixed.py: backend
- index_referral_plans_fixed.html: frontend (rename to index.html)
- logo.svg: existing SVG brand mark
- requirements.txt: Python dependencies

Deployment:
1. Put all files in the same service directory. Rename index_referral_plans_fixed.html to index.html.
2. Set Render Environment Variables: BOT_TOKEN (new token from BotFather), RENDER_EXTERNAL_URL (Render supplies this), and optionally BOT_USERNAME (without @).
3. Keep the existing persistent disk or restore the database backup before switching deployments. Do not delete trading_bot.db.
4. Start command: python cem_profit_bot_referral_plans_fixed.py
5. Open /health and then test Telegram /start ref_<your-user-id> with a second test account.

Referral behavior:
- A valid existing user shares https://t.me/<bot_username>?start=ref_<user_id>.
- The bot records attribution when the invited user sends /start with that payload.
- Their first successful plan purchase triggers one 0.05 USDT commission in the referrer's Commission Wallet.
- The referral is attributed only once; users who bought before being referred do not earn retroactive credit.

Plan behavior:
- Each account may hold one active plan of each amount tier at a time. The same amount can be purchased again after expiry. Other plan tiers can be bought concurrently.
- The live timer shows remaining term and a clearly labeled illustrative 50%/24h projection; it is not actual, guaranteed or withdrawable profit.
- Claim is available at any time only after verified trading profit is recorded by the admin. There is no artificial 24-hour wait between claims.

Security:
- BOT_TOKEN is intentionally not embedded in the source. The previously exposed token should be revoked in BotFather and replaced with a new token stored in Render Environment Variables.
- Keep trading_bot.db and backups safe. Test with small amounts before any real use.
