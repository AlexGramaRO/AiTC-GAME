"""
Promotion codes: admin-generated codes that grant time-limited platform access.
"""

import logging
import re
import secrets
import string
from datetime import datetime, timedelta, timezone

from user_auth import database_is_postgres, get_db_connection

logger = logging.getLogger(__name__)

PROMO_CODE_LENGTH = 10
PROMO_CODE_ALPHABET = string.ascii_uppercase + string.digits
PROMO_CODE_PATTERN = re.compile(r'^[A-Z0-9]{10}$')

_INITIALIZED = False


class PromotionError(Exception):
    """User-visible promotion redemption error."""


def _now_utc():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now_utc().strftime('%Y-%m-%dT%H:%M:%SZ')


def _row_to_dict(row):
    if row is None:
        return None
    if hasattr(row, 'keys'):
        return dict(row)
    return row


def init_promotions_store():
    global _INITIALIZED
    _init_promotion_tables()
    _INITIALIZED = True


def _init_promotion_tables():
    if database_is_postgres():
        ddl = [
            '''
            CREATE TABLE IF NOT EXISTS promotion_codes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code TEXT NOT NULL UNIQUE,
                duration_days INTEGER NOT NULL CHECK (duration_days > 0),
                max_uses INTEGER NOT NULL CHECK (max_uses > 0),
                use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_by UUID REFERENCES users(id) ON DELETE SET NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS promotion_redemptions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                promotion_id UUID NOT NULL REFERENCES promotion_codes(id) ON DELETE CASCADE,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                redeemed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (promotion_id, user_id)
            )
            ''',
            'CREATE INDEX IF NOT EXISTS idx_promotion_codes_code ON promotion_codes (code)',
            'CREATE INDEX IF NOT EXISTS idx_promotion_redemptions_user_id ON promotion_redemptions (user_id)',
        ]
    else:
        ddl = [
            '''
            CREATE TABLE IF NOT EXISTS promotion_codes (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                duration_days INTEGER NOT NULL CHECK (duration_days > 0),
                max_uses INTEGER NOT NULL CHECK (max_uses > 0),
                use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
                created_at TEXT NOT NULL,
                created_by TEXT
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS promotion_redemptions (
                id TEXT PRIMARY KEY,
                promotion_id TEXT NOT NULL REFERENCES promotion_codes(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                redeemed_at TEXT NOT NULL,
                UNIQUE (promotion_id, user_id)
            )
            ''',
            'CREATE INDEX IF NOT EXISTS idx_promotion_codes_code ON promotion_codes (code)',
            'CREATE INDEX IF NOT EXISTS idx_promotion_redemptions_user_id ON promotion_redemptions (user_id)',
        ]

    with get_db_connection() as conn:
        cur = conn.cursor()
        for stmt in ddl:
            cur.execute(stmt)
        cur.close()


def normalize_promo_code(raw):
    code = (raw or '').strip().upper()
    code = re.sub(r'[^A-Z0-9]', '', code)
    return code


def generate_promo_code():
    return ''.join(secrets.choice(PROMO_CODE_ALPHABET) for _ in range(PROMO_CODE_LENGTH))


def _promotion_to_api(row):
    row = _row_to_dict(row)
    if not row:
        return None
    max_uses = int(row.get('max_uses') or 0)
    use_count = int(row.get('use_count') or 0)
    return {
        'id': str(row.get('id') or ''),
        'code': row.get('code') or '',
        'durationDays': int(row.get('duration_days') or 0),
        'maxUses': max_uses,
        'useCount': use_count,
        'usesRemaining': max(0, max_uses - use_count),
        'createdAt': row.get('created_at'),
        'createdBy': str(row.get('created_by') or '') if row.get('created_by') else '',
    }


