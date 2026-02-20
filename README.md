# 🤖 GitHub Autopilot

> **Ek baar install karo — poora GitHub automatically manage hoga.**
> AI-powered PR reviews, commit fixing, issue triage, bot commands. Free forever.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

---

## ✨ Features

- 🔀 **PR Auto-Polish** — Title fix, professional description, labels, risk score
- 🧠 **AI Code Review** — Every file reviewed with score + fixes
- ⚡ **Commit Linter** — Conventional commits enforced
- 📋 **Issue Triage** — Auto-labeled by type & priority
- 🤖 **Bot Commands** — `/fix` `/explain` `/improve` `/test` `/docs`
- 🏷️ **Auto Labels** — 15+ labels created automatically

---

## 🚀 Setup (15 minutes, free forever)

### Step 1: Server deploy karo (Render.com — Free)

1. Yeh repo fork karo apne GitHub pe
2. [render.com](https://render.com) pe jao → Google se sign up
3. **"New Web Service"** → apna forked repo connect karo
4. Deploy ho jayega — URL milegi jaise: `https://github-autopilot-xxxx.onrender.com`

### Step 2: GitHub App banao

1. [github.com/settings/apps/new](https://github.com/settings/apps/new) pe jao
2. Fill karo:
   - **App name:** `github-autopilot-YOURNAME`
   - **Homepage URL:** apni Render URL
   - **Webhook URL:** `https://YOUR-RENDER-URL.onrender.com/webhook`
   - **Webhook secret:** koi bhi random string (yaad rakhna)
3. **Permissions** set karo:
   - Repository: Contents (Read), Issues (Read & Write), Pull requests (Read & Write), Metadata (Read)
4. **Subscribe to events:** Pull request, Issues, Issue comment, Push
5. **"Create GitHub App"** click karo

### Step 3: Private key generate karo

1. App settings mein scroll karo → **"Generate a private key"**
2. `.pem` file download hogi — yeh teri secret key hai

### Step 4: Render mein env variables add karo

Render dashboard → teri service → **Environment** tab:

| Key | Value |
|-----|-------|
| `GITHUB_APP_ID` | App ID (settings mein milega) |
| `GITHUB_PRIVATE_KEY` | `.pem` file ka poora content |
| `GITHUB_WEBHOOK_SECRET` | Step 2 mein jo secret rakha |
| `GROQ_API_KEY` | Free key from [console.groq.com](https://console.groq.com) |

### Step 5: App install karo

1. GitHub App settings → **"Install App"**
2. Apne repos select karo (ya all repositories)
3. **Done! 🎉**

---

## 🤖 Bot Commands

Kisi bhi PR ya Issue mein comment karo:

| Command | Kya karta hai |
|---------|--------------|
| `/fix` | Exact fix with code |
| `/explain` | Code explain karta hai |
| `/improve` | Improvements with examples |
| `/test` | Test cases generate |
| `/docs` | Docstrings + README sections |

---

## 💰 Cost: $0

- **Render.com** free tier — 750 hours/month (enough for personal use)
- **Groq AI** free tier — 14,400 requests/day
- **GitHub App** — completely free

---

*Built with ❤️ — Star ⭐ the repo if it helps!*
