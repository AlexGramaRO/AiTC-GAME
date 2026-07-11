"""
Persistent platform settings (admin password, email, labels, etc.).

Stored in the same PostgreSQL / SQLite database as users and content so Railway
redeploys retain configuration. Legacy data/admin_settings.json is imported once
when the database row does not exist yet.
"""

import json
import logging
import os
from datetime import datetime, timezone

from user_auth import database_is_postgres, get_db_connection

logger = logging.getLogger(__name__)

ADMIN_SETTINGS_KEY = 'admin'
_INITIALIZED = False


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _read_json_file(path, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return default


def _document_param(document):
    if database_is_postgres():
        from psycopg2.extras import Json
        return Json(document)
    return json.dumps(document, ensure_ascii=False, separators=(',', ':'))


def _row_document(row):
    if row is None:
        return None
    raw = row['document'] if hasattr(row, 'keys') else row[1]
    if isinstance(raw, (dict, list)):
        return raw
    if raw is None:
        return None
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def init_platform_settings(data_dir):
    """Create settings table, migrate legacy file once, warn if Railway has no Postgres."""
    global _INITIALIZED
    _init_platform_settings_table()
    migrated = _migrate_admin_settings_from_file(data_dir)
    if migrated:
        logger.info('Migrated admin settings from admin_settings.json into the database')
    _log_persistence_mode()
    _INITIALIZED = True


def _log_persistence_mode():
    if database_is_postgres():
        logger.info('Persistent storage: PostgreSQL (content and admin settings survive redeploys)')
        return
    message = (
        'Persistent storage: local SQLite/file fallback. '
        'Custom airspaces, exercises, categories, and admin email settings will be LOST on redeploy.'
    )
    if os.environ.get('RAILWAY_ENVIRONMENT'):
        logger.error(
            '%s Link a Railway Postgres database to this service via DATABASE_URL.',
            message,
        )
    else:
        logger.warning(message)


def _init_platform_settings_table():
    if database_is_postgres():
        ddl = [
            '''
            CREATE TABLE IF NOT EXISTS platform_settings (
                setting_key TEXT PRIMARY KEY,
                document JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            ''',
        ]
    else:
        ddl = [
            '''
            CREATE TABLE IF NOT EXISTS platform_settings (
                setting_key TEXT PRIMARY KEY,
                document TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            ''',
        ]

    with get_db_connection() as conn:
        cur = conn.cursor()
        for stmt in ddl:
            cur.execute(stmt)
        cur.close()


def _migrate_admin_settings_from_file(data_dir):
    existing = load_platform_setting(ADMIN_SETTINGS_KEY)
    if isinstance(existing, dict) and existing:
        return False

    path = os.path.join(data_dir or 'data', 'admin_settings.json')
    legacy = _read_json_file(path, None)
    if not isinstance(legacy, dict) or not legacy:
        return False

    save_platform_setting(ADMIN_SETTINGS_KEY, legacy)
    return True


def load_platform_setting(setting_key):
    setting_key = str(setting_key or '').strip()
    if not setting_key:
        return None

    with get_db_connection() as conn:
        cur = conn.cursor()
        if database_is_postgres():
            cur.execute(
                'SELECT setting_key, document FROM platform_settings WHERE setting_key = %s',
                (setting_key,),
            )
        else:
            cur.execute(
                'SELECT setting_key, document FROM platform_settings WHERE setting_key = ?',
                (setting_key,),
            )
        row = cur.fetchone()
        cur.close()

    doc = _row_document(row)
    return doc if isinstance(doc, dict) else None


def save_platform_setting(setting_key, document):
    setting_key = str(setting_key or '').strip()
    if not setting_key:
        raise ValueError('setting_key is required')
    if not isinstance(document, dict):
        raise ValueError('document must be a dict')

    now = _now_iso()
    with get_db_connection() as conn:
        cur = conn.cursor()
        if database_is_postgres():
            cur.execute(
                '''
                INSERT INTO platform_settings (setting_key, document, created_at, updated_at)
                VALUES (%s, %s, NOW(), NOW())
                ON CONFLICT (setting_key) DO UPDATE
                SET document = EXCLUDED.document,
                    updated_at = NOW()
                ''',
                (setting_key, _document_param(document)),
            )
        else:
            cur.execute(
                'SELECT setting_key FROM platform_settings WHERE setting_key = ?',
                (setting_key,),
            )
            exists = cur.fetchone() is not None
            if exists:
                cur.execute(
                    '''
                    UPDATE platform_settings
                    SET document = ?, updated_at = ?
                    WHERE setting_key = ?
                    ''',
                    (_document_param(document), now, setting_key),
                )
            else:
                cur.execute(
                    '''
                    INSERT INTO platform_settings (setting_key, document, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ''',
                    (setting_key, _document_param(document), now, now),
                )
        cur.close()


def load_admin_settings_document(data_dir=None):
    """Load raw admin settings dict from DB, with one-time legacy file fallback."""
    doc = load_platform_setting(ADMIN_SETTINGS_KEY)
    if isinstance(doc, dict):
        return doc

    if data_dir:
        path = os.path.join(data_dir, 'admin_settings.json')
        legacy = _read_json_file(path, None)
        if isinstance(legacy, dict) and legacy:
            save_platform_setting(ADMIN_SETTINGS_KEY, legacy)
            return legacy
    return {}
