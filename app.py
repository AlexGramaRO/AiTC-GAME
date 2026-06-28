#!/usr/bin/env python3
"""
Air Traffic Control Simulator - Web Application
Main Flask server file
"""

import concurrent.futures
import copy
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import Flask, render_template, request, jsonify, send_from_directory, Response
from werkzeug.security import check_password_hash, generate_password_hash

from user_auth import auth_before_request, init_user_auth
from stripe_billing import init_stripe_billing

app = Flask(__name__)

# User manual (static HTML + CSS under /Manual)
_MANUAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Manual')

# Satellite tile source for the Edit Airspace "Google Earth background" feature. Tiles are fetched
# server-side and re-served from this origin so the browser can composite them onto a canvas without
# cross-origin taint (the background warp reads pixel data via getImageData). Override with env var.
# {s} subdomain, {x}/{y} XYZ tile coords, {z} zoom (Web Mercator). Default = Google satellite tiles.
SATELLITE_TILE_URL_TEMPLATE = os.environ.get(
    'SATELLITE_TILE_URL',
    'https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}'
)
SATELLITE_TILE_SUBDOMAINS = [s for s in os.environ.get('SATELLITE_TILE_SUBDOMAINS', '0,1,2,3').split(',') if s != '']

# In-memory active simulation sessions (hosted). Key: session_id, Value: session dict with state.
_sessions = {}

# If the host stops sending PUT /state (crash, lost network), drop the session after this idle time.
# Host tab close also triggers DELETE from the client (keepalive); this is a backup.
HOST_SESSION_IDLE_TTL_SEC = 120.0

# PP vertical merge disabled (speed/ALV/Mode-S come from host state + pp-lateral commands).
_pp_vertical_by_session = {}
_pp_vertical_lock = threading.RLock()
PP_VERTICAL_KEYS = frozenset()

# PP → host only (not merged on GET; EXE never sees this). Host polls lateral, ALV, speed commands.
_pp_lateral_by_session = {}
PP_LATERAL_CMD_TYPES = frozenset({'DCT', 'HDG', 'DCT_PATH', 'APPLY_PATH', 'ALV', 'SPD', 'ROC', 'HOLD_ARM', 'HOLD_CANCEL', 'RWY'})

# PP/HOST flight status (ASM/transfer): separate from EXE/PLN atmTrajectory metadata.
_pp_flight_status_by_session = {}  # session_id -> { 'patches': { aid: dict } }
PP_FLIGHT_STATUS_PATCH_KEYS = frozenset({
    'ppFlightStatus', 'ppFlightStatusAtcSectorId',
    'ppTransferFromAtcSectorId', 'ppTransferToSectorNameNorm', 'ppTransferReceiverAssumed',
})

# PP workload split (sticky assignments per ATC sector; recomputed on pp-presence POST).
_pp_workload_by_session = {}  # session_id -> { atc_sector_id: { 'peersKey': str, 'assignments': { aid: client_id } } }

# AI_PP OpenAI response sessions. Key: local browser session id; value keeps OpenAI previous_response_id.
_ai_pp_openai_sessions = {}
_ai_pp_openai_lock = threading.RLock()

# ATM (air traffic management display) trajectory: first EXE joiner is master id; patches are merged for
# EXE/PLN/INSTR/EVAL/OBS clients only. FMS/host + PP remain separate (host state + pp-lateral).
_atm_trajectory_lock = threading.RLock()
_atm_trajectory_by_session = {}  # session_id -> { 'masterClientId': str|None, 'patches': { aid: dict } }
# HOST: host pushes FMS-aligned ATM patches after waypoint progression / PP lateral (joiners merge via GET state.atmTrajectory).
ATM_TRAJECTORY_DL_ROLES = frozenset({'EXE', 'PLN', 'INSTR', 'EVAL', 'OBS', 'HOST'})

# Host/PP FMS must not populate EXE/PLN HDG/DCT label fields in the ATM bucket.
ATM_HOST_OMIT_PATCH_KEYS = frozenset({
    'selectedHeadingDeg', 'directToWpIdx', 'directToWpName',
    'silentDirectToWpIdx', 'silentDirectToTurnWpIdx',
    'selectedApproachId', 'selectedApproachName', 'approachActive',
    'headingNavLat', 'headingNavLon', 'headingNavLastUpdateSimSec',
    'headingTurnTargetDeg', 'headingTurnStartSimSec', 'headingTurnRateDps', 'currentHeadingDeg',
    '_headingNavStatesEntries',
    'routeProgressIdx', 'routeCompleted', 'navMode',
})

# Directory for shared data (sectors/exercises) - same for all clients
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recordings')
SECTORS_FILE = os.path.join(DATA_DIR, 'sectors.json')
EXERCISES_FILE = os.path.join(DATA_DIR, 'exercises.json')
FLOWS_LIBRARY_FILE = os.path.join(DATA_DIR, 'flows.json')
AIRCRAFT_OVERRIDES_FILE = os.path.join(DATA_DIR, 'aircraft_db_overrides.json')
AIRLINE_CALLSIGNS_OVERRIDES_FILE = os.path.join(DATA_DIR, 'airline_callsigns_overrides.json')
TURN_RATE_BANDS_FILE = os.path.join(DATA_DIR, 'turn_rate_bands.json')
SIM_SETTINGS_PRESETS_FILE = os.path.join(DATA_DIR, 'sim_settings_presets.json')
EN_MGMT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'EN-MGMT')
ADMIN_SETTINGS_FILE = os.path.join(DATA_DIR, 'admin_settings.json')

init_user_auth(app, DATA_DIR)
init_stripe_billing(app)


@app.before_request
def _require_user_login():
    return auth_before_request()


DEFAULT_AI_PILOT_GENERAL_INSTRUCTIONS = """AI Pilot — you are a pseudo-pilot answering an air traffic controller.

OUTPUT
Reply with JSON only, exactly two string fields:
{"tts":"<spoken readback with placeholders>","command":"<simulator command>"}
No markdown, no code fences, no labels, no extra keys, no text outside the JSON.

PROCESS (reason internally, never reveal it)
1. Identify the aircraft from the exercise aircraft callsign list (see IDENTIFICATION).
2. Build command: the matched ICAO callsign + supported tokens only.
3. Validate command, then write tts as a readback that uses PLACEHOLDERS for the cleared values and ends with {CALLSIGN}.
tts and command always refer to the same aircraft.

CALLSIGNS
The only valid callsigns are in the exercise aircraft callsign list appended for the running exercise; this prompt contains none.
- command begins with the exact ICAO callsign from that list, copied character-for-character.
- tts ends with {CALLSIGN}; never spell or speak the callsign yourself.
- Never invent, infer, or echo a garbled callsign, and never use one not on the list.

IDENTIFICATION (apply in order, against the exercise list)
1. Flight-number digits (primary): match the spoken/numeric digit group to the digit portion of a list callsign. Prefer digits over airline names.
2. Phonetic suffix + digits: if digits are ambiguous, map phonetic words to suffix letters and match.
3. Airline/operator name + digits (fallback only): when steps 1–2 fail.
Speech-to-text often garbles airline names but keeps digits — try digits first.
If no list entry matches: tts "Say again", command "".

FIELD: command (machine tokens only)
- Format: ICAO_CALLSIGN TOKEN [VALUES] ... — space-separated tokens, never a sentence or English phraseology.
- No words, punctuation, QNH, or spoken callsigns.

FIELD: tts (spoken readback — placeholders + spoken phraseology)
tts contains two kinds of content; combine them into ONE natural pilot reply that ends with {CALLSIGN}.

A) PLACEHOLDERS — for these cleared values use the placeholder ONLY; the simulator expands each into correct phraseology from the aircraft's live state (climb/descend, turn direction, increase/reduce). Do NOT speak these values or their verbs yourself:
- {ALVnnn} — assigned level, the same 3 digits as the ALV command (e.g. {ALV340}, {ALV030}).
- {TLHnnn} / {TRHnnn} — turn left / turn right to heading (e.g. {TLH200}).
- {HDGnnn} — fly heading, shortest turn (e.g. {HDG270}).
- {IASnnn} — indicated airspeed (e.g. {IAS280}).
- {Mnnn} — Mach, hundredths (e.g. {M078} = Mach 0.78).
- {CALLSIGN} — the aircraft's spoken callsign; MANDATORY as the LAST item of every non-empty tts.

B) SPOKEN PHRASEOLOGY — for EVERYTHING that has no placeholder, write the readback yourself, fully, in standard ICAO pilot phraseology. This covers DCT, RTE, HOLD/EXITHOLD, ROCD (rate), ILS/approach, QNH, frequency/contact changes, acknowledgements, and free questions/answers. NEVER read a raw command token aloud — translate it into how a pilot actually speaks:
- DCT BEMBO → "proceeding direct BEMBO" (NOT "DCT BEMBO")
- RTE LIMKO DENKO → "routing LIMKO DENKO"
- HOLD BEMBO → "holding at BEMBO"; EXITHOLD → "leaving the hold"
- ILS08R → "cleared ILS runway zero eight right"
- ROCD rate → full English number with thousands + "feet per minute" (1200 → "one thousand two hundred feet per minute"; never digit-by-digit, never drop "thousand")
- Frequency/contact → read the frequency back naturally; repeat QNH when given.
Never put ICAO codes or raw command tokens (TLH, ALV, DCT, HOLD, ILS, ...) in the spoken text — placeholders for group A, proper phraseology for group B.
Example shape: command "ETD855 DCT BEMBO ALV340" → tts "Proceeding direct BEMBO, {ALV340}, {CALLSIGN}".

EXERCISE CONTEXT INFORMATION
The exercise may include Context Information (set in the AI Pilot tab when editing the exercise). When present it is appended below as "Exercise-specific context" and is also resent with the General Instructions. Treat it as an extension of these general instructions and follow it — it may add scenario facts, role-play details, or special replies the pilot should give. It never overrides the JSON output format or the callsign rules.

COMMAND TOKENS
- Heading: TLHnnn (left), TRHnnn (right), HDGnnn (fly heading) — 3 digits.
- Direct/route/hold: DCT FIX | RTE FIX1 FIX2 ... | HOLD FIX | EXITHOLD (fixes from the exercise lists).
- Altitude: ALVnnn — 3 digits, no units. FL → those 3 digits (FL240 → ALV240); feet → feet/100 (3000 ft → ALV030).
- Speed: IASnnn, or Mach as Mnnn (Mach 0.78 → M078).
- Rate of climb/descent: ROCDfpm, signed, digits only (climb 1500 → ROCD1500; descent 2500 → ROCD-2500). Exact controller value 100–5000, do not round. Use ALV alone for a level with no rate; use ROCD only when a rate is instructed.
- ILS/approach: ILSnnX (e.g. ILS08R).
- Label transfer-in: LBL_TIN-nnn (exercise initial-call scripts only; never spoken in tts).

COMPOUND (several instructions, same aircraft)
- command: chain tokens after one callsign, or repeat the callsign per clause separated by semicolons.
- tts: one readback with the matching placeholders, ending with {CALLSIGN}.

ON FREQUENCY (Single User live session)
Each controller transmission includes LIVE SESSION STATE listing every aircraft as onFrequency=yes/no.
- yes: respond normally when addressed.
- no: the aircraft is not on your frequency — respond {"tts":"","command":""} even if the controller uses its callsign.

FREQUENCIES & HANDOFF
Sector names and frequencies are in the exercise data and LIVE SESSION STATE. When told to contact another sector, read back the frequency in plain tts (command "" unless a separate token clearance also applies); "point" or "decimal" marks the decimal separator. After a contact-handoff readback the simulator sets that aircraft off your frequency.

SCRIPTS (exercise timeline, not live RTF)
[SCRIPT EVENT] messages carry fields: Silent, Use AI response for TTS, optional preset TTS, optional pre-approved command, instruction line.
- Silent=yes: respond {"tts":"","command":""}.
- Silent=no: respond with JSON per the rules above.
Newer events supersede older context for the same aircraft.

SUMMARY
- JSON only: tts + command, same aircraft.
- Build and validate command first, then derive tts from it.
- Placeholders ({ALV/TLH/TRH/HDG/IAS/M}) for cleared values; write every other readback yourself in standard phraseology (never speak raw tokens like DCT/HOLD/ILS — e.g. DCT → "proceeding direct ...").
- tts ends with {CALLSIGN}; never spell or infer the callsign, level verb, turn direction, or speed change yourself.
- Follow any Exercise Context Information as an extension of these instructions.
- Unknown aircraft → tts "Say again", command "".
- Off-frequency aircraft → {"tts":"","command":""}.
- No supported instruction → spoken tts readback ending with {CALLSIGN}, command "".
- tts is never empty except [SCRIPT EVENT] Silent=yes."""
DEFAULT_OPENAI_MODEL = 'gpt-5.4-mini'
OPENAI_MODEL_OPTIONS = frozenset({
    'gpt-5.4-mini',
    'gpt-5.4',
})
JOIN_SLOTS_DB = os.path.join(DATA_DIR, 'session_join_slots.sqlite3')
JOIN_SLOT_EXCLUSIVE_ROLES = frozenset({'EXE', 'PLN', 'INSTR', 'EVAL'})
JOIN_SLOT_ALLOWED_ROLES = frozenset({'EXE', 'PLN', 'INSTR', 'EVAL', 'OBS', 'PP'})
JOIN_SLOT_NON_EXCLUSIVE_ROLES = frozenset({'OBS', 'PP'})  # no DB row; unlimited users
JOIN_SLOT_TTL_SEC = 10.0  # drop slot ~10s after last heartbeat (disconnect)
_join_slots_lock = threading.RLock()
AI_PP_MAX_AUDIO_BYTES = 25 * 1024 * 1024
AI_PP_WHISPER_MODEL_SIZE = os.environ.get('AI_PP_WHISPER_MODEL', 'base')
AI_PP_WHISPER_DEVICE = os.environ.get('AI_PP_WHISPER_DEVICE', 'auto')
AI_PP_WHISPER_COMPUTE_TYPE = os.environ.get('AI_PP_WHISPER_COMPUTE_TYPE', 'default')
AI_PP_TRANSCRIBE_TIMEOUT_SEC = float(os.environ.get('AI_PP_TRANSCRIBE_TIMEOUT_SEC', '90'))
_ai_pp_whisper_model = None
_ai_pp_whisper_lock = threading.RLock()
_ai_pp_transcribe_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='ai-pp-whisper')


def _ai_pp_ffmpeg_available():
    return bool(shutil.which('ffmpeg'))


