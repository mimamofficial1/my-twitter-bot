# 🐦➡️📱 Twitter → Telegram Auto-Forward Bot

Automatically forwards tweets from any Twitter/X accounts to your Telegram channel or group.

---

## 🚀 Setup Guide

### Step 1 — Get Twitter API Access (Free)
1. Go to https://developer.twitter.com/en/portal/dashboard
2. Create a new Project + App
3. Under **Keys and Tokens**, copy your **Bearer Token**
4. Paste it in `config.json` → `twitter_bearer_token`

> ⚠️ Free tier allows ~500,000 tweets/month read. Set poll_interval_seconds ≥ 60.

---

### Step 2 — Create a Telegram Bot
1. Open Telegram, search `@BotFather`
2. Send `/newbot` and follow the steps
3. Copy the **HTTP API token**
4. Paste in `config.json` → `telegram_bot_token`

---

### Step 3 — Get Your Chat ID
**For a channel:**
- Add the bot as **Admin** to your channel
- Use `@your_channel_name` as `telegram_chat_id`

**For a group/DM:**
- Add bot to the group, send a message
- Visit: `https://api.telegram.org/bot<TOKEN>/getUpdates`
- Find `"chat":{"id":...}` — use that number as `telegram_chat_id`

---

### Step 4 — Configure `config.json`
```json
{
  "twitter_usernames": ["@elonmusk", "@OpenAI"],
  "poll_interval_seconds": 60,
  "include_retweets": false,
  "include_replies": false,
  "forward_media": true
}
```

---

### Step 5 — Install & Run
```bash
pip install -r requirements.txt
python bot.py
```

---

## 🔄 Run 24/7 (Optional)

**Using screen (Linux/Mac):**
```bash
screen -S twitterbot
python bot.py
# Press Ctrl+A then D to detach
```

**Using PM2:**
```bash
npm install -g pm2
pm2 start bot.py --interpreter python3
pm2 save
```

---

## 📁 Files
| File | Purpose |
|------|---------|
| `bot.py` | Main bot script |
| `config.json` | Your settings & API keys |
| `state.json` | Auto-created, tracks last seen tweets |
| `bot.log` | Auto-created, logs all activity |
