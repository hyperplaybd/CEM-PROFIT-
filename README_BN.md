# CEM PROFIT — Professional Telegram Mini App

## GitHub-এ কোন ফাইল আপলোড করবেন

Repository-তে এই ফাইলগুলো রাখুন:
- `cem_profit_bot.py`
- `index.html`
- `logo.svg`
- `requirements.txt`
- `.env.example`
- `.gitignore`
- `README_BN.md`

**`.env` কখনো GitHub-এ আপলোড করবেন না।** আসল Bot Token, Admin ID ও wallet configuration সার্ভারের Environment Variables-এ অথবা private `.env` ফাইলে থাকবে। `.gitignore`-এ `.env` আগে থেকেই বাদ দেওয়া আছে।

এই অ্যাপটি শুধু GitHub Pages-এ static hosting করলে চলবে না: Mini App-এর `/api/...` endpoint একই Python backend origin থেকে আসে। GitHub repository-কে source হিসেবে রেখে Python hosting (যেমন Render/Railway/VPS)-এ পুরো app চালান; সেই hosting-এর HTTPS URL-টি `BOT_WEBAPP_URL`-এ দিন।

এই প্যাকেজে বট, Mini App UI এবং API একই Python সার্ভারে রাখা হয়েছে। ফলে TxID যাচাইয়ের ফল আর শুধু Telegram chat-এ পাঠানো হয় না; ব্যবহারকারী Mini App-এর ভেতরেই Pending / Success / Failed অবস্থা দেখতে পান।

## আগে নিরাপত্তা

আপলোড করা পুরোনো Python ফাইলে Bot Token এবং BscScan API Key সরাসরি লেখা ছিল। এগুলো প্রকাশিত হয়েছে ধরে নিয়ে:

1. Telegram-এর **@BotFather** খুলে `/revoke` দিয়ে পুরোনো bot token বাতিল করুন এবং নতুন token নিন।
2. পুরোনো BscScan API key-টিও আর ব্যবহার করবেন না। এই সংস্করণে BscScan API key লাগে না; BNB Smart Chain JSON-RPC ব্যবহার করা হয়েছে।
3. নতুন Bot Token শুধু সার্ভারের `.env` ফাইলে রাখবেন। HTML বা GitHub public repository-তে কখনো token রাখবেন না।

## ফাইল

- `cem_profit_bot.py` — Telegram polling, HTTPS ওয়েব অ্যাপের API, Telegram session যাচাই, BSC transaction verifier এবং SQLite database।
- `index.html` — নতুন responsive Mini App; no-emoji interface, subtle animation, deposit/withdraw/history screens।
- `logo.svg` — আলাদা ব্যবহারযোগ্য CEM brand mark এবং browser favicon।
- `.env.example` — প্রয়োজনীয় configuration-এর নমুনা।
- `requirements.txt` — প্রয়োজনীয় Python package।

## সেটআপ

1. সব ফাইল একই project folder-এ রাখুন।
2. `.env.example` কপি করে `.env` নাম দিন।
3. `.env`-এ `BOT_TOKEN`-এ BotFather থেকে নেওয়া **নতুন** token বসান।
4. `DEPOSIT_WALLET`-এ আপনার নিয়ন্ত্রণে থাকা সঠিক BNB Smart Chain wallet address দিন। `ADMIN_CHAT_ID` যাচাই করুন।
5. `BOT_WEBAPP_URL`-এ এই Python সার্ভারের public HTTPS URL দিন, যেমন `https://your-app.example.com/`। URL-টি `/` দিয়ে শেষ হবে।
6. Python 3.10+ ব্যবহার করুন। তারপর চালান:

```bash
pip install -r requirements.txt
python cem_profit_bot.py
```

সার্ভার চালু হলে `/health` এ `{"ok": true}` দেখা যাবে। Telegram Mini App-এর জন্য public HTTPS domain আবশ্যক; শুধু `localhost`, `http://` বা GitHub Pages-এ static HTML রাখলে এই একই-origin API কাজ করবে না। Render, Railway, VPS বা HTTPS reverse proxy-সহ hosting ব্যবহার করুন। Hosting provider-এ `.env`-এর variable-গুলো Environment Variables হিসেবেও বসাতে পারেন।

## Telegram-এ Mini App URL সেট করা

- `BOT_WEBAPP_URL`-এ public HTTPS URL বসান।
- Bot চালু রাখুন এবং Telegram-এ bot-এর `/start` চাপুন।
- `Open CEM PROFIT` button অথবা chat menu button থেকে Mini App খুলুন।

## ডিপোজিট যাচাই কীভাবে কাজ করে

- ব্যবহারকারী TxID জমা দেন; backend USDT token contract, Transfer event, নির্দিষ্ট receiver wallet এবং successful receipt যাচাই করে।
- `MIN_CONFIRMATIONS` পূরণ না হওয়া পর্যন্ত UI-তে pending status ও confirmation count দেখায়। ডিফল্ট ১২; `.env`-এ পরিবর্তন করা যায়। নেটওয়ার্কে সময় ১০ সেকেন্ডের বেশি লাগতে পারে—কোনো নির্দিষ্ট সময়ের নিশ্চয়তা দেওয়া হয় না।
- পরিমাণ browser থেকে বিশ্বাস করা হয় না; blockchain event থেকে হিসাব করা হয়।
- প্রতিটি TxID database-এর unique key; একই hash দ্বিতীয়বার credit হবে না।
- SQLite-তে integer base units দিয়ে ব্যালেন্স রাখা হয়, তাই float rounding সমস্যা এড়ানো হয়।

## উত্তোলন

উত্তোলন এখন **request + manual admin processing**। Admin notification-এ `Mark as paid` এবং `Reject request` বোতাম যায়। Admin বাস্তবে wallet-এ টাকা পাঠানোর পর `Mark as paid` চাপলে ব্যালেন্স থেকে পরিমাণ কাটা হয়। Reject করলে reserved amount আবার ব্যবহারযোগ্য হয়। এই কোড নিজে blockchain payout পাঠায় না এবং private key সংরক্ষণ করে না।

## পুরোনো ডেটাবেস

ডিফল্ট `DATABASE_PATH=trading_bot.db` রাখা হয়েছে। আপনার আগের বট একই `trading_bot.db` ব্যবহার করে থাকলে পুরোনো `balance` এবং `used_txids` রেকর্ড থেকে integer-unit field তৈরি হবে। নতুন বট চালানোর আগে পুরোনো বট বন্ধ করুন এবং database-এর backup রাখুন।

## গুরুত্বপূর্ণ অপারেশনাল নোট

- একই database একাধিক bot process দিয়ে একসঙ্গে চালাবেন না।
- Production-এ HTTPS, secure hosting, database backup এবং RPC provider-এর rate limit পর্যবেক্ষণ করুন।
- Deposit address, token contract এবং chain অবশ্যই নিজেরা যাচাই করুন। ভুল address/contract দিলে payment credit হবে না।
- এই UI-তে কোনো guaranteed profit বা return দাবি করা হয়নি।