def _get_ai_pp_whisper_model():
    """Lazy-load faster-whisper so normal app startup does not pay the model load cost."""
    global _ai_pp_whisper_model
    with _ai_pp_whisper_lock:
        if _ai_pp_whisper_model is not None:
            return _ai_pp_whisper_model
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise RuntimeError('faster-whisper is not installed. Install requirements and ensure ffmpeg is available.') from exc
        kwargs = {}
        if AI_PP_WHISPER_DEVICE and AI_PP_WHISPER_DEVICE != 'auto':
            kwargs['device'] = AI_PP_WHISPER_DEVICE
        if AI_PP_WHISPER_COMPUTE_TYPE and AI_PP_WHISPER_COMPUTE_TYPE != 'default':
            kwargs['compute_type'] = AI_PP_WHISPER_COMPUTE_TYPE
        _ai_pp_whisper_model = WhisperModel(AI_PP_WHISPER_MODEL_SIZE, **kwargs)
        return _ai_pp_whisper_model


def _transcribe_ai_pp_audio_file(temp_path):
    """Run faster-whisper on one saved audio clip (may block; call from worker thread)."""
    model = _get_ai_pp_whisper_model()
    segments, info = model.transcribe(temp_path, beam_size=1, vad_filter=True)
    text = ' '.join((seg.text or '').strip() for seg in segments).strip()
    return {
        'text': text,
        'language': getattr(info, 'language', None),
        'duration': getattr(info, 'duration', None),
    }


def _merge_pp_vertical_into_state(session_id, state):
    """Apply latest PP vertical patches onto a session state dict (mutates state)."""
    if not isinstance(state, dict):
        return
    with _pp_vertical_lock:
        ov_all = dict(_pp_vertical_by_session.get(session_id) or {})
    if not ov_all:
        return
    sim = state.get('simulationData') or {}
    aircraft = sim.get('aircraft')
    if not isinstance(aircraft, list):
        return
    for ac in aircraft:
        if not isinstance(ac, dict):
            continue
        aid = ac.get('id')
        if aid is None:
            continue
        patch = ov_all.get(str(aid))
        if not isinstance(patch, dict):
            continue
        for k, v in patch.items():
            if k in PP_VERTICAL_KEYS:
                ac[k] = v


def _get_pp_peer_client_ids_on_sector(conn, session_id, atc_sector_id, now=None):
    """Active PP client ids on one ATC sector (sorted, deduped)."""
    t = now if now is not None else time.time()
    cur = conn.execute(
        'SELECT client_id FROM session_pp_presence WHERE session_id = ? AND atc_sector_id = ? AND (? - last_seen) <= ?',
        (session_id, atc_sector_id, t, JOIN_SLOT_TTL_SEC),
    )
    out = sorted({row[0] for row in cur.fetchall() if row and row[0]})
    return out


def _pick_pp_workload_min_count_peer(peers, counts):
    best = peers[0]
    best_count = counts.get(best, 0)
    for p in peers[1:]:
        c = counts.get(p, 0)
        if c < best_count or (c == best_count and p < best):
            best = p
            best_count = c
    return best


def _update_pp_workload_assignments(session_id, atc_sector_id, peers, eligible_aids):
    """Sticky PP split: full rebalance when peer set changes; new aircraft only when peers unchanged."""
    peers = sorted({str(p) for p in (peers or []) if p})
    eligible = sorted({str(a) for a in (eligible_aids or []) if a is not None and str(a) != ''})
    if len(peers) < 2:
        with _pp_vertical_lock:
            bucket = _pp_workload_by_session.get(session_id)
            if isinstance(bucket, dict):
                bucket.pop(atc_sector_id, None)
        return {}
    peers_key = '|'.join(peers)
    with _pp_vertical_lock:
        session_bucket = _pp_workload_by_session.setdefault(session_id, {})
        sec = session_bucket.get(atc_sector_id)
        if not isinstance(sec, dict):
            sec = {'peersKey': '', 'assignments': {}}
        assignments = dict(sec.get('assignments') or {})
        if sec.get('peersKey') != peers_key:
            assignments = {}
            counts = {p: 0 for p in peers}
            for aid in eligible:
                pick = _pick_pp_workload_min_count_peer(peers, counts)
                assignments[aid] = pick
                counts[pick] = counts.get(pick, 0) + 1
            sec = {'peersKey': peers_key, 'assignments': assignments}
            session_bucket[atc_sector_id] = sec
            return copy.deepcopy(assignments)
        assignments = {
            aid: cid for aid, cid in assignments.items()
            if aid in eligible and cid in peers
        }
        counts = {p: 0 for p in peers}
        for cid in assignments.values():
            counts[cid] = counts.get(cid, 0) + 1
        for aid in eligible:
            if aid in assignments:
                continue
            pick = _pick_pp_workload_min_count_peer(peers, counts)
            assignments[aid] = pick
            counts[pick] = counts.get(pick, 0) + 1
        sec = {'peersKey': peers_key, 'assignments': assignments}
        session_bucket[atc_sector_id] = sec
        return copy.deepcopy(assignments)


def _join_slots_run(fn):
    """Run fn(conn) with a short-lived SQLite connection (shared across workers)."""
    _ensure_data_dir()
    with _join_slots_lock:
        conn = sqlite3.connect(JOIN_SLOTS_DB, timeout=20, check_same_thread=False)
        try:
            conn.execute(
                '''CREATE TABLE IF NOT EXISTS session_join_slots (
                    session_id TEXT NOT NULL,
                    atc_sector_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    last_seen REAL NOT NULL,
                    PRIMARY KEY (session_id, atc_sector_id, role)
                )'''
            )
            conn.execute(
                '''CREATE TABLE IF NOT EXISTS session_pp_presence (
                    session_id TEXT NOT NULL,
                    atc_sector_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    last_seen REAL NOT NULL,
                    PRIMARY KEY (session_id, atc_sector_id, client_id)
                )'''
            )
            conn.commit()
            return fn(conn)
        finally:
            conn.close()


def _join_slots_prune(conn, now=None):
    t = now if now is not None else time.time()
    conn.execute(
        'DELETE FROM session_join_slots WHERE (? - last_seen) > ?',
        (t, JOIN_SLOT_TTL_SEC),
    )
    conn.execute(
        'DELETE FROM session_pp_presence WHERE (? - last_seen) > ?',
        (t, JOIN_SLOT_TTL_SEC),
    )


def _join_slots_prune_stale_global():
    """Expire disconnected users' slots (no heartbeat within TTL). Must run on hot paths:
    join-slots alone is not enough if nobody has the join modal open."""
    def work(conn):
        _join_slots_prune(conn, time.time())
        conn.commit()

    try:
        _join_slots_run(work)
    except Exception:
        pass


def _reset_session_runtime_buckets(session_id):
    """Clear PP lateral / ATM trajectory overlays for a session restart (join slots unchanged)."""
    with _pp_vertical_lock:
        _pp_vertical_by_session.pop(session_id, None)
        _pp_flight_status_by_session.pop(session_id, None)
        _pp_workload_by_session.pop(session_id, None)
    _pp_lateral_by_session.pop(session_id, None)
    with _atm_trajectory_lock:
        _atm_trajectory_by_session.pop(session_id, None)


def _remove_session_and_cleanup(session_id):
    """Remove hosted session from memory and SQLite join rows. Idempotent."""

    def work(conn):
        conn.execute('DELETE FROM session_join_slots WHERE session_id = ?', (session_id,))
        conn.execute('DELETE FROM session_pp_presence WHERE session_id = ?', (session_id,))
        conn.commit()

    try:
        _join_slots_run(work)
    except Exception:
        pass
    if session_id in _sessions:
        del _sessions[session_id]
    _reset_session_runtime_buckets(session_id)


def _merge_atm_into_state(session_id, state_out):
    """Attach ATM display trajectory (non-FMS) for DL clients; PP/host ignore when merging client-side."""
    if not isinstance(state_out, dict):
        return
    with _atm_trajectory_lock:
        bucket = _atm_trajectory_by_session.get(session_id)
    if not bucket:
        return
    state_out['atmTrajectory'] = {
        'masterClientId': bucket.get('masterClientId'),
        'patches': copy.deepcopy(bucket.get('patches') or {}),
    }


def _prune_stale_host_sessions():
    """Drop sessions whose host has not refreshed activity within HOST_SESSION_IDLE_TTL_SEC."""
    now = time.time()
    for sid, s in list(_sessions.items()):
        last = float(s.get('lastHostActivityAt') or s.get('createdAt') or 0)
        if now - last > HOST_SESSION_IDLE_TTL_SEC:
            _remove_session_and_cleanup(sid)


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _ensure_recordings_dir():
    os.makedirs(RECORDINGS_DIR, exist_ok=True)


def _ensure_en_mgmt_dir():
    os.makedirs(EN_MGMT_DIR, exist_ok=True)


def _safe_en_mgmt_routes_stem(name):
    text = str(name or '').strip()
    text = re.sub(r'[^A-Za-z0-9._ -]+', '_', text)
    text = re.sub(r'\s+', ' ', text).strip('._- ')
    if text.lower().endswith('.json'):
        text = text[:-5].strip('._- ')
    return text[:80] if text else None


def _en_mgmt_routes_file_path(name):
    stem = _safe_en_mgmt_routes_stem(name)
    if not stem:
        return None, None
    filename = stem + '.json'
    return os.path.join(EN_MGMT_DIR, filename), filename


def _list_en_mgmt_route_files():
    _ensure_en_mgmt_dir()
    names = []
    try:
        for entry in os.listdir(EN_MGMT_DIR):
            if not entry.lower().endswith('.json'):
                continue
            path = os.path.join(EN_MGMT_DIR, entry)
            if os.path.isfile(path):
                names.append(entry[:-5])
    except OSError:
        return []
    names.sort(key=lambda s: s.lower())
    return names


def _safe_filename_part(value, fallback='recording'):
    text = str(value or '').strip()
    text = re.sub(r'[^A-Za-z0-9._ -]+', '_', text)
    text = re.sub(r'\s+', '_', text).strip('._- ')
    return (text[:80] or fallback)


def _safe_recording_filename(filename):
    name = os.path.basename(str(filename or '').strip())
    if not name or name in ('.', '..') or not name.lower().endswith('.json'):
        return None
    return name


def _recording_path(filename):
    name = _safe_recording_filename(filename)
    if not name:
        return None
    return os.path.join(RECORDINGS_DIR, name)


def _read_json(path, default):
    _ensure_data_dir()
    if not os.path.isfile(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path, data):
    _ensure_data_dir()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _default_global_label_layout():
    """5x5 aircraft label grid (matches client SIM_SETTINGS_DEFAULTS.labelLayout)."""
    empty = {'field': '', 'freeText': ''}
    return [
        [
            {'field': 'CALLSIGN', 'freeText': ''},
            {'field': 'RWY', 'freeText': ''},
            {'field': 'EXIT_WAYPOINT', 'freeText': ''},
            {'field': 'AIRCRAFT_TYPE', 'freeText': ''},
            empty.copy(),
        ],
        [
            {'field': 'MODE_C_ALT', 'freeText': ''},
            {'field': 'GS', 'freeText': ''},
            {'field': 'HDG_DCT', 'freeText': ''},
            {'field': 'DESTINATION', 'freeText': ''},
            empty.copy(),
        ],
        [
            {'field': 'ALV', 'freeText': ''},
            {'field': 'MODE_S_ALT', 'freeText': ''},
            {'field': 'IAS', 'freeText': ''},
            {'field': 'ROC', 'freeText': ''},
            {'field': 'EXIT_LEVEL', 'freeText': ''},
        ],
        [
            {'field': 'FREE_TEXT', 'freeText': ''},
            empty.copy(),
            empty.copy(),
            empty.copy(),
            empty.copy(),
        ],
        [empty.copy() for _ in range(5)],
    ]


def _normalize_label_cell(cell):
    if not isinstance(cell, dict):
        return {'field': '', 'freeText': ''}
    field = cell.get('field')
    free_text = cell.get('freeText')
    return {
        'field': field.strip() if isinstance(field, str) else '',
        'freeText': free_text.strip() if isinstance(free_text, str) else '',
    }


def _normalize_label_layout(layout):
    default = _default_global_label_layout()
    if not isinstance(layout, list) or len(layout) != 5:
        return default
    rows = []
    for r in range(5):
        row = layout[r] if r < len(layout) else []
        if not isinstance(row, list):
            row = []
        cells = []
        for c in range(5):
            cell = row[c] if c < len(row) else {}
            cells.append(_normalize_label_cell(cell))
        rows.append(cells)
    return rows


def _normalize_label_setups(setups):
    if not isinstance(setups, list):
        return []
    out = []
    for setup in setups:
        if not isinstance(setup, dict):
            continue
        sid = (setup.get('id') or '').strip()
        name = (setup.get('name') or '').strip()
        if not sid or not name:
            continue
        normalized = {
            'id': sid,
            'name': name,
            'layout': _normalize_label_layout(setup.get('layout')),
        }
        for key in ('createdAt', 'updatedAt'):
            val = setup.get(key)
            if isinstance(val, str) and val.strip():
                normalized[key] = val.strip()
        out.append(normalized)
    return out


ADMIN_FST_KEYS = ('CIN', 'COU', 'TIN', 'TOU', 'ASM', 'UOU', 'UIN')
HOSTED_SESSION_LABEL_ROLES = ('HOST', 'EXE', 'PLN', 'INSTR', 'EVAL', 'OBS', 'PP')


def _default_fst_label_setup_ids():
    empty = {'nonHover': '', 'hover': ''}
    return {key: dict(empty) for key in ADMIN_FST_KEYS}


def _normalize_fst_label_entry(entry):
    if isinstance(entry, str):
        return {'nonHover': entry.strip(), 'hover': ''}
    if not isinstance(entry, dict):
        return {'nonHover': '', 'hover': ''}
    non_hover = entry.get('nonHover')
    hover = entry.get('hover')
    return {
        'nonHover': non_hover.strip() if isinstance(non_hover, str) else '',
        'hover': hover.strip() if isinstance(hover, str) else '',
    }


def _normalize_fst_label_setup_ids(raw):
    out = _default_fst_label_setup_ids()
    if not isinstance(raw, dict):
        return out
    for key in ADMIN_FST_KEYS:
        if key in raw:
            out[key] = _normalize_fst_label_entry(raw.get(key))
    return out


def _normalize_hosted_sessions_role_label_setup_ids(raw, legacy_fst=None):
    out = {role: _default_fst_label_setup_ids() for role in HOSTED_SESSION_LABEL_ROLES}
    if isinstance(raw, dict):
        for role in HOSTED_SESSION_LABEL_ROLES:
            if role in raw:
                out[role] = _normalize_fst_label_setup_ids(raw.get(role))
    if legacy_fst is not None:
        legacy = _normalize_fst_label_setup_ids(legacy_fst)
        any_set = any(
            (out[role][key].get('nonHover') or out[role][key].get('hover'))
            for role in HOSTED_SESSION_LABEL_ROLES
            for key in ADMIN_FST_KEYS
        )
        if not any_set:
            for role in HOSTED_SESSION_LABEL_ROLES:
                out[role] = {key: dict(entry) for key, entry in legacy.items()}
    return out


