"""
User accounts, admin approval, and subscriptions for AiTC (PostgreSQL on Railway).

Local dev without DATABASE_URL falls back to SQLite at data/users.sqlite3.
"""

import os
import re
import secrets
import hashlib
import hmac
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from email_service import (
    merge_email_config,
    is_email_configured,
    send_signup_verification_email,
)

auth_bp = Blueprint('user_auth', __name__)

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_SUBSCRIPTION_MONTH_DAYS = 31
_ONE_DAY_PASS_HOURS = 24
PASS_TYPE_MONTHLY = 'monthly'
PASS_TYPE_ONE_DAY = 'one_day'
PASS_TYPE_PROMO = 'promo'
ADMIN_SIM_STRIPE_PREFIX = 'admin_sim_'
SIGNUP_CODE_TTL_MINUTES = 5
SIGNUP_MAX_VERIFY_ATTEMPTS = 8

_USE_POSTGRES = False
_DB_PATH = None
_pg = None
_AUTH_DATA_DIR = None
_APP_SECRET = ''


def _now_utc():
    return datetime.now(timezone.utc)


def _iso_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def subscription_end_date(start):
    """One subscription month = 31 calendar days from start (inclusive span)."""
    if isinstance(start, str):
        start = date.fromisoformat(start)
    return start + timedelta(days=_SUBSCRIPTION_MONTH_DAYS)


def one_day_pass_expires_at(start=None):
    """One Day Pass = 24 hours of platform access from activation."""
    start = start or _now_utc()
    if isinstance(start, date) and not isinstance(start, datetime):
        start = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start + timedelta(hours=_ONE_DAY_PASS_HOURS)


def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def admin_simulated_stripe_subscription_id(subscription_id):
    return f'{ADMIN_SIM_STRIPE_PREFIX}{subscription_id}'


def is_admin_simulated_stripe_subscription_id(stripe_subscription_id):
    return (stripe_subscription_id or '').strip().startswith(ADMIN_SIM_STRIPE_PREFIX)


def is_admin_simulated_stripe_subscription(sub):
    return is_admin_simulated_stripe_subscription_id((sub or {}).get('stripe_subscription_id'))


def _normalized_pass_type(sub):
    if not sub:
        return PASS_TYPE_MONTHLY
    pass_type = (sub.get('pass_type') or PASS_TYPE_MONTHLY).strip().lower()
    plan_name = (sub.get('plan_name') or '').strip().lower()
    if pass_type == PASS_TYPE_PROMO or plan_name in ('promo-access', 'promo_access'):
        return PASS_TYPE_PROMO
    return pass_type


def is_user_cancellable_monthly_subscription(sub):
    if not sub or _normalized_pass_type(sub) != PASS_TYPE_MONTHLY:
        return False
    return bool((sub.get('stripe_subscription_id') or '').strip())


def _subscription_display_plan_name(sub):
    pass_type = _normalized_pass_type(sub)
    if pass_type == PASS_TYPE_PROMO:
        return 'Promo access'
    raw = (sub.get('plan_name') or 'standard').strip()
    if pass_type == PASS_TYPE_ONE_DAY and raw in ('one-day-pass', 'admin-one-day-pass'):
        return 'One Day Pass'
    return raw or 'standard'


def _subscription_covers_now(sub, now=None):
    if not sub or sub.get('status') != 'active':
        return False
    now = now or _now_utc()
    pass_type = _normalized_pass_type(sub)
    if pass_type in (PASS_TYPE_ONE_DAY, PASS_TYPE_PROMO):
        expires = _parse_dt(sub.get('expires_at'))
        return bool(expires and expires > now)
    today = now.date()
    start = sub.get('start_date')
    end = sub.get('end_date')
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)
    return bool(start and end and start <= today <= end)


def _normalize_database_url(url):
    url = (url or '').strip()
    if url.startswith('postgres://'):
        return 'postgresql://' + url[len('postgres://'):]
    return url


def _configure_db(data_dir):
    global _USE_POSTGRES, _DB_PATH, _pg

    database_url = _normalize_database_url(os.environ.get('DATABASE_URL', ''))
    if database_url.startswith('postgresql://'):
        try:
            import psycopg2
            import psycopg2.extras  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                'DATABASE_URL is set but psycopg2 is not installed. '
                'Add psycopg2-binary to requirements.txt (Railway/Docker) or unset DATABASE_URL for local SQLite.'
            ) from exc
        _USE_POSTGRES = True
        _DB_PATH = None
        _pg = psycopg2
        return

    _USE_POSTGRES = False
    _pg = None
    os.makedirs(data_dir, exist_ok=True)
    _DB_PATH = os.path.join(data_dir, 'users.sqlite3')


@contextmanager
def _db_conn():
    if _USE_POSTGRES:
        conn = _pg.connect(_normalize_database_url(os.environ.get('DATABASE_URL', '')))
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    conn = sqlite3.connect(_DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_dict(row):
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if hasattr(row, 'keys'):
        return dict(row)
    return row


def _new_user_id():
    return str(uuid.uuid4())


def _signup_email_config():
    return merge_email_config(data_dir=_AUTH_DATA_DIR)


def _generate_signup_code():
    return f'{secrets.randbelow(900000) + 100000:06d}'


def _hash_signup_code(verification_id, code):
    secret = (_APP_SECRET or 'dev-insecure-secret-change-me').encode('utf-8')
    payload = f'{verification_id}:{code}'.encode('utf-8')
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _verify_signup_code(verification_id, code, code_hash):
    if not verification_id or not code or not code_hash:
        return False
    expected = _hash_signup_code(verification_id, code.strip())
    return hmac.compare_digest(expected, code_hash)


def _cleanup_expired_signup_verifications():
    now = _now_utc()
    now_s = now.isoformat()
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute('DELETE FROM signup_verifications WHERE expires_at < %s', (now,))
            cur.close()
            return
        conn.execute('DELETE FROM signup_verifications WHERE expires_at < ?', (now_s,))


def _delete_signup_verifications_for_email(email):
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute('DELETE FROM signup_verifications WHERE email = %s', (email,))
            cur.close()
            return
        conn.execute('DELETE FROM signup_verifications WHERE email = ?', (email,))


def _fetch_signup_verification(verification_id):
    verification_id = str(verification_id or '').strip()
    if not verification_id:
        return None
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''SELECT id, email, password_hash, display_name, code_hash, expires_at, attempt_count, created_at
                   FROM signup_verifications WHERE id = %s''',
                (verification_id,),
            )
            row = cur.fetchone()
            cur.close()
        else:
            row = conn.execute(
                '''SELECT id, email, password_hash, display_name, code_hash, expires_at, attempt_count, created_at
                   FROM signup_verifications WHERE id = ?''',
                (verification_id,),
            ).fetchone()
    if not row:
        return None
    if isinstance(row, sqlite3.Row) or hasattr(row, 'keys'):
        data = dict(row)
    else:
        keys = ['id', 'email', 'password_hash', 'display_name', 'code_hash', 'expires_at', 'attempt_count', 'created_at']
        data = dict(zip(keys, row))
    expires_at = _parse_dt(data.get('expires_at'))
    data['expires_at_dt'] = expires_at
    return data


def _create_signup_verification(email, password_hash, display_name, code):
    verification_id = _new_user_id()
    now = _now_utc()
    expires_at = now + timedelta(minutes=SIGNUP_CODE_TTL_MINUTES)
    code_hash = _hash_signup_code(verification_id, code)
    _delete_signup_verifications_for_email(email)
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO signup_verifications (
                    id, email, password_hash, display_name, code_hash, expires_at, attempt_count, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 0, %s)''',
                (verification_id, email, password_hash, display_name or None, code_hash, expires_at, now),
            )
            cur.close()
        else:
            conn.execute(
                '''INSERT INTO signup_verifications (
                    id, email, password_hash, display_name, code_hash, expires_at, attempt_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)''',
                (verification_id, email, password_hash, display_name or None, code_hash, expires_at.isoformat(), now.isoformat()),
            )
    return verification_id, expires_at


def _increment_signup_verification_attempts(verification_id):
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                'UPDATE signup_verifications SET attempt_count = attempt_count + 1 WHERE id = %s',
                (verification_id,),
            )
            cur.close()
            return
        conn.execute(
            'UPDATE signup_verifications SET attempt_count = attempt_count + 1 WHERE id = ?',
            (verification_id,),
        )


