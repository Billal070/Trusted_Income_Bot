"""
Database module for the Telegram bot system.

RAILWAY DEPLOYMENT NOTE:
Railway's default filesystem is ephemeral - database.db will be wiped on every
redeploy/restart unless it lives on a mounted Volume. Before going live, either:
  1. Attach a Railway Volume and set DB_PATH=/data/database.db in your env vars, OR
  2. Migrate to Railway's managed Postgres add-on for stronger persistence.
"""

import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from config import DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                credits INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                joined_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                file_hash TEXT,
                is_sent INTEGER DEFAULT 0,
                sent_to_user_id INTEGER,
                sent_at TIMESTAMP,
                added_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                content_type TEXT,
                content TEXT,
                caption TEXT,
                status TEXT DEFAULT 'pending',
                submitted_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS credit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER,
                action TEXT,
                admin_id INTEGER,
                timestamp TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_photos_available
                ON photos(is_sent, id);
            CREATE INDEX IF NOT EXISTS idx_submissions_status
                ON submissions(status);
        """)
        # Migration: add caption column if upgrading from older DB
        try:
            conn.execute("ALTER TABLE submissions ADD COLUMN caption TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN last_nid_at TIMESTAMP")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN bdt_balance INTEGER DEFAULT 0")
        except Exception:
            pass
        # Deposits table for wallet system
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                method TEXT NOT NULL,
                amount INTEGER NOT NULL,
                trx_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP,
                verified_at TIMESTAMP,
                verified_by INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status);
            CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits(user_id);
        """)


# -- User helpers ----------------------------------------------

def get_user(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def register_user(user_id: int, username: str | None, first_name: str | None):
    with get_conn() as conn:
        existing = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, credits, is_banned, joined_at) VALUES (?, ?, ?, 0, 0, ?)",
                (user_id, username, first_name, _now()),
            )


def update_username(user_id: int, username: str | None, first_name: str | None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
            (username, first_name, user_id),
        )


def get_all_user_ids() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT user_id FROM users WHERE is_banned = 0").fetchall()
        return [row["user_id"] for row in rows]


def search_user(identifier: str) -> dict | None:
    with get_conn() as conn:
        if identifier.startswith("@"):
            row = conn.execute("SELECT * FROM users WHERE username = ?", (identifier[1:],)).fetchone()
        elif identifier.isdigit():
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (int(identifier),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (identifier,)).fetchone()
        return dict(row) if row else None


def add_credit(user_id: int, amount: int, admin_id: int) -> int:
    with get_conn() as conn:
        conn.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
        conn.execute(
            "INSERT INTO credit_logs (user_id, amount, action, admin_id, timestamp) VALUES (?, ?, 'add', ?, ?)",
            (user_id, amount, admin_id, _now()),
        )
        row = conn.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["credits"] if row else 0


def deduct_credit(user_id: int, amount: int, admin_id: int) -> int:
    with get_conn() as conn:
        conn.execute("UPDATE users SET credits = credits - ? WHERE user_id = ?", (amount, user_id))
        conn.execute(
            "INSERT INTO credit_logs (user_id, amount, action, admin_id, timestamp) VALUES (?, ?, 'deduct', ?, ?)",
            (user_id, amount, admin_id, _now()),
        )
        row = conn.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["credits"] if row else 0


def claim_photo_for_user(user_id: int):
    """Reserve one available photo atomically (marks is_sent=1). No credit deducted yet.
    Returns (photo_id, file_id) or (None, None) if none available or race lost.
    Caller must call confirm_photo_delivery() on success or refund_photo_claim() on failure.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, file_id FROM photos WHERE is_sent = 0 ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None, None
        photo_id = row["id"]
        file_id = row["file_id"]
        cursor = conn.execute(
            "UPDATE photos SET is_sent = 1, sent_to_user_id = ?, sent_at = ? WHERE id = ? AND is_sent = 0",
            (user_id, _now(), photo_id),
        )
        if cursor.rowcount == 0:
            return None, None
        return photo_id, file_id


def confirm_photo_delivery(user_id: int) -> int | None:
    """Deduct 1 credit after successful photo send. Returns new_balance or None if insufficient (race)."""
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE users SET credits = credits - 1 WHERE user_id = ? AND credits >= 1",
            (user_id,),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["credits"] if row else 0


def refund_photo_claim(photo_id: int):
    """Undo a photo claim (e.g. send failed before deduct). Does NOT refund credit — credit was never deducted."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE photos SET is_sent = 0, sent_to_user_id = NULL, sent_at = NULL WHERE id = ?",
            (photo_id,),
        )