def _normalize_single_user_label_profiles(profiles):
    if not isinstance(profiles, list):
        return []
    out = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        pid = (profile.get('id') or '').strip()
        name = (profile.get('name') or '').strip()
        if not pid or not name:
            continue
        normalized = {
            'id': pid,
            'name': name,
            'fstLabelSetupIds': _normalize_fst_label_setup_ids(profile.get('fstLabelSetupIds')),
        }
        for key in ('createdAt', 'updatedAt'):
            val = profile.get(key)
            if isinstance(val, str) and val.strip():
                normalized[key] = val.strip()
        out.append(normalized)
    return out


def _normalize_hosted_sessions_label_profiles(profiles):
    if not isinstance(profiles, list):
        return []
    out = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        pid = (profile.get('id') or '').strip()
        name = (profile.get('name') or '').strip()
        if not pid or not name:
            continue
        legacy_fst = profile.get('fstLabelSetupIds') if isinstance(profile.get('fstLabelSetupIds'), dict) else None
        normalized = {
            'id': pid,
            'name': name,
            'roleLabelSetupIds': _normalize_hosted_sessions_role_label_setup_ids(
                profile.get('roleLabelSetupIds'),
                legacy_fst=legacy_fst,
            ),
        }
        for key in ('createdAt', 'updatedAt'):
            val = profile.get(key)
            if isinstance(val, str) and val.strip():
                normalized[key] = val.strip()
        out.append(normalized)
    return out


def _read_admin_settings():
    data = _read_json(ADMIN_SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    if not data.get('passwordHash'):
        data['passwordHash'] = generate_password_hash('admin123')
        data.setdefault('openaiApiKey', '')
        data.setdefault('openaiModel', DEFAULT_OPENAI_MODEL)
        data.setdefault('aiPilotGeneralInstructions', DEFAULT_AI_PILOT_GENERAL_INSTRUCTIONS)
        _write_json(ADMIN_SETTINGS_FILE, data)
    data.setdefault('openaiApiKey', '')
    data.setdefault('openaiModel', DEFAULT_OPENAI_MODEL)
    data.setdefault('aiPilotGeneralInstructions', DEFAULT_AI_PILOT_GENERAL_INSTRUCTIONS)
    data.setdefault('recordSimulations', True)
    data.setdefault('globalLabelLayout', _default_global_label_layout())
    data.setdefault('globalLabelSetupId', '')
    data.setdefault('globalLabelSetups', [])
    data.setdefault('singleUserLabelProfiles', [])
    data.setdefault('singleUserLabelAssignedProfileId', '')
    data.setdefault('hostedSessionsLabelProfiles', [])
    data.setdefault('hostedSessionsLabelAssignedProfileId', '')
    data.setdefault('defaultGenericLabelSetupId', '')
    data.setdefault('defaultSingleUserLabelProfileId', '')
    data.setdefault('defaultHostedSessionsLabelProfileId', '')
    data['globalLabelLayout'] = _normalize_label_layout(data.get('globalLabelLayout'))
    data['globalLabelSetupId'] = (data.get('globalLabelSetupId') or '').strip() if isinstance(data.get('globalLabelSetupId'), str) else ''
    data['globalLabelSetups'] = _normalize_label_setups(data.get('globalLabelSetups'))
    data['singleUserLabelProfiles'] = _normalize_single_user_label_profiles(data.get('singleUserLabelProfiles'))
    assigned = data.get('singleUserLabelAssignedProfileId', '')
    data['singleUserLabelAssignedProfileId'] = assigned.strip() if isinstance(assigned, str) else ''
    data['hostedSessionsLabelProfiles'] = _normalize_hosted_sessions_label_profiles(data.get('hostedSessionsLabelProfiles'))
    hosted_assigned = data.get('hostedSessionsLabelAssignedProfileId', '')
    data['hostedSessionsLabelAssignedProfileId'] = hosted_assigned.strip() if isinstance(hosted_assigned, str) else ''
    for key in ('defaultGenericLabelSetupId', 'defaultSingleUserLabelProfileId', 'defaultHostedSessionsLabelProfileId'):
        val = data.get(key, '')
        data[key] = val.strip() if isinstance(val, str) else ''
    return data


def _admin_record_simulations_enabled(admin_data=None):
    """True when completed simulation sessions should be persisted for Playback."""
    data = admin_data if isinstance(admin_data, dict) else _read_admin_settings()
    val = data.get('recordSimulations', True)
    if isinstance(val, str):
        return val.strip().lower() not in ('no', 'false', '0', 'off')
    return True if val is None else bool(val)


def _normalize_openai_model(model):
    m = (model or '').strip() if isinstance(model, str) else ''
    if m in OPENAI_MODEL_OPTIONS:
        return m
    return DEFAULT_OPENAI_MODEL


def _get_admin_openai_model(admin_data=None):
    data = admin_data if isinstance(admin_data, dict) else _read_admin_settings()
    return _normalize_openai_model(data.get('openaiModel'))


def _admin_password_ok(password):
    pw = password if isinstance(password, str) else ''
    if not pw:
        return False
    data = _read_admin_settings()
    return check_password_hash(data.get('passwordHash') or '', pw)


def _find_exercise(exercise_id):
    if exercise_id is None:
        return None
    exercise_id = str(exercise_id)
    exercises = _read_json(EXERCISES_FILE, [])
    if not isinstance(exercises, list):
        return None
    for exercise in exercises:
        if isinstance(exercise, dict) and str(exercise.get('id')) == exercise_id:
            return exercise
    return None


def _find_sector(sector_id):
    if sector_id is None:
        return None
    sector_id = str(sector_id)
    sectors = _read_json(SECTORS_FILE, [])
    if not isinstance(sectors, list):
        return None
    for sector in sectors:
        if isinstance(sector, dict) and str(sector.get('id')) == sector_id:
            return sector
    return None


def _get_exercise_ai_pilot_context(exercise_id):
    exercise = _find_exercise(exercise_id)
    if not exercise:
        return ''
    ctx = exercise.get('aiPilotContext', '')
    return ctx if isinstance(ctx, str) else ''


AI_PP_MAX_WAYPOINT_NAMES = 1500


def _parse_flight_route_tokens(route_str):
    """Likely fix names from a flight route string (e.g. OMAA_DEP_ALPOB_ORMID)."""
    if not route_str or not isinstance(route_str, str):
        return []
    tokens = []
    for part in re.split(r'[_\-\s]+', route_str.strip()):
        tok = part.strip().upper()
        if tok and re.fullmatch(r'[A-Z0-9]{2,6}', tok) and tok not in ('DEP', 'ARR'):
            tokens.append(tok)
    return tokens


def _get_exercise_callsigns(exercise):
    if not isinstance(exercise, dict):
        return []
    seen = set()
    callsigns = []
    for flight in exercise.get('flights') or []:
        if not isinstance(flight, dict):
            continue
        cs = (flight.get('callsign') or '').strip().upper()
        if cs and cs not in seen:
            seen.add(cs)
            callsigns.append(cs)
    return sorted(callsigns)


def _sector_fix_name_by_id(sector):
    by_id = {}
    if not isinstance(sector, dict):
        return by_id
    for fix in (sector.get('waypoints') or []) + (sector.get('navaids') or []):
        if not isinstance(fix, dict):
            continue
        fix_id = fix.get('id')
        name = (fix.get('name') or '').strip().upper()
        if fix_id is not None and name:
            by_id[str(fix_id)] = name
    return by_id


def _get_exercise_waypoint_names(exercise, sector):
    """Fix names available in this exercise (sector waypoints/navaids, route-aware if list is huge)."""
    if not isinstance(sector, dict):
        return []

    wp_by_id = _sector_fix_name_by_id(sector)
    all_sector_names = sorted(set(wp_by_id.values()))
    if not all_sector_names:
        return []

    if len(all_sector_names) <= AI_PP_MAX_WAYPOINT_NAMES:
        return all_sector_names

    priority = set()
    route_names_used = set()
    if isinstance(exercise, dict):
        for flight in exercise.get('flights') or []:
            if not isinstance(flight, dict):
                continue
            route_str = (flight.get('route') or '').strip()
            if route_str:
                route_names_used.add(route_str.upper())
            for tok in _parse_flight_route_tokens(route_str):
                priority.add(tok)

        for route in sector.get('routes') or []:
            if not isinstance(route, dict):
                continue
            rname = (route.get('name') or '').strip().upper()
            if not rname:
                continue
            on_exercise = rname in route_names_used or any(
                rname in rn or rn.startswith(rname) or rname.startswith(rn.split('_')[0])
                for rn in route_names_used
            )
            if not on_exercise:
                continue
            for wid in route.get('waypointIds') or []:
                name = wp_by_id.get(str(wid))
                if name:
                    priority.add(name)

    names = sorted(priority)
    if len(names) < AI_PP_MAX_WAYPOINT_NAMES:
        for name in all_sector_names:
            if name not in priority:
                names.append(name)
            if len(names) >= AI_PP_MAX_WAYPOINT_NAMES:
                break
    return names[:AI_PP_MAX_WAYPOINT_NAMES]


def _get_exercise_map_waypoint_names(exercise, sector):
    """All waypoint and navaid names shown on the exercise airspace map."""
    if not isinstance(sector, dict):
        return []
    names = set()
    for fix in (sector.get('waypoints') or []) + (sector.get('navaids') or []):
        if not isinstance(fix, dict):
            continue
        name = (fix.get('name') or '').strip().upper()
        if name:
            names.add(name)
    out = sorted(names)
    if len(out) > AI_PP_MAX_WAYPOINT_NAMES:
        return out[:AI_PP_MAX_WAYPOINT_NAMES]
    return out


def _build_exercise_ai_pp_aircraft_list_section(exercise_id, on_frequency_codes=None):
    exercise = _find_exercise(exercise_id)
    callsigns = _get_exercise_callsigns(exercise) if exercise else []
    on_set = set()
    if on_frequency_codes is not None:
        for code in on_frequency_codes:
            c = str(code or '').strip().upper()
            if c:
                on_set.add(c)
        show_flags = True
    else:
        show_flags = False
    lines = [
        f'EXERCISE AIRCRAFT LIST ({len(callsigns)} aircraft):',
        'These are the ONLY valid aircraft callsigns for this session.',
    ]
    if show_flags:
        lines.append('Each line shows onFrequency=yes/no (Single User live session).')
    if callsigns:
        for cs in callsigns:
            if show_flags:
                lines.append(f'  - {cs} onFrequency={"yes" if cs in on_set else "no"}')
            else:
                lines.append(f'  - {cs}')
    else:
        lines.append('  (none defined)')
    return '\n'.join(lines)


def _build_exercise_ai_pp_map_waypoints_section(exercise_id):
    exercise = _find_exercise(exercise_id)
    sector = _find_sector(exercise.get('sectorId')) if exercise else None
    names = _get_exercise_map_waypoint_names(exercise, sector)
    wp_by_id = _sector_fix_name_by_id(sector) if sector else {}
    total_on_map = len(set(wp_by_id.values()))
    trunc_note = ''
    if total_on_map > len(names):
        trunc_note = (
            f'\n(Showing {len(names)} of {total_on_map} map fixes — use exact spelling from this list.)'
        )
    wp_text = ', '.join(names) if names else '(none defined)'
    return (
        f'EXERCISE MAP WAYPOINTS AND NAVAIDS ({len(names)} names on this exercise airspace map):\n'
        f'{wp_text}{trunc_note}'
    )


def _normalize_approach_name(raw):
    if not raw:
        return ''
    return re.sub(r'[^A-Z0-9._-]', '', str(raw).strip().upper())[:16]


def _get_exercise_approach_names(exercise, sector):
    """ILS/approach procedure names at destination airports in this exercise."""
    if not isinstance(exercise, dict) or not isinstance(sector, dict):
        return []
    dest_icaos = set()
    for flight in exercise.get('flights') or []:
        if not isinstance(flight, dict):
            continue
        dest = (flight.get('destination') or '').strip().upper()
        if dest:
            dest_icaos.add(dest)
    if not dest_icaos:
        return []
    names = []
    seen = set()
    for ap in sector.get('airports') or []:
        if not isinstance(ap, dict):
            continue
        icao = (ap.get('icao') or '').strip().upper()
        if icao not in dest_icaos:
            continue
        for rw in ap.get('runways') or []:
            if not isinstance(rw, dict):
                continue
            for app in rw.get('approaches') or []:
                if not isinstance(app, dict):
                    continue
                nm = _normalize_approach_name(app.get('name'))
                if nm and nm not in seen:
                    seen.add(nm)
                    names.append(nm)
    return sorted(names)


def _normalize_atc_sector_frequency(raw):
    if raw is None:
        return ''
    return str(raw).strip()


def _build_exercise_ai_pp_sector_frequencies_section(exercise_id):
    exercise = _find_exercise(exercise_id)
    sector = _find_sector(exercise.get('sectorId')) if exercise else None
    if not sector:
        return 'ATC sector frequencies (MHz): (exercise airspace not found)'
    atc_sectors = sector.get('atcSectors') if isinstance(sector.get('atcSectors'), list) else []
    if not atc_sectors:
        return 'ATC sector frequencies (MHz): (none defined in this airspace)'
    lines = ['ATC sector frequencies in this exercise airspace (MHz):']
    for s in atc_sectors:
        if not isinstance(s, dict):
            continue
        name = (s.get('name') or s.get('id') or '').strip()
        freq = _normalize_atc_sector_frequency(s.get('frequency'))
        if freq:
            lines.append(f'  - {name}: {freq} MHz')
        else:
            lines.append(f'  - {name}: (no frequency defined)')
    return '\n'.join(lines)


def _build_exercise_ai_pp_roster_section(exercise_id):
    exercise = _find_exercise(exercise_id)
    if not exercise:
        return (
            '=== RUNNING EXERCISE — AIRCRAFT & FIX DATA (automatic) ===\n'
            'EXERCISE AIRCRAFT CALLSIGN LIST: (exercise not found)\n'
            'Valid waypoint/fix names: (exercise not found)'
        )

    callsigns = _get_exercise_callsigns(exercise)
    sector = _find_sector(exercise.get('sectorId'))
    fix_names = _get_exercise_waypoint_names(exercise, sector)
    approach_names = _get_exercise_approach_names(exercise, sector)
    wp_by_id = _sector_fix_name_by_id(sector) if sector else {}
    total_fixes = len(set(wp_by_id.values()))
    truncated = total_fixes > len(fix_names)

    if callsigns:
        cs_list_lines = '\n'.join(f'  - {cs}' for cs in callsigns)
        cs_section = (
            f'EXERCISE AIRCRAFT CALLSIGN LIST — AUTHORITATIVE ({len(callsigns)} aircraft in this running exercise)\n'
            'These are the ONLY valid aircraft callsigns for this session. Identify the addressed aircraft from this list only, '
            'then use the matched entry exactly in both command and tts:\n'
            f'{cs_list_lines}\n'
            '- command MUST begin with the exact ICAO string from one line above (character-for-character).\n'
            '- tts MUST be the spoken form of that SAME line — never a different aircraft, never a callsign not on this list.\n'
            '- Match flight-number DIGITS against this list first; try airline/operator name only if digit matching does not identify one list entry.'
        )
    else:
        cs_section = (
            'EXERCISE AIRCRAFT CALLSIGN LIST — AUTHORITATIVE (0 aircraft in this running exercise)\n'
            '(none defined — no aircraft to address)'
        )

    wp_text = ', '.join(fix_names) if fix_names else '(none defined)'
    app_text = ', '.join(approach_names) if approach_names else '(none defined)'
    trunc_note = (
        f'\n(Showing {len(fix_names)} of {total_fixes} sector fixes — '
        'exercise-route fixes listed first; use best match from this list.)'
        if truncated else ''
    )

    return (
        '=== RUNNING EXERCISE — AIRCRAFT & FIX DATA (automatic) ===\n'
        f'{cs_section}\n\n'
        f'Valid waypoint/fix names in this exercise ({len(fix_names)}): {wp_text}'
        f'{trunc_note}\n'
        f'Valid approach procedure names at destination airports ({len(approach_names)}): {app_text}\n\n'
        'Callsign matching reminder:\n'
        '- The exercise aircraft callsign list above is the complete set of aircraft in this running exercise.\n'
        '- command and tts must use the same aircraft from that list — exact ICAO in command, spoken form of that same entry in tts.\n'
        '- Identify by flight-number digits against the list first; use airline/operator name only if digits do not match any list entry.\n'
        '- LIVE SESSION STATE (appended to each controller transmission in Single User) lists onFrequency=yes/no per aircraft. '
        'If onFrequency=no for the addressed aircraft, respond {"tts":"","command":""}.\n'
        '- ALWAYS respond with non-empty tts unless [SCRIPT EVENT] Silent=yes or the addressed aircraft is off-frequency (onFrequency=no). '
        'If aircraft cannot be identified from the list, tts "Say again" and command "".\n'
        '- If the instruction is not a supported command type, give a relevant tts readback and command "".\n'
        '- For DCT, RTE, or HOLD, the fix MUST be one of the waypoint names above (exact spelling).\n'
        '- For cleared ILS/approach, use the exact approach token from the list above.'
    )


def _format_script_preview_time_label(sec):
    s = max(0, int(sec or 0))
    return f'{s // 60:02d}:{s % 60:02d}'


def _get_exercise_ai_pilot_scripts(exercise_id):
    exercise = _find_exercise(exercise_id)
    if not exercise:
        return []
    raw = exercise.get('aiPilotScripts')
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict)]