def _delete_signup_verification(verification_id):
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute('DELETE FROM signup_verifications WHERE id = %s', (verification_id,))
            cur.close()
            return
        conn.execute('DELETE FROM signup_verifications WHERE id = ?', (verification_id,))


def _validate_signup_payload(body):
    email = (body.get('email') or '').strip().lower()
    password = body.get('password') or ''
    display_name = (body.get('displayName') or '').strip()
    if not email or not _EMAIL_RE.match(email):
        return None, None, None, ('Enter a valid email address', 400)
    if len(password) < 8:
        return None, None, None, ('Password must be at least 8 characters', 400)
    if _fetch_user_by_email(email):
        return None, None, None, ('An account with this email already exists', 409)
    return email, password, display_name, None


def init_db():
    if _USE_POSTGRES:
        ddl_users = '''
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected', 'disabled')),
                is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                stripe_customer_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                approved_at TIMESTAMPTZ,
                approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
                rejected_at TIMESTAMPTZ,
                rejected_by UUID REFERENCES users(id) ON DELETE SET NULL
            )
        '''
        ddl_subs = '''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                plan_name TEXT NOT NULL DEFAULT 'standard',
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'expired', 'cancelled')),
                notes TEXT,
                stripe_subscription_id TEXT,
                pass_type TEXT NOT NULL DEFAULT 'monthly',
                expires_at TIMESTAMPTZ,
                stripe_checkout_session_id TEXT,
                cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                CHECK (end_date >= start_date)
            )
        '''
        indexes = [
            'CREATE INDEX IF NOT EXISTS idx_users_email ON users (email)',
            'CREATE INDEX IF NOT EXISTS idx_users_status ON users (status)',
            'CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions (user_id)',
            'CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions (status)',
            'CREATE INDEX IF NOT EXISTS idx_subscriptions_end_date ON subscriptions (end_date)',
        ]
        with _db_conn() as conn:
            cur = conn.cursor()
            cur.execute(ddl_users)
            cur.execute(ddl_subs)
            for stmt in indexes:
                cur.execute(stmt)
            cur.close()
        _ensure_signup_verifications_table()
        _migrate_schema()
        return

    ddl_users = '''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'disabled')),
            is_admin INTEGER NOT NULL DEFAULT 0,
            stripe_customer_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT,
            approved_by TEXT,
            rejected_at TEXT,
            rejected_by TEXT
        )
    '''
    ddl_subs = '''
        CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plan_name TEXT NOT NULL DEFAULT 'standard',
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'expired', 'cancelled')),
            notes TEXT,
            stripe_subscription_id TEXT,
            pass_type TEXT NOT NULL DEFAULT 'monthly',
            expires_at TEXT,
            stripe_checkout_session_id TEXT,
            cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            created_by TEXT,
            CHECK (end_date >= start_date)
        )
    '''
    indexes = [
        'CREATE INDEX IF NOT EXISTS idx_users_email ON users (email)',
        'CREATE INDEX IF NOT EXISTS idx_users_status ON users (status)',
        'CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions (user_id)',
        'CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions (status)',
        'CREATE INDEX IF NOT EXISTS idx_subscriptions_end_date ON subscriptions (end_date)',
    ]
    with _db_conn() as conn:
        conn.execute(ddl_users)
        conn.execute(ddl_subs)
        for stmt in indexes:
            conn.execute(stmt)

    _ensure_signup_verifications_table()
    _migrate_schema()


def _ensure_signup_verifications_table():
    ddl = '''
        CREATE TABLE IF NOT EXISTS signup_verifications (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            code_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    '''
    indexes = [
        'CREATE INDEX IF NOT EXISTS idx_signup_verifications_email ON signup_verifications (email)',
        'CREATE INDEX IF NOT EXISTS idx_signup_verifications_expires_at ON signup_verifications (expires_at)',
    ]
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''
                CREATE TABLE IF NOT EXISTS signup_verifications (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    display_name TEXT,
                    code_hash TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                '''
            )
            for stmt in indexes:
                cur.execute(stmt)
            cur.close()
            return
        conn.execute(ddl)
        for stmt in indexes:
            conn.execute(stmt)


def _sqlite_has_column(conn, table, column):
    rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
    return any(row['name'] == column for row in rows)


def _migrate_schema():
    """Add Stripe-related columns to existing deployments."""
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT')
            cur.execute('ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT')
            cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS pass_type TEXT NOT NULL DEFAULT 'monthly'")
            cur.execute('ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ')
            cur.execute('ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_checkout_session_id TEXT')
            cur.execute('ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE')
            cur.execute(
                '''CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_stripe_subscription_id
                   ON subscriptions (stripe_subscription_id)
                   WHERE stripe_subscription_id IS NOT NULL'''
            )
            cur.execute(
                '''CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_stripe_checkout_session_id
                   ON subscriptions (stripe_checkout_session_id)
                   WHERE stripe_checkout_session_id IS NOT NULL'''
            )
            cur.execute(
                '''UPDATE subscriptions SET pass_type = 'promo'
                   WHERE pass_type = 'one_day'
                     AND LOWER(COALESCE(plan_name, '')) IN ('promo-access', 'promo_access')'''
            )
            cur.execute(
                '''UPDATE subscriptions SET stripe_subscription_id = 'admin_sim_' || id::text
                   WHERE status = 'active'
                     AND pass_type = 'monthly'
                     AND stripe_subscription_id IS NULL
                     AND stripe_checkout_session_id IS NULL
                     AND created_by IS NOT NULL'''
            )
            cur.close()
            return

        if not _sqlite_has_column(conn, 'users', 'stripe_customer_id'):
            conn.execute('ALTER TABLE users ADD COLUMN stripe_customer_id TEXT')
        if not _sqlite_has_column(conn, 'subscriptions', 'stripe_subscription_id'):
            conn.execute('ALTER TABLE subscriptions ADD COLUMN stripe_subscription_id TEXT')
        if not _sqlite_has_column(conn, 'subscriptions', 'pass_type'):
            conn.execute("ALTER TABLE subscriptions ADD COLUMN pass_type TEXT NOT NULL DEFAULT 'monthly'")
        if not _sqlite_has_column(conn, 'subscriptions', 'expires_at'):
            conn.execute('ALTER TABLE subscriptions ADD COLUMN expires_at TEXT')
        if not _sqlite_has_column(conn, 'subscriptions', 'stripe_checkout_session_id'):
            conn.execute('ALTER TABLE subscriptions ADD COLUMN stripe_checkout_session_id TEXT')
        if not _sqlite_has_column(conn, 'subscriptions', 'cancel_at_period_end'):
            conn.execute('ALTER TABLE subscriptions ADD COLUMN cancel_at_period_end INTEGER NOT NULL DEFAULT 0')
        conn.execute(
            '''CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_stripe_subscription_id
               ON subscriptions (stripe_subscription_id)
               WHERE stripe_subscription_id IS NOT NULL'''
        )
        conn.execute(
            '''CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_stripe_checkout_session_id
               ON subscriptions (stripe_checkout_session_id)
               WHERE stripe_checkout_session_id IS NOT NULL'''
        )
        conn.execute(
            '''UPDATE subscriptions SET pass_type = 'promo'
               WHERE pass_type = 'one_day'
                 AND LOWER(COALESCE(plan_name, '')) IN ('promo-access', 'promo_access')'''
        )
        conn.execute(
            '''UPDATE subscriptions SET stripe_subscription_id = 'admin_sim_' || id
               WHERE status = 'active'
                 AND pass_type = 'monthly'
                 AND (stripe_subscription_id IS NULL OR stripe_subscription_id = '')
                 AND (stripe_checkout_session_id IS NULL OR stripe_checkout_session_id = '')
                 AND created_by IS NOT NULL'''
        )


def _admin_env_credentials():
    """Railway / production admin login from environment variables.

    Primary (set these in Railway):
      AITC_ADMIN_EMAIL      — admin sign-in email
      AITC_ADMIN_PASSWORD   — admin sign-in password (min 8 characters)

    Optional:
      AITC_ADMIN_DISPLAY_NAME — shown in admin UI (default: Administrator)

    Legacy aliases (still supported):
      BOOTSTRAP_ADMIN_EMAIL, BOOTSTRAP_ADMIN_PASSWORD, BOOTSTRAP_ADMIN_DISPLAY_NAME
    """
    email = (
        os.environ.get('AITC_ADMIN_EMAIL')
        or os.environ.get('BOOTSTRAP_ADMIN_EMAIL')
        or ''
    ).strip().lower()
    password = (
        os.environ.get('AITC_ADMIN_PASSWORD')
        or os.environ.get('BOOTSTRAP_ADMIN_PASSWORD')
        or ''
    ).strip()
    display_name = (
        os.environ.get('AITC_ADMIN_DISPLAY_NAME')
        or os.environ.get('BOOTSTRAP_ADMIN_DISPLAY_NAME')
        or 'Administrator'
    ).strip() or 'Administrator'
    return email, password, display_name


def bootstrap_admin_user():
    """Create or update the Railway-configured admin account on every app start."""
    email, password, display_name = _admin_env_credentials()
    if not email or not password:
        return
    if len(password) < 8:
        return

    password_hash = generate_password_hash(password)
    now = _now_utc()
    existing = _fetch_user_by_email(email)

    with _db_conn() as conn:
        if existing:
            user_id = existing['id']
            if _USE_POSTGRES:
                cur = conn.cursor()
                cur.execute(
                    '''UPDATE users SET
                        password_hash = %s,
                        display_name = %s,
                        status = 'approved',
                        is_admin = TRUE,
                        approved_at = COALESCE(approved_at, %s),
                        updated_at = %s
                       WHERE id = %s''',
                    (password_hash, display_name, now, now, user_id),
                )
                cur.close()
            else:
                conn.execute(
                    '''UPDATE users SET
                        password_hash = ?,
                        display_name = ?,
                        status = 'approved',
                        is_admin = 1,
                        approved_at = COALESCE(approved_at, ?),
                        updated_at = ?
                       WHERE id = ?''',
                    (password_hash, display_name, now.isoformat(), now.isoformat(), user_id),
                )
            return

        user_id = _new_user_id()
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO users (
                    id, email, password_hash, display_name, status, is_admin,
                    created_at, updated_at, approved_at
                ) VALUES (%s, %s, %s, %s, 'approved', TRUE, %s, %s, %s)''',
                (user_id, email, password_hash, display_name, now, now, now),
            )
            cur.close()
        else:
            now_s = now.isoformat()
            conn.execute(
                '''INSERT INTO users (
                    id, email, password_hash, display_name, status, is_admin,
                    created_at, updated_at, approved_at
                ) VALUES (?, ?, ?, ?, 'approved', 1, ?, ?, ?)''',
                (user_id, email, password_hash, display_name, now_s, now_s, now_s),
            )


