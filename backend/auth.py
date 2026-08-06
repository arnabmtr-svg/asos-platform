"""
auth.py — JWT authentication + user management
"""

import os, hashlib, secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
import jwt

from database import get_db

SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
ALGORITHM  = "HS256"
TOKEN_EXP  = int(os.getenv("TOKEN_EXPIRE_HOURS", 72))

security = HTTPBearer()


# ── Pydantic models ──────────────────────────────────────────────────────────
class UserIn(BaseModel):
    email: str
    password: str
    name: str = ""

class UserLogin(BaseModel):
    email: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type:   str
    user:         dict


# ── Password hashing (no bcrypt dependency — use sha256 + salt) ──────────────
def hash_password(password: str) -> str:
    salt   = secrets.token_hex(16)
    hashed = hashlib.sha256((password + salt).encode()).hexdigest()
    return f"{salt}:{hashed}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hashed = stored.split(":")
        return hashlib.sha256((password + salt).encode()).hexdigest() == hashed
    except Exception:
        return False


# ── Token helpers ─────────────────────────────────────────────────────────────
def create_token(data: dict) -> str:
    payload = {**data, "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXP)}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


# ── DB helpers ────────────────────────────────────────────────────────────────
def create_user(email: str, password: str, name: str = "") -> Optional[dict]:
    db = get_db()
    if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        return None   # already exists
    pw_hash = hash_password(password)
    cur = db.execute(
        "INSERT INTO users (email, password_hash, name) VALUES (?,?,?)",
        (email, pw_hash, name)
    )
    db.commit()
    return {"id": cur.lastrowid, "email": email, "name": name}

def verify_user(email: str, password: str) -> Optional[dict]:
    db  = get_db()
    row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "email": row["email"], "name": row["name"]}


# ── FastAPI dependency ────────────────────────────────────────────────────────
async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(security)
) -> dict:
    payload = decode_token(creds.credentials)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    db  = get_db()
    row = db.execute("SELECT id, email, name FROM users WHERE email=?",
                     (payload["sub"],)).fetchone()
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return {"id": row["id"], "email": row["email"], "name": row["name"]}