def _build_exercise_ai_pp_scripts_section(exercise_id):
    scripts = _get_exercise_ai_pilot_scripts(exercise_id)
    if not scripts:
        return 'Exercise scripts scheduled: (none)'
    scripts = sorted(scripts, key=lambda s: int(s.get('previewTimeSec') or 0))
    lines = ['Exercise scripts scheduled (simulation time — may fire during session):']
    for s in scripts:
        t = _format_script_preview_time_label(s.get('previewTimeSec'))
        cs = (s.get('callsign') or '').strip()
        silent = bool(s.get('aiInformationSilent'))
        use_ai_tts = bool(s.get('ttsUseAiResponse'))
        has_cmd = bool((s.get('aiTransformedCommand') or '').strip())
        lines.append(
            f'- {t} {cs} silent={"yes" if silent else "no"} '
            f'useAiTts={"yes" if use_ai_tts else "no"} preApprovedCmd={"yes" if has_cmd else "no"}'
        )
    return '\n'.join(lines)


AI_PP_RESPONSE_JSON_SCHEMA = {
    'type': 'object',
    'properties': {
        'tts': {'type': 'string'},
        'command': {'type': 'string'},
    },
    'required': ['tts', 'command'],
    'additionalProperties': False,
}

AI_PILOT_TRANSFORM_COMMAND_JSON_SCHEMA = {
    'type': 'object',
    'properties': {
        'command': {'type': 'string'},
    },
    'required': ['command'],
    'additionalProperties': False,
}


def _build_transform_command_instructions(admin):
    general = admin.get('aiPilotGeneralInstructions') or DEFAULT_AI_PILOT_GENERAL_INSTRUCTIONS
    return (
        f"{general}\n\n"
        "TASK: Convert the user's input text into machine simulator command format ONLY.\n"
        "Return JSON with exactly one field: {\"command\":\"...\"}\n"
        "command must use compact ICAO callsign + supported COMMAND TOKENS — never a grammatical sentence.\n"
        "Use the provided callsign in compact ICAO form at the start of command.\n"
        "If the input cannot be mapped to supported COMMAND TOKENS, return {\"command\":\"\"}.\n"
        "Do NOT include a tts field. Do NOT include markdown or explanation text."
    )


def _build_transform_command_user_input(callsign, input_text):
    return (
        f"Callsign: {callsign}\n"
        "Turn the following into command format:\n"
        f"{input_text}"
    )


def _ai_pilot_transform_command_payload(admin, instructions, user_input):
    return {
        'model': _get_admin_openai_model(admin),
        'instructions': instructions,
        'input': user_input,
        'temperature': 0.2,
        'text': {
            'format': {
                'type': 'json_schema',
                'name': 'ai_pilot_transform_command',
                'schema': AI_PILOT_TRANSFORM_COMMAND_JSON_SCHEMA,
                'strict': True,
            }
        },
    }


def _build_ai_pp_general_instructions(admin):
    """AI Pilot General Instructions only (Admin copy or built-in default)."""
    return admin.get('aiPilotGeneralInstructions') or DEFAULT_AI_PILOT_GENERAL_INSTRUCTIONS


def _build_ai_pp_general_instructions_with_context(admin, exercise_id):
    """General Instructions + the exercise's Context Information (AI Pilot tab), so the exercise's
    special instructions travel with the General Instructions every time they are sent (e.g. PTT prime)."""
    general = _build_ai_pp_general_instructions(admin)
    ctx = _get_exercise_ai_pilot_context(exercise_id)
    if ctx and ctx.strip():
        return f"{general}\n\nExercise-specific context:\n{ctx.strip()}"
    return general


def _build_ai_pp_session_instructions(admin, exercise_id):
    """Static exercise context sent once at session start (OpenAI Responses instructions field).

    Includes General Instructions, exercise roster/fixes, sector frequencies, and scripts.
    General Instructions alone are resent on each controller PTT press (pttPrime) before release/transcription.
    Per-transmission LIVE SESSION STATE is appended on PTT release. ON FREQUENCY EVENT messages resend
    the exercise aircraft list with updated onFrequency flags.
    """
    general = _build_ai_pp_general_instructions(admin)
    exercise_ctx = _get_exercise_ai_pilot_context(exercise_id)
    roster = _build_exercise_ai_pp_roster_section(exercise_id)
    sector_freqs = _build_exercise_ai_pp_sector_frequencies_section(exercise_id)
    scripts = _build_exercise_ai_pp_scripts_section(exercise_id)
    return (
        f"{general}\n\n"
        "Exercise-specific context:\n"
        f"{exercise_ctx.strip() if exercise_ctx.strip() else '(none)'}\n\n"
        f"{roster}\n\n"
        f"{sector_freqs}\n\n"
        f"{scripts}\n\n"
        "REMINDER — reply JSON ONLY: {\"tts\":\"...\",\"command\":\"...\"}\n"
        "Build and validate command FIRST (exact exercise-list ICAO callsign + tokens only, never a sentence), then write tts. "
        "tts uses PLACEHOLDERS for cleared values — {ALVnnn}, {TLHnnn}, {TRHnnn}, {HDGnnn}, {IASnnn}, {Mnnn} — and ENDS with {CALLSIGN}. "
        "Never spell the callsign and never speak climb/descend, turn direction, or increase/reduce yourself; the simulator expands placeholders from live aircraft state.\n"
        "Use only callsigns from the exercise aircraft callsign list above. "
        "Match flight-number digits against that list first; airline name only if digits fail. "
        "Use TLH/TRH/HDG (never HEADING). "
        "Feet altitude → ALVnnn (feet÷100). Plain English (no placeholder) for DCT/RTE/HOLD/ILS/QNH/frequencies. Climb/descent rate → ROCDfpm (signed fpm); rate tts in plain English with thousands (1200 → one thousand two hundred feet per minute, not one two hundred). "
        "Off-frequency aircraft (onFrequency=no in LIVE SESSION STATE) → empty tts and command. "
        "Unknown aircraft → tts Say again, command empty. [SCRIPT EVENT] Silent=yes → empty tts and command."
    )


def _ai_pp_openai_payload(admin, user_input, previous_response_id=None, instructions=None):
    payload = {
        'model': _get_admin_openai_model(admin),
        'input': user_input,
        'temperature': 0.2,
        'text': {
            'format': {
                'type': 'json_schema',
                'name': 'ai_pilot_response',
                'schema': AI_PP_RESPONSE_JSON_SCHEMA,
                'strict': True,
            }
        },
    }
    if instructions is not None:
        payload['instructions'] = instructions
    if previous_response_id:
        payload['previous_response_id'] = previous_response_id
    return payload


def _ai_pp_seed_conversation_input(exercise_id=None, on_frequency_codes=None, session_instructions=None):
    """Persistent developer instructions + few-shot examples + exercise data.

    The full session instructions are placed here as a developer-role message (NOT in the top-level
    `instructions` param) so they persist across the whole session via previous_response_id. OpenAI does
    not carry the top-level `instructions` field across a previous_response_id chain, but role messages in
    the input array are replayed on every turn.
    """
    seed = []
    if session_instructions:
        seed.append({'role': 'developer', 'content': session_instructions})
    seed.extend([
        {
            'role': 'user',
            'content': 'Controller transmission: (addressed to one aircraft on the exercise roster) turn left heading one zero zero',
        },
        {
            'role': 'assistant',
            'content': '{"tts":"{TLH100}, {CALLSIGN}","command":"<exact exercise-list ICAO entry> TLH100"}',
        },
        {
            'role': 'user',
            'content': 'Controller transmission: (addressed to one aircraft on the exercise roster) proceed direct BEMBO',
        },
        {
            'role': 'assistant',
            'content': '{"tts":"Proceeding direct BEMBO, {CALLSIGN}","command":"<exact exercise-list ICAO entry> DCT BEMBO"}',
        },
        {
            'role': 'user',
            'content': 'Correct. Build and validate command first. In tts use PLACEHOLDERS ({ALVnnn}, {TLHnnn}, {TRHnnn}, {HDGnnn}, {IASnnn}, {Mnnn}) for those cleared values and end every tts with {CALLSIGN}. For everything without a placeholder (DCT, RTE, HOLD/EXITHOLD, ROCD, ILS, QNH, frequencies) write the full spoken pilot readback yourself in standard phraseology — never speak a raw token (e.g. DCT BEMBO -> "proceeding direct BEMBO"). Never spell the callsign and never speak the climb/descend, turn-direction, or increase/reduce verbs. Use only callsigns from the exercise aircraft callsign list (exact ICAO in command). Follow this for every controller transmission this session.',
        },
    ])
    if not exercise_id:
        return seed
    aircraft = _build_exercise_ai_pp_aircraft_list_section(exercise_id, on_frequency_codes=on_frequency_codes or [])
    waypoints = _build_exercise_ai_pp_map_waypoints_section(exercise_id)
    seed.append({
        'role': 'user',
        'content': (
            '[EXERCISE SESSION DATA — initial transmission]\n'
            f'{aircraft}\n\n'
            f'{waypoints}\n\n'
            'ON FREQUENCY EVENT messages later in this session will resend the exercise aircraft list with '
            'updated onFrequency flags. Respond {"tts":"","command":""} to session-data messages unless '
            'a controller transmission requires a pilot readback.'
        ),
    })
    seed.append({
        'role': 'assistant',
        'content': '{"tts":"","command":""}',
    })
    return seed


def _openai_responses_request(api_key, payload):
    req = urllib.request.Request(
        'https://api.openai.com/v1/responses',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            msg = json.loads(body).get('error', {}).get('message') or body
        except Exception:
            msg = body
        raise RuntimeError(f'OpenAI error: {msg}')


def _extract_openai_response_text(data):
    if not isinstance(data, dict):
        return ''
    txt = data.get('output_text')
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    chunks = []
    for item in data.get('output') or []:
        if not isinstance(item, dict):
            continue
        for content in item.get('content') or []:
            if not isinstance(content, dict):
                continue
            if isinstance(content.get('text'), str):
                chunks.append(content.get('text'))
    return '\n'.join(chunks).strip()


def _parse_ai_pp_openai_reply(text):
    raw = text if isinstance(text, str) else ''
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {
                'tts': str(parsed.get('tts') or parsed.get('TTS') or '').strip(),
                'command': str(parsed.get('command') or parsed.get('Command') or '').strip(),
                'raw': raw
            }
    except Exception:
        pass
    m = re.search(r'\{.*\}', raw, flags=re.S)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return {
                    'tts': str(parsed.get('tts') or parsed.get('TTS') or '').strip(),
                    'command': str(parsed.get('command') or parsed.get('Command') or '').strip(),
                    'raw': raw
                }
        except Exception:
            pass
    tts_match = re.search(r'^\s*\(?TTS\)?\s*:?\s*(.+)$', raw, flags=re.I | re.M)
    cmd_match = re.search(r'^\s*\(?Command\)?\s*:?\s*(.+)$', raw, flags=re.I | re.M)
    return {
        'tts': tts_match.group(1).strip() if tts_match else '',
        'command': cmd_match.group(1).strip() if cmd_match else '',
        'raw': raw
    }


def _sector_summary(sector):
    """Small airspace payload for startup lists; heavy map data is loaded on demand."""
    if not isinstance(sector, dict):
        return {}
    summary = {
        'id': sector.get('id'),
        'name': sector.get('name'),
        'createdAt': sector.get('createdAt'),
        '_summaryOnly': True,
    }
    for key in ('updatedAt',):
        if key in sector:
            summary[key] = sector.get(key)
    return summary


def _merge_sector_summaries(existing, incoming):
    """Preserve full saved airspaces when the client posts summary-only rows."""
    existing_by_id = {
        str(s.get('id')): s
        for s in (existing if isinstance(existing, list) else [])
        if isinstance(s, dict) and s.get('id') is not None
    }
    merged = []
    for sector in incoming if isinstance(incoming, list) else []:
        if not isinstance(sector, dict):
            continue
        sid = sector.get('id')
        sid_key = str(sid) if sid is not None else ''
        if sector.get('_summaryOnly') and sid_key in existing_by_id:
            merged.append(existing_by_id[sid_key])
        else:
            clean = dict(sector)
            clean.pop('_summaryOnly', None)
            merged.append(clean)
    return merged


@app.route('/')
def index():
    """Main index page with navigation buttons"""
    return render_template('index.html')


@app.route('/api/satellite-tile/<int:z>/<int:x>/<int:y>')
def satellite_tile(z, x, y):
    """Proxy a single satellite map tile (Web Mercator XYZ).

    Serving the tile from this origin lets the client composite tiles onto a <canvas> and read pixel
    data without cross-origin taint, which the Edit Airspace map-background warp requires.
    """
    if z < 0 or z > 22:
        return ('Invalid zoom', 400)
    n = 1 << z
    if x < 0 or x >= n or y < 0 or y >= n:
        return ('Tile out of range', 400)
    sub = SATELLITE_TILE_SUBDOMAINS[(x + y) % len(SATELLITE_TILE_SUBDOMAINS)] if SATELLITE_TILE_SUBDOMAINS else ''
    url = SATELLITE_TILE_URL_TEMPLATE.format(s=sub, x=x, y=y, z=z)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; AiTC-RampControl/1.0)',
        'Referer': 'https://www.google.com/maps',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            content_type = resp.headers.get('Content-Type', 'image/jpeg')
    except urllib.error.HTTPError as e:
        return (f'Tile fetch failed: {e.code}', 502)
    except Exception:
        return ('Tile fetch failed', 502)
    return Response(data, mimetype=content_type, headers={'Cache-Control': 'public, max-age=86400'})


