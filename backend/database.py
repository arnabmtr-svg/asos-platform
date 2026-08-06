"""database.py — SQLite setup"""
import sqlite3, os

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "asos.db"))
_conn: sqlite3.Connection = None

def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        email             TEXT UNIQUE NOT NULL,
        password_hash     TEXT NOT NULL,
        name              TEXT DEFAULT '',
        kite_api_key      TEXT DEFAULT '',
        kite_api_secret   TEXT DEFAULT '',
        kite_access_token TEXT DEFAULT '',
        kite_connected    INTEGER DEFAULT 0,
        created_at        TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id          INTEGER PRIMARY KEY REFERENCES users(id),
        sip_amount       REAL    DEFAULT 100000,
        target_cagr      REAL    DEFAULT 20,
        target_year      INTEGER DEFAULT 2047,
        telegram_token   TEXT    DEFAULT '',
        telegram_chat_id TEXT    DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS watchlist (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER REFERENCES users(id),
        ticker        TEXT,
        sector        TEXT DEFAULT '',
        roce          REAL DEFAULT 0,
        de            REAL DEFAULT 0,
        rev_cagr      REAL DEFAULT 0,
        score         REAL DEFAULT 0,
        thesis        TEXT DEFAULT '',
        entry_trigger TEXT DEFAULT '',
        added_at      TEXT DEFAULT (datetime('now')),
        UNIQUE(user_id, ticker)
    );
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER REFERENCES users(id),
        corpus     REAL,
        pnl        REAL,
        snapshot_date TEXT DEFAULT (date('now'))
    );
    """)
    db.commit()
    print("✅  Database initialised")