def _fetch_user_by_id(user_id):
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor(cursor_factory=_pg.extras.RealDictCursor)
            cur.execute('SELECT * FROM users WHERE id = %s', (user_id,))
            row = cur.fetchone()
            cur.close()
            return _row_to_dict(row)

        row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        return _row_to_dict(row)


def _fetch_user_by_email(email):
    email = (email or '').strip().lower()
    if not email:
        return None
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor(cursor_factory=_pg.extras.RealDictCursor)
            cur.execute('SELECT * FROM users WHERE LOWER(email) = LOWER(%s)', (email,))
            row = cur.fetchone()
            cur.close()
            return _row_to_dict(row)

        row = conn.execute('SELECT * FROM users WHERE email = ? COLLATE NOCASE', (email,)).fetchone()
        return _row_to_dict(row)


def _fetch_active_subscription(user_id):
    now = _now_utc()
    today = now.date()
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor(cursor_factory=_pg.extras.RealDictCursor)
            cur.execute(
                '''SELECT * FROM subscriptions
                   WHERE user_id = %s AND status = 'active'
                     AND (
                       (pass_type IN ('one_day', 'promo') AND expires_at IS NOT NULL AND expires_at > %s)
                       OR (
                         COALESCE(pass_type, 'monthly') NOT IN ('one_day', 'promo')
                         AND start_date <= %s AND end_date >= %s
                       )
                     )
                   ORDER BY
                     CASE
                       WHEN pass_type IN ('one_day', 'promo') THEN expires_at
                       ELSE (end_date::timestamp AT TIME ZONE 'UTC')
                     END DESC
                   LIMIT 1''',
                (user_id, now, today, today),
            )
            row = cur.fetchone()
            cur.close()
            sub = _row_to_dict(row)
            return sub if _subscription_covers_now(sub, now) else None

        now_s = now.isoformat()
        today_s = today.isoformat()
        rows = conn.execute(
            '''SELECT * FROM subscriptions
               WHERE user_id = ? AND status = 'active'
                 AND (
                   (pass_type IN ('one_day', 'promo') AND expires_at IS NOT NULL AND expires_at > ?)
                   OR (
                     COALESCE(pass_type, 'monthly') NOT IN ('one_day', 'promo')
                     AND start_date <= ? AND end_date >= ?
                   )
                 )
               ORDER BY expires_at DESC, end_date DESC''',
            (user_id, now_s, today_s, today_s),
        ).fetchall()
        for row in rows:
            sub = _row_to_dict(row)
            if _subscription_covers_now(sub, now):
                return sub
        return None


def _subscription_to_api(sub):
    if not sub:
        return None
    return {
        'id': str(sub['id']),
        'planName': sub.get('plan_name') or 'standard',
        'displayPlanName': _subscription_display_plan_name(sub),
        'passType': _normalized_pass_type(sub),
        'startDate': _iso_dt(sub.get('start_date')),
        'endDate': _iso_dt(sub.get('end_date')),
        'expiresAt': _iso_dt(sub.get('expires_at')),
        'status': sub.get('status') or 'active',
        'notes': sub.get('notes') or '',
        'stripeSubscriptionId': sub.get('stripe_subscription_id') or '',
        'source': 'stripe' if (sub.get('stripe_subscription_id') or sub.get('stripe_checkout_session_id')) else 'admin',
        'cancelAtPeriodEnd': bool(sub.get('cancel_at_period_end')),
        'autoRenew': not bool(sub.get('cancel_at_period_end')),
    }


def _user_to_api(user, include_subscription=False):
    if not user:
        return None
    out = {
        'id': str(user['id']),
        'email': user['email'],
        'displayName': user.get('display_name') or '',
        'status': user['status'],
        'isAdmin': bool(user.get('is_admin')),
        'createdAt': _iso_dt(user.get('created_at')),
        'approvedAt': _iso_dt(user.get('approved_at')),
    }
    if include_subscription:
        out['activeSubscription'] = _subscription_to_api(_fetch_active_subscription(user['id']))
    return out


def _session_user_id():
    uid = session.get('user_id')
    return uid.strip() if isinstance(uid, str) and uid.strip() else None


def get_current_user():
    uid = _session_user_id()
    if not uid:
        return None
    return _fetch_user_by_id(uid)


def user_is_approved(user):
    return bool(user) and user.get('status') == 'approved'


def user_can_access_platform(user):
    """Approved users with an active subscription may use simulator platform features. Admins always may."""
    if not user_is_approved(user):
        return False
    if user.get('is_admin'):
        return True
    return _fetch_active_subscription(user['id']) is not None


def user_can_access_simulator(user):
    """Alias kept for existing callers."""
    return user_can_access_platform(user)


def _platform_access_reason(user):
    if not user:
        return 'Sign in required.'
    if user.get('status') == 'pending':
        return 'Your account is awaiting administrator approval.'
    if user.get('status') == 'rejected':
        return 'Your sign-up was not approved.'
    if user.get('status') == 'disabled':
        return 'Your account has been disabled.'
    if not user_is_approved(user):
        return 'Your account is not approved for access.'
    if user.get('is_admin'):
        return None
    if not _fetch_active_subscription(user['id']):
        return 'Subscribe to unlock the webATC platform. Your account is approved but has no active subscription.'
    return None


def set_user_stripe_customer_id(user_id, stripe_customer_id):
    now = _now_utc()
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                'UPDATE users SET stripe_customer_id = %s, updated_at = %s WHERE id = %s',
                (stripe_customer_id, now, user_id),
            )
            cur.close()
        else:
            conn.execute(
                'UPDATE users SET stripe_customer_id = ?, updated_at = ? WHERE id = ?',
                (stripe_customer_id, now.isoformat(), user_id),
            )


def fetch_user_by_stripe_customer_id(stripe_customer_id):
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor(cursor_factory=_pg.extras.RealDictCursor)
            cur.execute('SELECT * FROM users WHERE stripe_customer_id = %s', (stripe_customer_id,))
            row = cur.fetchone()
            cur.close()
            return _row_to_dict(row)

        row = conn.execute('SELECT * FROM users WHERE stripe_customer_id = ?', (stripe_customer_id,)).fetchone()
        return _row_to_dict(row)