@app.route('/manual')
@app.route('/manual/')
def manual_index():
    """AiTC user manual (HTML + CSS)."""
    return send_from_directory(_MANUAL_DIR, 'index.html')


@app.route('/manual/<path:filename>')
def manual_static(filename):
    """CSS and other assets for the manual."""
    return send_from_directory(_MANUAL_DIR, filename)


@app.route('/health')
def health():
    """Health check endpoint"""
    return {'status': 'ok', 'service': 'ATC Simulator'}


@app.route('/api/sectors', methods=['GET'])
def api_get_sectors():
    """Return sectors shared across all clients."""
    data = _read_json(SECTORS_FILE, [])
    sectors = data if isinstance(data, list) else []
    if request.args.get('summary') in ('1', 'true', 'yes'):
        return jsonify({'sectors': [_sector_summary(s) for s in sectors if isinstance(s, dict)]})
    return jsonify({'sectors': sectors})


@app.route('/api/sectors/<sector_id>', methods=['GET'])
def api_get_sector(sector_id):
    """Return one full airspace when the user explicitly loads it."""
    data = _read_json(SECTORS_FILE, [])
    sectors = data if isinstance(data, list) else []
    sid = str(sector_id)
    for sector in sectors:
        if isinstance(sector, dict) and str(sector.get('id')) == sid:
            return jsonify({'sector': sector})
    return jsonify({'error': 'Airspace not found'}), 404


@app.route('/api/sectors', methods=['POST', 'PUT'])
def api_save_sectors():
    """Save sectors (shared across all clients)."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        sectors = body.get('sectors', [])
        if not isinstance(sectors, list):
            sectors = []
        if body.get('mergeSummaries'):
            existing = _read_json(SECTORS_FILE, [])
            sectors = _merge_sector_summaries(existing, sectors)
        _write_json(SECTORS_FILE, sectors)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/exercises', methods=['GET'])
def api_get_exercises():
    """Return exercises shared across all clients."""
    data = _read_json(EXERCISES_FILE, [])
    return jsonify({'exercises': data if isinstance(data, list) else []})


@app.route('/api/exercises', methods=['POST', 'PUT'])
def api_save_exercises():
    """Save exercises (shared across all clients)."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        exercises = body.get('exercises', [])
        if not isinstance(exercises, list):
            exercises = []
        _write_json(EXERCISES_FILE, exercises)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/recordings', methods=['POST'])
def api_save_recording():
    """Persist one completed simulation recording as JSON."""
    try:
        if not _admin_record_simulations_enabled():
            return jsonify({'ok': False, 'error': 'Simulation recording is disabled in Admin settings.'}), 403
        body = request.get_json(force=True, silent=True) or {}
        recording = body.get('recording')
        if not isinstance(recording, dict):
            return jsonify({'ok': False, 'error': 'Invalid recording payload'}), 400
        meta = recording.get('metadata') if isinstance(recording.get('metadata'), dict) else {}
        exercise_name = _safe_filename_part(meta.get('exerciseName'), 'exercise')
        timestamp = _safe_filename_part(
            meta.get('endedAt') or meta.get('startedAt') or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ'),
            'recording'
        )
        filename = f'{exercise_name}_{timestamp}_{uuid.uuid4().hex[:8]}.json'
        _ensure_recordings_dir()
        path = os.path.join(RECORDINGS_DIR, filename)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(recording, f, indent=2, ensure_ascii=False)
        return jsonify({'ok': True, 'filename': filename})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/recordings', methods=['GET'])
def api_list_recordings():
    """List saved simulation recording JSON files."""
    try:
        _ensure_recordings_dir()
        recordings = []
        for filename in os.listdir(RECORDINGS_DIR):
            if not filename.lower().endswith('.json'):
                continue
            path = os.path.join(RECORDINGS_DIR, filename)
            if not os.path.isfile(path):
                continue
            stat = os.stat(path)
            item = {
                'filename': filename,
                'sizeBytes': stat.st_size,
                'modifiedAt': datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'exerciseName': '',
                'startedAt': '',
                'endedAt': '',
                'durationSimSec': None,
                'eventCount': None,
            }
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    rec = json.load(f)
                meta = rec.get('metadata') if isinstance(rec, dict) and isinstance(rec.get('metadata'), dict) else {}
                item.update({
                    'exerciseName': str(meta.get('exerciseName') or ''),
                    'startedAt': str(meta.get('startedAt') or ''),
                    'endedAt': str(meta.get('endedAt') or ''),
                    'durationSimSec': meta.get('durationSimSec'),
                    'eventCount': len(rec.get('events') or []) if isinstance(rec, dict) and isinstance(rec.get('events'), list) else None,
                })
            except Exception:
                pass
            recordings.append(item)
        recordings.sort(key=lambda r: r.get('modifiedAt') or '', reverse=True)
        return jsonify({'recordings': recordings})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/recordings/<path:filename>', methods=['GET'])
def api_get_recording(filename):
    """Load one saved simulation recording."""
    try:
        path = _recording_path(filename)
        if not path or not os.path.isfile(path):
            return jsonify({'ok': False, 'error': 'Recording not found'}), 404
        with open(path, 'r', encoding='utf-8') as f:
            recording = json.load(f)
        return jsonify({'recording': recording})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/recordings/<path:filename>', methods=['DELETE'])
def api_delete_recording(filename):
    """Delete one saved simulation recording."""
    try:
        path = _recording_path(filename)
        if not path or not os.path.isfile(path):
            return jsonify({'ok': False, 'error': 'Recording not found'}), 404
        os.remove(path)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/ai-pp/transcribe/warmup', methods=['POST'])
def api_ai_pp_transcribe_warmup():
    """Pre-load faster-whisper so the first PTT clip does not wait on model init."""
    try:
        if not _ai_pp_ffmpeg_available():
            return jsonify({
                'ok': False,
                'error': 'ffmpeg is not installed or not on PATH. Install ffmpeg for AI_PP voice transcription.',
            }), 503
        future = _ai_pp_transcribe_executor.submit(_get_ai_pp_whisper_model)
        future.result(timeout=AI_PP_TRANSCRIBE_TIMEOUT_SEC)
        return jsonify({'ok': True, 'model': AI_PP_WHISPER_MODEL_SIZE})
    except concurrent.futures.TimeoutError:
        return jsonify({'ok': False, 'error': 'Speech model load timed out.'}), 504
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 503
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/ai-pp/transcribe', methods=['POST'])
def api_ai_pp_transcribe():
    """Transcribe one AI_PP push-to-talk audio clip with faster-whisper."""
    temp_path = None
    try:
        content_length = request.content_length or 0
        if content_length and content_length > AI_PP_MAX_AUDIO_BYTES:
            return jsonify({'ok': False, 'error': 'Audio clip is too large'}), 413
        audio = request.files.get('audio')
        if not audio:
            return jsonify({'ok': False, 'error': 'No audio file uploaded'}), 400
        filename = audio.filename or 'ai_pp_audio.webm'
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ('.webm', '.wav', '.mp3', '.m4a', '.ogg'):
            ext = '.webm'
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            temp_path = tmp.name
            audio.save(tmp)
        if not os.path.getsize(temp_path):
            return jsonify({'ok': False, 'error': 'Empty audio clip'}), 400
        if not _ai_pp_ffmpeg_available():
            return jsonify({
                'ok': False,
                'error': 'ffmpeg is not installed or not on PATH. Install ffmpeg for AI_PP voice transcription.',
            }), 503
        future = _ai_pp_transcribe_executor.submit(_transcribe_ai_pp_audio_file, temp_path)
        try:
            result = future.result(timeout=AI_PP_TRANSCRIBE_TIMEOUT_SEC)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return jsonify({
                'ok': False,
                'error': 'Transcription timed out. Try a shorter clip or check faster-whisper/ffmpeg setup.',
            }), 504
        return jsonify({'ok': True, **result})
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 503
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except Exception:
                pass


@app.route('/api/flows', methods=['GET'])
def api_get_flows_library():
    """Named flow packages (per airspace), stored in flows.json."""
    data = _read_json(FLOWS_LIBRARY_FILE, [])
    return jsonify({'flows': data if isinstance(data, list) else []})


@app.route('/api/flows', methods=['POST', 'PUT'])
def api_save_flows_library():
    """Append a flow package or replace the full library."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        if isinstance(body.get('append'), dict):
            entry = body['append']
            flows_list = _read_json(FLOWS_LIBRARY_FILE, [])
            if not isinstance(flows_list, list):
                flows_list = []
            pkg_name = str(entry.get('packageName') or '').strip() or 'Untitled'
            sector_id = str(entry.get('sectorId') or '').strip()
            sector_name = str(entry.get('sectorName') or '').strip()
            raw_flows = entry.get('flows')
            flows_payload = raw_flows if isinstance(raw_flows, list) else []
            new_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:10]}"
            package = {
                'id': new_id,
                'packageName': pkg_name,
                'sectorId': sector_id,
                'sectorName': sector_name,
                'savedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'flows': copy.deepcopy(flows_payload),
            }
            flows_list.append(package)
            _write_json(FLOWS_LIBRARY_FILE, flows_list)
            return jsonify({'ok': True, 'id': new_id, 'flows': flows_list})
        flows_list = body.get('flows', [])
        if not isinstance(flows_list, list):
            flows_list = []
        _write_json(FLOWS_LIBRARY_FILE, flows_list)
        return jsonify({'ok': True, 'flows': flows_list})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/aircraft-db-overrides', methods=['GET'])
def api_get_aircraft_db_overrides():
    """Return aircraft DB overrides (raw text) shared across all clients."""
    data = _read_json(AIRCRAFT_OVERRIDES_FILE, {})
    raw = data.get('raw', '') if isinstance(data, dict) else ''
    return jsonify({'raw': raw})


@app.route('/api/aircraft-db-overrides', methods=['POST', 'PUT'])
def api_save_aircraft_db_overrides():
    """Save aircraft DB overrides (shared across all clients)."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        raw = body.get('raw', '')
        if not isinstance(raw, str):
            raw = ''
        _write_json(AIRCRAFT_OVERRIDES_FILE, {'raw': raw})
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


def _normalize_airline_callsigns_overrides_payload(data):
    if not isinstance(data, dict):
        return {'overrides': {}, 'deletions': []}
    overrides_raw = data.get('overrides')
    deletions_raw = data.get('deletions')
    overrides = {}
    if isinstance(overrides_raw, dict):
        for key, val in overrides_raw.items():
            code = str(key or '').strip().upper()
            callsign = str(val or '').strip()
            if code and callsign:
                overrides[code] = callsign
    deletions = []
    if isinstance(deletions_raw, list):
        seen = set()
        for item in deletions_raw:
            code = str(item or '').strip().upper()
            if code and code not in seen:
                seen.add(code)
                deletions.append(code)
    return {'overrides': overrides, 'deletions': deletions}


@app.route('/api/airline-callsigns-overrides', methods=['GET'])
def api_get_airline_callsigns_overrides():
    """Return airline callsign overrides shared across all clients."""
    data = _read_json(AIRLINE_CALLSIGNS_OVERRIDES_FILE, {})
    payload = _normalize_airline_callsigns_overrides_payload(data)
    return jsonify(payload)


