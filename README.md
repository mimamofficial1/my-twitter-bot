# 🐦 Twitter → Telegram Auto-Forward Bot

Nitter RSS se tweets fetch karke Telegram pe automatically forward karta hai.
Koi Twitter API key nahi chahiye!

---

## 📁 Files

```
bot.py              ← Main bot code
requirements.txt    ← Python dependencies
Procfile            ← Railway worker command
runtime.txt         ← Python version
railway.toml        ← Railway config
```

---

## 🚀 Railway Deploy Steps

### Step 1 — GitHub Repo banao
1. GitHub pe naya repo banao (e.g. `twitter-telegram-bot`)
2. Yeh saari files upload karo

### Step 2 — Railway pe project banao
1. [railway.com](https://railway.com) pe jao → Login karo
2. **New Project** → **Deploy from GitHub repo**
3. Apna repo select karo

### Step 3 — Environment Variables set karo
Railway dashboard mein → **Variables** tab → yeh add karo:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather se mila token |
| `TELEGRAM_CHAT_ID` | Channel/group ID (e.g. `-1001234567890`) |
| `TWITTER_USERNAMES` | `user1,user2,user3` (comma separated) |
| `CHECK_INTERVAL` | `120` (optional, default 2 min) |

### Step 4 — Deploy!
Variables save karo → Railway automatically deploy karega ✅

---

## 🤖 Telegram Bot & Chat ID kaise pata karo?

### Bot Token:
1. Telegram mein [@BotFather](https://t.me/BotFather) pe jao
2. `/newbot` → naam do → token copy karo

### Chat ID (Channel ke liye):
1. Bot ko channel mein admin banao
2. `https://api.telegram.org/bot<TOKEN>/getUpdates` open karo
3. `chat.id` copy karo (negative number hoga e.g. `-1001234567890`)

---

## ✨ Features

- 📸 Image upar + caption neeche format
- 🔄 5 Nitter instances — ek fail ho toh doosra automatically
- ✅ Pehli run pe purane tweets skip (spam nahi aayega)
- ✅ Image nahi hai tweet mein → sirf text bhejega
- 🔗 Links twitter.com pe point karte hain
- ♻️ Railway pe crash ho toh auto-restart

---

## 🛠️ Local Test karna ho toh

```bash
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="-100xxxxxxxxx"
export TWITTER_USERNAMES="elonmusk,OpenAI"

python bot.py
```