def fetch_subscription_by_stripe_id(stripe_subscription_id):
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor(cursor_factory=_pg.extras.RealDictCursor)
            cur.execute('SELECT * FROM subscriptions WHERE stripe_subscription_id = %s', (stripe_subscription_id,))
            row = cur.fetchone()
            cur.close()
            return _row_to_dict(row)

        row = conn.execute(
            'SELECT * FROM subscriptions WHERE stripe_subscription_id = ?',
            (stripe_subscription_id,),
        ).fetchone()
        return _row_to_dict(row)


def fetch_subscription_by_checkout_session_id(stripe_checkout_session_id):
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor(cursor_factory=_pg.extras.RealDictCursor)
            cur.execute(
                'SELECT * FROM subscriptions WHERE stripe_checkout_session_id = %s',
                (stripe_checkout_session_id,),
            )
            row = cur.fetchone()
            cur.close()
            return _row_to_dict(row)

        row = conn.execute(
            'SELECT * FROM subscriptions WHERE stripe_checkout_session_id = ?',
            (stripe_checkout_session_id,),
        ).fetchone()
        return _row_to_dict(row)


def create_one_day_pass(
    user_id,
    plan_name='one-day-pass',
    notes=None,
    created_by=None,
    stripe_checkout_session_id=None,
    activated_at=None,
):
    """Grant 24 hours of platform access."""
    if stripe_checkout_session_id:
        existing = fetch_subscription_by_checkout_session_id(stripe_checkout_session_id)
        if existing:
            return existing

    start = activated_at or _now_utc()
    expires = one_day_pass_expires_at(start)
    start_date = start.date() if isinstance(start, datetime) else start
    end_date = expires.date()
    sub_id = _new_user_id()
    now = _now_utc()
    notes = notes or 'One Day Pass (24 hours of platform access)'

    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO subscriptions (
                    id, user_id, plan_name, start_date, end_date, status, notes,
                    created_at, updated_at, created_by, pass_type, expires_at,
                    stripe_checkout_session_id
                ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)''',
                (
                    sub_id, user_id, plan_name, start_date, end_date, notes,
                    now, now, created_by, PASS_TYPE_ONE_DAY, expires, stripe_checkout_session_id,
                ),
            )
            cur.close()
        else:
            conn.execute(
                '''INSERT INTO subscriptions (
                    id, user_id, plan_name, start_date, end_date, status, notes,
                    created_at, updated_at, created_by, pass_type, expires_at,
                    stripe_checkout_session_id
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)''',
                (
                    sub_id, user_id, plan_name, start_date.isoformat(), end_date.isoformat(), notes,
                    now.isoformat(), now.isoformat(), created_by, PASS_TYPE_ONE_DAY,
                    expires.isoformat(), stripe_checkout_session_id,
                ),
            )

    if stripe_checkout_session_id:
        return fetch_subscription_by_checkout_session_id(stripe_checkout_session_id)
    return _fetch_subscription_by_id(sub_id)


class PromoAccessError(Exception):
    """Raised when a promotion cannot be applied to the user's current access."""


def grant_promo_access(user_id, duration_days, promo_code_label, created_by=None):
    """Extend active promo access or create a new promo pass from a promotion."""
    monthly = _fetch_active_subscription_by_type(user_id, PASS_TYPE_MONTHLY)
    if monthly:
        raise PromoAccessError('You already have a subscription.')

    duration_days = int(duration_days)
    if duration_days < 1:
        raise PromoAccessError('Invalid promotion.')

    now = _now_utc()
    extra = timedelta(days=duration_days)
    promo = _fetch_active_subscription_by_type(user_id, PASS_TYPE_PROMO)
    notes = f'Promotion code {promo_code_label} (+{duration_days} day(s))'

    if promo:
        current_expires = _parse_dt(promo.get('expires_at')) or now
        base = max(current_expires, now)
        new_expires = base + extra
        new_end_date = new_expires.date()
        sub_id = promo['id']
        with _db_conn() as conn:
            if _USE_POSTGRES:
                cur = conn.cursor()
                cur.execute(
                    '''UPDATE subscriptions SET
                        expires_at = %s, end_date = %s, updated_at = %s,
                        notes = CASE
                            WHEN notes IS NULL OR notes = '' THEN %s
                            ELSE notes || ' | ' || %s
                        END
                       WHERE id = %s AND status = 'active' ''',
                    (new_expires, new_end_date, now, notes, notes, sub_id),
                )
                cur.close()
            else:
                existing_notes = (promo.get('notes') or '').strip()
                merged_notes = notes if not existing_notes else f'{existing_notes} | {notes}'
                conn.execute(
                    '''UPDATE subscriptions SET
                        expires_at = ?, end_date = ?, updated_at = ?, notes = ?
                       WHERE id = ? AND status = 'active' ''',
                    (new_expires.isoformat(), new_end_date.isoformat(), now.isoformat(), merged_notes, sub_id),
                )
        return _subscription_to_api(_fetch_subscription_by_id(sub_id))

    expires = now + extra
    start_date = now.date()
    end_date = expires.date()
    sub_id = _new_user_id()
    plan_name = 'promo-access'
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO subscriptions (
                    id, user_id, plan_name, start_date, end_date, status, notes,
                    created_at, updated_at, created_by, pass_type, expires_at
                ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s)''',
                (
                    sub_id, user_id, plan_name, start_date, end_date, notes,
                    now, now, created_by, PASS_TYPE_PROMO, expires,
                ),
            )
            cur.close()
        else:
            conn.execute(
                '''INSERT INTO subscriptions (
                    id, user_id, plan_name, start_date, end_date, status, notes,
                    created_at, updated_at, created_by, pass_type, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)''',
                (
                    sub_id, user_id, plan_name, start_date.isoformat(), end_date.isoformat(), notes,
                    now.isoformat(), now.isoformat(), created_by, PASS_TYPE_PROMO, expires.isoformat(),
                ),
            )
    return _subscription_to_api(_fetch_subscription_by_id(sub_id))


def _fetch_subscription_by_id(subscription_id):
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor(cursor_factory=_pg.extras.RealDictCursor)
            cur.execute('SELECT * FROM subscriptions WHERE id = %s', (subscription_id,))
            row = cur.fetchone()
            cur.close()
            return _row_to_dict(row)

        row = conn.execute('SELECT * FROM subscriptions WHERE id = ?', (subscription_id,)).fetchone()
        return _row_to_dict(row)