@app.route('/api/airline-callsigns-overrides', methods=['POST', 'PUT'])
def api_save_airline_callsigns_overrides():
    """Save airline callsign overrides (shared across all clients)."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        payload = _normalize_airline_callsigns_overrides_payload(body)
        _write_json(AIRLINE_CALLSIGNS_OVERRIDES_FILE, payload)
        return jsonify({'ok': True, **payload})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/ai-pp/session/start', methods=['POST'])
def api_ai_pp_session_start():
    """Start an AI_PP OpenAI response session with Admin + exercise context."""
    try:
        body = request.get_json(silent=True) or {}
        admin = _read_admin_settings()
        api_key = (admin.get('openaiApiKey') or '').strip()
        if not api_key:
            return jsonify({'ok': False, 'error': 'OpenAI API Key is not configured in ADMIN settings.'}), 400

        exercise_id = body.get('exerciseId')
        on_frequency_codes = body.get('onFrequencyCallsigns')
        if not isinstance(on_frequency_codes, list):
            on_frequency_codes = []
        instructions = _build_ai_pp_session_instructions(admin, exercise_id)
        payload = _ai_pp_openai_payload(
            admin,
            _ai_pp_seed_conversation_input(
                exercise_id,
                on_frequency_codes=on_frequency_codes,
                session_instructions=instructions,
            ),
        )
        data = _openai_responses_request(api_key, payload)
        previous_response_id = data.get('id')
        if not previous_response_id:
            return jsonify({'ok': False, 'error': 'OpenAI did not return a response id.'}), 400
        session_id = uuid.uuid4().hex
        with _ai_pp_openai_lock:
            _ai_pp_openai_sessions[session_id] = {
                'previousResponseId': previous_response_id,
                'exerciseId': str(exercise_id or ''),
                'instructionsLength': len(instructions),
                'createdAt': time.time(),
                'updatedAt': time.time()
            }
        return jsonify({'ok': True, 'sessionId': session_id, 'instructionsLength': len(instructions)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/ai-pp/respond', methods=['POST'])
def api_ai_pp_respond():
    """Send one transcribed controller command to the active AI_PP OpenAI session."""
    try:
        body = request.get_json(silent=True) or {}
        session_id = (body.get('sessionId') or '').strip()
        transcript = (body.get('text') or '').strip()
        if not session_id:
            return jsonify({'ok': False, 'error': 'AI_PP session is not initialized.'}), 400
        if not transcript:
            return jsonify({'ok': False, 'error': 'No transcribed command to send.'}), 400

        admin = _read_admin_settings()
        api_key = (admin.get('openaiApiKey') or '').strip()
        if not api_key:
            return jsonify({'ok': False, 'error': 'OpenAI API Key is not configured in ADMIN settings.'}), 400

        with _ai_pp_openai_lock:
            sess = _ai_pp_openai_sessions.get(session_id)
        if not sess:
            return jsonify({'ok': False, 'error': 'AI_PP session expired. Turn AI_PP off and on again.'}), 404

        previous_response_id = sess.get('previousResponseId')
        if not previous_response_id:
            return jsonify({'ok': False, 'error': 'AI_PP session is missing OpenAI context. Turn AI_PP off and on again.'}), 400
        mode = (body.get('mode') or '').strip().lower()
        live_state = (body.get('liveState') or '').strip()
        refresh_instructions = None
        if mode == 'pttprime':
            prime_exercise_id = sess.get('exerciseId') or body.get('exerciseId')
            refresh_instructions = _build_ai_pp_general_instructions_with_context(admin, prime_exercise_id)
            user_input = transcript or (
                '[PTT PRESSED — AI Pilot General Instructions refreshed for the upcoming controller '
                'transmission. Respond {"tts":"","command":""} now.]'
            )
        elif mode == 'onfrequencyevent':
            user_input = transcript
        elif mode == 'script':
            user_input = transcript
        else:
            user_input = f'Controller transmission: {transcript}'
            if live_state:
                user_input = f'{user_input}\n{live_state}'
        payload = _ai_pp_openai_payload(
            admin,
            user_input,
            previous_response_id=previous_response_id,
            instructions=refresh_instructions,
        )
        data = _openai_responses_request(api_key, payload)
        parsed = _parse_ai_pp_openai_reply(_extract_openai_response_text(data))
        if mode == 'pttprime':
            parsed = {'tts': '', 'command': '', 'raw': '{"tts":"","command":""}'}
        with _ai_pp_openai_lock:
            if session_id in _ai_pp_openai_sessions:
                _ai_pp_openai_sessions[session_id]['previousResponseId'] = data.get('id')
                _ai_pp_openai_sessions[session_id]['updatedAt'] = time.time()
        extra = {'pttPrimed': True} if mode == 'pttprime' else {}
        return jsonify({'ok': True, **extra, **parsed})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/ai-pp/session/end', methods=['POST'])
def api_ai_pp_session_end():
    """Delete an AI_PP OpenAI session. Called when the user stops the run or turns AI Pilot off,
    so server-side session state (and the OpenAI response chain reference) does not leak."""
    try:
        body = request.get_json(silent=True) or {}
        session_id = (body.get('sessionId') or '').strip()
        removed = False
        with _ai_pp_openai_lock:
            if session_id and session_id in _ai_pp_openai_sessions:
                _ai_pp_openai_sessions.pop(session_id, None)
                removed = True
        return jsonify({'ok': True, 'removed': removed})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/ai-pilot/transform-command', methods=['POST'])
def api_ai_pilot_transform_command():
    """Convert natural-language / draft text into AI Pilot machine command format."""
    try:
        body = request.get_json(silent=True) or {}
        callsign = (body.get('callsign') or '').strip()
        input_text = (body.get('inputText') or body.get('text') or '').strip()
        if not callsign:
            return jsonify({'ok': False, 'error': 'Callsign is required.'}), 400
        if not input_text:
            return jsonify({'ok': False, 'error': 'Command input text is required.'}), 400

        admin = _read_admin_settings()
        api_key = (admin.get('openaiApiKey') or '').strip()
        if not api_key:
            return jsonify({'ok': False, 'error': 'OpenAI API Key is not configured in ADMIN settings.'}), 400

        instructions = _build_transform_command_instructions(admin)
        user_input = _build_transform_command_user_input(callsign, input_text)
        payload = _ai_pilot_transform_command_payload(admin, instructions, user_input)
        data = _openai_responses_request(api_key, payload)
        parsed = _parse_ai_pp_openai_reply(_extract_openai_response_text(data))
        command = str(parsed.get('command') or '').strip()
        return jsonify({'ok': True, 'command': command, 'raw': parsed.get('raw') or ''})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/admin/public-config', methods=['GET'])
def api_admin_public_config():
    """Non-sensitive admin flags needed by the client before Admin unlock."""
    try:
        data = _read_admin_settings()
        return jsonify({
            'recordSimulations': _admin_record_simulations_enabled(data),
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/admin/unlock', methods=['POST'])
def api_admin_unlock():
    """Verify admin password and return current admin settings."""
    try:
        body = request.get_json(silent=True) or {}
        if not _admin_password_ok(body.get('password', '')):
            return jsonify({'ok': False, 'error': 'Invalid admin password'}), 403
        data = _read_admin_settings()
        return jsonify({
            'ok': True,
            'openaiApiKey': data.get('openaiApiKey') or '',
            'openaiModel': _get_admin_openai_model(data),
            'aiPilotGeneralInstructions': data.get('aiPilotGeneralInstructions') or DEFAULT_AI_PILOT_GENERAL_INSTRUCTIONS,
            'defaultAiPilotGeneralInstructions': DEFAULT_AI_PILOT_GENERAL_INSTRUCTIONS,
            'recordSimulations': _admin_record_simulations_enabled(data),
            'globalLabelLayout': data.get('globalLabelLayout') or _default_global_label_layout(),
            'globalLabelSetupId': data.get('globalLabelSetupId') or '',
            'globalLabelSetups': data.get('globalLabelSetups') or [],
            'singleUserLabelProfiles': data.get('singleUserLabelProfiles') or [],
            'singleUserLabelAssignedProfileId': data.get('singleUserLabelAssignedProfileId') or '',
            'hostedSessionsLabelProfiles': data.get('hostedSessionsLabelProfiles') or [],
            'hostedSessionsLabelAssignedProfileId': data.get('hostedSessionsLabelAssignedProfileId') or '',
            'defaultGenericLabelSetupId': data.get('defaultGenericLabelSetupId') or '',
            'defaultSingleUserLabelProfileId': data.get('defaultSingleUserLabelProfileId') or '',
            'defaultHostedSessionsLabelProfileId': data.get('defaultHostedSessionsLabelProfileId') or '',
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/label-simulation-config', methods=['GET'])
def api_label_simulation_config():
    """Label setups, profiles, and active simulation defaults (no admin password)."""
    try:
        data = _read_admin_settings()
        return jsonify({
            'defaultGenericLabelSetupId': data.get('defaultGenericLabelSetupId') or '',
            'defaultSingleUserLabelProfileId': data.get('defaultSingleUserLabelProfileId') or '',
            'defaultHostedSessionsLabelProfileId': data.get('defaultHostedSessionsLabelProfileId') or '',
            'globalLabelSetups': data.get('globalLabelSetups') or [],
            'singleUserLabelProfiles': data.get('singleUserLabelProfiles') or [],
            'hostedSessionsLabelProfiles': data.get('hostedSessionsLabelProfiles') or [],
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/admin/label-simulation-defaults', methods=['POST', 'PUT'])
def api_save_admin_label_simulation_defaults():
    """Commit Generic / Single User / Hosted Sessions selections as simulation defaults."""
    try:
        body = request.get_json(silent=True) or {}
        if not _admin_password_ok(body.get('currentPassword', '')):
            return jsonify({'ok': False, 'error': 'Invalid admin password'}), 403
        data = _read_admin_settings()
        if 'defaultGenericLabelSetupId' in body:
            gid = body.get('defaultGenericLabelSetupId', '')
            data['defaultGenericLabelSetupId'] = gid.strip() if isinstance(gid, str) else ''
        if 'defaultSingleUserLabelProfileId' in body:
            sid = body.get('defaultSingleUserLabelProfileId', '')
            data['defaultSingleUserLabelProfileId'] = sid.strip() if isinstance(sid, str) else ''
        if 'defaultHostedSessionsLabelProfileId' in body:
            hid = body.get('defaultHostedSessionsLabelProfileId', '')
            data['defaultHostedSessionsLabelProfileId'] = hid.strip() if isinstance(hid, str) else ''
        data['updatedAt'] = datetime.now(timezone.utc).isoformat()
        _write_json(ADMIN_SETTINGS_FILE, data)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/admin/single-user-label-config', methods=['POST', 'PUT'])
def api_save_admin_single_user_label_config():
    """Save Single User label configuration profiles (Admin)."""
    try:
        body = request.get_json(silent=True) or {}
        if not _admin_password_ok(body.get('currentPassword', '')):
            return jsonify({'ok': False, 'error': 'Invalid admin password'}), 403
        data = _read_admin_settings()
        if 'singleUserLabelProfiles' in body:
            data['singleUserLabelProfiles'] = _normalize_single_user_label_profiles(body.get('singleUserLabelProfiles'))
        if 'singleUserLabelAssignedProfileId' in body:
            assigned = body.get('singleUserLabelAssignedProfileId', '')
            data['singleUserLabelAssignedProfileId'] = assigned.strip() if isinstance(assigned, str) else ''
        data['updatedAt'] = datetime.now(timezone.utc).isoformat()
        _write_json(ADMIN_SETTINGS_FILE, data)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/admin/hosted-sessions-label-config', methods=['POST', 'PUT'])
def api_save_admin_hosted_sessions_label_config():
    """Save Hosted Sessions label configuration profiles (Admin)."""
    try:
        body = request.get_json(silent=True) or {}
        if not _admin_password_ok(body.get('currentPassword', '')):
            return jsonify({'ok': False, 'error': 'Invalid admin password'}), 403
        data = _read_admin_settings()
        if 'hostedSessionsLabelProfiles' in body:
            data['hostedSessionsLabelProfiles'] = _normalize_hosted_sessions_label_profiles(body.get('hostedSessionsLabelProfiles'))
        if 'hostedSessionsLabelAssignedProfileId' in body:
            assigned = body.get('hostedSessionsLabelAssignedProfileId', '')
            data['hostedSessionsLabelAssignedProfileId'] = assigned.strip() if isinstance(assigned, str) else ''
        data['updatedAt'] = datetime.now(timezone.utc).isoformat()
        _write_json(ADMIN_SETTINGS_FILE, data)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/admin/global-aircraft-label', methods=['POST', 'PUT'])
def api_save_admin_global_aircraft_label():
    """Save global aircraft label layout and named setups (Admin)."""
    try:
        body = request.get_json(silent=True) or {}
        if not _admin_password_ok(body.get('currentPassword', '')):
            return jsonify({'ok': False, 'error': 'Invalid admin password'}), 403
        data = _read_admin_settings()
        if 'labelLayout' in body:
            data['globalLabelLayout'] = _normalize_label_layout(body.get('labelLayout'))
        if 'labelSetupId' in body:
            setup_id = body.get('labelSetupId', '')
            data['globalLabelSetupId'] = setup_id.strip() if isinstance(setup_id, str) else ''
        if 'labelSetups' in body:
            data['globalLabelSetups'] = _normalize_label_setups(body.get('labelSetups'))
        data['updatedAt'] = datetime.now(timezone.utc).isoformat()
        _write_json(ADMIN_SETTINGS_FILE, data)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/admin/settings', methods=['POST', 'PUT'])
def api_save_admin_settings():
    """Save admin password/API key. Requires current admin password."""
    try:
        body = request.get_json(silent=True) or {}
        current_password = body.get('currentPassword', '')
        if not _admin_password_ok(current_password):
            return jsonify({'ok': False, 'error': 'Invalid admin password'}), 403
        data = _read_admin_settings()
        new_password = (body.get('newPassword') or '').strip()
        if new_password:
            if len(new_password) < 6:
                return jsonify({'ok': False, 'error': 'New password must be at least 6 characters'}), 400
            data['passwordHash'] = generate_password_hash(new_password)
        if 'openaiApiKey' in body:
            openai_key = body.get('openaiApiKey', '')
            data['openaiApiKey'] = openai_key if isinstance(openai_key, str) else ''
        if 'aiPilotGeneralInstructions' in body:
            instructions = body.get('aiPilotGeneralInstructions', '')
            data['aiPilotGeneralInstructions'] = instructions if isinstance(instructions, str) else DEFAULT_AI_PILOT_GENERAL_INSTRUCTIONS
        if 'openaiModel' in body:
            data['openaiModel'] = _normalize_openai_model(body.get('openaiModel'))
        if 'recordSimulations' in body:
            data['recordSimulations'] = bool(body.get('recordSimulations'))
        data['updatedAt'] = datetime.now(timezone.utc).isoformat()
        _write_json(ADMIN_SETTINGS_FILE, data)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/turn-rate-bands', methods=['GET'])
def api_get_turn_rate_bands():
    """Return turn rate bands (turn radius config) shared across all clients."""
    data = _read_json(TURN_RATE_BANDS_FILE, [])
    bands = data if isinstance(data, list) else []
    return jsonify({'bands': bands})


@app.route('/api/turn-rate-bands', methods=['POST', 'PUT'])
def api_save_turn_rate_bands():
    """Save turn rate bands (shared across all clients)."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        bands = body.get('bands', [])
        if not isinstance(bands, list):
            bands = []
        _write_json(TURN_RATE_BANDS_FILE, bands)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/sim-settings-presets', methods=['GET'])
def api_get_sim_settings_presets():
    """Return all named sim settings presets (shared across all clients)."""
    data = _read_json(SIM_SETTINGS_PRESETS_FILE, [])
    presets = data if isinstance(data, list) else []
    return jsonify({'presets': presets})