def refund_photo_and_credit(photo_id: int, user_id: int):
    """Undo a photo claim after credit was already deducted (send failed after deduct)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE photos SET is_sent = 0, sent_to_user_id = NULL, sent_at = NULL WHERE id = ?",
            (photo_id,),
        )
        conn.execute("UPDATE users SET credits = credits + 1 WHERE user_id = ?", (user_id,))


def deduct_user_credit_for_pic(user_id: int):
    """Legacy: deduct 1 credit and claim a photo atomically.
    Kept for backward compat — new code should use claim/confirm/refund.
    Returns (True, file_id, new_balance) or (False, None, None).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, file_id FROM photos WHERE is_sent = 0 ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return False, None, None

        photo_id = row["id"]
        file_id = row["file_id"]

        cursor = conn.execute(
            "UPDATE photos SET is_sent = 1, sent_to_user_id = ?, sent_at = ? WHERE id = ? AND is_sent = 0",
            (user_id, _now(), photo_id),
        )
        if cursor.rowcount == 0:
            return False, None, None

        conn.execute("UPDATE users SET credits = credits - 1 WHERE user_id = ?", (user_id,))
        new_balance = conn.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()["credits"]
        return True, file_id, new_balance


def set_banned(user_id: int, banned: bool):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if banned else 0, user_id))


def is_banned(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row and row["is_banned"])