def upsert_stripe_subscription(user_id, stripe_subscription_id, start_date, end_date, plan_name='stripe-monthly', cancel_at_period_end=False):
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()
    now = _now_utc()
    cancel_flag = bool(cancel_at_period_end)
    existing = fetch_subscription_by_stripe_id(stripe_subscription_id)
    was_cancel_pending = bool(existing and existing.get('cancel_at_period_end'))
    with _db_conn() as conn:
        if existing:
            if _USE_POSTGRES:
                cur = conn.cursor()
                cur.execute(
                    '''UPDATE subscriptions SET
                        start_date = %s, end_date = %s, status = 'active',
                        plan_name = %s, pass_type = %s, updated_at = %s, expires_at = NULL,
                        cancel_at_period_end = %s
                       WHERE id = %s''',
                    (start_date, end_date, plan_name, PASS_TYPE_MONTHLY, now, cancel_flag, existing['id']),
                )
                cur.close()
            else:
                conn.execute(
                    '''UPDATE subscriptions SET
                        start_date = ?, end_date = ?, status = 'active',
                        plan_name = ?, pass_type = ?, updated_at = ?, expires_at = NULL,
                        cancel_at_period_end = ?
                       WHERE id = ?''',
                    (start_date.isoformat(), end_date.isoformat(), plan_name, PASS_TYPE_MONTHLY, now.isoformat(), 1 if cancel_flag else 0, existing['id']),
                )
            updated = fetch_subscription_by_stripe_id(stripe_subscription_id)
            if cancel_flag and not was_cancel_pending:
                from billing_notifications import maybe_notify_subscription_cancellation_scheduled
                maybe_notify_subscription_cancellation_scheduled(existing['user_id'], updated or existing, was_cancel_pending)
            return updated

        sub_id = _new_user_id()
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO subscriptions (
                    id, user_id, plan_name, start_date, end_date, status, notes,
                    created_at, updated_at, created_by, stripe_subscription_id, pass_type,
                    cancel_at_period_end
                ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, NULL, %s, %s, %s)''',
                (
                    sub_id, user_id, plan_name, start_date, end_date,
                    'Stripe recurring subscription (31-day billing period)',
                    now, now, stripe_subscription_id, PASS_TYPE_MONTHLY, cancel_flag,
                ),
            )
            cur.close()
        else:
            conn.execute(
                '''INSERT INTO subscriptions (
                    id, user_id, plan_name, start_date, end_date, status, notes,
                    created_at, updated_at, created_by, stripe_subscription_id, pass_type,
                    cancel_at_period_end
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL, ?, ?, ?)''',
                (
                    sub_id, user_id, plan_name, start_date.isoformat(), end_date.isoformat(),
                    'Stripe recurring subscription (31-day billing period)',
                    now.isoformat(), now.isoformat(), stripe_subscription_id, PASS_TYPE_MONTHLY,
                    1 if cancel_flag else 0,
                ),
            )
        created = fetch_subscription_by_stripe_id(stripe_subscription_id)
        from billing_notifications import notify_monthly_subscription_activated
        notify_monthly_subscription_activated(user_id, subscription=created, source='Stripe')
        return created


def set_subscription_cancel_at_period_end(stripe_subscription_id, cancel_at_period_end=True):
    existing = fetch_subscription_by_stripe_id(stripe_subscription_id)
    was_cancel_pending = bool(existing and existing.get('cancel_at_period_end'))
    now = _now_utc()
    cancel_flag = bool(cancel_at_period_end)
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''UPDATE subscriptions SET cancel_at_period_end = %s, updated_at = %s
                   WHERE stripe_subscription_id = %s AND status = 'active' ''',
                (cancel_flag, now, stripe_subscription_id),
            )
            updated = cur.rowcount
            cur.close()
        else:
            cur = conn.execute(
                '''UPDATE subscriptions SET cancel_at_period_end = ?, updated_at = ?
                   WHERE stripe_subscription_id = ? AND status = 'active' ''',
                (1 if cancel_flag else 0, now.isoformat(), stripe_subscription_id),
            )
            updated = cur.rowcount
    if updated and cancel_flag and not was_cancel_pending and existing:
        updated_row = fetch_subscription_by_stripe_id(stripe_subscription_id)
        from billing_notifications import maybe_notify_subscription_cancellation_scheduled
        maybe_notify_subscription_cancellation_scheduled(existing['user_id'], updated_row or existing, was_cancel_pending)
    return updated > 0


def cancel_subscription_by_stripe_id(stripe_subscription_id):
    now = _now_utc()
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''UPDATE subscriptions SET status = 'cancelled', updated_at = %s
                   WHERE stripe_subscription_id = %s AND status = 'active' ''',
                (now, stripe_subscription_id),
            )
            updated = cur.rowcount
            cur.close()
        else:
            cur = conn.execute(
                '''UPDATE subscriptions SET status = 'cancelled', updated_at = ?
                   WHERE stripe_subscription_id = ? AND status = 'active' ''',
                (now.isoformat(), stripe_subscription_id),
            )
            updated = cur.rowcount
    return updated > 0


def fetch_active_stripe_subscription_ids_for_user(user_id):
    today = date.today()
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''SELECT stripe_subscription_id FROM subscriptions
                   WHERE user_id = %s AND status = 'active' AND stripe_subscription_id IS NOT NULL
                     AND start_date <= %s AND end_date >= %s''',
                (user_id, today, today),
            )
            rows = [r[0] for r in cur.fetchall() if r and r[0]]
            cur.close()
            return rows

        today_s = today.isoformat()
        rows = conn.execute(
            '''SELECT stripe_subscription_id FROM subscriptions
               WHERE user_id = ? AND status = 'active' AND stripe_subscription_id IS NOT NULL
                 AND start_date <= ? AND end_date >= ?''',
            (user_id, today_s, today_s),
        ).fetchall()
        return [row['stripe_subscription_id'] for row in rows if row['stripe_subscription_id']]


def _fetch_active_subscriptions_for_user(user_id):
    """All subscriptions that currently grant access (monthly and/or one-day)."""
    subs = _fetch_user_subscriptions(user_id)
    now = _now_utc()
    return [sub for sub in subs if _subscription_covers_now(sub, now)]


def _fetch_active_subscription_by_type(user_id, pass_type):
    for sub in _fetch_active_subscriptions_for_user(user_id):
        if _normalized_pass_type(sub) == pass_type:
            return sub
    return None


def _cancel_subscription_record(sub):
    """Cancel one subscription row and linked Stripe subscription when present."""
    if not sub or sub.get('status') != 'active':
        return False
    subscription_id = sub.get('id')
    if not _revoke_subscription(subscription_id):
        return False
    stripe_sub_id = sub.get('stripe_subscription_id')
    if stripe_sub_id and not is_admin_simulated_stripe_subscription_id(stripe_sub_id):
        try:
            from stripe_billing import cancel_stripe_subscription
            cancel_stripe_subscription(stripe_sub_id)
        except Exception:
            pass
    return True


def _cancel_active_by_pass_type(user_id, pass_type):
    cancelled = 0
    for sub in list(_fetch_active_subscriptions_for_user(user_id)):
        if _normalized_pass_type(sub) != pass_type:
            continue
        if _cancel_subscription_record(sub):
            cancelled += 1
    return cancelled


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = get_current_user()
        if not user:
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Authentication required'}), 401
            return redirect(url_for('user_auth.login_page', next=request.path))
        if not user_can_access_simulator(user):
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Account not approved for simulator access'}), 403
            return redirect(url_for('user_auth.login_page', reason='not_approved'))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = get_current_user()
        if not user:
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Authentication required'}), 401
            return redirect(url_for('user_auth.login_page', next=request.path))
        if not user.get('is_admin'):
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Admin access required'}), 403
            return redirect(url_for('index'))
        return view(*args, **kwargs)

    return wrapped


PUBLIC_EXACT_PATHS = frozenset({
    '/login',
    '/signup',
    '/health',
    '/api/auth/login',
    '/api/auth/signup',
    '/api/auth/signup/start',
    '/api/auth/signup/verify',
    '/api/auth/logout',
    '/api/auth/me',
    '/api/billing/stripe/webhook',
})

BILLING_APPROVED_PATHS = frozenset({
    '/subscribe',
    '/subscribe/success',
    '/manage-subscription',
    '/api/billing/config',
    '/api/billing/status',
    '/api/billing/create-checkout-session',
    '/api/billing/customer-portal',
    '/api/billing/redeem-promo',
})

PUBLIC_PREFIXES = (
    '/static/',
    '/manual',
)


def auth_before_request():
    path = request.path or '/'
    if path in PUBLIC_EXACT_PATHS:
        return None
    for prefix in PUBLIC_PREFIXES:
        if path == prefix or path.startswith(prefix + '/') or path.startswith(prefix):
            return None

    user = get_current_user()
    if not user:
        if path.startswith('/api/'):
            return jsonify({'ok': False, 'error': 'Authentication required'}), 401
        if path in ('/',):
            return redirect(url_for('user_auth.login_page'))
        return redirect(url_for('user_auth.login_page', next=path))

    if path in ('/login', '/signup'):
        if user_is_approved(user):
            if user_can_access_platform(user):
                return redirect(url_for('index'))
            return redirect(url_for('stripe_billing.subscribe_page'))
        return None

    if path.startswith('/admin') or path.startswith('/api/admin/'):
        if user.get('is_admin'):
            return None
        if path.startswith('/api/'):
            return jsonify({'ok': False, 'error': 'Admin access required'}), 403
        return redirect(url_for('index'))

    if not user_is_approved(user):
        reason = user.get('status') or 'pending'
        if path.startswith('/api/'):
            return jsonify({'ok': False, 'error': 'Account not approved for access'}), 403
        return redirect(url_for('user_auth.login_page', reason=reason))

    if path in BILLING_APPROVED_PATHS:
        return None

    if path == '/':
        if not user_can_access_platform(user):
            return redirect(url_for('stripe_billing.subscribe_page'))
        return None

    if not user_can_access_platform(user):
        if path.startswith('/api/'):
            return jsonify({'ok': False, 'error': _platform_access_reason(user) or 'Platform access denied'}), 403
        return redirect(url_for('stripe_billing.subscribe_page'))

    return None


def get_db_connection():
    """Shared PostgreSQL / SQLite connection for app content tables."""
    return _db_conn()


def database_is_postgres():
    return _USE_POSTGRES


