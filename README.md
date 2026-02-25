# 🤖 AI Repo Manager

> **Install once — your entire GitHub gets managed automatically.**
> AI-powered PR reviews, commit fixing, issue triage, and bot commands. Free forever.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)
[![Live Server](https://img.shields.io/badge/Server-Live%20on%20Render-46E3B7?logo=render&logoColor=white)](https://github-autopilot-1.onrender.com)
[![GitHub App](https://img.shields.io/badge/GitHub%20App-ai--repo--manager-181717?logo=github)](https://github.com/apps/ai-repo-manager)
[![Powered by Groq](https://img.shields.io/badge/AI-Groq%20Llama%203.3%2070B-F55036)](https://console.groq.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Made by Shweta](https://img.shields.io/badge/Made%20by-Shweta%20Mishra-ff69b4)](https://github.com/Shweta-Mishra-ai)

---

## 🌐 Live Demo

| Resource | Link |
|----------|------|
| 🚀 **Live Server** | [https://github-autopilot-1.onrender.com](https://github-autopilot-1.onrender.com) |
| 🤖 **GitHub App** | [ai-repo-manager](https://github.com/apps/ai-repo-manager) |
| 📦 **Source Code** | [Shweta-Mishra-ai/github-autopilot](https://github.com/Shweta-Mishra-ai/github-autopilot) |

---

## ✨ What It Does

Once installed on your repository, this GitHub App automatically:

- 🔀 **PR Auto-Polish** — Rewrites PR titles to conventional commit format, generates professional descriptions with summary, changes, testing checklist, and risk assessment
- 🧠 **AI Code Review** — Reviews every changed file with a score out of 10, highlights critical issues, suggests fixes
- ⚡ **Commit Linter** — Enforces conventional commit standards, suggests corrections for non-compliant commits
- 📋 **Issue Triage** — Auto-labels issues by type (bug/feature/question) and priority (high/medium/low), posts a structured acknowledgment
- 🤖 **Bot Commands** — Responds to slash commands in any PR or issue comment
- 🏷️ **Auto Labels** — Creates and applies 15+ professional labels automatically

---

## 🤖 Bot Commands

Type any of these in a PR or issue comment:

| Command | What it does |
|---------|-------------|
| `/fix` | Provides root cause analysis and a complete code fix |
| `/explain` | Explains the code or error in plain English |
| `/improve` | Suggests specific improvements with examples |
| `/test` | Generates test cases (pytest, jest, unittest) |
| `/docs` | Creates docstrings, README sections, usage examples |

---

## 🚀 Setup Guide (15 minutes, free forever)

### Step 1 — Deploy the Server (Render.com — Free)

1. Fork this repository to your GitHub account
2. Go to [render.com](https://render.com) and sign up with Google
3. Click **"New +"** → **"Web Service"**
4. Connect your forked repository
5. Set the following:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
   - **Instance Type:** Free
6. Click **"Create Web Service"** — your server URL will be: `https://YOUR-APP.onrender.com`

> ✅ **Live example:** [https://github-autopilot-1.onrender.com](https://github-autopilot-1.onrender.com)

### Step 2 — Create a GitHub App

1. Go to [github.com/settings/apps/new](https://github.com/settings/apps/new)
2. Fill in the following:
   - **App name:** `ai-repo-manager-YOURNAME` (must be unique)
   - **Homepage URL:** Your Render URL
   - **Webhook URL:** `https://YOUR-RENDER-URL.onrender.com/webhook`
   - **Webhook Secret:** Any random string (save it — you'll need it later)
3. Set **Permissions:**
   - Contents → Read-only
   - Issues → Read and write
   - Pull requests → Read and write
   - Metadata → Read-only (mandatory)
4. **Subscribe to events:** Pull request, Issues, Issue comment, Push
5. Under **"Where can this app be installed?"** → select **"Any account"**
6. Click **"Create GitHub App"**

### Step 3 — Generate a Private Key

1. On your GitHub App settings page, scroll down to **"Private keys"**
2. Click **"Generate a private key"** — a `.pem` file will download
3. Open the `.pem` file in Notepad and copy all the text

### Step 4 — Add Environment Variables to Render

Go to Render dashboard → your service → **Environment** tab and add:

| Key | Value |
|-----|-------|
| `GITHUB_APP_ID` | Your App ID (shown on the GitHub App settings page) |
| `GITHUB_PRIVATE_KEY` | Full contents of the `.pem` file |
| `GITHUB_WEBHOOK_SECRET` | The webhook secret you set in Step 2 |
| `GROQ_API_KEY` | Free API key from [console.groq.com](https://console.groq.com) |

Click **Save** → then **Manual Deploy** → **"Deploy latest commit"**

### Step 5 — Install the App on Your Repositories

1. Go to your GitHub App settings → **"Install App"**
2. Select your account
3. Choose **"All repositories"** or select specific ones
4. Click **Install**

**Done! 🎉 The bot will now automatically manage your repositories.**

---

## 💰 Cost: $0

| Service | Free Tier |
|---------|-----------|
| **Render.com** | 750 hours/month |
| **Groq AI** | 14,400 requests/day |
| **GitHub App** | Completely free |

---

## 🔧 Tech Stack

- **Server:** Python + Flask
- **AI Model:** Llama 3.3 70B via Groq API
- **Deployment:** Render.com → [https://github-autopilot-1.onrender.com](https://github-autopilot-1.onrender.com)
- **Authentication:** GitHub App (JWT + installation tokens)

---

## 🌍 How Others Can Use This

Anyone can use this system on their own repositories for free:

1. Fork this repo
2. Deploy to Render (free)
3. Create their own GitHub App
4. Add their own free Groq API key
5. Install and go — no central server, no shared costs

---

## 📄 License

MIT License — free to use, modify, and distribute.

---

*Built with ❤️ by [Shweta Mishra](https://github.com/Shweta-Mishra-ai) — Star ⭐ the repo if it helps!*
