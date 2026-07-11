"""
Persistent storage for airspaces, exercises, and exercise categories.

Uses the same PostgreSQL / SQLite database as user_auth. Bundled JSON files under
data/ are imported on startup only for records that do not already exist in the DB
(so Railway redeploys retain live data and pick up new bundled defaults safely).
"""

import json
import logging
import os
from datetime import datetime, timezone

from user_auth import database_is_postgres, get_db_connection

logger = logging.getLogger(__name__)

_CONTENT_INITIALIZED = False


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
    return _json_dumps(document)


def _json_dumps(document):
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


def init_content_store(data_dir):
    """Create content tables and import bundled JSON rows that are not yet stored."""
    global _CONTENT_INITIALIZED
    _init_content_tables()
    inserted = _seed_from_json_files(data_dir)
    _CONTENT_INITIALIZED = True
    if inserted:
        logger.info('Content store seeded %s record(s) from bundled JSON files', inserted)


def _init_content_tables():
    if database_is_postgres():
        ddl = [
            '''
            CREATE TABLE IF NOT EXISTS airspaces (
                id TEXT PRIMARY KEY,
                document JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS exercises (
                id TEXT PRIMARY KEY,
                document JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS exercise_categories (
                id TEXT PRIMARY KEY,
                document JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            ''',
            'CREATE INDEX IF NOT EXISTS idx_exercises_document_sector_id ON exercises ((document->>\'sectorId\'))',
        ]
    else:
        ddl = [
            '''
            CREATE TABLE IF NOT EXISTS airspaces (
                id TEXT PRIMARY KEY,
                document TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS exercises (
                id TEXT PRIMARY KEY,
                document TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS exercise_categories (
                id TEXT PRIMARY KEY,
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


def _seed_from_json_files(data_dir):
    sectors_path = os.path.join(data_dir, 'sectors.json')
    exercises_path = os.path.join(data_dir, 'exercises.json')
    categories_path = os.path.join(data_dir, 'exercise_categories.json')

    inserted = 0
    sectors = _read_json_file(sectors_path, [])
    if isinstance(sectors, list):
        inserted += _insert_documents_if_absent('airspaces', sectors)

    exercises = _read_json_file(exercises_path, [])
    if isinstance(exercises, list):
        inserted += _insert_documents_if_absent('exercises', exercises)

    categories = _read_json_file(categories_path, [])
    if isinstance(categories, list):
        inserted += _insert_documents_if_absent('exercise_categories', categories)

    return inserted


def _insert_documents_if_absent(table_name, documents):
    inserted = 0
    now = _now_iso()
    with get_db_connection() as conn:
        cur = conn.cursor()
        for document in documents:
            if not isinstance(document, dict):
                continue
            doc_id = str(document.get('id') or '').strip()
            if not doc_id:
                continue
            clean = dict(document)
            clean.pop('_summaryOnly', None)
            if database_is_postgres():
                cur.execute(
                    f'''
                    INSERT INTO {table_name} (id, document, created_at, updated_at)
                    VALUES (%s, %s, NOW(), NOW())
                    ON CONFLICT (id) DO NOTHING
                    ''',
                    (doc_id, _document_param(clean)),
                )
            else:
                cur.execute(
                    f'''
                    INSERT OR IGNORE INTO {table_name} (id, document, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ''',
                    (doc_id, _json_dumps(clean), now, now),
                )
            if cur.rowcount > 0:
                inserted += 1
        cur.close()
    return inserted


def _fetch_all_documents(table_name):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f'SELECT id, document FROM {table_name} ORDER BY id'
        )
        rows = cur.fetchall()
        cur.close()
    documents = []
    for row in rows:
        doc = _row_document(row)
        if isinstance(doc, dict):
            documents.append(doc)
    return documents


def _fetch_document(table_name, record_id):
    record_id = str(record_id)
    with get_db_connection() as conn:
        cur = conn.cursor()
        if database_is_postgres():
            cur.execute(
                f'SELECT id, document FROM {table_name} WHERE id = %s',
                (record_id,),
            )
        else:
            cur.execute(
                f'SELECT id, document FROM {table_name} WHERE id = ?',
                (record_id,),
            )
        row = cur.fetchone()
        cur.close()
    return _row_document(row)


def _replace_all_documents(table_name, documents):
    incoming = []
    incoming_ids = []
    seen_ids = set()
    for document in documents if isinstance(documents, list) else []:
        if not isinstance(document, dict):
            continue
        doc_id = str(document.get('id') or '').strip()
        if not doc_id or doc_id in seen_ids:
            continue
        clean = dict(document)
        clean.pop('_summaryOnly', None)
        incoming.append((doc_id, clean))
        incoming_ids.append(doc_id)
        seen_ids.add(doc_id)

    now = _now_iso()
    with get_db_connection() as conn:
        cur = conn.cursor()
        for doc_id, clean in incoming:
            if database_is_postgres():
                cur.execute(
                    f'''
                    INSERT INTO {table_name} (id, document, created_at, updated_at)
                    VALUES (%s, %s, NOW(), NOW())
                    ON CONFLICT (id) DO UPDATE
                    SET document = EXCLUDED.document,
                        updated_at = NOW()
                    ''',
                    (doc_id, _document_param(clean)),
                )
            else:
                cur.execute(
                    f'SELECT id FROM {table_name} WHERE id = ?',
                    (doc_id,),
                )
                exists = cur.fetchone() is not None
                if exists:
                    cur.execute(
                        f'''
                        UPDATE {table_name}
                        SET document = ?, updated_at = ?
                        WHERE id = ?
                        ''',
                        (_json_dumps(clean), now, doc_id),
                    )
                else:
                    cur.execute(
                        f'''
                        INSERT INTO {table_name} (id, document, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        ''',
                        (doc_id, _json_dumps(clean), now, now),
                    )

        if incoming_ids:
            placeholders = ','.join(['%s'] * len(incoming_ids)) if database_is_postgres() else ','.join(['?'] * len(incoming_ids))
            cur.execute(
                f'DELETE FROM {table_name} WHERE id NOT IN ({placeholders})',
                tuple(incoming_ids),
            )
        else:
            cur.execute(f'DELETE FROM {table_name}')
        cur.close()


def get_all_sectors():
    return _fetch_all_documents('airspaces')


def get_sector(sector_id):
    return _fetch_document('airspaces', sector_id)


def replace_all_sectors(sectors):
    _replace_all_documents('airspaces', sectors)


def get_all_exercises():
    return _fetch_all_documents('exercises')


def get_exercise(exercise_id):
    return _fetch_document('exercises', exercise_id)


def replace_all_exercises(exercises):
    _replace_all_documents('exercises', exercises)


def get_all_exercise_categories():
    return _fetch_all_documents('exercise_categories')


def replace_all_exercise_categories(categories):
    _replace_all_documents('exercise_categories', categories)