def get_auth_data_dir():
    return _AUTH_DATA_DIR


def init_user_auth(app, data_dir):
    global _AUTH_DATA_DIR, _APP_SECRET
    _AUTH_DATA_DIR = data_dir
    _configure_db(data_dir)

    secret = (os.environ.get('SECRET_KEY') or '').strip()
    if not secret:
        secret = 'dev-insecure-secret-change-me'
        app.logger.warning('SECRET_KEY is not set; using insecure default (set SECRET_KEY on Railway).')

    app.config['SECRET_KEY'] = secret
    _APP_SECRET = secret
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    if os.environ.get('SESSION_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes'):
        app.config['SESSION_COOKIE_SECURE'] = True
    elif os.environ.get('RAILWAY_ENVIRONMENT'):
        app.config['SESSION_COOKIE_SECURE'] = True

    init_db()
    bootstrap_admin_user()
    from promotions_store import init_promotions_store
    init_promotions_store()
    app.register_blueprint(auth_bp)


@auth_bp.route('/login')
def login_page():
    user = get_current_user()
    if user and user_is_approved(user):
        if user_can_access_platform(user):
            return redirect(url_for('index'))
        return redirect(url_for('stripe_billing.subscribe_page'))
    return render_template(
        'login.html',
        mode='login',
        reason=request.args.get('reason') or '',
        next_url=request.args.get('next') or '',
    )


@auth_bp.route('/signup')
def signup_page():
    user = get_current_user()
    if user and user_is_approved(user):
        if user_can_access_platform(user):
            return redirect(url_for('index'))
        return redirect(url_for('stripe_billing.subscribe_page'))
    return render_template(
        'login.html',
        mode='signup',
        reason=request.args.get('reason') or '',
        next_url=request.args.get('next') or '',
    )


@auth_bp.route('/api/auth/signup/start', methods=['POST'])
def api_auth_signup_start():
    _cleanup_expired_signup_verifications()
    body = request.get_json(silent=True) or {}
    email, password, display_name, error = _validate_signup_payload(body)
    if error:
        message, status = error
        return jsonify({'ok': False, 'error': message}), status

    email_config = _signup_email_config()
    if not is_email_configured(email_config):
        return jsonify({
            'ok': False,
            'error': 'Email verification is not configured yet. Please contact the administrator.',
        }), 503

    code = _generate_signup_code()
    password_hash = generate_password_hash(password)
    verification_id = None
    try:
        verification_id, expires_at = _create_signup_verification(email, password_hash, display_name, code)
        send_signup_verification_email(email_config, email, code)
    except Exception:
        if verification_id:
            _delete_signup_verification(verification_id)
        return jsonify({
            'ok': False,
            'error': 'Could not send the verification email. Check email settings or try again later.',
        }), 500

    return jsonify({
        'ok': True,
        'verificationId': verification_id,
        'email': email,
        'expiresAt': _iso_dt(expires_at),
        'expiresInSeconds': SIGNUP_CODE_TTL_MINUTES * 60,
    })


