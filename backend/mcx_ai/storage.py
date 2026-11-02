"""SQLite connection helpers and persistence defaults for the single-user app."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from .config import settings

def now():
    """Return a local, timezone-aware ISO timestamp for persisted events."""
    return datetime.now().astimezone().isoformat()
@contextmanager
def db():
    """Yield a row-mapped SQLite connection and commit successful operations."""
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(settings.db_path); con.row_factory = sqlite3.Row
    try: yield con; con.commit()
    finally: con.close()
def init_db():
    """Create tables if absent and seed risk/signal settings without overwrites."""
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, side TEXT NOT NULL, entry REAL, stop REAL, target REAL, confidence REAL NOT NULL, rationale TEXT NOT NULL, reports TEXT NOT NULL, status TEXT NOT NULL, validation TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY, signal_id INTEGER NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, side TEXT NOT NULL, entry_price REAL NOT NULL, exit_price REAL, quantity INTEGER NOT NULL, estimated_entry_cost REAL NOT NULL, estimated_exit_cost REAL NOT NULL DEFAULT 0, gross_pnl REAL, net_pnl REAL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS backtests (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, config TEXT NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS replay_sessions (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, start_at TEXT NOT NULL, interval_minutes INTEGER NOT NULL, current_index INTEGER NOT NULL, status TEXT NOT NULL, config TEXT NOT NULL, candles TEXT NOT NULL, steps TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fundamental_cache (cache_id INTEGER PRIMARY KEY CHECK(cache_id=1), fetched_at TEXT, last_attempt_at TEXT, last_error TEXT, feed_signature TEXT NOT NULL, sources TEXT NOT NULL);
        ''')
        defaults={"max_stop_points":settings.max_stop_points,"risk_per_trade_inr":settings.risk_per_trade_inr,"max_daily_loss_inr":settings.max_daily_loss_inr,"contract_multiplier":settings.contract_multiplier,"entry_cutoff":settings.entry_cutoff,"exit_cutoff":settings.exit_cutoff,"signal_ttl_minutes":settings.signal_ttl_minutes}
        for key,value in defaults.items(): con.execute("INSERT OR IGNORE INTO settings VALUES (?,?)",(key,str(value)))
def audit(event, detail):
    """Append an operator-visible lifecycle event to the audit table."""
    with db() as con: con.execute("INSERT INTO audit_log(at,event,detail) VALUES(?,?,?)",(now(),event,detail))
def configured():
    """Read effective editable settings from SQLite as string values."""
    with db() as con: return {r['key']: r['value'] for r in con.execute('SELECT key,value FROM settings')}