@app.route('/api/sim-settings-presets', methods=['POST'])
def api_save_sim_settings_preset():
    """Save a new named preset (shared across all clients). Body: { "name": "...", "settings": { ... } }."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        name = (body.get('name') or '').strip()
        if not name:
            return jsonify({'ok': False, 'error': 'Name is required'}), 400
        settings = body.get('settings')
        if not isinstance(settings, dict):
            return jsonify({'ok': False, 'error': 'settings object is required'}), 400
        presets = _read_json(SIM_SETTINGS_PRESETS_FILE, [])
        if not isinstance(presets, list):
            presets = []
        import time
        preset_id = str(int(time.time() * 1000))
        presets.append({
            'id': preset_id,
            'name': name,
            'createdAt': preset_id,
            'settings': settings
        })
        _write_json(SIM_SETTINGS_PRESETS_FILE, presets)
        return jsonify({'ok': True, 'id': preset_id})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/sim-settings-presets/<preset_id>', methods=['DELETE'])
def api_delete_sim_settings_preset(preset_id):
    """Remove a saved preset by id."""
    try:
        pid = (preset_id or '').strip()
        if not pid:
            return jsonify({'ok': False, 'error': 'id required'}), 400
        presets = _read_json(SIM_SETTINGS_PRESETS_FILE, [])
        if not isinstance(presets, list):
            presets = []
        new_presets = [p for p in presets if not (isinstance(p, dict) and str(p.get('id')) == pid)]
        if len(new_presets) == len(presets):
            return jsonify({'ok': False, 'error': 'Preset not found'}), 404
        _write_json(SIM_SETTINGS_PRESETS_FILE, new_presets)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/en-mgmt/routes', methods=['GET'])
def api_list_en_mgmt_routes():
    """List saved EN-MGMT route file names (without .json extension)."""
    return jsonify({'ok': True, 'files': _list_en_mgmt_route_files()})


@app.route('/api/en-mgmt/routes/<path:filename>', methods=['GET', 'DELETE'])
def api_en_mgmt_routes_file(filename):
    """Load or delete a saved EN-MGMT routes file."""
    path, safe_name = _en_mgmt_routes_file_path(filename)
    if not path or not safe_name:
        return jsonify({'ok': False, 'error': 'Invalid file name'}), 400

    if request.method == 'DELETE':
        if not os.path.isfile(path):
            return jsonify({'ok': False, 'error': 'File not found'}), 404
        try:
            os.remove(path)
            return jsonify({'ok': True, 'name': safe_name[:-5]})
        except OSError as e:
            return jsonify({'ok': False, 'error': str(e)}), 400

    data = _read_json(path, None)
    if data is None:
        return jsonify({'ok': False, 'error': 'File not found'}), 404
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'Invalid file format'}), 400
    return jsonify({'ok': True, 'name': safe_name[:-5], 'data': data})


@app.route('/api/en-mgmt/routes', methods=['POST'])
def api_save_en_mgmt_routes():
    """Save EN-MGMT routes to EN-MGMT/<name>.json. Body: { name, overwrite?, data }."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        name = (body.get('name') or '').strip()
        if not name:
            return jsonify({'ok': False, 'error': 'Name is required'}), 400
        path, safe_name = _en_mgmt_routes_file_path(name)
        if not path or not safe_name:
            return jsonify({'ok': False, 'error': 'Invalid file name'}), 400
        payload = body.get('data')
        if not isinstance(payload, dict):
            return jsonify({'ok': False, 'error': 'data object is required'}), 400
        overwrite = bool(body.get('overwrite'))
        if os.path.isfile(path) and not overwrite:
            return jsonify({'ok': False, 'error': 'File already exists', 'exists': True}), 409
        save_doc = {
            'version': 1,
            'savedAt': datetime.now(timezone.utc).isoformat(),
            'routes': payload.get('routes') if isinstance(payload.get('routes'), list) else [],
            'mergeGroups': payload.get('mergeGroups') if isinstance(payload.get('mergeGroups'), list) else [],
            'routeNextId': payload.get('routeNextId'),
            'mergeGroupNextId': payload.get('mergeGroupNextId'),
        }
        _ensure_en_mgmt_dir()
        _write_json(path, save_doc)
        return jsonify({'ok': True, 'name': safe_name[:-5], 'filename': safe_name})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


# --- Simulation sessions (Host / Join) ---

@app.route('/api/sessions', methods=['GET'])
def api_list_sessions():
    """List active sessions (for Join). Includes sessions before first /state PUT so the list is not empty during host startup."""
    _prune_stale_host_sessions()
    _join_slots_prune_stale_global()
    out = []
    for sid, s in list(_sessions.items()):
        out.append({
            'id': sid,
            'name': s.get('name', ''),
            'mode': s.get('mode', 'DL'),
            'exerciseName': s.get('exerciseName', ''),
            'sectorName': s.get('sectorName', ''),
            'hasState': bool(s.get('state')),
        })
    return jsonify({'sessions': out})


@app.route('/api/sessions/check-name', methods=['GET'])
def api_check_session_name():
    """Check if a session name is available (no duplicate active names)."""
    name = (request.args.get('name') or '').strip()
    if not name:
        return jsonify({'available': False, 'error': 'Name is required'}), 400
    for s in _sessions.values():
        if (s.get('name') or '').strip().lower() == name.lower():
            return jsonify({'available': False})
    return jsonify({'available': True})