@auth_bp.route('/api/auth/signup/verify', methods=['POST'])
def api_auth_signup_verify():
    _cleanup_expired_signup_verifications()
    body = request.get_json(silent=True) or {}
    verification_id = (body.get('verificationId') or '').strip()
    code = (body.get('code') or '').strip()

    if not verification_id or not code:
        return jsonify({'ok': False, 'error': 'Verification id and code are required'}), 400
    if not re.fullmatch(r'\d{6}', code):
        return jsonify({'ok': False, 'error': 'Enter the 6-digit code from your email'}), 400

    pending = _fetch_signup_verification(verification_id)
    if not pending:
        return jsonify({'ok': False, 'error': 'Verification expired or not found. Please sign up again.'}), 404

    expires_at = pending.get('expires_at_dt')
    if not expires_at or expires_at <= _now_utc():
        _delete_signup_verification(verification_id)
        return jsonify({'ok': False, 'error': 'Verification code expired. Please sign up again.'}), 410

    attempt_count = int(pending.get('attempt_count') or 0)
    if attempt_count >= SIGNUP_MAX_VERIFY_ATTEMPTS:
        _delete_signup_verification(verification_id)
        return jsonify({'ok': False, 'error': 'Too many incorrect attempts. Please sign up again.'}), 429

    if not _verify_signup_code(verification_id, code, pending.get('code_hash')):
        _increment_signup_verification_attempts(verification_id)
        return jsonify({'ok': False, 'error': 'Incorrect verification code. Try again.'}), 400

    email = (pending.get('email') or '').strip().lower()
    if _fetch_user_by_email(email):
        _delete_signup_verification(verification_id)
        return jsonify({'ok': False, 'error': 'An account with this email already exists'}), 409

    user_id = _new_user_id()
    now = _now_utc()
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO users (
                    id, email, password_hash, display_name, status, is_admin,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'pending', FALSE, %s, %s)''',
                (
                    user_id,
                    email,
                    pending.get('password_hash'),
                    pending.get('display_name') or None,
                    now,
                    now,
                ),
            )
            cur.close()
        else:
            now_s = now.isoformat()
            conn.execute(
                '''INSERT INTO users (
                    id, email, password_hash, display_name, status, is_admin,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)''',
                (
                    user_id,
                    email,
                    pending.get('password_hash'),
                    pending.get('display_name') or None,
                    now_s,
                    now_s,
                ),
            )

    _delete_signup_verification(verification_id)
    return jsonify({
        'ok': True,
        'message': (
            'Your account has been created and is awaiting administrator approval. '
            'You will be notified once approved — please allow up to 24 hours.'
        ),
        'user': _user_to_api(_fetch_user_by_id(user_id)),
    })


@auth_bp.route('/api/auth/signup', methods=['POST'])
def api_auth_signup():
    return jsonify({
        'ok': False,
        'error': 'Email verification is required. Submit the sign-up form to receive a verification code.',
    }), 400


@auth_bp.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    body = request.get_json(silent=True) or {}
    email = (body.get('email') or '').strip().lower()
    password = body.get('password') or ''

    if not email or not password:
        return jsonify({'ok': False, 'error': 'Email and password are required'}), 400

    user = _fetch_user_by_email(email)
    if not user or not check_password_hash(user.get('password_hash') or '', password):
        return jsonify({'ok': False, 'error': 'Invalid email or password'}), 401

    if user.get('status') == 'disabled':
        return jsonify({'ok': False, 'error': 'This account has been disabled'}), 403
    if user.get('status') == 'rejected':
        return jsonify({'ok': False, 'error': 'Your sign-up was not approved. Contact an administrator.'}), 403

    session.clear()
    session['user_id'] = str(user['id'])
    session.permanent = True

    payload = {
        'ok': True,
        'user': _user_to_api(user, include_subscription=True),
    }

    if user.get('status') == 'pending':
        payload['message'] = 'Your account is awaiting administrator approval.'
        return jsonify(payload), 200

    if user.get('status') != 'approved':
        return jsonify({'ok': False, 'error': 'Account is not approved for access'}), 403

    next_url = (body.get('next') or '').strip()
    if user_can_access_platform(user):
        if next_url.startswith('/') and not next_url.startswith('//'):
            payload['redirect'] = next_url
        else:
            payload['redirect'] = url_for('index')
    else:
        payload['redirect'] = url_for('stripe_billing.subscribe_page')

    return jsonify(payload)


@auth_bp.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    session.clear()
    return jsonify({'ok': True, 'redirect': url_for('user_auth.login_page')})


@auth_bp.route('/api/auth/me', methods=['GET'])
def api_auth_me():
    user = get_current_user()
    if not user:
        return jsonify({'ok': True, 'authenticated': False})
    monthly = _fetch_active_subscription_by_type(user['id'], PASS_TYPE_MONTHLY)
    one_day = _fetch_active_subscription_by_type(user['id'], PASS_TYPE_ONE_DAY)
    promo = _fetch_active_subscription_by_type(user['id'], PASS_TYPE_PROMO)
    has_stripe_monthly = is_user_cancellable_monthly_subscription(monthly)
    has_real_stripe_monthly = bool(
        has_stripe_monthly and monthly and not is_admin_simulated_stripe_subscription(monthly)
    )
    pending_cancel = bool(monthly and monthly.get('cancel_at_period_end'))
    return jsonify({
        'ok': True,
        'authenticated': True,
        'isApproved': user_is_approved(user),
        'canAccessPlatform': user_can_access_platform(user),
        'canAccessSimulator': user_can_access_platform(user),
        'platformAccessReason': _platform_access_reason(user),
        'canCancelViaStripe': has_real_stripe_monthly,
        'canCancelSubscription': bool(has_stripe_monthly and not pending_cancel),
        'subscriptionPendingCancellation': pending_cancel,
        'activeMonthlySubscription': _subscription_to_api(monthly),
        'activeOneDayPass': _subscription_to_api(one_day),
        'activePromoAccess': _subscription_to_api(promo),
        'user': _user_to_api(user, include_subscription=True),
    })


@auth_bp.route('/admin/users')
@admin_required
def admin_users_page():
    return render_template('admin_users.html')


@auth_bp.route('/api/admin/user-accounts', methods=['GET'])
@admin_required
def api_admin_list_users():
    status_filter = (request.args.get('status') or '').strip().lower()
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor(cursor_factory=_pg.extras.RealDictCursor)
            if status_filter in ('pending', 'approved', 'rejected', 'disabled'):
                cur.execute(
                    'SELECT * FROM users WHERE status = %s ORDER BY created_at DESC',
                    (status_filter,),
                )
            else:
                cur.execute('SELECT * FROM users ORDER BY created_at DESC')
            rows = cur.fetchall()
            cur.close()
        else:
            if status_filter in ('pending', 'approved', 'rejected', 'disabled'):
                rows = conn.execute(
                    'SELECT * FROM users WHERE status = ? ORDER BY created_at DESC',
                    (status_filter,),
                ).fetchall()
            else:
                rows = conn.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()

    users_out = []
    for row in rows:
        user = _row_to_dict(row)
        entry = _user_to_api(user, include_subscription=True)
        subs = _fetch_user_subscriptions(user['id'])
        entry['subscriptions'] = [_subscription_to_api(s) for s in subs]
        monthly = _fetch_active_subscription_by_type(user['id'], PASS_TYPE_MONTHLY)
        one_day = _fetch_active_subscription_by_type(user['id'], PASS_TYPE_ONE_DAY)
        promo = _fetch_active_subscription_by_type(user['id'], PASS_TYPE_PROMO)
        entry['activeMonthlySubscription'] = _subscription_to_api(monthly)
        entry['activeOneDayPass'] = _subscription_to_api(one_day)
        entry['activePromoAccess'] = _subscription_to_api(promo)
        users_out.append(entry)

    return jsonify({'ok': True, 'users': users_out})


def _fetch_user_subscriptions(user_id):
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor(cursor_factory=_pg.extras.RealDictCursor)
            cur.execute(
                'SELECT * FROM subscriptions WHERE user_id = %s ORDER BY start_date DESC, created_at DESC',
                (user_id,),
            )
            rows = cur.fetchall()
            cur.close()
            return [_row_to_dict(r) for r in rows]

        rows = conn.execute(
            'SELECT * FROM subscriptions WHERE user_id = ? ORDER BY start_date DESC, created_at DESC',
            (user_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def _set_user_status(user_id, status, actor_id):
    now = _now_utc()
    with _db_conn() as conn:
        if status == 'approved':
            if _USE_POSTGRES:
                cur = conn.cursor()
                cur.execute(
                    '''UPDATE users SET status = 'approved', approved_at = %s, approved_by = %s,
                       rejected_at = NULL, rejected_by = NULL, updated_at = %s WHERE id = %s''',
                    (now, actor_id, now, user_id),
                )
                cur.close()
            else:
                conn.execute(
                    '''UPDATE users SET status = 'approved', approved_at = ?, approved_by = ?,
                       rejected_at = NULL, rejected_by = NULL, updated_at = ? WHERE id = ?''',
                    (now.isoformat(), actor_id, now.isoformat(), user_id),
                )
        elif status == 'rejected':
            if _USE_POSTGRES:
                cur = conn.cursor()
                cur.execute(
                    '''UPDATE users SET status = 'rejected', rejected_at = %s, rejected_by = %s, updated_at = %s
                       WHERE id = %s''',
                    (now, actor_id, now, user_id),
                )
                cur.close()
            else:
                conn.execute(
                    '''UPDATE users SET status = 'rejected', rejected_at = ?, rejected_by = ?, updated_at = ?
                       WHERE id = ?''',
                    (now.isoformat(), actor_id, now.isoformat(), user_id),
                )
        elif status == 'disabled':
            if _USE_POSTGRES:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE users SET status = 'disabled', updated_at = %s WHERE id = %s",
                    (now, user_id),
                )
                cur.close()
            else:
                conn.execute(
                    "UPDATE users SET status = 'disabled', updated_at = ? WHERE id = ?",
                    (now.isoformat(), user_id),
                )


@auth_bp.route('/api/admin/user-accounts/<user_id>/approve', methods=['POST'])
@admin_required
def api_admin_approve_user(user_id):
    actor = get_current_user()
    target = _fetch_user_by_id(user_id)
    if not target:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    _set_user_status(user_id, 'approved', actor['id'])
    return jsonify({'ok': True, 'user': _user_to_api(_fetch_user_by_id(user_id), include_subscription=True)})


@auth_bp.route('/api/admin/user-accounts/<user_id>/reject', methods=['POST'])
@admin_required
def api_admin_reject_user(user_id):
    actor = get_current_user()
    target = _fetch_user_by_id(user_id)
    if not target:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    if target.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Cannot reject an admin account'}), 400
    _set_user_status(user_id, 'rejected', actor['id'])
    return jsonify({'ok': True, 'user': _user_to_api(_fetch_user_by_id(user_id))})


@auth_bp.route('/api/admin/user-accounts/<user_id>/disable', methods=['POST'])
@admin_required
def api_admin_disable_user(user_id):
    target = _fetch_user_by_id(user_id)
    if not target:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    if target.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Cannot disable an admin account'}), 400
    _set_user_status(user_id, 'disabled', None)
    return jsonify({'ok': True, 'user': _user_to_api(_fetch_user_by_id(user_id))})


@auth_bp.route('/api/admin/user-accounts/<user_id>/subscriptions', methods=['POST'])
@admin_required
def api_admin_create_subscription(user_id):
    """Create a monthly subscription (31 calendar days). Subscription UI comes later."""
    actor = get_current_user()
    target = _fetch_user_by_id(user_id)
    if not target:
        return jsonify({'ok': False, 'error': 'User not found'}), 404

    body = request.get_json(silent=True) or {}
    plan_name = (body.get('planName') or 'standard').strip() or 'standard'
    notes = (body.get('notes') or '').strip() or None

    start_raw = (body.get('startDate') or '').strip()
    if start_raw:
        try:
            start = date.fromisoformat(start_raw)
        except ValueError:
            return jsonify({'ok': False, 'error': 'Invalid startDate (use YYYY-MM-DD)'}), 400
    else:
        start = date.today()

    end = subscription_end_date(start)
    sub_id = _new_user_id()
    now = _now_utc()
    sim_stripe_id = admin_simulated_stripe_subscription_id(sub_id)

    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO subscriptions (
                    id, user_id, plan_name, start_date, end_date, status, notes,
                    created_at, updated_at, created_by, pass_type, stripe_subscription_id
                ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s)''',
                (sub_id, user_id, plan_name, start, end, notes, now, now, actor['id'], PASS_TYPE_MONTHLY, sim_stripe_id),
            )
            cur.close()
        else:
            now_s = now.isoformat()
            conn.execute(
                '''INSERT INTO subscriptions (
                    id, user_id, plan_name, start_date, end_date, status, notes,
                    created_at, updated_at, created_by, pass_type, stripe_subscription_id
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)''',
                (sub_id, user_id, plan_name, start.isoformat(), end.isoformat(), notes, now_s, now_s, actor['id'], PASS_TYPE_MONTHLY, sim_stripe_id),
            )

    sub_row = None
    for s in _fetch_user_subscriptions(user_id):
        if str(s['id']) == sub_id:
            sub_row = s
            break

    sub_api = _subscription_to_api(sub_row)
    from billing_notifications import notify_monthly_subscription_activated
    notify_monthly_subscription_activated(user_id, subscription=sub_api, source='Administrator')

    return jsonify({'ok': True, 'subscription': sub_api})


