"""
User accounts, admin approval, and subscriptions for AiTC (PostgreSQL on Railway).

Local dev without DATABASE_URL falls back to SQLite at data/users.sqlite3.
"""

import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

auth_bp = Blueprint('user_auth', __name__)

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_SUBSCRIPTION_MONTH_DAYS = 31

_USE_POSTGRES = False
_DB_PATH = None
_pg = None


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
    today = date.today()
    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor(cursor_factory=_pg.extras.RealDictCursor)
            cur.execute(
                '''SELECT * FROM subscriptions
                   WHERE user_id = %s AND status = 'active'
                     AND start_date <= %s AND end_date >= %s
                   ORDER BY end_date DESC
                   LIMIT 1''',
                (user_id, today, today),
            )
            row = cur.fetchone()
            cur.close()
            return _row_to_dict(row)

        today_s = today.isoformat()
        row = conn.execute(
            '''SELECT * FROM subscriptions
               WHERE user_id = ? AND status = 'active'
                 AND start_date <= ? AND end_date >= ?
               ORDER BY end_date DESC
               LIMIT 1''',
            (user_id, today_s, today_s),
        ).fetchone()
        return _row_to_dict(row)


def _subscription_to_api(sub):
    if not sub:
        return None
    return {
        'id': str(sub['id']),
        'planName': sub.get('plan_name') or 'standard',
        'startDate': _iso_dt(sub.get('start_date')),
        'endDate': _iso_dt(sub.get('end_date')),
        'status': sub.get('status') or 'active',
        'notes': sub.get('notes') or '',
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


def user_can_access_simulator(user):
    if not user:
        return False
    if user.get('status') != 'approved':
        return False
    if user.get('is_admin'):
        return True
    # Subscription enforcement can be tightened later; for now approved users may access.
    return True


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
    '/api/auth/logout',
    '/api/auth/me',
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
        if user_can_access_simulator(user):
            return redirect(url_for('index'))
        return None

    if path.startswith('/admin') or path.startswith('/api/admin/user-accounts'):
        if user.get('is_admin'):
            return None
        if path.startswith('/api/'):
            return jsonify({'ok': False, 'error': 'Admin access required'}), 403
        return redirect(url_for('index'))

    if not user_can_access_simulator(user):
        if path.startswith('/api/'):
            return jsonify({'ok': False, 'error': 'Account pending admin approval'}), 403
        return redirect(url_for('user_auth.login_page', reason='pending'))

    return None


def init_user_auth(app, data_dir):
    _configure_db(data_dir)

    secret = (os.environ.get('SECRET_KEY') or '').strip()
    if not secret:
        secret = 'dev-insecure-secret-change-me'
        app.logger.warning('SECRET_KEY is not set; using insecure default (set SECRET_KEY on Railway).')

    app.config['SECRET_KEY'] = secret
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    if os.environ.get('SESSION_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes'):
        app.config['SESSION_COOKIE_SECURE'] = True
    elif os.environ.get('RAILWAY_ENVIRONMENT'):
        app.config['SESSION_COOKIE_SECURE'] = True

    init_db()
    bootstrap_admin_user()
    app.register_blueprint(auth_bp)


@auth_bp.route('/login')
def login_page():
    user = get_current_user()
    if user and user_can_access_simulator(user):
        return redirect(url_for('index'))
    return render_template(
        'login.html',
        mode='login',
        reason=request.args.get('reason') or '',
        next_url=request.args.get('next') or '',
    )


@auth_bp.route('/signup')
def signup_page():
    user = get_current_user()
    if user and user_can_access_simulator(user):
        return redirect(url_for('index'))
    return render_template(
        'login.html',
        mode='signup',
        reason=request.args.get('reason') or '',
        next_url=request.args.get('next') or '',
    )


@auth_bp.route('/api/auth/signup', methods=['POST'])
def api_auth_signup():
    body = request.get_json(silent=True) or {}
    email = (body.get('email') or '').strip().lower()
    password = body.get('password') or ''
    display_name = (body.get('displayName') or '').strip()

    if not email or not _EMAIL_RE.match(email):
        return jsonify({'ok': False, 'error': 'Enter a valid email address'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400
    if _fetch_user_by_email(email):
        return jsonify({'ok': False, 'error': 'An account with this email already exists'}), 409

    user_id = _new_user_id()
    now = _now_utc()
    password_hash = generate_password_hash(password)

    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO users (
                    id, email, password_hash, display_name, status, is_admin,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'pending', FALSE, %s, %s)''',
                (user_id, email, password_hash, display_name or None, now, now),
            )
            cur.close()
        else:
            now_s = now.isoformat()
            conn.execute(
                '''INSERT INTO users (
                    id, email, password_hash, display_name, status, is_admin,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)''',
                (user_id, email, password_hash, display_name or None, now_s, now_s),
            )

    return jsonify({
        'ok': True,
        'message': 'Sign-up submitted. An administrator must approve your account before you can use the simulator.',
        'user': _user_to_api(_fetch_user_by_id(user_id)),
    })


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
    if next_url.startswith('/') and not next_url.startswith('//'):
        payload['redirect'] = next_url
    else:
        payload['redirect'] = url_for('index')

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
    return jsonify({
        'ok': True,
        'authenticated': True,
        'canAccessSimulator': user_can_access_simulator(user),
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

    with _db_conn() as conn:
        if _USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO subscriptions (
                    id, user_id, plan_name, start_date, end_date, status, notes,
                    created_at, updated_at, created_by
                ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, %s)''',
                (sub_id, user_id, plan_name, start, end, notes, now, now, actor['id']),
            )
            cur.close()
        else:
            now_s = now.isoformat()
            conn.execute(
                '''INSERT INTO subscriptions (
                    id, user_id, plan_name, start_date, end_date, status, notes,
                    created_at, updated_at, created_by
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)''',
                (sub_id, user_id, plan_name, start.isoformat(), end.isoformat(), notes, now_s, now_s, actor['id']),
            )

    sub_row = None
    for s in _fetch_user_subscriptions(user_id):
        if str(s['id']) == sub_id:
            sub_row = s
            break

    return jsonify({'ok': True, 'subscription': _subscription_to_api(sub_row)})