@app.route('/api/sessions', methods=['POST'])
def api_create_session():
    """Create a new hosted session. Body: name, exerciseId, sectorId, exerciseName, sectorName. Mode is always DL."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        name = (body.get('name') or '').strip()
        if not name:
            return jsonify({'ok': False, 'error': 'Session name is required'}), 400
        for s in _sessions.values():
            if (s.get('name') or '').strip().lower() == name.lower():
                return jsonify({'ok': False, 'error': 'Session name already in use'}), 409
        session_id = str(uuid.uuid4())
        now = time.time()
        # Hosted sessions are always DL (host full control); client cannot override.
        _sessions[session_id] = {
            'id': session_id,
            'name': name,
            'mode': 'DL',
            'exerciseId': body.get('exerciseId'),
            'sectorId': body.get('sectorId'),
            'exerciseName': body.get('exerciseName', ''),
            'sectorName': body.get('sectorName', ''),
            'createdAt': now,
            'lastHostActivityAt': now,
            'state': None,
            'hostSessionRestartSeq': 0,
        }
        return jsonify({'ok': True, 'sessionId': session_id, 'name': name, 'mode': _sessions[session_id]['mode']})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


class JoinSlotClaimConflict(Exception):
    """Raised when an exclusive role is already held by another client."""


@app.route('/api/sessions/<session_id>/join-slots', methods=['GET'])
def api_get_join_slots(session_id):
    """List active exclusive slots; pass ?clientId= so UI can mark occupiedByOther."""
    if session_id not in _sessions:
        return jsonify({'error': 'Session not found'}), 404
    viewer = (request.args.get('clientId') or '').strip()

    def work(conn):
        now = time.time()
        _join_slots_prune(conn, now)
        cur = conn.execute(
            'SELECT atc_sector_id, role, client_id FROM session_join_slots WHERE session_id = ?',
            (session_id,),
        )
        taken = []
        for atc_id, role, cid in cur.fetchall():
            if role not in JOIN_SLOT_EXCLUSIVE_ROLES:
                continue
            cid = cid or ''
            taken.append({
                'atcSectorId': atc_id,
                'role': role,
                'clientId': cid,
                'occupiedByOther': bool(cid and cid != viewer),
            })
        cur2 = conn.execute(
            'SELECT atc_sector_id, client_id FROM session_pp_presence WHERE session_id = ? AND (? - last_seen) <= ?',
            (session_id, now, JOIN_SLOT_TTL_SEC),
        )
        pp_presence = []
        pp_sectors_set = set()
        for atc_id, cid in cur2.fetchall():
            if not atc_id or not cid:
                continue
            pp_presence.append({'atcSectorId': atc_id, 'clientId': cid})
            pp_sectors_set.add(atc_id)
        pp_sectors = list(pp_sectors_set)
        conn.commit()
        return taken, pp_sectors, pp_presence

    taken, pp_sectors, pp_presence = _join_slots_run(work)
    return jsonify({'taken': taken, 'ppSectors': pp_sectors, 'ppPresence': pp_presence})


@app.route('/api/sessions/<session_id>/join-slots/claim', methods=['POST'])
def api_claim_join_slot(session_id):
    """Reserve an exclusive role (one holder per session+sector+role). OBS and PP do not use DB."""
    if session_id not in _sessions:
        return jsonify({'ok': False, 'error': 'Session not found'}), 404
    body = request.get_json(force=True, silent=True) or {}
    client_id = (body.get('clientId') or '').strip()
    atc_sector_id = (body.get('atcSectorId') or '').strip()
    role = (body.get('role') or '').strip().upper()
    if not client_id or not atc_sector_id or not role:
        return jsonify({'ok': False, 'error': 'clientId, atcSectorId and role are required'}), 400
    if role not in JOIN_SLOT_ALLOWED_ROLES:
        return jsonify({'ok': False, 'error': 'Invalid role'}), 400
    if role in JOIN_SLOT_NON_EXCLUSIVE_ROLES:
        return jsonify({'ok': True})

    try:
        def work(conn):
            now = time.time()
            _join_slots_prune(conn, now)
            cur = conn.execute(
                '''SELECT client_id, last_seen FROM session_join_slots
                   WHERE session_id = ? AND atc_sector_id = ? AND role = ?''',
                (session_id, atc_sector_id, role),
            )
            row = cur.fetchone()
            if row:
                other_id, last_seen = row[0], float(row[1] or 0)
                if other_id and other_id != client_id and (now - last_seen) <= JOIN_SLOT_TTL_SEC:
                    raise JoinSlotClaimConflict()
            conn.execute(
                '''INSERT OR REPLACE INTO session_join_slots
                   (session_id, atc_sector_id, role, client_id, last_seen)
                   VALUES (?, ?, ?, ?, ?)''',
                (session_id, atc_sector_id, role, client_id, now),
            )
            conn.commit()

        _join_slots_run(work)
        if role == 'EXE':
            with _atm_trajectory_lock:
                b = _atm_trajectory_by_session.setdefault(session_id, {'masterClientId': None, 'patches': {}})
                if not b.get('masterClientId'):
                    b['masterClientId'] = client_id
        return jsonify({'ok': True})
    except JoinSlotClaimConflict:
        return jsonify({'ok': False, 'error': 'Role already taken'}), 409
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/sessions/<session_id>/join-slots/heartbeat', methods=['POST'])
def api_heartbeat_join_slot(session_id):
    """Refresh lease for an exclusive slot."""
    if session_id not in _sessions:
        return jsonify({'ok': False, 'error': 'Session not found'}), 404
    body = request.get_json(force=True, silent=True) or {}
    client_id = (body.get('clientId') or '').strip()
    atc_sector_id = (body.get('atcSectorId') or '').strip()
    role = (body.get('role') or '').strip().upper()
    if not client_id:
        return jsonify({'ok': False, 'error': 'clientId required'}), 400
    if role in JOIN_SLOT_NON_EXCLUSIVE_ROLES or role not in JOIN_SLOT_EXCLUSIVE_ROLES:
        return jsonify({'ok': True})

    def work(conn):
        now = time.time()
        _join_slots_prune(conn, now)
        cur = conn.execute(
            '''UPDATE session_join_slots SET last_seen = ?
               WHERE session_id = ? AND atc_sector_id = ? AND role = ? AND client_id = ?''',
            (now, session_id, atc_sector_id, role, client_id),
        )
        conn.commit()
        return cur.rowcount > 0

    ok = _join_slots_run(work)
    if not ok:
        return jsonify({'ok': False, 'error': 'No active claim'}), 404
    return jsonify({'ok': True})


@app.route('/api/sessions/<session_id>/join-slots/release', methods=['POST'])
def api_release_join_slots(session_id):
    """Release all exclusive slots for this client in this session."""
    body = request.get_json(force=True, silent=True) or {}
    client_id = (body.get('clientId') or '').strip()
    if not client_id:
        return jsonify({'ok': False, 'error': 'clientId required'}), 400

    def work(conn):
        conn.execute(
            'DELETE FROM session_join_slots WHERE session_id = ? AND client_id = ?',
            (session_id, client_id),
        )
        conn.execute(
            'DELETE FROM session_pp_presence WHERE session_id = ? AND client_id = ?',
            (session_id, client_id),
        )
        conn.commit()

    _join_slots_run(work)
    return jsonify({'ok': True})


@app.route('/api/sessions/<session_id>/join-slots/pp-presence', methods=['POST'])
def api_pp_presence_heartbeat(session_id):
    """PP (and similar) clients heartbeat so EXE can know a PP is active on this ATC sector."""
    if session_id not in _sessions:
        return jsonify({'ok': False, 'error': 'Session not found'}), 404
    body = request.get_json(force=True, silent=True) or {}
    client_id = (body.get('clientId') or '').strip()
    atc_sector_id = (body.get('atcSectorId') or '').strip()
    if not client_id or not atc_sector_id:
        return jsonify({'ok': False, 'error': 'clientId and atcSectorId are required'}), 400
    raw_eligible = body.get('eligibleAircraftIds')
    eligible_aids = raw_eligible if isinstance(raw_eligible, list) else []

    def work(conn):
        now = time.time()
        _join_slots_prune(conn, now)
        conn.execute(
            '''INSERT OR REPLACE INTO session_pp_presence
               (session_id, atc_sector_id, client_id, last_seen)
               VALUES (?, ?, ?, ?)''',
            (session_id, atc_sector_id, client_id, now),
        )
        peers = _get_pp_peer_client_ids_on_sector(conn, session_id, atc_sector_id, now)
        conn.commit()
        return peers

    peers = _join_slots_run(work)
    assignments = _update_pp_workload_assignments(session_id, atc_sector_id, peers, eligible_aids)
    return jsonify({
        'ok': True,
        'ppPeersOnSector': peers,
        'atcSectorId': atc_sector_id,
        'ppWorkloadAssignments': assignments,
    })


@app.route('/api/sessions/<session_id>/pp-vertical', methods=['POST'])
def api_pp_vertical_push(session_id):
    """PP client: optional vertical keys (currently none); speed/ALV use pp-lateral."""
    if session_id not in _sessions:
        return jsonify({'ok': False, 'error': 'Session not found'}), 404
    body = request.get_json(force=True, silent=True) or {}
    client_id = (body.get('clientId') or '').strip()
    aircraft = body.get('aircraft')
    if not client_id:
        return jsonify({'ok': False, 'error': 'clientId required'}), 400
    if not isinstance(aircraft, dict):
        return jsonify({'ok': False, 'error': 'aircraft map required'}), 400

    with _pp_vertical_lock:
        bucket = _pp_vertical_by_session.setdefault(session_id, {})
        for aid, patch in aircraft.items():
            if not isinstance(patch, dict):
                continue
            key = str(aid)
            cleaned = {k: v for k, v in patch.items() if k in PP_VERTICAL_KEYS}
            if not cleaned:
                continue
            prev = bucket.get(key)
            if isinstance(prev, dict):
                prev.update(cleaned)
                bucket[key] = prev
            else:
                bucket[key] = cleaned
    return jsonify({'ok': True})


def _clean_pp_lateral_aircraft_patch(patch):
    """Keep cmdSeq + cmd object with allowed type."""
    if not isinstance(patch, dict):
        return None
    out = {}
    try:
        out['cmdSeq'] = int(patch.get('cmdSeq'))
    except (TypeError, ValueError):
        return None
    cmd = patch.get('cmd')
    if not isinstance(cmd, dict):
        return None
    ctype = (cmd.get('type') or '').strip().upper()
    if ctype not in PP_LATERAL_CMD_TYPES:
        return None
    cleaned_cmd = {'type': ctype}
    if ctype == 'DCT':
        try:
            cleaned_cmd['wpIdx'] = int(cmd.get('wpIdx'))
        except (TypeError, ValueError):
            return None
        cleaned_cmd['immediate'] = bool(cmd.get('immediate', True))
    elif ctype == 'HDG':
        try:
            cleaned_cmd['headingDeg'] = int(cmd.get('headingDeg'))
        except (TypeError, ValueError):
            return None
        cleaned_cmd['immediate'] = bool(cmd.get('immediate', True))
    elif ctype == 'DCT_PATH':
        pts = cmd.get('pathPoints')
        if not isinstance(pts, list):
            return None
        path_points = []
        for p in pts:
            if not isinstance(p, dict):
                continue
            try:
                lat = float(p.get('lat'))
                lon = float(p.get('lon'))
            except (TypeError, ValueError):
                continue
            if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
                continue
            name = p.get('name')
            path_points.append({
                'lat': lat,
                'lon': lon,
                'name': (name if isinstance(name, str) else '') or '',
            })
        if len(pts) > 0 and not path_points:
            return None
        try:
            rj = int(cmd.get('rejoinPointIdx'))
        except (TypeError, ValueError):
            return None
        cleaned_cmd['pathPoints'] = path_points
        cleaned_cmd['rejoinPointIdx'] = rj
    elif ctype == 'APPLY_PATH':
        pts = cmd.get('pathPoints')
        if not isinstance(pts, list) or not pts:
            return None
        path_points = []
        for p in pts:
            if not isinstance(p, dict):
                continue
            try:
                lat = float(p.get('lat'))
                lon = float(p.get('lon'))
            except (TypeError, ValueError):
                continue
            if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
                continue
            name = p.get('name')
            path_points.append({
                'lat': lat,
                'lon': lon,
                'name': (name if isinstance(name, str) else '') or '',
            })
        if not path_points:
            return None
        cleaned_cmd['pathPoints'] = path_points
    elif ctype == 'ALV':
        try:
            tf = int(cmd.get('targetFl'))
        except (TypeError, ValueError):
            return None
        if tf < 0 or tf > 500 or (tf % 10) != 0:
            return None
        cleaned_cmd['targetFl'] = tf
    elif ctype in ('HOLD_ARM', 'HOLD_CANCEL'):
        if ctype == 'HOLD_ARM':
            wid = cmd.get('waypointId')
            if wid is None or not isinstance(wid, str) or not wid.strip():
                return None
            cleaned_cmd['waypointId'] = wid.strip()
    elif ctype == 'SPD':
        st = (cmd.get('speedType') or '').strip().upper()
        if st not in ('IAS', 'MACH'):
            return None
        cleaned_cmd['speedType'] = st
        if st == 'IAS':
            try:
                iv = int(cmd.get('value'))
            except (TypeError, ValueError):
                return None
            if iv < 0 or iv > 500 or (iv % 10) != 0:
                return None
            cleaned_cmd['value'] = iv
        else:
            try:
                fv = float(cmd.get('value'))
            except (TypeError, ValueError):
                return None
            if fv < 0.29 or fv > 0.96:
                return None
            cleaned_cmd['value'] = round(fv, 2)
    elif ctype == 'ROC':
        try:
            rv = int(cmd.get('value'))
        except (TypeError, ValueError):
            return None
        if rv < -50 or rv > 50 or (rv % 5) != 0:
            return None
        cleaned_cmd['value'] = rv
    elif ctype == 'RWY':
        rw_id = cmd.get('rwId')
        if rw_id is None or not str(rw_id).strip():
            return None
        rw_end = (cmd.get('rwEnd') or '').strip().upper()
        if rw_end not in ('A', 'B'):
            return None
        rw_name = (cmd.get('rwName') or '').strip().upper()
        if not rw_name:
            return None
        cleaned_cmd['rwId'] = str(rw_id).strip()
        cleaned_cmd['rwEnd'] = rw_end
        cleaned_cmd['rwName'] = rw_name[:32]
    out['cmd'] = cleaned_cmd
    return out


def _clean_pp_flight_status_patch(patch):
    if not isinstance(patch, dict):
        return None
    out = {}
    for k in PP_FLIGHT_STATUS_PATCH_KEYS:
        if k in patch:
            out[k] = copy.deepcopy(patch[k])
    return out if out else None


def _merge_pp_flight_status_into_state(session_id, state_out):
    """Attach PP/HOST flight-status patches for PP joiners and host sector view."""
    if not isinstance(state_out, dict):
        return
    with _pp_vertical_lock:
        bucket = _pp_flight_status_by_session.get(session_id)
    if not bucket:
        return
    patches = bucket.get('patches')
    if not isinstance(patches, dict) or not patches:
        return
    state_out['ppFlightStatus'] = {'patches': copy.deepcopy(patches)}


@app.route('/api/sessions/<session_id>/pp-flight-status', methods=['POST'])
def api_pp_flight_status(session_id):
    """PP/HOST POST: ASM + transfer metadata (not merged into EXE/PLN atmTrajectory)."""
    if session_id not in _sessions:
        return jsonify({'ok': False, 'error': 'Session not found'}), 404
    body = request.get_json(force=True, silent=True) or {}
    client_id = (body.get('clientId') or '').strip()
    aircraft = body.get('aircraft')
    if not client_id:
        return jsonify({'ok': False, 'error': 'clientId required'}), 400
    if not isinstance(aircraft, dict):
        return jsonify({'ok': False, 'error': 'aircraft map required'}), 400
    stored = {}
    with _pp_vertical_lock:
        bucket = _pp_flight_status_by_session.setdefault(session_id, {'patches': {}})
        patches = bucket.setdefault('patches', {})
        for aid, patch in aircraft.items():
            cleaned = _clean_pp_flight_status_patch(patch)
            if cleaned is None:
                continue
            sid_key = str(aid)
            if sid_key in patches and isinstance(patches[sid_key], dict):
                merged = copy.deepcopy(patches[sid_key])
                merged.update(cleaned)
                patches[sid_key] = merged
            else:
                patches[sid_key] = cleaned
            stored[sid_key] = copy.deepcopy(patches[sid_key])
    _sessions[session_id]['lastHostActivityAt'] = time.time()
    return jsonify({'ok': True, 'stored': stored})


@app.route('/api/sessions/<session_id>/pp-lateral', methods=['GET', 'POST'])
def api_pp_lateral(session_id):
    """PP POST: lateral commands for host only. GET: host polls (not used by EXE)."""
    if session_id not in _sessions:
        return jsonify({'ok': False, 'error': 'Session not found'}), 404
    if request.method == 'GET':
        with _pp_vertical_lock:
            snap = copy.deepcopy(_pp_lateral_by_session.get(session_id) or {})
            pp_fs = copy.deepcopy((_pp_flight_status_by_session.get(session_id) or {}).get('patches') or {})
        return jsonify({'ok': True, 'aircraft': snap, 'ppFlightStatus': {'patches': pp_fs}})
    body = request.get_json(force=True, silent=True) or {}
    client_id = (body.get('clientId') or '').strip()
    aircraft = body.get('aircraft')
    if not client_id:
        return jsonify({'ok': False, 'error': 'clientId required'}), 400
    if not isinstance(aircraft, dict):
        return jsonify({'ok': False, 'error': 'aircraft map required'}), 400

    stored = {}
    with _pp_vertical_lock:
        bucket = _pp_lateral_by_session.setdefault(session_id, {})
        for aid, patch in aircraft.items():
            cleaned = _clean_pp_lateral_aircraft_patch(patch)
            if not cleaned:
                continue
            sid_key = str(aid)
            prev_seq = 0
            try:
                prev_seq = int((bucket.get(sid_key) or {}).get('cmdSeq') or 0)
            except (TypeError, ValueError):
                prev_seq = 0
            # Monotonic server seq — PP client seq may reset on rejoin; host must never skip a new command.
            cleaned['cmdSeq'] = prev_seq + 1
            bucket[sid_key] = cleaned
            stored[sid_key] = cleaned
    return jsonify({'ok': True, 'stored': stored})


@app.route('/api/sessions/<session_id>/state', methods=['PUT'])
def api_update_session_state(session_id):
    """Host pushes simulation state (called periodically by host)."""
    s = _sessions.get(session_id)
    if not s:
        return jsonify({'ok': False, 'error': 'Session not found'}), 404
    _join_slots_prune_stale_global()
    try:
        body = request.get_json(force=True, silent=True) or {}
        prev_state = s.get('state') or {}
        if isinstance(prev_state, dict) and isinstance(body, dict):
            if 'trajectoryAircraftIds' not in body and 'trajectoryAircraftIds' in prev_state:
                body['trajectoryAircraftIds'] = prev_state['trajectoryAircraftIds']
        # Monotonic per host PUT so joiners can reject stale HTTP out-of-order GETs without blocking rewind
        # (rewind lowers simulationSimTimeMs but always carries a newer hostStateSeq).
        if isinstance(body, dict):
            prev_seq = 0
            if isinstance(prev_state, dict):
                try:
                    prev_seq = int(prev_state.get('hostStateSeq') or 0)
                except (TypeError, ValueError):
                    prev_seq = 0
            body['hostStateSeq'] = prev_seq + 1
        s['state'] = body
        s['lastHostActivityAt'] = time.time()
        with _pp_vertical_lock:
            pp_lat = copy.deepcopy(_pp_lateral_by_session.get(session_id) or {})
            pp_fs_bucket = _pp_flight_status_by_session.get(session_id) or {}
            pp_fs = copy.deepcopy(pp_fs_bucket.get('patches') or {})
        return jsonify({'ok': True, 'ppLateral': pp_lat, 'ppFlightStatus': {'patches': pp_fs}})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/sessions/<session_id>/restart', methods=['POST'])
def api_restart_session(session_id):
    """Host restarts the exercise: clear PP lateral + ATM patches; bump restart seq for joiners."""
    s = _sessions.get(session_id)
    if not s:
        return jsonify({'ok': False, 'error': 'Session not found'}), 404
    _reset_session_runtime_buckets(session_id)
    try:
        seq = int(s.get('hostSessionRestartSeq') or 0) + 1
    except (TypeError, ValueError):
        seq = 1
    s['hostSessionRestartSeq'] = seq
    s['lastHostActivityAt'] = time.time()
    st = s.get('state')
    if isinstance(st, dict):
        st['trajectoryAircraftIds'] = []
    return jsonify({'ok': True, 'hostSessionRestartSeq': seq})


@app.route('/api/sessions/<session_id>/trajectory-toggle', methods=['POST'])
def api_trajectory_toggle(session_id):
    """Toggle middle-click trajectory overlay for an aircraft (shared across all session clients)."""
    s = _sessions.get(session_id)
    if not s:
        return jsonify({'ok': False, 'error': 'Session not found'}), 404
    st = s.get('state')
    # Never create a minimal state dict here — that would wipe simulationData until the next host PUT.
    if not isinstance(st, dict) or not st.get('simulationData'):
        return jsonify({'ok': False, 'error': 'Session state not ready (host must push state first)'}), 409
    body = request.get_json(force=True, silent=True) or {}
    aid = body.get('aircraftId')
    force_off = bool(body.get('forceOff'))
    ids = []
    raw = st.get('trajectoryAircraftIds')
    if isinstance(raw, list):
        ids = list(raw)
    key_set = {str(x) for x in ids}
    aid_str = str(aid)
    if force_off or aid_str in key_set:
        ids = [x for x in ids if str(x) != aid_str]
    else:
        ids.append(aid)
    st['trajectoryAircraftIds'] = ids
    s['lastHostActivityAt'] = time.time()
    return jsonify({'ok': True, 'trajectoryAircraftIds': ids})


@app.route('/api/sessions/<session_id>/atm-trajectory', methods=['POST'])
def api_atm_trajectory_patch(session_id):
    """ATM display trajectory edits from EXE/PLN/INSTR/EVAL/OBS (not FMS). Merges into session bucket; GET state.atmTrajectory
    fans out to every joiner (including the first EXE client id in masterClientId). Same path for XLV (exitLevelFl), transfer
    metadata (transferFromAtcSectorId, transferToSectorNameNorm, transferReceiverAssumed), and flight status (exeFlightStatus,
    etc.) so TOU/TIN/ASSUME/COU/UOU stay aligned across all connected DL positions."""
    if session_id not in _sessions:
        return jsonify({'ok': False, 'error': 'Session not found'}), 404
    body = request.get_json(force=True, silent=True) or {}
    role = (body.get('role') or '').strip().upper()
    client_id = (body.get('clientId') or '').strip()
    if role == 'HOST':
        client_id = client_id or 'host'
    patches_in = body.get('patches')
    if not client_id:
        return jsonify({'ok': False, 'error': 'clientId required'}), 400
    if role == 'PP':
        return jsonify({'ok': False, 'error': 'PP uses host FMS (pp-lateral), not ATM'}), 400
    if role not in ATM_TRAJECTORY_DL_ROLES:
        return jsonify({'ok': False, 'error': 'Invalid role'}), 400
    if not isinstance(patches_in, dict):
        return jsonify({'ok': False, 'error': 'patches object required'}), 400
    with _atm_trajectory_lock:
        bucket = _atm_trajectory_by_session.setdefault(session_id, {'masterClientId': None, 'patches': {}})
        out = bucket['patches']
        for aid, patch in patches_in.items():
            if patch is None:
                out.pop(str(aid), None)
                continue
            if not isinstance(patch, dict):
                continue
            sid = str(aid)
            if role == 'HOST':
                for k in ATM_HOST_OMIT_PATCH_KEYS:
                    patch.pop(k, None)
            if sid in out and isinstance(out[sid], dict):
                merged = copy.deepcopy(out[sid])
                merged.update(patch)
                out[sid] = merged
            else:
                out[sid] = copy.deepcopy(patch)
    _sessions[session_id]['lastHostActivityAt'] = time.time()
    return jsonify({'ok': True})


@app.route('/api/sessions/<session_id>', methods=['GET'])
def api_get_session(session_id):
    """Get session metadata and current state (for Join)."""
    _prune_stale_host_sessions()
    s = _sessions.get(session_id)
    if not s:
        return jsonify({'error': 'Session not found'}), 404
    state = s.get('state')
    if not state:
        return jsonify({'error': 'Session has no state yet'}), 404
    _join_slots_prune_stale_global()
    state_out = copy.deepcopy(state)
    _merge_pp_vertical_into_state(session_id, state_out)
    _merge_pp_flight_status_into_state(session_id, state_out)
    _merge_atm_into_state(session_id, state_out)
    return jsonify({
        'id': s['id'],
        'name': s.get('name', ''),
        'mode': s.get('mode', 'DL'),
        'exerciseName': s.get('exerciseName', ''),
        'sectorName': s.get('sectorName', ''),
        'state': state_out,
    })


@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def api_delete_session(session_id):
    """Remove session (e.g. when host stops simulation or closes the tab)."""
    _remove_session_and_cleanup(session_id)
    return jsonify({'ok': True})


if __name__ == '__main__':
    import argparse

    default_port = int(os.environ.get('PORT', '5000'))
    parser = argparse.ArgumentParser(description='Air Traffic Control Simulator')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=default_port, help='Port (default: PORT env or 5000)')
    args = parser.parse_args()

    print('Starting Air Traffic Control Simulator (development server)')
    print(f'Server running on http://{args.host}:{args.port}')

    app.run(host=args.host, port=args.port, debug=True, use_reloader=False, threaded=True)