@auth_bp.route('/api/admin/user-accounts/<user_id>/one-day-pass', methods=['POST'])
@admin_required
def api_admin_create_one_day_pass(user_id):
    """Grant 24 hours of manual platform access."""
    actor = get_current_user()
    target = _fetch_user_by_id(user_id)
    if not target:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    if target.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Admin accounts do not require a pass'}), 400

    body = request.get_json(silent=True) or {}
    plan_name = (body.get('planName') or 'admin-one-day-pass').strip() or 'admin-one-day-pass'
    sub = create_one_day_pass(user_id, plan_name=plan_name, created_by=actor['id'])
    if not sub:
        return jsonify({'ok': False, 'error': 'Could not create One Day Pass'}), 500

    from billing_notifications import notify_one_day_pass_activated
    notify_one_day_pass_activated(user_id, subscription=sub, source='Administrator')

    return jsonify({
        'ok': True,
        'subscription': _subscription_to_api(sub),
        'user': _user_to_api(_fetch_user_by_id(user_id), include_subscription=True),
    })


def _revoke_subscription(subscription_id):
    now = _now_utc()
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''UPDATE subscriptions SET status = 'cancelled', updated_at = %s
                   WHERE id = %s AND status = 'active' ''',
                (now, subscription_id),
            )
            updated = cur.rowcount
            cur.close()
        else:
            cur = conn.execute(
                '''UPDATE subscriptions SET status = 'cancelled', updated_at = ?
                   WHERE id = ? AND status = 'active' ''',
                (now.isoformat(), subscription_id),
            )
            updated = cur.rowcount
    return updated > 0


def _revoke_active_subscriptions_for_user(user_id):
    now = _now_utc()
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''UPDATE subscriptions SET status = 'cancelled', updated_at = %s
                   WHERE user_id = %s AND status = 'active' ''',
                (now, user_id),
            )
            updated = cur.rowcount
            cur.close()
        else:
            cur = conn.execute(
                '''UPDATE subscriptions SET status = 'cancelled', updated_at = ?
                   WHERE user_id = ? AND status = 'active' ''',
                (now.isoformat(), user_id),
            )
            updated = cur.rowcount
    return updated


@auth_bp.route('/api/admin/user-accounts/subscriptions/<subscription_id>/revoke', methods=['POST'])
@admin_required
def api_admin_revoke_subscription(subscription_id):
    target_sub = None
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor(cursor_factory=_pg.extras.RealDictCursor)
            cur.execute('SELECT * FROM subscriptions WHERE id = %s', (subscription_id,))
            target_sub = cur.fetchone()
            cur.close()
        else:
            row = conn.execute('SELECT * FROM subscriptions WHERE id = ?', (subscription_id,)).fetchone()
            target_sub = _row_to_dict(row)

    if not target_sub:
        return jsonify({'ok': False, 'error': 'Subscription not found'}), 404
    if target_sub.get('status') != 'active':
        return jsonify({'ok': False, 'error': 'Subscription is not active'}), 400

    if not _cancel_subscription_record(_row_to_dict(target_sub)):
        return jsonify({'ok': False, 'error': 'Subscription could not be cancelled'}), 400

    user_id = str(target_sub['user_id'])
    return jsonify({
        'ok': True,
        'subscription': _subscription_to_api({**_row_to_dict(target_sub), 'status': 'cancelled'}),
        'user': _user_to_api(_fetch_user_by_id(user_id), include_subscription=True),
    })


@auth_bp.route('/api/admin/user-accounts/<user_id>/cancel-subscription', methods=['POST'])
@admin_required
def api_admin_cancel_subscription(user_id):
    """Cancel the user's active monthly subscription (admin or Stripe)."""
    target = _fetch_user_by_id(user_id)
    if not target:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    if target.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Admin accounts do not have subscriptions'}), 400

    count = _cancel_active_by_pass_type(user_id, PASS_TYPE_MONTHLY)
    if count <= 0:
        return jsonify({'ok': False, 'error': 'No active monthly subscription to cancel'}), 400

    return jsonify({
        'ok': True,
        'cancelledCount': count,
        'user': _user_to_api(_fetch_user_by_id(user_id), include_subscription=True),
    })


@auth_bp.route('/api/admin/user-accounts/<user_id>/cancel-one-day-pass', methods=['POST'])
@admin_required
def api_admin_cancel_one_day_pass(user_id):
    """Cancel the user's active One Day Pass."""
    target = _fetch_user_by_id(user_id)
    if not target:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    if target.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Admin accounts do not have passes'}), 400

    count = _cancel_active_by_pass_type(user_id, PASS_TYPE_ONE_DAY)
    if count <= 0:
        return jsonify({'ok': False, 'error': 'No active One Day Pass to cancel'}), 400

    return jsonify({
        'ok': True,
        'cancelledCount': count,
        'user': _user_to_api(_fetch_user_by_id(user_id), include_subscription=True),
    })


@auth_bp.route('/api/admin/user-accounts/<user_id>/cancel-promo-access', methods=['POST'])
@admin_required
def api_admin_cancel_promo_access(user_id):
    """Cancel the user's active promo access."""
    target = _fetch_user_by_id(user_id)
    if not target:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    if target.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Admin accounts do not have promo access'}), 400

    count = _cancel_active_by_pass_type(user_id, PASS_TYPE_PROMO)
    if count <= 0:
        return jsonify({'ok': False, 'error': 'No active promo access to cancel'}), 400

    return jsonify({
        'ok': True,
        'cancelledCount': count,
        'user': _user_to_api(_fetch_user_by_id(user_id), include_subscription=True),
    })


@auth_bp.route('/api/admin/user-accounts/<user_id>/subscriptions/revoke-active', methods=['POST'])
@admin_required
def api_admin_revoke_active_subscriptions(user_id):
    target = _fetch_user_by_id(user_id)
    if not target:
        return jsonify({'ok': False, 'error': 'User not found'}), 404

    active = _fetch_active_subscriptions_for_user(user_id)
    if not active:
        return jsonify({'ok': False, 'error': 'No active subscription or pass to cancel'}), 400

    cancelled = 0
    for sub in active:
        if _cancel_subscription_record(sub):
            cancelled += 1
    if cancelled <= 0:
        return jsonify({'ok': False, 'error': 'Could not cancel active access'}), 400

    return jsonify({
        'ok': True,
        'cancelledCount': cancelled,
        'user': _user_to_api(_fetch_user_by_id(user_id), include_subscription=True),
    })


@auth_bp.route('/admin/promotions')
@admin_required
def admin_promotions_page():
    return render_template('admin_promotions.html')


@auth_bp.route('/api/admin/promotions', methods=['GET'])
@admin_required
def api_admin_list_promotions():
    from promotions_store import list_promotion_codes, promotions_storage_mode
    return jsonify({
        'ok': True,
        'promotions': list_promotion_codes(),
        'storage': promotions_storage_mode(),
    })


@auth_bp.route('/api/admin/promotions', methods=['POST'])
@admin_required
def api_admin_create_promotion():
    from promotions_store import create_promotion_code

    body = request.get_json(silent=True) or {}
    try:
        duration_days = int(body.get('durationDays') or body.get('duration_days') or 0)
        max_uses = int(body.get('maxUses') or body.get('max_uses') or 0)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'durationDays and maxUses must be numbers'}), 400

    actor = get_current_user()
    try:
        promotion = create_promotion_code(
            duration_days,
            max_uses,
            created_by=actor['id'] if actor else None,
        )
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc) or 'Could not generate promotion code.'}), 500

    return jsonify({'ok': True, 'promotion': promotion})


@auth_bp.route('/api/admin/promotions/<promotion_id>/stop', methods=['POST'])
@admin_required
def api_admin_stop_promotion(promotion_id):
    from promotions_store import set_promotion_active
    promotion = set_promotion_active(promotion_id, False)
    if not promotion:
        return jsonify({'ok': False, 'error': 'Promotion code not found'}), 404
    return jsonify({'ok': True, 'promotion': promotion})


@auth_bp.route('/api/admin/promotions/<promotion_id>/resume', methods=['POST'])
@admin_required
def api_admin_resume_promotion(promotion_id):
    from promotions_store import set_promotion_active
    promotion = set_promotion_active(promotion_id, True)
    if not promotion:
        return jsonify({'ok': False, 'error': 'Promotion code not found'}), 404
    return jsonify({'ok': True, 'promotion': promotion})


@auth_bp.route('/api/admin/promotions/<promotion_id>', methods=['DELETE'])
@admin_required
def api_admin_delete_promotion(promotion_id):
    from promotions_store import delete_promotion_code
    if not delete_promotion_code(promotion_id):
        return jsonify({'ok': False, 'error': 'Promotion code not found'}), 404
    return jsonify({'ok': True})
