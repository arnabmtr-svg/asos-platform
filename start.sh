#!/usr/bin/env bash
# start.sh — one-click startup for Mac / Linux
# Usage: chmod +x start.sh && ./start.sh

set -e
echo "🚀  Starting ASOS Wealth Platform..."

cd "$(dirname "$0")/backend"

# Create venv if missing
if [ ! -d "venv" ]; then
    echo "📦  Creating Python virtual environment..."
    python3 -m venv venv
fi

# Activate
source venv/bin/activate

# Install deps if requirements changed
pip install -r requirements.txt -q

# Copy .env if missing
if [ ! -f "../.env" ]; then
    cp ../.env.example ../.env
    echo "📋  Created .env from template — edit SECRET_KEY before production use"
fi

echo ""
echo "✅  Backend starting at http://localhost:8000"
echo "    Login page: http://localhost:8000/static/login.html"
echo "    API docs:   http://localhost:8000/docs"
echo ""

uvicorn main:app --reload --port 8000 --host 0.0.0.0