def get_credits(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["credits"] if row else 0


# -- Cooldown (2m30s) -------------------------------------------

NID_COOLDOWN = 150  # seconds = 2*60+30

def get_cooldown_remaining(user_id: int) -> int:
    """Returns seconds remaining on Get Nid cooldown, 0 if ready."""
    with get_conn() as conn:
        row = conn.execute("SELECT last_nid_at FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row or not row["last_nid_at"]:
            return 0
        try:
            last = datetime.fromisoformat(row["last_nid_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except Exception:
            return 0
        now = datetime.now(timezone.utc)
        elapsed = (now - last).total_seconds()
        remaining = NID_COOLDOWN - elapsed
        return int(remaining) if remaining > 0 else 0


def set_last_nid(user_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_nid_at = ? WHERE user_id = ?", (_now(), user_id))


# -- Wallet / Deposits ----------------------------------------

def get_bdt_balance(user_id: int) -> float:
    with get_conn() as conn:
        row = conn.execute("SELECT bdt_balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return 0.0
        try:
            return float(row["bdt_balance"] or 0)
        except Exception:
            return 0.0

def deduct_bdt_for_purchase(user_id: int, total_cost: float) -> float | None:
    """Atomically deduct BDT for product purchase. Returns new balance or None if insufficient."""
    with get_conn() as conn:
        row = conn.execute("SELECT bdt_balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        cur = float(row["bdt_balance"] or 0) if row else 0.0
        if cur < total_cost - 1e-9:
            return None
        new_bal = cur - total_cost
        conn.execute("UPDATE users SET bdt_balance = ? WHERE user_id = ?", (new_bal, user_id))
        # log as bdt purchase
        conn.execute("INSERT INTO credit_logs (user_id, amount, action, admin_id, timestamp) VALUES (?, ?, 'bdt_purchase', 0, ?)", (user_id, int(total_cost) if total_cost.is_integer() else total_cost, _now()))
        return new_bal

def refund_bdt_purchase(user_id: int, total_cost: float):
    with get_conn() as conn:
        conn.execute("UPDATE users SET bdt_balance = COALESCE(bdt_balance,0)+? WHERE user_id = ?", (total_cost, user_id))

def create_deposit(user_id: int, username: str | None, method: str, amount: int, trx_id: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO deposits (user_id, username, method, amount, trx_id, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (user_id, username, method, amount, trx_id, _now()),
        )
        return cur.lastrowid

def get_deposit(deposit_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM deposits WHERE id = ?", (deposit_id,)).fetchone()
        return dict(row) if row else None

def get_pending_deposits(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM deposits WHERE status='pending' ORDER BY created_at ASC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

def approve_deposit(deposit_id: int, admin_id: int) -> dict | None:
    """Approve deposit: set status approved, add ONLY bdt_balance (strict separation). Returns deposit dict or None."""
    with get_conn() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id = ? AND status='pending'", (deposit_id,)).fetchone()
        if not dep:
            return None
        dep = dict(dep)
        amt = int(dep["amount"])
        uid = dep["user_id"]
        conn.execute("UPDATE deposits SET status='approved', verified_at=?, verified_by=? WHERE id=?", (_now(), admin_id, deposit_id))
        # Strict separation: ONLY bdt_balance, NOT credits
        conn.execute("UPDATE users SET bdt_balance = COALESCE(bdt_balance,0)+? WHERE user_id=?", (amt, uid))
        row = conn.execute("SELECT credits, bdt_balance FROM users WHERE user_id=?", (uid,)).fetchone()
        dep["new_credits"] = row["credits"] if row else 0
        dep["new_bdt"] = row["bdt_balance"] if row and "bdt_balance" in row.keys() else amt
        return dep

def add_bdt_balance(user_id: int, amount: int, admin_id: int) -> int:
    with get_conn() as conn:
        conn.execute("UPDATE users SET bdt_balance = COALESCE(bdt_balance,0)+? WHERE user_id=?", (amount, user_id))
        conn.execute("INSERT INTO credit_logs (user_id, amount, action, admin_id, timestamp) VALUES (?, ?, 'bdt_add', ?, ?)", (user_id, amount, admin_id, _now()))
        row = conn.execute("SELECT bdt_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["bdt_balance"] or 0) if row else amount

def remove_bdt_balance(user_id: int, amount: int, admin_id: int) -> int:
    with get_conn() as conn:
        # Prevent negative
        cur = conn.execute("SELECT bdt_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        cur_bal = int(cur["bdt_balance"] or 0) if cur else 0
        deduct = min(cur_bal, amount)
        conn.execute("UPDATE users SET bdt_balance = COALESCE(bdt_balance,0)-? WHERE user_id=?", (deduct, user_id))
        conn.execute("INSERT INTO credit_logs (user_id, amount, action, admin_id, timestamp) VALUES (?, ?, 'bdt_deduct', ?, ?)", (user_id, deduct, admin_id, _now()))
        row = conn.execute("SELECT bdt_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["bdt_balance"] or 0) if row else 0

def reject_deposit(deposit_id: int, admin_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM deposits WHERE id=? AND status='pending'", (deposit_id,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE deposits SET status='rejected', verified_at=?, verified_by=? WHERE id=?", (_now(), admin_id, deposit_id))
        return dict(row)


# -- Photo helpers ---------------------------------------------

def add_photo(file_id: str, file_hash: str | None = None) -> int | None:
    """Add a photo to the pool. Returns new photo id if added, None if duplicate hash."""
    with get_conn() as conn:
        if file_hash:
            existing = conn.execute("SELECT id FROM photos WHERE file_hash = ?", (file_hash,)).fetchone()
            if existing:
                return None
        cur = conn.execute(
            "INSERT INTO photos (file_id, file_hash, is_sent, added_at) VALUES (?, ?, 0, ?)",
            (file_id, file_hash, _now()),
        )
        return cur.lastrowid


def update_photo_file_id(photo_id: int, new_file_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE photos SET file_id = ? WHERE id = ?", (new_file_id, photo_id))


def get_pool_stats() -> dict:
    with get_conn() as conn:
        available = conn.execute("SELECT COUNT(*) as c FROM photos WHERE is_sent = 0").fetchone()["c"]
        sent = conn.execute("SELECT COUNT(*) as c FROM photos WHERE is_sent = 1").fetchone()["c"]
        return {"available": available, "sent": sent}


# -- Submission helpers ----------------------------------------

def add_submission(user_id: int, content_type: str, content: str, caption: str | None = None) -> int:
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO submissions (user_id, content_type, content, caption, status, submitted_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (user_id, content_type, content, caption, _now()),
        )
        return cursor.lastrowid


def get_pending_submissions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM submissions WHERE status = 'pending' ORDER BY submitted_at ASC").fetchall()
        return [dict(r) for r in rows]


# -- Paginated users + per-user stats -------------------------

USERS_PER_PAGE = 5

def count_users() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]


def get_users_paginated(limit: int = 5, offset: int = 0) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]


def get_submission_count(user_id: int) -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) as c FROM submissions WHERE user_id = ?", (user_id,)).fetchone()["c"]


def get_credit_summary(user_id: int) -> dict:
    with get_conn() as conn:
        added = conn.execute(
            "SELECT COALESCE(SUM(amount),0) as s FROM credit_logs WHERE user_id = ? AND action='add'", (user_id,)
        ).fetchone()["s"]
        deducted = conn.execute(
            "SELECT COALESCE(SUM(amount),0) as s FROM credit_logs WHERE user_id = ? AND action='deduct'", (user_id,)
        ).fetchone()["s"]
        return {"added": added, "deducted": deducted}

# -- E-commerce metrics (strict separation, no deduct stats in UI) --

def get_total_spent() -> float:
    with get_conn() as conn:
        row = conn.execute("SELECT COALESCE(SUM(amount),0) as s FROM credit_logs WHERE action='bdt_purchase'").fetchone()
        return float(row["s"] or 0)

def get_lifetime_deposits_total() -> float:
    with get_conn() as conn:
        row = conn.execute("SELECT COALESCE(SUM(amount),0) as s FROM deposits WHERE status='approved'").fetchone()
        return float(row["s"] or 0)

def get_total_credit_added() -> float:
    with get_conn() as conn:
        row = conn.execute("SELECT COALESCE(SUM(amount),0) as s FROM credit_logs WHERE action='add'").fetchone()
        return float(row["s"] or 0)

def get_user_spent(user_id: int) -> float:
    with get_conn() as conn:
        row = conn.execute("SELECT COALESCE(SUM(amount),0) as s FROM credit_logs WHERE user_id=? AND action='bdt_purchase'", (user_id,)).fetchone()
        return float(row["s"] or 0)

def get_user_lifetime_deposits(user_id: int) -> float:
    with get_conn() as conn:
        row = conn.execute("SELECT COALESCE(SUM(amount),0) as s FROM deposits WHERE user_id=? AND status='approved'", (user_id,)).fetchone()
        return float(row["s"] or 0)

def get_user_total_credit_added(user_id: int) -> float:
    with get_conn() as conn:
        row = conn.execute("SELECT COALESCE(SUM(amount),0) as s FROM credit_logs WHERE user_id=? AND action='add'", (user_id,)).fetchone()
        return float(row["s"] or 0)


# -- Stats -----------------------------------------------------

def get_stats() -> dict:
    with get_conn() as conn:
        total_users = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        total_credits = conn.execute("SELECT COALESCE(SUM(credits), 0) as c FROM users").fetchone()["c"]
        photos = get_pool_stats()
        pending = conn.execute("SELECT COUNT(*) as c FROM submissions WHERE status = 'pending'").fetchone()["c"]
        return {
            "total_users": total_users,
            "total_credits": total_credits,
            "photos_available": photos["available"],
            "photos_sent": photos["sent"],
            "pending_submissions": pending,
        }