def list_promotion_codes():
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT id, code, duration_days, max_uses, use_count, created_at, created_by
            FROM promotion_codes
            ORDER BY created_at DESC
            '''
        )
        rows = cur.fetchall()
        cur.close()
    return [_promotion_to_api(row) for row in rows if row]


def create_promotion_code(duration_days, max_uses, created_by=None):
    duration_days = int(duration_days)
    max_uses = int(max_uses)
    if duration_days < 1:
        raise ValueError('duration_days must be at least 1')
    if max_uses < 1:
        raise ValueError('max_uses must be at least 1')

    from user_auth import _new_user_id

    for _ in range(32):
        code = generate_promo_code()
        promo_id = _new_user_id()
        now = _now_iso()
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                if database_is_postgres():
                    cur.execute(
                        '''
                        INSERT INTO promotion_codes (
                            id, code, duration_days, max_uses, use_count, created_at, created_by
                        ) VALUES (%s, %s, %s, %s, 0, NOW(), %s)
                        ''',
                        (promo_id, code, duration_days, max_uses, created_by),
                    )
                else:
                    cur.execute(
                        '''
                        INSERT INTO promotion_codes (
                            id, code, duration_days, max_uses, use_count, created_at, created_by
                        ) VALUES (?, ?, ?, ?, 0, ?, ?)
                        ''',
                        (promo_id, code, duration_days, max_uses, now, created_by),
                    )
                cur.close()
            return fetch_promotion_by_id(promo_id)
        except Exception as exc:
            message = str(exc).lower()
            if 'unique' in message or 'duplicate' in message:
                continue
            raise
    raise RuntimeError('Could not generate a unique promotion code')


def fetch_promotion_by_id(promotion_id):
    with get_db_connection() as conn:
        cur = conn.cursor()
        if database_is_postgres():
            cur.execute(
                '''
                SELECT id, code, duration_days, max_uses, use_count, created_at, created_by
                FROM promotion_codes WHERE id = %s
                ''',
                (promotion_id,),
            )
        else:
            cur.execute(
                '''
                SELECT id, code, duration_days, max_uses, use_count, created_at, created_by
                FROM promotion_codes WHERE id = ?
                ''',
                (promotion_id,),
            )
        row = cur.fetchone()
        cur.close()
    return _promotion_to_api(row)


def fetch_promotion_by_code(code):
    code = normalize_promo_code(code)
    if not code:
        return None
    with get_db_connection() as conn:
        cur = conn.cursor()
        if database_is_postgres():
            cur.execute(
                '''
                SELECT id, code, duration_days, max_uses, use_count, created_at, created_by
                FROM promotion_codes WHERE code = %s
                ''',
                (code,),
            )
        else:
            cur.execute(
                '''
                SELECT id, code, duration_days, max_uses, use_count, created_at, created_by
                FROM promotion_codes WHERE code = ? COLLATE NOCASE
                ''',
                (code,),
            )
        row = cur.fetchone()
        cur.close()
    return _promotion_to_api(row)


def redeem_promotion_code(user_id, raw_code):
    from user_auth import PromoAccessError, grant_promo_access

    code = normalize_promo_code(raw_code)
    if not PROMO_CODE_PATTERN.fullmatch(code):
        raise PromotionError('Enter a valid 10-character promotion code.')

    promo = fetch_promotion_by_code(code)
    if not promo:
        raise PromotionError('Promotion code not found.')

    promo_id = promo['id']
    use_count = int(promo.get('useCount') or 0)
    max_uses = int(promo.get('maxUses') or 0)
    duration_days = int(promo.get('durationDays') or 0)

    if use_count >= max_uses:
        raise PromotionError('This promotion code has reached its usage limit.')

    with get_db_connection() as conn:
        cur = conn.cursor()
        if database_is_postgres():
            cur.execute(
                'SELECT id FROM promotion_redemptions WHERE promotion_id = %s AND user_id = %s',
                (promo_id, user_id),
            )
        else:
            cur.execute(
                'SELECT id FROM promotion_redemptions WHERE promotion_id = ? AND user_id = ?',
                (promo_id, user_id),
            )
        if cur.fetchone():
            cur.close()
            raise PromotionError('You have already used this promotion code.')
        cur.close()

    try:
        subscription = grant_promo_access(user_id, duration_days, promo.get('code') or code)
    except PromoAccessError as exc:
        raise PromotionError(str(exc)) from exc

    from user_auth import _new_user_id

    redemption_id = _new_user_id()
    now = _now_iso()
    with get_db_connection() as conn:
        cur = conn.cursor()
        if database_is_postgres():
            cur.execute(
                'SELECT use_count, max_uses FROM promotion_codes WHERE id = %s FOR UPDATE',
                (promo_id,),
            )
        else:
            cur.execute(
                'SELECT use_count, max_uses FROM promotion_codes WHERE id = ?',
                (promo_id,),
            )
        row = _row_to_dict(cur.fetchone())
        if not row or int(row.get('use_count') or 0) >= int(row.get('max_uses') or 0):
            cur.close()
            raise PromotionError('This promotion code has reached its usage limit.')
        if database_is_postgres():
            cur.execute(
                '''
                INSERT INTO promotion_redemptions (id, promotion_id, user_id, redeemed_at)
                VALUES (%s, %s, %s, NOW())
                ''',
                (redemption_id, promo_id, user_id),
            )
            cur.execute(
                'UPDATE promotion_codes SET use_count = use_count + 1 WHERE id = %s',
                (promo_id,),
            )
        else:
            cur.execute(
                '''
                INSERT INTO promotion_redemptions (id, promotion_id, user_id, redeemed_at)
                VALUES (?, ?, ?, ?)
                ''',
                (redemption_id, promo_id, user_id, now),
            )
            cur.execute(
                'UPDATE promotion_codes SET use_count = use_count + 1 WHERE id = ?',
                (promo_id,),
            )
        cur.close()

    return {
        'promotion': fetch_promotion_by_id(promo_id),
        'subscription': subscription,
    }
