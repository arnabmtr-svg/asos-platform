# ASOS Wealth Platform — Setup Guide

## What you get
A full-stack wealth management platform combining:
- Portfolio transition tracker (95 → Core 22)
- Zerodha live holdings, options chain, quotes
- Iron Condor trade builder with live strike selection
- AI-powered market intelligence (Claude API)
- Multi-user login with JWT auth

---

## Prerequisites
- Python 3.10+ (`python --version`)
- VS Code with Python extension
- A Zerodha account (for live data)

---

## 1. Clone / open in VS Code

```bash
# Open the project folder in VS Code
code asos-platform
```

---

## 2. Create Python virtual environment

In VS Code terminal (Ctrl+` to open):

```bash
cd backend
python -m venv venv

# Activate (Mac/Linux)
source venv/bin/activate

# Activate (Windows)
venv\Scripts\activate
```

---

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

## 4. Set up environment

```bash
cp ../.env.example ../.env
# Edit .env — set SECRET_KEY to a random string
```

---

## 5. Start the backend

```bash
uvicorn main:app --reload --port 8000
```

You should see:
```
✅  Database initialised
✅  ASOS Platform ready — http://localhost:8000
INFO:     Uvicorn running on http://0.0.0.0:8000
```

---

## 6. Open the app

Open your browser: **http://localhost:8000**

This redirects to the login page automatically.

### Demo account (no Zerodha needed)
Click **"Try demo account"** — uses mock data so you can explore everything.

### Create your own account
Click **"Create account"** → register with any email + password.

---

## 7. Connect Zerodha (for live data)

### Step A — Create a Kite app
1. Go to https://developers.kite.trade
2. Sign in with your Zerodha account
3. Click **"Create new app"**
4. Set these values:
   - **App name**: ASOS Wealth
   - **Redirect URL**: `http://localhost:8000/kite/callback`
   - **Postback URL**: leave blank
5. Note your **API Key** and **API Secret**

### Step B — Connect in the app
1. Log in at http://localhost:8000
2. The Zerodha connect section appears below the login form
3. Enter your API Key and API Secret → click **Save API key**
4. Click **Open Zerodha login** → a popup opens
5. Log in with your Zerodha credentials
6. You'll be redirected back — Zerodha is now connected ✅

### Step C — That's it!
Live holdings, quotes, and options chain will now load from Zerodha.

---

## 8. VS Code tips

### Run backend on startup
Create `.vscode/launch.json`:
```json
{
  "configurations": [
    {
      "name": "ASOS Backend",
      "type": "python",
      "request": "launch",
      "module": "uvicorn",
      "args": ["main:app", "--reload", "--port", "8000"],
      "cwd": "${workspaceFolder}/backend",
      "envFile": "${workspaceFolder}/.env"
    }
  ]
}
```
Press **F5** to start the server.

### Recommended extensions
- Python (Microsoft)
- Pylance
- REST Client (for testing API endpoints)
- SQLite Viewer (to inspect asos.db)

---

## 9. API documentation

Once running, visit: **http://localhost:8000/docs**

Interactive Swagger UI for all endpoints.

---

## 10. Deploy to production

### Option A — Railway (easiest, free tier)
```bash
npm install -g @railway/cli
railway login
railway init
railway up
```
Set environment variables in Railway dashboard.

### Option B — Render
1. Push to GitHub
2. Connect repo at render.com
3. Set `Start command`: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add environment variables

### Option C — Docker
```bash
docker build -t asos-platform .
docker run -p 8000:8000 asos-platform
```

---

## Zerodha redirect URL for production
If deploying to `https://your-app.railway.app`, update the redirect URL in Kite developer console to:
```
https://your-app.railway.app/kite/callback
```

---

## Folder structure
```
asos-platform/
├── backend/
│   ├── main.py           ← FastAPI app (all routes)
│   ├── auth.py           ← JWT + user auth
│   ├── kite_service.py   ← Zerodha + Black-Scholes
│   ├── market_data.py    ← Yahoo Finance (IVP, ADX, RSI)
│   ├── database.py       ← SQLite setup
│   └── requirements.txt
├── frontend/
│   ├── login.html        ← Login / register page
│   └── app.html          ← Main platform (copy from asos_wealth_platform.html)
├── .env.example
├── asos.db               ← Created automatically on first run
└── README.md
```

---

## Common issues

**"Cannot reach server"** — Make sure uvicorn is running: `uvicorn main:app --reload`

**"kiteconnect not installed"** — Run `pip install kiteconnect` inside your venv

**"Invalid redirect URL"** — Kite requires the exact URL. For local: `http://localhost:8000/kite/callback`

**Port 8000 in use** — Change port: `uvicorn main:app --reload --port 8001`

**SQLite locked** — Restart the server. SQLite is single-writer.

---

## Support
Raise an issue or ask the AI Scout inside the platform.
