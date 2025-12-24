from flask import Flask, render_template, request, jsonify, session, redirect
from datetime import datetime, timedelta, timezone
from functools import wraps
import sqlite3
import os
import json
import time
import io
import re

# PostgreSQL support
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRESQL_AVAILABLE = True
except ImportError:
    POSTGRESQL_AVAILABLE = False

# Google OAuth imports
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    
    # Configure OAuth for Railway's proxy environment
    if 'RAILWAY_ENVIRONMENT' in os.environ:
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    
    GOOGLE_OAUTH_AVAILABLE = True
except ImportError:
    print("⚠️  Warning: Google OAuth libraries not found. Admin authentication will be disabled.")
    GOOGLE_OAUTH_AVAILABLE = False

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv not installed; fallback to a minimal .env loader so local dev still works.
    def _load_env_file_fallback(filename='.env'):
        """
        Minimal .env parser:
        - supports KEY=VALUE
        - ignores blank lines and comments (# ...)
        - strips surrounding single/double quotes
        - does not override existing environment variables
        """
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(base_dir, filename)
            if not os.path.exists(path):
                return
            with open(path, 'r', encoding='utf-8') as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    if not key:
                        continue
                    # Strip surrounding quotes
                    if len(value) >= 2 and ((value[0] == value[-1]) and value[0] in ("'", '"')):
                        value = value[1:-1]
                    if key not in os.environ:
                        os.environ[key] = value
        except Exception:
            # Best-effort; ignore parsing errors
            return

    _load_env_file_fallback('.env')

try:
    from groq import Groq
except (ImportError, Exception):
    Groq = None
try:
    import google.generativeai as genai
except (ImportError, Exception):
    genai = None

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key-here-change-in-production')
app.config['SESSION_COOKIE_SECURE'] = os.getenv('RAILWAY_ENVIRONMENT') is not None
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

def _env_truthy(name, default=None):
    v = os.getenv(name)
    if v is None:
        return default
    v = str(v).strip().lower()
    if v in ('1', 'true', 'yes', 'y', 'on'):
        return True
    if v in ('0', 'false', 'no', 'n', 'off'):
        return False
    return default

def drive_feature_enabled():
    """
    Drive integration is SECURITY-SENSITIVE. Default behavior:
    - Enabled on localhost only
    - Disabled on deployed environments unless explicitly enabled via DRIVE_FEATURE_ENABLED=1
    """
    override = _env_truthy('DRIVE_FEATURE_ENABLED', default=None)
    if override is not None:
        return bool(override)
    # Default: only allow on localhost (prevents accidental exposure on deploy even if RAILWAY_ENVIRONMENT isn't set).
    try:
        host = (request.host or '').split(':')[0].lower()
        return host in ('localhost', '127.0.0.1')
    except Exception:
        # Outside request context: safest default is disabled.
        return False

# Database configuration
# Use Railway's DATABASE_URL if available (PostgreSQL), otherwise use local SQLite
DATABASE_URL = os.getenv('DATABASE_URL')
USE_POSTGRESQL = bool(DATABASE_URL and ('postgresql' in DATABASE_URL or 'postgres' in DATABASE_URL))

# Local dev safety: if DATABASE_URL is set but psycopg2 isn't installed, fall back to SQLite
# (common when .env includes Railway's DATABASE_URL).
if USE_POSTGRESQL and not POSTGRESQL_AVAILABLE and os.getenv('RAILWAY_ENVIRONMENT') is None:
    print("⚠️  DATABASE_URL is set but psycopg2 is not installed. Falling back to SQLite for local development.")
    USE_POSTGRESQL = False

if USE_POSTGRESQL:
    DATABASE = DATABASE_URL  # Full PostgreSQL connection string
else:
    DATABASE = 'interview_prep.db'  # Local SQLite file

# Google OAuth Configuration
if GOOGLE_OAUTH_AVAILABLE:
    GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
    GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
    # Admin allowlist (production hard requirement).
    # Supports ADMIN_EMAILS="a@b.com,c@d.com" and/or ADMIN_EMAIL="a@b.com".
    _admin_emails_env = []
    if os.getenv('ADMIN_EMAIL'):
        _admin_emails_env.append(os.getenv('ADMIN_EMAIL'))
    _admin_emails_env.extend((os.getenv('ADMIN_EMAILS') or '').split(','))
    ADMIN_EMAILS = [e.strip() for e in _admin_emails_env if str(e or '').strip()]

    # If credentials are not configured, treat OAuth as disabled (prevents broken auth flows on deploy).
    OAUTH_CONFIGURED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
    
    # OAuth scopes
    SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/userinfo.profile']
    DRIVE_READONLY_SCOPE = 'https://www.googleapis.com/auth/drive.readonly'
    
    # OAuth flow configuration
    CLIENT_CONFIG = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": []
        }
    }
else:
    GOOGLE_CLIENT_ID = None
    GOOGLE_CLIENT_SECRET = None
    ADMIN_EMAILS = []
    OAUTH_CONFIGURED = False
    SCOPES = []
    DRIVE_READONLY_SCOPE = None
    CLIENT_CONFIG = None

def _is_local_request():
    """
    Local dev detection for admin convenience.
    IMPORTANT: we never want to accidentally treat a deployed host as local.
    """
    try:
        host = (request.host or '').split(':')[0].lower()
        return host in ('localhost', '127.0.0.1')
    except Exception:
        return False

def get_db():
    """Get database connection - supports both SQLite and PostgreSQL"""
    if USE_POSTGRESQL:
        if not POSTGRESQL_AVAILABLE:
            raise Exception("PostgreSQL URL provided but psycopg2 not installed. Run: pip install psycopg2-binary")
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    else:
        # Reduce "database is locked" errors (common under Flask reloader)
        conn = sqlite3.connect(DATABASE, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON;')
            conn.execute('PRAGMA journal_mode=WAL;')
            conn.execute('PRAGMA synchronous=NORMAL;')
            conn.execute('PRAGMA busy_timeout=30000;')
        except Exception:
            pass
        return conn

def db_execute(conn, query, params=None):
    """Execute a query - converts ? to %s for PostgreSQL and returns cursor-like object"""
    if USE_POSTGRESQL:
        # Convert SQLite ? placeholders to PostgreSQL %s
        if params:
            query = query.replace('?', '%s')
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(query, params or ())
        return cursor
    else:
        return conn.execute(query, params or ())

def db_fetchone(cursor):
    """Fetch one row - works with both SQLite and PostgreSQL"""
    if USE_POSTGRESQL:
        return cursor.fetchone()
    else:
        return cursor.fetchone()

def db_fetchall(cursor):
    """Fetch all rows - works with both SQLite and PostgreSQL"""
    if USE_POSTGRESQL:
        return cursor.fetchall()
    else:
        return cursor.fetchall()

def db_lastrowid(cursor, conn):
    """Get last inserted row ID - works with both SQLite and PostgreSQL"""
    if USE_POSTGRESQL:
        # For PostgreSQL with RETURNING clause, fetch from cursor
        if cursor.rowcount > 0:
            result = cursor.fetchone()
            return result['id'] if result else None
        return None
    else:
        return cursor.lastrowid

def init_db():
    """Initialize database tables - supports both SQLite and PostgreSQL"""
    conn = get_db()
    
    # Determine ID column syntax based on database type
    if USE_POSTGRESQL:
        id_col = "id SERIAL PRIMARY KEY"
        foreign_key_syntax = "FOREIGN KEY (interview_id) REFERENCES interviews (id)"
        cursor = conn.cursor()
    else:
        id_col = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        foreign_key_syntax = "FOREIGN KEY (interview_id) REFERENCES interviews (id)"
        cursor = conn.cursor()
    
    # Create interviews table
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS interviews (
            {id_col},
            company TEXT,
            position TEXT,
            interview_date TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'active'
        )
    ''')
    
    # Create topics table
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS topics (
            {id_col},
            interview_id INTEGER,
            topic_name TEXT,
            category_name TEXT,
            priority TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'pending',
            notes TEXT,
            ai_guidance TEXT,
            ai_notes TEXT,
            {foreign_key_syntax}
        )
    ''')
    
    # Add columns if they don't exist (for existing databases)
    if USE_POSTGRESQL:
        # PostgreSQL: Check if column exists before adding
        try:
            cursor.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='topics' AND column_name='ai_guidance'
            """)
            if not cursor.fetchone():
                cursor.execute('ALTER TABLE topics ADD COLUMN ai_guidance TEXT')
        except Exception:
            pass  # Column already exists or error
        
        try:
            cursor.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='topics' AND column_name='category_name'
            """)
            if not cursor.fetchone():
                cursor.execute('ALTER TABLE topics ADD COLUMN category_name TEXT')
        except Exception:
            pass  # Column already exists or error
        try:
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name='topics' AND column_name='ai_notes'
            """)
            if not cursor.fetchone():
                cursor.execute('ALTER TABLE topics ADD COLUMN ai_notes TEXT')
        except Exception:
            pass
    else:
        # SQLite: Try to add, ignore if exists
        try:
            cursor.execute('ALTER TABLE topics ADD COLUMN ai_guidance TEXT')
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            cursor.execute('ALTER TABLE topics ADD COLUMN category_name TEXT')
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            cursor.execute('ALTER TABLE topics ADD COLUMN ai_notes TEXT')
        except sqlite3.OperationalError:
            pass  # Column already exists
    
    # Create study_sessions table
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS study_sessions (
            {id_col},
            interview_id INTEGER,
            topic_id INTEGER,
            date TEXT,
            duration INTEGER,
            notes TEXT,
            FOREIGN KEY (interview_id) REFERENCES interviews (id),
            FOREIGN KEY (topic_id) REFERENCES topics (id)
        )
    ''')

    # App settings (simple key/value store)
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    ''')

    # OAuth token store (server-side; avoids cookie session limits)
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS oauth_tokens (
            email TEXT NOT NULL,
            scopes_key TEXT NOT NULL,
            token_json TEXT NOT NULL,
            updated_at TEXT,
            PRIMARY KEY (email, scopes_key)
        )
    ''')

    # Drive file index + extracted topics (for incremental processing)
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS drive_files (
            file_id TEXT PRIMARY KEY,
            folder_id TEXT,
            name TEXT,
            mime_type TEXT,
            modified_time TEXT,
            size INTEGER,
            path TEXT,
            parent_id TEXT,
            extracted_topics_json TEXT,
            text_excerpt TEXT,
            extracted_at TEXT,
            indexed_at TEXT
        )
    ''')

    # Add folder_id column if missing (existing DBs)
    if not USE_POSTGRESQL:
        try:
            cursor.execute('ALTER TABLE drive_files ADD COLUMN folder_id TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute('ALTER TABLE drive_files ADD COLUMN text_excerpt TEXT')
        except sqlite3.OperationalError:
            pass
    else:
        try:
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name='drive_files' AND column_name='folder_id'
            """)
            if not cursor.fetchone():
                cursor.execute('ALTER TABLE drive_files ADD COLUMN folder_id TEXT')
        except Exception:
            pass
        try:
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name='drive_files' AND column_name='text_excerpt'
            """)
            if not cursor.fetchone():
                cursor.execute('ALTER TABLE drive_files ADD COLUMN text_excerpt TEXT')
        except Exception:
            pass

    # Drive-generated study guides (e.g., concise master doc)
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS drive_guides (
            {id_col},
            folder_id TEXT,
            kind TEXT,
            content_markdown TEXT,
            created_at TEXT
        )
    ''')

    # Add folder_id column if missing (existing DBs)
    if not USE_POSTGRESQL:
        try:
            cursor.execute('ALTER TABLE drive_guides ADD COLUMN folder_id TEXT')
        except sqlite3.OperationalError:
            pass
    else:
        try:
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name='drive_guides' AND column_name='folder_id'
            """)
            if not cursor.fetchone():
                cursor.execute('ALTER TABLE drive_guides ADD COLUMN folder_id TEXT')
        except Exception:
            pass

    # Optional cache of per-topic concise summaries to avoid regenerating repeatedly
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS drive_topic_summaries (
            topic_key TEXT PRIMARY KEY,
            summary_markdown TEXT,
            updated_at TEXT
        )
    ''')

    # Drive flashcard decks (generated from file excerpts)
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS drive_flashcard_decks (
            {id_col},
            folder_id TEXT,
            kind TEXT,
            deck_json TEXT,
            created_at TEXT
        )
    ''')

    # Add folder_id column if missing (existing DBs)
    if not USE_POSTGRESQL:
        try:
            cursor.execute('ALTER TABLE drive_flashcard_decks ADD COLUMN folder_id TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute('ALTER TABLE drive_flashcard_decks ADD COLUMN kind TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute('ALTER TABLE drive_flashcard_decks ADD COLUMN deck_json TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute('ALTER TABLE drive_flashcard_decks ADD COLUMN created_at TEXT')
        except sqlite3.OperationalError:
            pass
    else:
        # PostgreSQL migrations are best-effort; if table exists but columns missing, ignore.
        pass

    # Global AI guidance cache (reusable across interviews)
    # Keys are normalized to maximize cache hits and avoid duplicate regeneration.
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS ai_guidance_cache (
            {id_col},
            position_key TEXT,
            topic_key TEXT,
            topic_path_key TEXT,
            ai_guidance TEXT,
            model_provider TEXT,
            model_name TEXT,
            updated_at TEXT,
            UNIQUE(position_key, topic_key, topic_path_key)
        )
    ''')

    # Global Study Notes cache (compiled/curated format, reusable across interviews)
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS study_notes_cache (
            {id_col},
            position_key TEXT,
            topic_key TEXT,
            topic_path_key TEXT,
            notes_markdown TEXT,
            model_provider TEXT,
            model_name TEXT,
            updated_at TEXT,
            UNIQUE(position_key, topic_key, topic_path_key)
        )
    ''')
    
    conn.commit()
    cursor.close()
    conn.close()

def get_setting(key, default=None):
    """Get a setting value from DB (string)."""
    try:
        conn = get_db()
        if USE_POSTGRESQL:
            cursor = db_execute(conn, 'SELECT value FROM app_settings WHERE key = %s LIMIT 1', (key,))
            row = db_fetchone(cursor)
            cursor.close()
        else:
            cursor = db_execute(conn, 'SELECT value FROM app_settings WHERE key = ? LIMIT 1', (key,))
            row = db_fetchone(cursor)
        conn.close()
        if not row:
            return default
        return dict(row).get('value') if USE_POSTGRESQL else row[0]
    except Exception:
        return default

def set_setting(key, value):
    """Upsert a setting value into DB."""
    conn = get_db()
    updated_at = datetime.now(timezone.utc).isoformat()
    if USE_POSTGRESQL:
        cursor = db_execute(conn, '''
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = EXCLUDED.updated_at
        ''', (key, str(value), updated_at))
        cursor.close()
    else:
        db_execute(conn, '''
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        ''', (key, str(value), updated_at))
    conn.commit()
    conn.close()

def _scopes_key(scopes):
    """Stable key for token storage."""
    uniq = sorted(set(scopes or []))
    return ' '.join(uniq)

def _get_token_json(email, scopes):
    """Fetch token JSON for a user+scopes from DB."""
    if not email:
        return None
    conn = get_db()
    key = _scopes_key(scopes)
    try:
        if USE_POSTGRESQL:
            cursor = db_execute(conn, 'SELECT token_json FROM oauth_tokens WHERE email = %s AND scopes_key = %s LIMIT 1', (email, key))
            row = db_fetchone(cursor)
            cursor.close()
            conn.close()
            if not row:
                return None
            return dict(row).get('token_json')
        else:
            cursor = db_execute(conn, 'SELECT token_json FROM oauth_tokens WHERE email = ? AND scopes_key = ? LIMIT 1', (email, key))
            row = db_fetchone(cursor)
            conn.close()
            if not row:
                return None
            return row[0]
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None

def _set_token_json(email, scopes, token_json):
    """Upsert token JSON for a user+scopes into DB."""
    if not email or not token_json:
        return
    conn = get_db()
    key = _scopes_key(scopes)
    updated_at = datetime.now(timezone.utc).isoformat()
    if USE_POSTGRESQL:
        cursor = db_execute(conn, '''
            INSERT INTO oauth_tokens (email, scopes_key, token_json, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (email, scopes_key) DO UPDATE SET
                token_json = EXCLUDED.token_json,
                updated_at = EXCLUDED.updated_at
        ''', (email, key, str(token_json), updated_at))
        cursor.close()
    else:
        db_execute(conn, '''
            INSERT INTO oauth_tokens (email, scopes_key, token_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(email, scopes_key) DO UPDATE SET
                token_json = excluded.token_json,
                updated_at = excluded.updated_at
        ''', (email, key, str(token_json), updated_at))
    conn.commit()
    conn.close()

def _get_google_credentials(email, scopes):
    """Build google Credentials from stored token_json and refresh if needed."""
    if not GOOGLE_OAUTH_AVAILABLE:
        return None
    token_json = _get_token_json(email, scopes)
    if not token_json:
        return None
    try:
        info = json.loads(token_json) if isinstance(token_json, str) else token_json
        creds = Credentials.from_authorized_user_info(info, scopes=scopes)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _set_token_json(email, scopes, creds.to_json())
        return creds
    except Exception:
        return None

def _normalize_cache_key(value):
    """Normalize strings for cache keys (stable across whitespace/case variations)."""
    if not value:
        return ''
    if not isinstance(value, str):
        value = str(value)
    # Collapse whitespace, lower-case, trim
    return ' '.join(value.strip().lower().split())

def _get_cached_ai_guidance(position, topic_name, topic_path):
    """Fetch cached AI guidance (if any) for a given position/topic/path."""
    conn = get_db()
    position_key = _normalize_cache_key(position)
    topic_key = _normalize_cache_key(topic_name)
    topic_path_key = _normalize_cache_key(topic_path)

    try:
        if USE_POSTGRESQL:
            cursor = db_execute(conn, '''
                SELECT ai_guidance
                FROM ai_guidance_cache
                WHERE position_key = %s AND topic_key = %s AND topic_path_key = %s
                LIMIT 1
            ''', (position_key, topic_key, topic_path_key))
            row = db_fetchone(cursor)
            cursor.close()
        else:
            cursor = db_execute(conn, '''
                SELECT ai_guidance
                FROM ai_guidance_cache
                WHERE position_key = ? AND topic_key = ? AND topic_path_key = ?
                LIMIT 1
            ''', (position_key, topic_key, topic_path_key))
            row = db_fetchone(cursor)
    except sqlite3.OperationalError as e:
        # If migrations haven't run yet, treat as cache miss (do not fail request)
        if 'no such table' in str(e).lower():
            conn.close()
            return None
        conn.close()
        raise
    conn.close()
    if not row:
        return None
    return dict(row).get('ai_guidance') if USE_POSTGRESQL else row[0] if isinstance(row, tuple) else dict(row).get('ai_guidance')

def _upsert_cached_ai_guidance(position, topic_name, topic_path, ai_guidance, model_provider=None, model_name=None):
    """Insert/update cached AI guidance for reuse across interviews."""
    if not ai_guidance:
        return
    conn = get_db()
    position_key = _normalize_cache_key(position)
    topic_key = _normalize_cache_key(topic_name)
    topic_path_key = _normalize_cache_key(topic_path)
    updated_at = datetime.now(timezone.utc).isoformat()

    try:
        if USE_POSTGRESQL:
            cursor = db_execute(conn, '''
                INSERT INTO ai_guidance_cache (position_key, topic_key, topic_path_key, ai_guidance, model_provider, model_name, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (position_key, topic_key, topic_path_key)
                DO UPDATE SET
                    ai_guidance = EXCLUDED.ai_guidance,
                    model_provider = EXCLUDED.model_provider,
                    model_name = EXCLUDED.model_name,
                    updated_at = EXCLUDED.updated_at
            ''', (position_key, topic_key, topic_path_key, ai_guidance, model_provider, model_name, updated_at))
            cursor.close()
        else:
            db_execute(conn, '''
                INSERT INTO ai_guidance_cache (position_key, topic_key, topic_path_key, ai_guidance, model_provider, model_name, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_key, topic_key, topic_path_key)
                DO UPDATE SET
                    ai_guidance = excluded.ai_guidance,
                    model_provider = excluded.model_provider,
                    model_name = excluded.model_name,
                    updated_at = excluded.updated_at
            ''', (position_key, topic_key, topic_path_key, ai_guidance, model_provider, model_name, updated_at))
    except sqlite3.OperationalError as e:
        # If cache table doesn't exist yet, just skip caching.
        if 'no such table' in str(e).lower():
            conn.close()
            return
        conn.close()
        raise

    conn.commit()
    conn.close()

def _get_cached_study_notes(position, topic_name, topic_path):
    """Fetch cached study notes (if any) for a given position/topic/path."""
    conn = get_db()
    position_key = _normalize_cache_key(position)
    topic_key = _normalize_cache_key(topic_name)
    topic_path_key = _normalize_cache_key(topic_path)

    try:
        if USE_POSTGRESQL:
            cursor = db_execute(conn, '''
                SELECT notes_markdown
                FROM study_notes_cache
                WHERE position_key = %s AND topic_key = %s AND topic_path_key = %s
                LIMIT 1
            ''', (position_key, topic_key, topic_path_key))
            row = db_fetchone(cursor)
            cursor.close()
        else:
            cursor = db_execute(conn, '''
                SELECT notes_markdown
                FROM study_notes_cache
                WHERE position_key = ? AND topic_key = ? AND topic_path_key = ?
                LIMIT 1
            ''', (position_key, topic_key, topic_path_key))
            row = db_fetchone(cursor)
    except sqlite3.OperationalError as e:
        if 'no such table' in str(e).lower():
            conn.close()
            return None
        conn.close()
        raise

    conn.close()
    if not row:
        return None
    if USE_POSTGRESQL:
        return dict(row).get('notes_markdown')
    if isinstance(row, tuple):
        return row[0]
    return dict(row).get('notes_markdown')

def _upsert_cached_study_notes(position, topic_name, topic_path, notes_markdown, model_provider=None, model_name=None):
    """Insert/update cached study notes for reuse across interviews."""
    if not notes_markdown:
        return
    conn = get_db()
    position_key = _normalize_cache_key(position)
    topic_key = _normalize_cache_key(topic_name)
    topic_path_key = _normalize_cache_key(topic_path)
    updated_at = datetime.now(timezone.utc).isoformat()

    try:
        if USE_POSTGRESQL:
            cursor = db_execute(conn, '''
                INSERT INTO study_notes_cache (position_key, topic_key, topic_path_key, notes_markdown, model_provider, model_name, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (position_key, topic_key, topic_path_key)
                DO UPDATE SET
                    notes_markdown = EXCLUDED.notes_markdown,
                    model_provider = EXCLUDED.model_provider,
                    model_name = EXCLUDED.model_name,
                    updated_at = EXCLUDED.updated_at
            ''', (position_key, topic_key, topic_path_key, notes_markdown, model_provider, model_name, updated_at))
            cursor.close()
        else:
            db_execute(conn, '''
                INSERT INTO study_notes_cache (position_key, topic_key, topic_path_key, notes_markdown, model_provider, model_name, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_key, topic_key, topic_path_key)
                DO UPDATE SET
                    notes_markdown = excluded.notes_markdown,
                    model_provider = excluded.model_provider,
                    model_name = excluded.model_name,
                    updated_at = excluded.updated_at
            ''', (position_key, topic_key, topic_path_key, notes_markdown, model_provider, model_name, updated_at))
    except sqlite3.OperationalError as e:
        if 'no such table' in str(e).lower():
            conn.close()
            return
        conn.close()
        raise

    conn.commit()
    conn.close()

def _hydrate_topic_ai_from_cache(conn, topic_id, position, topic_name, category_name):
    """
    If we have cached AI guidance/notes (global caches), populate the newly-created topic row.
    This makes recreated interviews immediately show previously-generated material without re-generation.
    """
    try:
        parent_path_raw = category_name.strip() if isinstance(category_name, str) and category_name.strip() else None
        topic_path_key_source = f"{parent_path_raw} > {topic_name}" if parent_path_raw else (topic_name or '')

        position_key = _normalize_cache_key(position)
        topic_key = _normalize_cache_key(topic_name)
        topic_path_key = _normalize_cache_key(topic_path_key_source)

        cached_guidance = None
        cached_notes = None

        # Guidance cache
        try:
            cur = db_execute(conn, '''
                SELECT ai_guidance
                FROM ai_guidance_cache
                WHERE position_key = ? AND topic_key = ? AND topic_path_key = ?
                LIMIT 1
            ''', (position_key, topic_key, topic_path_key))
            row = db_fetchone(cur)
            if USE_POSTGRESQL:
                cur.close()
            if row:
                cached_guidance = dict(row).get('ai_guidance') if USE_POSTGRESQL else row[0] if isinstance(row, tuple) else dict(row).get('ai_guidance')
        except Exception:
            # Cache table may not exist yet or query may fail; ignore.
            cached_guidance = None

        # Notes cache
        try:
            cur = db_execute(conn, '''
                SELECT notes_markdown
                FROM study_notes_cache
                WHERE position_key = ? AND topic_key = ? AND topic_path_key = ?
                LIMIT 1
            ''', (position_key, topic_key, topic_path_key))
            row = db_fetchone(cur)
            if USE_POSTGRESQL:
                cur.close()
            if row:
                cached_notes = dict(row).get('notes_markdown') if USE_POSTGRESQL else row[0] if isinstance(row, tuple) else dict(row).get('notes_markdown')
        except Exception:
            cached_notes = None

        if cached_guidance or cached_notes:
            # Update the topic row; only set fields that are present.
            if USE_POSTGRESQL:
                cur = db_execute(conn, '''
                    UPDATE topics
                    SET ai_guidance = COALESCE(%s, ai_guidance),
                        ai_notes   = COALESCE(%s, ai_notes)
                    WHERE id = %s
                ''', (cached_guidance, cached_notes, topic_id))
                cur.close()
            else:
                db_execute(conn, '''
                    UPDATE topics
                    SET ai_guidance = COALESCE(?, ai_guidance),
                        ai_notes   = COALESCE(?, ai_notes)
                    WHERE id = ?
                ''', (cached_guidance, cached_notes, topic_id))
    except Exception:
        # Best-effort only
        return

@app.route('/')
def index():
    return render_template('index.html')

# OAuth Helper Functions
def is_admin_email(email):
    """Check if email is in admin whitelist"""
    if not email:
        return False
    email = email.strip().lower()
    # In local dev, allow easy access even without configuring OAuth/admin allowlist.
    if _is_local_request():
        if not GOOGLE_OAUTH_AVAILABLE or not OAUTH_CONFIGURED:
            return True
        # If OAuth is configured locally, still use the allowlist if provided.
        if not ADMIN_EMAILS:
            return True
    # In non-local environments, require explicit allowlist.
    if not ADMIN_EMAILS:
        return False
    return email in [admin.strip().lower() for admin in ADMIN_EMAILS if admin.strip()]

def login_required(f):
    """Decorator to require Google OAuth login for admin routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # For local development only, bypass OAuth if running on localhost
        if _is_local_request():
            print("DEBUG: Local development detected, bypassing OAuth")
            return f(*args, **kwargs)

        # Production safety: admin must not be publicly accessible.
        if not GOOGLE_OAUTH_AVAILABLE or not OAUTH_CONFIGURED:
            return ("Admin is disabled: Google OAuth is not configured. "
                    "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (and ADMIN_EMAILS) to enable /admin."), 403
        if not ADMIN_EMAILS:
            return ("Admin is disabled: no admin allowlist configured. "
                    "Set ADMIN_EMAILS (or ADMIN_EMAIL) to your email to enable /admin."), 403
        
        # Check if user is logged in
        if ('user_email' not in session or 
            not session.get('user_email')):
            print("DEBUG: No valid session found, redirecting to login")
            return redirect('/auth/login')

        # Require a stored token for base scopes (server-side)
        creds = _get_google_credentials(session.get('user_email'), SCOPES)
        if not creds:
            print("DEBUG: No stored OAuth token found, redirecting to login")
            return redirect('/auth/login')
        
        # Check if user is admin
        if not is_admin_email(session['user_email']):
            return jsonify({'error': f'Access denied for {session["user_email"]}. Contact administrator.'}), 403
        
        print(f"DEBUG: Authenticated user: {session['user_email']}")
        return f(*args, **kwargs)
    return decorated_function

def drive_login_required(f):
    """Require OAuth with Drive read-only scope (no localhost bypass)."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not drive_feature_enabled():
            return jsonify({'error': 'Not found'}), 404
        if not GOOGLE_OAUTH_AVAILABLE or not OAUTH_CONFIGURED:
            return jsonify({'error': 'Google OAuth not available. Install google-auth-oauthlib and google-api-python-client.'}), 500
        email = session.get('user_email')
        if not email:
            return jsonify({'error': 'Not authenticated', 'auth_url': '/auth/login?drive=1'}), 401
        scopes = SCOPES + [DRIVE_READONLY_SCOPE]
        creds = _get_google_credentials(email, scopes)
        if not creds:
            return jsonify({'error': 'Drive not connected', 'auth_url': '/auth/login?drive=1'}), 401
        return f(*args, **kwargs)
    return decorated_function

@app.route('/auth/login')
def auth_login():
    """Initiate Google OAuth login"""
    if not GOOGLE_OAUTH_AVAILABLE or not OAUTH_CONFIGURED:
        # Local dev convenience only; never auto-open admin on deploy.
        if _is_local_request():
            return redirect('/admin')
        return ("OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET "
                "(and ADMIN_EMAILS) to enable admin login."), 403
    
    # For local development, redirect directly to admin
    is_local = _is_local_request()
    
    # For local development, only bypass for admin access.
    # Drive integration requires real OAuth even on localhost, but we keep Drive feature local-only by default.
    if request.args.get('drive') == '1' and not drive_feature_enabled():
        return redirect('/admin')
    if is_local and request.args.get('drive') != '1':
        return redirect('/admin')
    
    try:
        # Local dev: allow OAuth over http://localhost (otherwise oauthlib blocks with insecure_transport).
        if is_local:
            os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

        scopes = SCOPES
        if request.args.get('drive') == '1':
            scopes = SCOPES + [DRIVE_READONLY_SCOPE]
        session['oauth_scopes'] = scopes

        # Create flow with proper configuration
        flow = Flow.from_client_config(CLIENT_CONFIG, scopes)
        
        # Construct redirect URI - ensure HTTPS for Railway
        if 'railway.app' in request.host or os.getenv('RAILWAY_ENVIRONMENT'):
            # Railway uses HTTPS, but request might come as HTTP due to proxy
            redirect_uri = f"https://{request.host}/auth/callback"
        else:
            redirect_uri = request.url_root.rstrip('/') + '/auth/callback'
        
        flow.redirect_uri = redirect_uri
        print(f"DEBUG: OAuth redirect URI: {redirect_uri}")
        
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )
        
        session['state'] = state
        return redirect(authorization_url)
    except Exception as e:
        print(f"OAuth login error: {e}")
        import traceback
        print(traceback.format_exc())
        return f"OAuth error: {e}", 500

@app.route('/auth/callback')
def auth_callback():
    """Handle Google OAuth callback"""
    if not GOOGLE_OAUTH_AVAILABLE or not OAUTH_CONFIGURED:
        if _is_local_request():
            return redirect('/admin')
        return ("OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET "
                "(and ADMIN_EMAILS) to enable admin login."), 403
    
    try:
        # Local dev: allow OAuth over http://localhost (otherwise oauthlib blocks with insecure_transport).
        is_local = _is_local_request()
        if is_local:
            os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

        scopes = session.get('oauth_scopes') or SCOPES
        # Create flow
        flow = Flow.from_client_config(CLIENT_CONFIG, scopes)
        
        # Construct redirect URI - ensure HTTPS for Railway
        if 'railway.app' in request.host or os.getenv('RAILWAY_ENVIRONMENT'):
            # Railway uses HTTPS, but request might come as HTTP due to proxy
            redirect_uri = f"https://{request.host}/auth/callback"
        else:
            redirect_uri = request.url_root.rstrip('/') + '/auth/callback'
        
        flow.redirect_uri = redirect_uri
        print(f"DEBUG: OAuth callback redirect URI: {redirect_uri}")
        
        # Handle Railway proxy HTTPS issue
        authorization_response = request.url
        if 'railway.app' in request.host or os.getenv('RAILWAY_ENVIRONMENT'):
            authorization_response = authorization_response.replace('http://', 'https://')
        
        print(f"DEBUG: Authorization response URL: {authorization_response}")
        flow.fetch_token(authorization_response=authorization_response)
        
        credentials = flow.credentials
        service = build('oauth2', 'v2', credentials=credentials)
        user_info = service.userinfo().get().execute()
        
        email = user_info.get('email')
        name = user_info.get('name')
        
        if not is_admin_email(email):
            return f"Access denied for {email}. Contact administrator.", 403
        
        # Store user info in session
        session['user_email'] = email
        session['user_name'] = name
        # Store tokens server-side; cookie sessions can overflow with OAuth JSON.
        _set_token_json(email, scopes, credentials.to_json())
        
        return redirect('/admin')
        
    except Exception as e:
        print(f"OAuth callback error: {e}")
        return f"OAuth callback error: {e}", 500

@app.route('/auth/logout')
def auth_logout():
    """Logout user"""
    # Clear all session data
    session.clear()
    
    if 'user_email' in session:
        del session['user_email']
    if 'user_name' in session:
        del session['user_name']
    if 'state' in session:
        del session['state']
    if 'oauth_scopes' in session:
        del session['oauth_scopes']
    
    session.permanent = False
    return redirect('/')

@app.route('/admin')
@login_required
def admin():
    return render_template('admin.html', session=session, drive_enabled=drive_feature_enabled())

@app.route('/drive/guide/view/latest')
@drive_login_required
def drive_guide_view_latest():
    kind = (request.args.get('kind') or '').strip().lower() or None
    folder_id = (request.args.get('folder_id') or '').strip() or None
    guide = _fetch_latest_drive_guide(kind=kind, folder_id=folder_id)
    if not guide:
        guide = {'content_markdown': '# No guide generated yet\n\nGo back to Admin → Drive and click **Generate Concise Guide**.\n', 'created_at': ''}
    return render_template('drive_guide_view.html', guide=guide)

# ---- Google Drive (PDF + ipynb) ingestion ----

def _drive_service_for_session():
    email = session.get('user_email')
    scopes = SCOPES + [DRIVE_READONLY_SCOPE]
    creds = _get_google_credentials(email, scopes)
    if not creds:
        return None
    return build('drive', 'v3', credentials=creds)

def _drive_list_folder_recursive(service, root_folder_id, include_subfolders=True, max_files=20000):
    """
    Recursively list files in a Drive folder. Returns list of dicts with path + metadata.
    Limits to max_files to avoid runaway indexing.
    """
    results = []
    queue = [(root_folder_id, '')]  # (folder_id, path_prefix)
    seen_folders = set()

    while queue and len(results) < max_files:
        folder_id, prefix = queue.pop(0)
        if folder_id in seen_folders:
            continue
        seen_folders.add(folder_id)

        page_token = None
        while True:
            resp = service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken, files(id,name,mimeType,modifiedTime,size,parents)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageSize=1000,
                pageToken=page_token
            ).execute()
            files = resp.get('files', [])

            for f in files:
                name = f.get('name') or ''
                mime = f.get('mimeType') or ''
                is_folder = mime == 'application/vnd.google-apps.folder'
                path = f"{prefix}/{name}".lstrip('/')
                item = {
                    'id': f.get('id'),
                    'name': name,
                    'mimeType': mime,
                    'modifiedTime': f.get('modifiedTime'),
                    'size': int(f.get('size') or 0),
                    'parents': f.get('parents') or [],
                    'path': path,
                    'isFolder': is_folder
                }
                results.append(item)
                if include_subfolders and is_folder and item['id']:
                    queue.append((item['id'], path))

                if len(results) >= max_files:
                    break

            page_token = resp.get('nextPageToken')
            if not page_token or len(results) >= max_files:
                break

    return results

def _drive_download_bytes(service, file_id):
    """Download a file's bytes from Drive."""
    req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    try:
        from googleapiclient.http import MediaIoBaseDownload
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    except Exception:
        data = req.execute()
        fh.write(data if isinstance(data, (bytes, bytearray)) else bytes(data))
    return fh.getvalue()

def _extract_text_ipynb(raw_bytes, max_chars=120000):
    """Extract text content from a Jupyter notebook (.ipynb)."""
    try:
        data = json.loads(raw_bytes.decode('utf-8', errors='ignore'))
    except Exception:
        try:
            data = json.loads(raw_bytes)
        except Exception:
            return ''
    cells = data.get('cells', []) if isinstance(data, dict) else []
    parts = []
    for c in cells:
        ctype = c.get('cell_type')
        src = c.get('source', '')
        if isinstance(src, list):
            src = ''.join(src)
        src = str(src or '')
        if ctype == 'markdown':
            parts.append(src)
        elif ctype == 'code':
            code = src.strip()
            if not code:
                continue
            keep = []
            for line in code.splitlines():
                l = line.strip()
                if not l:
                    continue
                if l.startswith('import ') or l.startswith('from '):
                    keep.append(line)
                elif l.startswith('def ') or l.startswith('class '):
                    keep.append(line)
                elif l.startswith('#'):
                    keep.append(line)
            if keep:
                parts.append("```python\n" + "\n".join(keep[:80]) + "\n```")
    text = "\n\n".join(parts).strip()
    return text[:max_chars]

def _extract_text_pdf(raw_bytes, max_chars=160000):
    """Extract text from a PDF using PyPDF2 (best-effort)."""
    try:
        from PyPDF2 import PdfReader
    except Exception:
        return ''
    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        out = []
        for page in reader.pages:
            try:
                t = (page.extract_text() or '').strip()
                if t:
                    out.append(t)
            except Exception:
                continue
            if sum(len(x) for x in out) > max_chars:
                break
        text = "\n\n".join(out).strip()
        return text[:max_chars]
    except Exception:
        return ''

def _extract_candidate_topics_heuristic(text, max_topics=30):
    """Quick heuristic topic extractor (markdown headings + title-like lines)."""
    if not text:
        return []
    topics = []
    seen = set()
    for line in text.splitlines():
        l = line.strip()
        if not l:
            continue
        m = re.match(r'^(#{1,6})\s+(.+)$', l)
        if m:
            title = m.group(2).strip().strip('#').strip()
            if 3 <= len(title) <= 90:
                k = title.lower()
                if k not in seen:
                    seen.add(k)
                    topics.append(title)
        if len(l) <= 60 and re.match(r'^[A-Za-z0-9][A-Za-z0-9\s\-\(\)\./:,&]+$', l) and l[0].isupper():
            if re.match(r'^(Answer|Page|Figure|Table)\b', l, re.I):
                continue
            k = l.lower()
            if k not in seen:
                seen.add(k)
                topics.append(l)
        if len(topics) >= max_topics:
            break
    return topics[:max_topics]

def _ai_extract_topics(text, max_topics=20, title_hint=None):
    """Use configured AI provider to extract a concise topic list."""
    if not text:
        return []
    groq_key = os.environ.get('GROQ_API_KEY') or os.getenv('GROQ_API_KEY')
    gemini_key = os.environ.get('GOOGLE_API_KEY') or os.getenv('GOOGLE_API_KEY')
    prompt = f"""
You are extracting study topics from course material.
Return ONLY a JSON array of strings (no markdown, no extra keys).
Constraints:
- 8 to {max_topics} items
- concise (2-6 words each), deduplicated, ordered from fundamental → advanced

Title: {title_hint or ''}

Material:
{text[:20000]}
""".strip()
    try:
        if groq_key and Groq is not None:
            client = Groq(api_key=groq_key)
            resp = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=600
            )
            raw = resp.choices[0].message.content.strip()
        elif gemini_key and genai is not None:
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel('gemini-pro')
            raw = (model.generate_content(prompt).text or '').strip()
        else:
            return []
        m = re.search(r'\[[\s\S]*\]', raw)
        if not m:
            return []
        arr = json.loads(m.group(0))
        if not isinstance(arr, list):
            return []
        cleaned = []
        seen = set()
        for t in arr:
            s = str(t).strip()
            if not s:
                continue
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            cleaned.append(s)
        return cleaned[:max_topics]
    except Exception:
        return []

@app.route('/api/drive/status', methods=['GET'])
@drive_login_required
def drive_status():
    return jsonify({'connected': True, 'email': session.get('user_email')})

@app.route('/api/drive/index', methods=['POST'])
@drive_login_required
def drive_index():
    """Index PDFs and ipynb files in a Drive folder (metadata-only)."""
    data = request.get_json(silent=True) or {}
    folder_id = (data.get('folder_id') or '').strip()
    if not folder_id:
        return jsonify({'error': 'folder_id is required'}), 400

    svc = _drive_service_for_session()
    if not svc:
        return jsonify({'error': 'Drive not connected', 'auth_url': '/auth/login?drive=1'}), 401

    items = _drive_list_folder_recursive(svc, folder_id, include_subfolders=True, max_files=20000)
    now = datetime.now(timezone.utc).isoformat()
    # Remember which folder we’re working with (used as default scope for extraction/guide generation).
    set_setting('drive_last_folder_id', folder_id)

    wanted = []
    for it in items:
        if it.get('isFolder'):
            continue
        name = (it.get('name') or '')
        mime = (it.get('mimeType') or '')
        is_pdf = mime == 'application/pdf' or name.lower().endswith('.pdf')
        is_ipynb = name.lower().endswith('.ipynb') or mime in ('application/x-ipynb+json',)
        if is_pdf or is_ipynb:
            wanted.append(it)

    conn = get_db()
    try:
        for it in wanted:
            file_id = it.get('id')
            if not file_id:
                continue
            name = it.get('name') or ''
            mime = it.get('mimeType') or ''
            modified = it.get('modifiedTime')
            size = int(it.get('size') or 0)
            path = it.get('path') or name
            parent_id = (it.get('parents') or [None])[0]

            if USE_POSTGRESQL:
                cur = db_execute(conn, '''
                    INSERT INTO drive_files (file_id, folder_id, name, mime_type, modified_time, size, path, parent_id, indexed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (file_id) DO UPDATE SET
                        folder_id = EXCLUDED.folder_id,
                        name = EXCLUDED.name,
                        mime_type = EXCLUDED.mime_type,
                        modified_time = EXCLUDED.modified_time,
                        size = EXCLUDED.size,
                        path = EXCLUDED.path,
                        parent_id = EXCLUDED.parent_id,
                        indexed_at = EXCLUDED.indexed_at
                ''', (file_id, folder_id, name, mime, modified, size, path, parent_id, now))
                cur.close()
            else:
                db_execute(conn, '''
                    INSERT INTO drive_files (file_id, folder_id, name, mime_type, modified_time, size, path, parent_id, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        folder_id = excluded.folder_id,
                        name = excluded.name,
                        mime_type = excluded.mime_type,
                        modified_time = excluded.modified_time,
                        size = excluded.size,
                        path = excluded.path,
                        parent_id = excluded.parent_id,
                        indexed_at = excluded.indexed_at
                ''', (file_id, folder_id, name, mime, modified, size, path, parent_id, now))
        conn.commit()
    finally:
        conn.close()

    return jsonify({
        'indexed': len(wanted),
        'total_seen': len(items),
        'pdf_count': sum(1 for x in wanted if (x.get('mimeType') == 'application/pdf' or (x.get('name') or '').lower().endswith('.pdf'))),
        'ipynb_count': sum(1 for x in wanted if (x.get('name') or '').lower().endswith('.ipynb')),
    })

@app.route('/api/drive/extract-topics', methods=['POST'])
@drive_login_required
def drive_extract_topics():
    """Download unprocessed files and extract topic lists."""
    data = request.get_json(silent=True) or {}
    limit = int(data.get('limit') or 10)
    if limit < 1 or limit > 100:
        limit = 10
    force = bool(data.get('force') or False)
    folder_id = (data.get('folder_id') or get_setting('drive_last_folder_id', '') or '').strip()
    if not folder_id:
        return jsonify({'error': 'folder_id is required (index a folder first)'}), 400

    svc = _drive_service_for_session()
    if not svc:
        return jsonify({'error': 'Drive not connected', 'auth_url': '/auth/login?drive=1'}), 401

    conn = get_db()
    rows = []
    try:
        if USE_POSTGRESQL:
            cur = db_execute(conn, '''
                SELECT file_id, folder_id, name, mime_type, modified_time, extracted_at, extracted_topics_json, text_excerpt
                FROM drive_files
                WHERE folder_id = %s
                ORDER BY indexed_at DESC NULLS LAST
                LIMIT %s
            ''', (folder_id, max(limit * 5, limit),))
            rows = [dict(r) for r in db_fetchall(cur)]
            cur.close()
        else:
            cur = db_execute(conn, '''
                SELECT file_id, folder_id, name, mime_type, modified_time, extracted_at, extracted_topics_json, text_excerpt
                FROM drive_files
                WHERE folder_id = ?
                ORDER BY indexed_at DESC
                LIMIT ?
            ''', (folder_id, max(limit * 5, limit),))
            rows = [dict(r) for r in db_fetchall(cur)]
    finally:
        conn.close()

    processed = []
    for f in rows:
        if len(processed) >= limit:
            break
        # If we've already extracted topics but never stored an excerpt, we still want to download once
        # to store text_excerpt for downstream features (flashcards, etc.).
        if not force and f.get('extracted_at') and f.get('text_excerpt'):
            continue
        file_id = f.get('file_id')
        name = f.get('name') or ''
        mime = f.get('mime_type') or ''
        if not file_id:
            continue
        try:
            raw = _drive_download_bytes(svc, file_id)
            is_pdf = mime == 'application/pdf' or name.lower().endswith('.pdf')
            is_ipynb = name.lower().endswith('.ipynb') or mime in ('application/x-ipynb+json',)
            if is_ipynb:
                text = _extract_text_ipynb(raw)
            elif is_pdf:
                text = _extract_text_pdf(raw)
            else:
                continue
            # Persist a short excerpt so later study tools (flashcards, etc.) can be grounded in the exact material.
            excerpt = (text or '').replace('\x00', '').strip()[:20000]

            # If topics were already extracted and we're not forcing, just store the excerpt quickly.
            if (not force) and f.get('extracted_at') and f.get('extracted_topics_json'):
                conn = get_db()
                try:
                    if USE_POSTGRESQL:
                        cur = db_execute(conn, 'UPDATE drive_files SET text_excerpt = %s WHERE file_id = %s', (excerpt, file_id))
                        cur.close()
                    else:
                        db_execute(conn, 'UPDATE drive_files SET text_excerpt = ? WHERE file_id = ?', (excerpt, file_id))
                    conn.commit()
                finally:
                    conn.close()
                processed.append({'file_id': file_id, 'name': name, 'excerpt_saved': True, 'topics': 'skipped'})
                continue

            heuristic = _extract_candidate_topics_heuristic(text, max_topics=30)
            ai_topics = _ai_extract_topics(text, max_topics=20, title_hint=name) if text else []
            topics = ai_topics or heuristic

            conn = get_db()
            try:
                now = datetime.now(timezone.utc).isoformat()
                payload = json.dumps({'topics': topics, 'heuristic': heuristic[:30], 'ai': ai_topics[:20]}, ensure_ascii=False)
                if USE_POSTGRESQL:
                    cur = db_execute(conn, '''
                        UPDATE drive_files
                        SET extracted_topics_json = %s,
                            text_excerpt = %s,
                            extracted_at = %s
                        WHERE file_id = %s
                    ''', (payload, excerpt, now, file_id))
                    cur.close()
                else:
                    db_execute(conn, '''
                        UPDATE drive_files
                        SET extracted_topics_json = ?,
                            text_excerpt = ?,
                            extracted_at = ?
                        WHERE file_id = ?
                    ''', (payload, excerpt, now, file_id))
                conn.commit()
            finally:
                conn.close()

            processed.append({'file_id': file_id, 'name': name, 'topics': topics, 'excerpt_saved': True})
        except Exception as e:
            processed.append({'file_id': file_id, 'name': name, 'error': str(e)})

    return jsonify({'processed': processed, 'count': len(processed)})

def _topic_key(topic):
    return ' '.join(str(topic or '').strip().lower().split())

def _get_drive_topic_summary(topic):
    """Fetch cached concise summary for a topic."""
    key = _topic_key(topic)
    if not key:
        return None
    conn = get_db()
    try:
        if USE_POSTGRESQL:
            cur = db_execute(conn, 'SELECT summary_markdown FROM drive_topic_summaries WHERE topic_key = %s LIMIT 1', (key,))
            row = db_fetchone(cur)
            cur.close()
            conn.close()
            return dict(row).get('summary_markdown') if row else None
        else:
            cur = db_execute(conn, 'SELECT summary_markdown FROM drive_topic_summaries WHERE topic_key = ? LIMIT 1', (key,))
            row = db_fetchone(cur)
            conn.close()
            return row[0] if row else None
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None

def _set_drive_topic_summary(topic, summary_markdown):
    """Upsert cached concise summary for a topic."""
    key = _topic_key(topic)
    if not key or not summary_markdown:
        return
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    if USE_POSTGRESQL:
        cur = db_execute(conn, '''
            INSERT INTO drive_topic_summaries (topic_key, summary_markdown, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (topic_key) DO UPDATE SET
                summary_markdown = EXCLUDED.summary_markdown,
                updated_at = EXCLUDED.updated_at
        ''', (key, str(summary_markdown), now))
        cur.close()
    else:
        db_execute(conn, '''
            INSERT INTO drive_topic_summaries (topic_key, summary_markdown, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(topic_key) DO UPDATE SET
                summary_markdown = excluded.summary_markdown,
                updated_at = excluded.updated_at
        ''', (key, str(summary_markdown), now))
    conn.commit()
    conn.close()

def _ai_concise_topic_bullets(topics):
    """
    Generate concise review bullets for a list of topics.
    Returns markdown text.
    """
    topics = [t for t in (topics or []) if str(t or '').strip()]
    if not topics:
        return ''
    groq_key = os.environ.get('GROQ_API_KEY') or os.getenv('GROQ_API_KEY')
    gemini_key = os.environ.get('GOOGLE_API_KEY') or os.getenv('GOOGLE_API_KEY')
    if not ((groq_key and Groq is not None) or (gemini_key and genai is not None)):
        raise Exception('No AI API key configured (set GROQ_API_KEY or GOOGLE_API_KEY).')

    topic_list = "\n".join([f"- {t}" for t in topics])
    prompt = f"""
You are producing a CONCISE review study guide for an AI/ML course.
For EACH topic below, output a markdown section exactly like:

## <Topic>
- <bullet 1>
- <bullet 2>
- <bullet 3>
- <bullet 4>

Rules:
- 4 to 7 bullets per topic
- bullets must be short, high-signal (definitions, key formula/intuition, common pitfall, when to use)
- avoid long paragraphs
- do NOT include any extra commentary outside the topic sections
- write any formulas using LaTeX (use \\( ... \\) inline and $$ ... $$ for display)

Topics:
{topic_list}
""".strip()

    if groq_key and Groq is not None:
        client = Groq(api_key=groq_key)
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.25,
            max_tokens=1800
        )
        return resp.choices[0].message.content.strip()
    # Gemini fallback
    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel('gemini-pro')
    return (model.generate_content(prompt).text or '').strip()

def _module_from_path(path):
    p = (path or '').strip()
    if not p:
        return 'Misc'
    return p.split('/')[0].strip() or 'Misc'

def _module_sort_key(name):
    # Sort by leading number if present: "1 - Basic Statistics" -> (1, name)
    m = re.match(r'^\s*(\d+)\s*[-–—]', str(name or ''))
    if m:
        return (int(m.group(1)), str(name))
    return (10**9, str(name))

def _is_noise_topic(topic):
    """
    Filter out low-signal notebook operational topics that clutter a study guide.
    Keep this conservative.
    """
    t = _topic_key(topic)
    if not t:
        return True
    noise = [
        'importing libraries',
        'import libraries',
        'scipy stats',
        'loading data',
        'load data',
        'data preparation',
        'data preprocessing',
        'data cleaning',
        'reading data',
        'visualization',
        'plotting',
        'exploratory data analysis',
        'eda',
        'installing',
        'setup',
    ]
    return any(n in t for n in noise)

def _ai_concise_module_review(module_name, topics):
    """
    Generate a concise, organized review section for a module:
    - Module overview bullets
    - Topic bullets under ### headings
    """
    topics = [t for t in (topics or []) if str(t or '').strip()]
    if not topics:
        return ''
    groq_key = os.environ.get('GROQ_API_KEY') or os.getenv('GROQ_API_KEY')
    gemini_key = os.environ.get('GOOGLE_API_KEY') or os.getenv('GOOGLE_API_KEY')
    if not ((groq_key and Groq is not None) or (gemini_key and genai is not None)):
        raise Exception('No AI API key configured (set GROQ_API_KEY or GOOGLE_API_KEY).')

    topic_list = "\n".join([f"- {t}" for t in topics])
    prompt = f"""
You are producing a CONCISE review study guide section for this module:
Module: {module_name}

Output markdown with EXACT structure:

## {module_name}
### Module overview
- <4-8 bullets (high-signal)>

Then for EACH topic below:
### <Topic>
- <4-7 bullets>

Rules:
- Bullets must be short, high-signal (definition, key intuition/formula, common pitfall, when to use).
- Avoid long paragraphs.
- Do not include any content outside this structure.
- Write formulas using LaTeX with proper delimiters: use \\( ... \\) inline and $$ ... $$ for display.
- Do NOT wrap formulas in plain parentheses like "( ... )" unless those parentheses are part of the LaTeX itself.

Topics:
{topic_list}
""".strip()

    if groq_key and Groq is not None:
        client = Groq(api_key=groq_key)
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.25,
            max_tokens=2200
        )
        return resp.choices[0].message.content.strip()
    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel('gemini-pro')
    return (model.generate_content(prompt).text or '').strip()

def _fetch_latest_drive_guide(kind=None, folder_id=None):
    conn = get_db()
    try:
        if USE_POSTGRESQL:
            if kind and folder_id:
                cur = db_execute(conn, '''
                    SELECT id, folder_id, kind, content_markdown, created_at
                    FROM drive_guides
                    WHERE kind = %s AND folder_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ''', (kind, folder_id))
            elif kind:
                cur = db_execute(conn, '''
                    SELECT id, folder_id, kind, content_markdown, created_at
                    FROM drive_guides
                    WHERE kind = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ''', (kind,))
            elif folder_id:
                cur = db_execute(conn, '''
                    SELECT id, folder_id, kind, content_markdown, created_at
                    FROM drive_guides
                    WHERE folder_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ''', (folder_id,))
            else:
                cur = db_execute(conn, '''
                    SELECT id, folder_id, kind, content_markdown, created_at
                    FROM drive_guides
                    ORDER BY created_at DESC
                    LIMIT 1
                ''')
            row = db_fetchone(cur)
            cur.close()
            conn.close()
            return dict(row) if row else None
        # SQLite
        if kind and folder_id:
            cur = db_execute(conn, '''
                SELECT id, folder_id, kind, content_markdown, created_at
                FROM drive_guides
                WHERE kind = ? AND folder_id = ?
                ORDER BY created_at DESC
                LIMIT 1
            ''', (kind, folder_id))
        elif kind:
            cur = db_execute(conn, '''
                SELECT id, folder_id, kind, content_markdown, created_at
                FROM drive_guides
                WHERE kind = ?
                ORDER BY created_at DESC
                LIMIT 1
            ''', (kind,))
        elif folder_id:
            cur = db_execute(conn, '''
                SELECT id, folder_id, kind, content_markdown, created_at
                FROM drive_guides
                WHERE folder_id = ?
                ORDER BY created_at DESC
                LIMIT 1
            ''', (folder_id,))
        else:
            cur = db_execute(conn, '''
                SELECT id, folder_id, kind, content_markdown, created_at
                FROM drive_guides
                ORDER BY created_at DESC
                LIMIT 1
            ''')
        row = db_fetchone(cur)
        conn.close()
        if not row:
            return None
        return {'id': row[0], 'folder_id': row[1], 'kind': row[2], 'content_markdown': row[3], 'created_at': row[4]}
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None

def _ai_generate_ds_mid_guide(topic_inventory_lines):
    """
    Produce a mid-level DS interview review guide from an inventory of topics.
    Returns markdown.
    """
    groq_key = os.environ.get('GROQ_API_KEY') or os.getenv('GROQ_API_KEY')
    gemini_key = os.environ.get('GOOGLE_API_KEY') or os.getenv('GOOGLE_API_KEY')
    if not ((groq_key and Groq is not None) or (gemini_key and genai is not None)):
        raise Exception('No AI API key configured (set GROQ_API_KEY or GOOGLE_API_KEY).')

    inventory = "\n".join((topic_inventory_lines or [])[:220])
    prompt = f"""
You are writing a MID-LEVEL DATA SCIENCE interview study guide.
Use the topic inventory (extracted from course PDFs + notebooks) as your source of truth.

Output: ONE well-organized markdown document for quick review.

Structure (must follow):

# Mid-level Data Science Interview Review Guide
## How to use this
- <5 bullets: how to study, what to practice>

## Statistics & Experimentation
### <Subtopic>
- <4-7 bullets: definition/intuition, key formulas, assumptions, common pitfall, when to use, how interviewers ask it>
... (multiple subtopics)

## SQL & Analytics
...

## Machine Learning (practical)
...

## Product & Metrics
...

## Case Study Playbook
- <10-18 bullets: end-to-end checklist>

Rules:
- Keep bullets concise (no long paragraphs).
- Prefer DS interview framing over course framing.
- Include “common traps” explicitly inside bullets when relevant.
- Don’t invent random topics not implied by the inventory; if something’s missing, skip it.
- Write formulas using LaTeX with proper delimiters: use \\( ... \\) inline and $$ ... $$ for display.
- Do NOT wrap formulas in plain parentheses like "( ... )" unless those parentheses are part of the LaTeX itself.

Topic inventory:
{inventory}
""".strip()

    if groq_key and Groq is not None:
        client = Groq(api_key=groq_key)
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=2600
        )
        return resp.choices[0].message.content.strip()

    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel('gemini-pro')
    return (model.generate_content(prompt).text or '').strip()

def _parse_json_array_loose(text):
    """
    Best-effort parse of a JSON array from LLM output.
    Accepts raw JSON or fenced output; returns a Python list or [].
    """
    if not text:
        return []
    s = str(text).strip()
    # Strip common code fences
    s = re.sub(r'^\s*```(?:json)?\s*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s*```\s*$', '', s)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, list) else []
    except Exception:
        pass
    # Try to locate the first JSON array
    try:
        i = s.find('[')
        j = s.rfind(']')
        if i != -1 and j != -1 and j > i:
            obj = json.loads(s[i:j+1])
            return obj if isinstance(obj, list) else []
    except Exception:
        pass
    return []

def _ai_generate_flashcards_from_excerpt(excerpt, title_hint='', n_cards=8):
    """
    Generate flashcards strictly from provided excerpt.
    Returns a list of {q, a, level, source}.
    """
    excerpt = (excerpt or '').strip()
    if not excerpt:
        return []
    if n_cards < 3:
        n_cards = 3
    if n_cards > 20:
        n_cards = 20

    groq_key = os.environ.get('GROQ_API_KEY') or os.getenv('GROQ_API_KEY')
    gemini_key = os.environ.get('GOOGLE_API_KEY') or os.getenv('GOOGLE_API_KEY')
    if not ((groq_key and Groq is not None) or (gemini_key and genai is not None)):
        raise Exception('No AI API key configured (set GROQ_API_KEY or GOOGLE_API_KEY).')

    prompt = f"""
You are generating study flashcards from EXACT SOURCE MATERIAL.

SOURCE FILE: {title_hint or "(unknown)"}

Rules (must follow):
- ONLY use facts/definitions/formulas that are present in the excerpt below. Do NOT add outside knowledge.
- If the excerpt is insufficient for a card, SKIP it.
- Make cards interview-friendly: crisp Q, precise A.
- Use LaTeX for formulas with proper delimiters: \\( ... \\) inline and $$ ... $$ for display.
- Output MUST be valid JSON: an array of objects with keys:
  - "q": string
  - "a": string
  - "level": one of ["easy","medium","hard"]
  - "source": string (use the SOURCE FILE)
- No markdown, no commentary, no code fences.

Return {n_cards} cards max.

EXCERPT:
{excerpt}
""".strip()

    out_text = ''
    if groq_key and Groq is not None:
        client = Groq(api_key=groq_key)
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1600
        )
        out_text = (resp.choices[0].message.content or '').strip()
    else:
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel('gemini-pro')
        out_text = (model.generate_content(prompt).text or '').strip()

    cards = _parse_json_array_loose(out_text)
    cleaned = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        q = str(c.get('q') or '').strip()
        a = str(c.get('a') or '').strip()
        level = str(c.get('level') or '').strip().lower()
        source = str(c.get('source') or title_hint or '').strip()
        if not q or not a:
            continue
        if level not in ('easy', 'medium', 'hard'):
            level = 'medium'
        if not source:
            source = title_hint or ''
        cleaned.append({'q': q, 'a': a, 'level': level, 'source': source})
    return cleaned

def _normalize_math_delimiters_backend(markdown):
    """
    Normalize common LLM output like:
      "Mean: ( \\bar{x} = ... )"
    into KaTeX-friendly delimiters:
      "Mean: \\( \\bar{x} = ... \\)"

    This prevents regressions when the model ignores formatting instructions.
    """
    if not markdown:
        return markdown
    s = str(markdown)

    # Collapse double-escaped KaTeX delimiters (common in LLM output):
    # "\\(" -> "\(" and similarly for "\)", "\[", "\]".
    s = s.replace('\\\\(', '\\(').replace('\\\\)', '\\)')
    s = s.replace('\\\\[', '\\[').replace('\\\\]', '\\]')

    # Replace "( <latex-like> )" -> "\\( <latex-like> \\)" conservatively.
    # We only convert when the inside contains a LaTeX command or ^/_.
    def repl(m):
        inner = (m.group(1) or '').strip()
        if not inner:
            return m.group(0)
        if ('\\left' in inner) or ('\\right' in inner):
            return m.group(0)
        if not (re.search(r'\\[A-Za-z]+', inner) or re.search(r'[_^]', inner)):
            return m.group(0)
        return f"\\( {inner} \\)"

    return re.sub(r'\(\s*([^\)]*?)\s*\)', repl, s)

@app.route('/api/drive/guide/latest', methods=['GET'])
@drive_login_required
def drive_guide_latest():
    """Fetch the latest generated Drive guide."""
    kind = (request.args.get('kind') or '').strip().lower() or None
    folder_id = (request.args.get('folder_id') or '').strip() or None
    guide = _fetch_latest_drive_guide(kind=kind, folder_id=folder_id)
    if not guide:
        return jsonify({'error': 'No guide generated yet'}), 404
    return jsonify(guide)

@app.route('/api/drive/guide/generate', methods=['POST'])
@drive_login_required
def drive_guide_generate():
    """
    Generate a concise master study guide from extracted topics.
    Uses cached per-topic summaries when available; otherwise batches AI generation.
    """
    data = request.get_json(silent=True) or {}
    kind = (data.get('kind') or 'concise').strip().lower()
    if kind not in ('concise', 'ds_mid'):
        return jsonify({'error': 'Unsupported kind'}), 400

    max_topics = int(data.get('max_topics') or 160)
    if max_topics < 20:
        max_topics = 20
    if max_topics > 400:
        max_topics = 400

    folder_id = (data.get('folder_id') or get_setting('drive_last_folder_id', '') or '').strip()
    if not folder_id:
        return jsonify({'error': 'folder_id is required (index a folder first)'}), 400

    # Gather topics grouped by module from extracted files
    conn = get_db()
    rows = []
    try:
        if USE_POSTGRESQL:
            cur = db_execute(conn, '''
                SELECT extracted_topics_json, path, name
                FROM drive_files
                WHERE extracted_topics_json IS NOT NULL AND folder_id = %s
            ''', (folder_id,))
            rows = db_fetchall(cur)
            cur.close()
        else:
            cur = db_execute(conn, '''
                SELECT extracted_topics_json, path, name
                FROM drive_files
                WHERE extracted_topics_json IS NOT NULL AND folder_id = ?
            ''', (folder_id,))
            rows = db_fetchall(cur)
    finally:
        conn.close()

    module_topics = {}  # module -> {topic_key: {'title': str, 'count': int}}
    for r in rows:
        raw = dict(r).get('extracted_topics_json') if USE_POSTGRESQL else r[0]
        path = dict(r).get('path') if USE_POSTGRESQL else r[1]
        name = dict(r).get('name') if USE_POSTGRESQL else r[2]
        if not raw:
            continue
        try:
            obj = json.loads(raw)
            topics = obj.get('topics') or []
        except Exception:
            topics = []
        mod = _module_from_path(path) if path else _module_from_path(name)
        bucket = module_topics.setdefault(mod, {})
        for t in topics:
            s = str(t or '').strip()
            if not s or _is_noise_topic(s):
                continue
            k = _topic_key(s)
            if not k:
                continue
            if k not in bucket:
                bucket[k] = {'title': s, 'count': 0}
            bucket[k]['count'] += 1

    if not module_topics:
        return jsonify({'error': 'No extracted topics found yet. Run Extract Topics first.'}), 400

    # Order modules and topics
    ordered_modules = sorted(module_topics.keys(), key=_module_sort_key)
    module_payload = []
    total_topics = 0
    for mod in ordered_modules:
        items = list(module_topics[mod].values())
        items.sort(key=lambda x: (-int(x.get('count') or 0), str(x.get('title') or '')))
        titles = [it['title'] for it in items][:60]
        if not titles:
            continue
        module_payload.append((mod, titles))
        total_topics += len(titles)
        if total_topics >= max_topics:
            break

    if not module_payload:
        return jsonify({'error': 'No usable topics found after filtering. Try extracting more files.'}), 400

    created_at = datetime.now(timezone.utc).isoformat()

    if kind == 'ds_mid':
        inv = []
        for mod, topics in module_payload:
            for t in topics[:40]:
                k = _topic_key(t)
                freq = 0
                try:
                    freq = int(module_topics.get(mod, {}).get(k, {}).get('count') or 0)
                except Exception:
                    freq = 0
                inv.append(f"- [{mod}] {t}{f' ({freq}x)' if freq else ''}")
        content = _ai_generate_ds_mid_guide(inv)
    else:
        # Build guide module-by-module (more coherent than a flat topic dump).
        sections = []
        toc_lines = []
        for mod, topics in module_payload:
            toc_lines.append(f"- {mod}")
            for t in topics[:35]:
                toc_lines.append(f"  - {t}")

            cached_sections = []
            missing = []
            for t in topics[:35]:
                cached = _get_drive_topic_summary(t)
                if cached:
                    cached_sections.append(re.sub(r'(?m)^##\\s+', '### ', cached.strip(), count=1))
                else:
                    missing.append(t)

            module_md = _ai_concise_module_review(mod, missing[:18]) if missing else f"## {mod}\n### Module overview\n- (topics already summarized from cache)\n"

            parts = re.split(r'(?m)^###\\s+', module_md.strip())
            for p in parts:
                if not p.strip():
                    continue
                sec = "### " + p.strip()
                heading = sec.splitlines()[0].replace('###', '').strip()
                if heading and heading.lower() not in ('module overview',):
                    _set_drive_topic_summary(heading, re.sub(r'(?m)^###\\s+', '## ', sec, count=1))

            if not module_md.lstrip().startswith(f"## {mod}"):
                module_md = f"## {mod}\n" + module_md
            sections.append(module_md.strip())
            if cached_sections:
                sections.append("\n".join(cached_sections).strip())

        header = f"# Concise Course Review Guide (Drive)\n\nGenerated: {created_at}\n\n"
        intro = "Concise, high-signal review notes to refresh core concepts quickly.\n\n"
        toc = "## Table of contents\n" + "\n".join(toc_lines) + "\n\n"
        content = header + intro + toc + "\n\n".join(sections).strip() + "\n"

    # Safety-net: normalize common "( \\latex ... )" into "\\( ... \\)" so KaTeX renders reliably.
    content = _normalize_math_delimiters_backend(content)

    # Store as latest guide row
    conn = get_db()
    try:
        if USE_POSTGRESQL:
            cur = db_execute(conn, '''
                INSERT INTO drive_guides (folder_id, kind, content_markdown, created_at)
                VALUES (%s, %s, %s, %s)
            ''', (folder_id, kind, content, created_at))
            cur.close()
        else:
            db_execute(conn, '''
                INSERT INTO drive_guides (folder_id, kind, content_markdown, created_at)
                VALUES (?, ?, ?, ?)
            ''', (folder_id, kind, content, created_at))
        conn.commit()
    finally:
        conn.close()

    return jsonify({'message': 'Guide generated', 'kind': kind, 'created_at': created_at, 'modules': len(module_payload)})

@app.route('/api/drive/flashcards/generate', methods=['POST'])
@drive_login_required
def drive_flashcards_generate():
    """
    Generate a flashcard deck grounded in the exact source material excerpts.
    Requires that /api/drive/extract-topics has been run (it stores text excerpts).
    """
    data = request.get_json(silent=True) or {}
    folder_id = (data.get('folder_id') or get_setting('drive_last_folder_id', '') or '').strip()
    if not folder_id:
        return jsonify({'error': 'folder_id is required (index a folder first)'}), 400

    svc = _drive_service_for_session()
    if not svc:
        return jsonify({'error': 'Drive not connected', 'auth_url': '/auth/login?drive=1'}), 401

    kind = (data.get('kind') or 'ds_mid').strip().lower()
    if kind not in ('ds_mid', 'course', 'concise'):
        kind = 'ds_mid'

    limit_files = int(data.get('limit_files') or 6)
    limit_files = max(1, min(25, limit_files))

    cards_per_file = int(data.get('cards_per_file') or 8)
    cards_per_file = max(3, min(20, cards_per_file))

    max_total = int(data.get('max_total') or 120)
    max_total = max(10, min(300, max_total))

    # Pull a larger candidate set; we'll auto-fill missing excerpts for the most recent files.
    conn = get_db()
    candidates = []
    try:
        if USE_POSTGRESQL:
            cur = db_execute(conn, '''
                SELECT file_id, name, path, mime_type, text_excerpt
                FROM drive_files
                WHERE folder_id = %s
                ORDER BY extracted_at DESC NULLS LAST, indexed_at DESC NULLS LAST
                LIMIT %s
            ''', (folder_id, max(limit_files * 4, limit_files),))
            candidates = [dict(r) for r in db_fetchall(cur)]
            cur.close()
        else:
            cur = db_execute(conn, '''
                SELECT file_id, name, path, mime_type, text_excerpt
                FROM drive_files
                WHERE folder_id = ?
                ORDER BY extracted_at DESC, indexed_at DESC
                LIMIT ?
            ''', (folder_id, max(limit_files * 4, limit_files),))
            rows = db_fetchall(cur)
            candidates = [{'file_id': r[0], 'name': r[1], 'path': r[2], 'mime_type': r[3], 'text_excerpt': r[4]} for r in rows]
    finally:
        conn.close()

    if not candidates:
        return jsonify({'error': 'No indexed files found for this folder. Run Index Folder first.'}), 400

    # Auto-fill excerpts (fast) if missing, so user doesn't have to rerun extraction with force.
    filled = 0
    for f in candidates:
        if filled >= limit_files:
            break
        if f.get('text_excerpt'):
            filled += 1
            continue
        file_id = f.get('file_id')
        name = (f.get('name') or '')
        mime = (f.get('mime_type') or '')
        if not file_id:
            continue
        try:
            raw = _drive_download_bytes(svc, file_id)
            is_pdf = mime == 'application/pdf' or name.lower().endswith('.pdf')
            is_ipynb = name.lower().endswith('.ipynb') or mime in ('application/x-ipynb+json',)
            if is_ipynb:
                text = _extract_text_ipynb(raw)
            elif is_pdf:
                text = _extract_text_pdf(raw)
            else:
                continue
            excerpt = (text or '').replace('\x00', '').strip()[:20000]
            if not excerpt:
                continue
            conn = get_db()
            try:
                if USE_POSTGRESQL:
                    cur = db_execute(conn, 'UPDATE drive_files SET text_excerpt = %s WHERE file_id = %s', (excerpt, file_id))
                    cur.close()
                else:
                    db_execute(conn, 'UPDATE drive_files SET text_excerpt = ? WHERE file_id = ?', (excerpt, file_id))
                conn.commit()
            finally:
                conn.close()
            f['text_excerpt'] = excerpt
            filled += 1
        except Exception:
            # Non-fatal; we'll keep going.
            continue

    files = [f for f in candidates if (f.get('text_excerpt') and str(f.get('text_excerpt')).strip())][:limit_files]
    if not files:
        return jsonify({'error': 'Could not extract any usable excerpts. Try Extract Topics, or check that folder contains PDFs/.ipynb.'}), 400

    cards = []
    seen = set()
    per_file_results = []
    for f in files:
        if len(cards) >= max_total:
            break
        name = (f.get('name') or '').strip()
        path = (f.get('path') or '').strip()
        title_hint = name or path or (f.get('file_id') or '')
        excerpt = f.get('text_excerpt') or ''
        try:
            new_cards = _ai_generate_flashcards_from_excerpt(excerpt, title_hint=title_hint, n_cards=cards_per_file)
            added = 0
            for c in new_cards:
                q = str(c.get('q') or '').strip()
                a = str(c.get('a') or '').strip()
                key = (q.lower(), a.lower())
                if not q or not a:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                cards.append(c)
                added += 1
                if len(cards) >= max_total:
                    break
            per_file_results.append({'file': title_hint, 'cards': added})
        except Exception as e:
            per_file_results.append({'file': title_hint, 'error': str(e)})

    deck_obj = {
        'kind': kind,
        'folder_id': folder_id,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'cards': cards,
        'files': per_file_results
    }

    # Persist deck
    conn = get_db()
    try:
        now = deck_obj['created_at']
        deck_json = json.dumps(deck_obj, ensure_ascii=False)
        if USE_POSTGRESQL:
            cur = db_execute(conn, '''
                INSERT INTO drive_flashcard_decks (folder_id, kind, deck_json, created_at)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            ''', (folder_id, kind, deck_json, now))
            row = db_fetchone(cur)
            cur.close()
            deck_id = dict(row).get('id') if row else None
        else:
            cur = db_execute(conn, '''
                INSERT INTO drive_flashcard_decks (folder_id, kind, deck_json, created_at)
                VALUES (?, ?, ?, ?)
            ''', (folder_id, kind, deck_json, now))
            deck_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    return jsonify({'ok': True, 'id': deck_id, 'kind': kind, 'folder_id': folder_id, 'card_count': len(cards), 'files': per_file_results})

def _fetch_latest_flashcard_deck(kind=None, folder_id=None):
    conn = get_db()
    try:
        if USE_POSTGRESQL:
            if kind and folder_id:
                cur = db_execute(conn, '''
                    SELECT id, folder_id, kind, deck_json, created_at
                    FROM drive_flashcard_decks
                    WHERE kind = %s AND folder_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ''', (kind, folder_id))
            elif kind:
                cur = db_execute(conn, '''
                    SELECT id, folder_id, kind, deck_json, created_at
                    FROM drive_flashcard_decks
                    WHERE kind = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ''', (kind,))
            elif folder_id:
                cur = db_execute(conn, '''
                    SELECT id, folder_id, kind, deck_json, created_at
                    FROM drive_flashcard_decks
                    WHERE folder_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ''', (folder_id,))
            else:
                cur = db_execute(conn, '''
                    SELECT id, folder_id, kind, deck_json, created_at
                    FROM drive_flashcard_decks
                    ORDER BY created_at DESC
                    LIMIT 1
                ''')
            row = db_fetchone(cur)
            cur.close()
            conn.close()
            if not row:
                return None
            d = dict(row)
            return {'id': d.get('id'), 'folder_id': d.get('folder_id'), 'kind': d.get('kind'), 'deck_json': d.get('deck_json'), 'created_at': d.get('created_at')}
        # SQLite
        if kind and folder_id:
            cur = db_execute(conn, '''
                SELECT id, folder_id, kind, deck_json, created_at
                FROM drive_flashcard_decks
                WHERE kind = ? AND folder_id = ?
                ORDER BY created_at DESC
                LIMIT 1
            ''', (kind, folder_id))
        elif kind:
            cur = db_execute(conn, '''
                SELECT id, folder_id, kind, deck_json, created_at
                FROM drive_flashcard_decks
                WHERE kind = ?
                ORDER BY created_at DESC
                LIMIT 1
            ''', (kind,))
        elif folder_id:
            cur = db_execute(conn, '''
                SELECT id, folder_id, kind, deck_json, created_at
                FROM drive_flashcard_decks
                WHERE folder_id = ?
                ORDER BY created_at DESC
                LIMIT 1
            ''', (folder_id,))
        else:
            cur = db_execute(conn, '''
                SELECT id, folder_id, kind, deck_json, created_at
                FROM drive_flashcard_decks
                ORDER BY created_at DESC
                LIMIT 1
            ''')
        row = db_fetchone(cur)
        conn.close()
        if not row:
            return None
        return {'id': row[0], 'folder_id': row[1], 'kind': row[2], 'deck_json': row[3], 'created_at': row[4]}
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None

@app.route('/api/drive/flashcards/latest', methods=['GET'])
@drive_login_required
def drive_flashcards_latest():
    kind = (request.args.get('kind') or '').strip().lower() or None
    folder_id = (request.args.get('folder_id') or '').strip() or None
    deck = _fetch_latest_flashcard_deck(kind=kind, folder_id=folder_id)
    if not deck:
        return jsonify({'error': 'No flashcard deck generated yet'}), 404
    return jsonify(deck)

@app.route('/drive/flashcards/view/latest', methods=['GET'])
@drive_login_required
def drive_flashcards_view_latest():
    kind = (request.args.get('kind') or '').strip().lower() or None
    folder_id = (request.args.get('folder_id') or '').strip() or None
    deck = _fetch_latest_flashcard_deck(kind=kind, folder_id=folder_id)
    if not deck:
        return "No flashcard deck generated yet.", 404
    try:
        obj = json.loads(deck.get('deck_json') or '{}')
    except Exception:
        obj = {'cards': []}
    return render_template('drive_flashcards_view.html', deck=obj)

@app.route('/favicon.ico')
def favicon():
    # Return 204 No Content to prevent 404 errors
    return '', 204

@app.route('/api/topics', methods=['GET'])
def get_topics_config():
    """Get topics configuration from JSON file"""
    try:
        with open('topics.json', 'r') as f:
            data = json.load(f)
            return jsonify(data)
    except FileNotFoundError:
        return jsonify({'categories': [], 'uncategorized_topics': []})
    except json.JSONDecodeError:
        return jsonify({'error': 'Invalid JSON file'}), 500

@app.route('/api/topics', methods=['POST'])
@login_required
def save_topics_config():
    """Save topics configuration to JSON file"""
    try:
        data = request.json
        # Validate structure
        if 'categories' not in data:
            return jsonify({'error': 'Missing categories field'}), 400
        
        # Backup existing file
        import shutil
        try:
            shutil.copy('topics.json', 'topics.json.backup')
        except:
            pass  # No backup if file doesn't exist
        
        # Write new data
        with open('topics.json', 'w') as f:
            json.dump(data, f, indent=2)
        
        return jsonify({'message': 'Topics configuration saved successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/settings', methods=['GET'])
@login_required
def get_admin_settings():
    """Get admin-editable settings."""
    # Defaults
    flashcards_count = int(get_setting('flashcards_count', '15') or 15)
    return jsonify({
        'flashcards_count': flashcards_count
    })

@app.route('/api/admin/settings', methods=['POST'])
@login_required
def update_admin_settings():
    """Update admin-editable settings."""
    data = request.get_json(silent=True) or {}
    if 'flashcards_count' in data:
        try:
            val = int(data.get('flashcards_count'))
            # Keep it sane
            if val < 5 or val > 40:
                return jsonify({'error': 'flashcards_count must be between 5 and 40'}), 400
            set_setting('flashcards_count', str(val))
        except Exception:
            return jsonify({'error': 'flashcards_count must be an integer'}), 400
    return jsonify({'message': 'Settings saved'})

@app.route('/api/debug/check-key', methods=['GET'])
def check_api_key():
    """Debug endpoint to check if API key is accessible"""
    groq_key = os.environ.get('GROQ_API_KEY') or os.getenv('GROQ_API_KEY')
    return jsonify({
        'has_groq_key': bool(groq_key),
        'key_length': len(groq_key) if groq_key else 0,
        'key_prefix': groq_key[:10] + '...' if groq_key else None,
        'groq_available': Groq is not None
    })

@app.route('/api/interviews', methods=['GET'])
def get_interviews():
    try:
        conn = get_db()
        # PostgreSQL requires all non-aggregated columns in GROUP BY
        if USE_POSTGRESQL:
            cursor = db_execute(conn, '''
                SELECT i.id, i.company, i.position, i.interview_date, i.created_at, i.status,
                       COUNT(DISTINCT t.id) as topic_count,
                       COUNT(DISTINCT CASE WHEN t.status = 'completed' THEN t.id END) as completed_topics
                FROM interviews i
                LEFT JOIN topics t ON i.id = t.interview_id
                WHERE i.status = 'active'
                GROUP BY i.id, i.company, i.position, i.interview_date, i.created_at, i.status
                ORDER BY CASE WHEN i.interview_date IS NULL THEN 1 ELSE 0 END, i.interview_date ASC, i.created_at DESC
            ''')
        else:
            cursor = db_execute(conn, '''
                SELECT i.*, 
                       COUNT(DISTINCT t.id) as topic_count,
                       COUNT(DISTINCT CASE WHEN t.status = 'completed' THEN t.id END) as completed_topics
                FROM interviews i
                LEFT JOIN topics t ON i.id = t.interview_id
                WHERE i.status = 'active'
                GROUP BY i.id
                ORDER BY CASE WHEN i.interview_date IS NULL THEN 1 ELSE 0 END, i.interview_date ASC, i.created_at DESC
            ''')
        interviews = db_fetchall(cursor)
        if USE_POSTGRESQL:
            cursor.close()
        conn.close()
        
        # Always return an array, even if empty
        result = [dict(row) for row in interviews] if interviews else []
        return jsonify(result)
    except Exception as e:
        import traceback
        error_msg = str(e)
        traceback.print_exc()
        print(f"Error in get_interviews: {error_msg}")
        app.logger.error(f"Error loading interviews: {error_msg}")
        # Return empty array on error so frontend doesn't break, but log the error
        # The error will be visible in Railway logs
        return jsonify([])

@app.route('/api/interviews', methods=['POST'])
def create_interview():
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        company = data.get('company', '').strip()
        # Default to generic US company if blank
        if not company:
            company = 'Generic Company (US)'
        
        interview_date = data.get('interview_date', '').strip()
        # Allow empty interview date
        
        conn = get_db()
        try:
            if USE_POSTGRESQL:
                cursor = db_execute(conn, '''
                    INSERT INTO interviews (company, position, interview_date, created_at)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                ''', (company, data.get('position', ''), 
                      interview_date if interview_date else None, datetime.now().isoformat()))
                result = db_fetchone(cursor)
                interview_id = result['id'] if result else None
                cursor.close()
            else:
                cursor = db_execute(conn, '''
                    INSERT INTO interviews (company, position, interview_date, created_at)
                    VALUES (?, ?, ?, ?)
                ''', (company, data.get('position', ''), 
                      interview_date if interview_date else None, datetime.now().isoformat()))
                interview_id = db_lastrowid(cursor, conn)
            conn.commit()
            conn.close()
            
            if not interview_id:
                return jsonify({'error': 'Failed to create study material'}), 500
            
            return jsonify({'id': interview_id, 'message': 'Study material created successfully'}), 201
        except Exception as db_error:
            conn.close()
            raise db_error
    except Exception as e:
        import traceback
        error_msg = str(e)
        traceback.print_exc()
        print(f"Error in create_interview: {error_msg}")
        app.logger.error(f"Error creating interview: {error_msg}")
        return jsonify({'error': f'Failed to create study material: {error_msg}'}), 500

@app.route('/api/interviews/<int:interview_id>', methods=['GET'])
def get_interview(interview_id):
    conn = get_db()
    cursor = db_execute(conn, 'SELECT * FROM interviews WHERE id = ?', (interview_id,))
    interview = db_fetchone(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    if not interview:
        conn.close()
        return jsonify({'error': 'Study material not found'}), 404
    
    cursor = db_execute(conn, 'SELECT * FROM topics WHERE interview_id = ? ORDER BY COALESCE(category_name, \'\'), priority DESC, topic_name ASC', 
                         (interview_id,))
    topics = db_fetchall(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    conn.close()
    
    interview_dict = dict(interview)
    # Convert topics to dicts and ensure no None values become strings
    topics_list = []
    for topic in topics:
        topic_dict = dict(topic)
        # Ensure topic_name is not None
        if topic_dict.get('topic_name') is None:
            topic_dict['topic_name'] = 'Untitled Topic'
        topics_list.append(topic_dict)
    interview_dict['topics'] = topics_list
    return jsonify(interview_dict)

@app.route('/api/interviews/<int:interview_id>', methods=['DELETE'])
def delete_interview(interview_id):
    conn = get_db()
    # Check if interview exists
    cursor = db_execute(conn, 'SELECT * FROM interviews WHERE id = ?', (interview_id,))
    interview = db_fetchone(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    if not interview:
        conn.close()
        return jsonify({'error': 'Study material not found'}), 404
    
    # Delete all related topics first (due to foreign key)
    cursor = db_execute(conn, 'DELETE FROM topics WHERE interview_id = ?', (interview_id,))
    if USE_POSTGRESQL:
        cursor.close()
    # Delete study sessions
    cursor = db_execute(conn, 'DELETE FROM study_sessions WHERE interview_id = ?', (interview_id,))
    if USE_POSTGRESQL:
        cursor.close()
    # Delete the interview
    cursor = db_execute(conn, 'DELETE FROM interviews WHERE id = ?', (interview_id,))
    if USE_POSTGRESQL:
        cursor.close()
    conn.commit()
    conn.close()
    return jsonify({'message': 'Study material deleted successfully'})

@app.route('/api/interviews/<int:interview_id>/topics', methods=['POST'])
def add_topic(interview_id):
    data = request.json
    topic_name = data.get('topic_name', '').strip()
    
    conn = get_db()
    cursor = db_execute(conn, 'SELECT * FROM interviews WHERE id = ?', (interview_id,))
    interview = db_fetchone(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    if not interview:
        conn.close()
        return jsonify({'error': 'Study material not found'}), 404
    
    position = dict(interview).get('position', 'Data Scientist')

    # If topic name is blank, generate common topics for the position
    if not topic_name:
        topics = generate_common_topics(position)
        topic_ids = []
        for topic in topics:
            if USE_POSTGRESQL:
                cursor = db_execute(conn, '''
                    INSERT INTO topics (interview_id, topic_name, category_name, priority, notes)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                ''', (interview_id, topic['name'], topic.get('category', None), 
                      topic.get('priority', 'medium'), topic.get('notes', '')))
                result = db_fetchone(cursor)
                new_id = result['id'] if result else None
                topic_ids.append(new_id)
                if new_id:
                    _hydrate_topic_ai_from_cache(conn, new_id, position, topic['name'], topic.get('category', None))
                cursor.close()
            else:
                cursor = db_execute(conn, '''
                    INSERT INTO topics (interview_id, topic_name, category_name, priority, notes)
                    VALUES (?, ?, ?, ?, ?)
                ''', (interview_id, topic['name'], topic.get('category', None), 
                      topic.get('priority', 'medium'), topic.get('notes', '')))
                new_id = db_lastrowid(cursor, conn)
                topic_ids.append(new_id)
                if new_id:
                    _hydrate_topic_ai_from_cache(conn, new_id, position, topic['name'], topic.get('category', None))
        conn.commit()
        conn.close()
        return jsonify({'ids': topic_ids, 'topics': topics, 'message': f'{len(topics)} common topics added successfully'}), 201
    
    # Add single topic
    if USE_POSTGRESQL:
        cursor = db_execute(conn, '''
            INSERT INTO topics (interview_id, topic_name, category_name, priority, notes)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        ''', (interview_id, topic_name, data.get('category_name'), data.get('priority', 'medium'), 
              data.get('notes', '')))
        result = db_fetchone(cursor)
        topic_id = result['id'] if result else None
        if topic_id:
            _hydrate_topic_ai_from_cache(conn, topic_id, position, topic_name, data.get('category_name'))
        cursor.close()
    else:
        cursor = db_execute(conn, '''
            INSERT INTO topics (interview_id, topic_name, category_name, priority, notes)
            VALUES (?, ?, ?, ?, ?)
        ''', (interview_id, topic_name, data.get('category_name'), data.get('priority', 'medium'), 
              data.get('notes', '')))
        topic_id = db_lastrowid(cursor, conn)
        if topic_id:
            _hydrate_topic_ai_from_cache(conn, topic_id, position, topic_name, data.get('category_name'))
    conn.commit()
    conn.close()
    return jsonify({'id': topic_id, 'message': 'Topic added successfully'}), 201

@app.route('/api/topics/<int:topic_id>', methods=['PUT'])
def update_topic(topic_id):
    data = request.json
    conn = get_db()
    
    # Get existing topic to preserve fields not being updated
    cursor = db_execute(conn, 'SELECT * FROM topics WHERE id = ?', (topic_id,))
    existing = db_fetchone(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    if not existing:
        conn.close()
        return jsonify({'error': 'Topic not found'}), 404
    
    existing_dict = dict(existing)
    
    # Update only provided fields, keep existing values for others
    topic_name = data.get('topic_name', existing_dict.get('topic_name'))
    priority = data.get('priority', existing_dict.get('priority'))
    status = data.get('status', existing_dict.get('status'))
    notes = data.get('notes', existing_dict.get('notes'))
    ai_guidance = data.get('ai_guidance', existing_dict.get('ai_guidance'))
    
    if USE_POSTGRESQL:
        cursor = db_execute(conn, '''
            UPDATE topics 
            SET topic_name = %s, priority = %s, status = %s, notes = %s, ai_guidance = %s
            WHERE id = %s
        ''', (topic_name, priority, status, notes, ai_guidance, topic_id))
        cursor.close()
    else:
        db_execute(conn, '''
            UPDATE topics 
            SET topic_name = ?, priority = ?, status = ?, notes = ?, ai_guidance = ?
            WHERE id = ?
        ''', (topic_name, priority, status, notes, ai_guidance, topic_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Topic updated successfully'})

@app.route('/api/topics/<int:topic_id>', methods=['DELETE'])
def delete_topic(topic_id):
    conn = get_db()
    cursor = db_execute(conn, 'DELETE FROM topics WHERE id = ?', (topic_id,))
    if USE_POSTGRESQL:
        cursor.close()
    conn.commit()
    conn.close()
    return jsonify({'message': 'Topic deleted successfully'})

@app.route('/api/interviews/<int:interview_id>/refresh-topics', methods=['POST'])
def refresh_topics(interview_id):
    """Refresh topics for an interview from topics.json - only updates categorized topics, preserves uncategorized"""
    conn = get_db()
    cursor = db_execute(conn, 'SELECT * FROM interviews WHERE id = ?', (interview_id,))
    interview = db_fetchone(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    if not interview:
        conn.close()
        return jsonify({'error': 'Study material not found'}), 404
    
    # Only delete topics that have a category_name (from topics.json)
    # Preserve uncategorized topics (category_name is NULL or empty)
    if USE_POSTGRESQL:
        cursor = db_execute(conn, 'DELETE FROM topics WHERE interview_id = %s AND category_name IS NOT NULL AND category_name != %s', (interview_id, ''))
    else:
        cursor = db_execute(conn, 'DELETE FROM topics WHERE interview_id = ? AND category_name IS NOT NULL AND category_name != ?', (interview_id, ''))
    if USE_POSTGRESQL:
        cursor.close()
    
    position = dict(interview).get('position', 'Data Scientist')

    # Generate new topics from topics.json
    topics = generate_common_topics(position)
    topic_ids = []
    for topic in topics:
        if USE_POSTGRESQL:
            cursor = db_execute(conn, '''
                INSERT INTO topics (interview_id, topic_name, category_name, priority, notes)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            ''', (interview_id, topic['name'], topic.get('category', None), 
                  topic.get('priority', 'medium'), topic.get('notes', '')))
            result = db_fetchone(cursor)
            new_id = result['id'] if result else None
            topic_ids.append(new_id)
            if new_id:
                _hydrate_topic_ai_from_cache(conn, new_id, position, topic['name'], topic.get('category', None))
            cursor.close()
        else:
            cursor = db_execute(conn, '''
                INSERT INTO topics (interview_id, topic_name, category_name, priority, notes)
                VALUES (?, ?, ?, ?, ?)
            ''', (interview_id, topic['name'], topic.get('category', None), 
                  topic.get('priority', 'medium'), topic.get('notes', '')))
            new_id = db_lastrowid(cursor, conn)
            topic_ids.append(new_id)
            if new_id:
                _hydrate_topic_ai_from_cache(conn, new_id, position, topic['name'], topic.get('category', None))
    
    conn.commit()
    conn.close()
    return jsonify({'ids': topic_ids, 'topics': topics, 'message': f'{len(topics)} topics refreshed from topics.json'}), 200

@app.route('/api/topics/<int:topic_id>/ai-guidance', methods=['POST'])
def generate_ai_guidance(topic_id):
    """Generate AI-powered study guidance for a topic based on the position"""
    data = request.get_json(silent=True) or {}
    force = bool(data.get('force')) or str(request.args.get('force', '')).lower() in ('1', 'true', 'yes')
    conn = get_db()
    cursor = db_execute(conn, 'SELECT * FROM topics WHERE id = ?', (topic_id,))
    topic = db_fetchone(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    if not topic:
        conn.close()
        return jsonify({'error': 'Topic not found'}), 404
    
    cursor = db_execute(conn, 'SELECT * FROM interviews WHERE id = ?', (dict(topic)['interview_id'],))
    interview = db_fetchone(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    if not interview:
        conn.close()
        return jsonify({'error': 'Study material not found'}), 404
    
    conn.close()
    
    position = dict(interview).get('position', 'Data Scientist')
    topic_name = dict(topic).get('topic_name', '')
    category_name = dict(topic).get('category_name')
    existing_ai_guidance = dict(topic).get('ai_guidance')
    # If we already have guidance saved for this topic, return it (unless forced)
    if existing_ai_guidance and not force:
        return jsonify({'ai_guidance': existing_ai_guidance, 'message': 'Using cached AI guidance'})

    parent_path_raw = category_name.strip() if isinstance(category_name, str) and category_name.strip() else None
    parent_path_display = parent_path_raw.replace(' > ', ' → ') if parent_path_raw else None
    full_topic_path = f"{parent_path_display} → {topic_name}" if parent_path_display else topic_name
    parent_context = f"\nTopic path: {full_topic_path}\n" if full_topic_path else ""

    # Global cache: reuse across interviews when possible (unless forced)
    if not force:
        # Use raw " > " path for a stable key (matches topics.json storage), plus the leaf topic
        topic_path_key_source = f"{parent_path_raw} > {topic_name}" if parent_path_raw else topic_name
        cached = _get_cached_ai_guidance(position, topic_name, topic_path_key_source)
        if cached:
            _save_ai_guidance(topic_id, cached)
            return jsonify({'ai_guidance': cached, 'message': 'Using global cached AI guidance'})
    
    prompt = f"""You are an expert interview preparation coach specializing in {position} roles. Provide comprehensive, interview-focused guidance for: {topic_name}{parent_context}

For this topic, break it down into specific, actionable learning points that are commonly tested in interviews. For each point, include:

1. **Core Concept**: What is it? (1-2 sentences)
2. **Interview Focus**: What specific aspects are typically tested? (common questions, problem types, edge cases)
3. **Practical Application**: How is this used in real work? (examples, use cases)
4. **Key Details to Know**: Important nuances, gotchas, or advanced points

Structure your response as:
- Start with a title line: **Topic:** {topic_name}
- Then a short **Where this fits** section (2-4 bullets) that explicitly references the topic path (if provided) and calls out key prerequisites.
- Use **bold** for subtopic names
- Use bullet points for details under each subtopic
- Be specific and actionable - focus on what candidates actually need to know
- Include concrete examples when helpful
- Prioritize interview-relevant information over theoretical depth

Keep it concise but comprehensive - aim for 3-5 main subtopics with 2-4 key points each. Focus on practical knowledge that helps someone prepare effectively for interviews."""

    # Try Groq first (fastest, good free tier)
    groq_key = os.environ.get('GROQ_API_KEY') or os.getenv('GROQ_API_KEY')
    if groq_key and Groq:
        try:
            client = Groq(api_key=groq_key)
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",  # Fast and free
                messages=[
                    {"role": "system", "content": "You are an expert interview preparation coach with deep knowledge of technical interviews. Your guidance is practical, interview-focused, and actionable. You break down complex topics into learnable components, emphasize what's actually tested, and provide concrete examples. You use clear formatting with bold headers and bullet points."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=500,
                temperature=0.7
            )
            ai_guidance = response.choices[0].message.content.strip()
            _save_ai_guidance(topic_id, ai_guidance)
            topic_path_key_source = f"{parent_path_raw} > {topic_name}" if parent_path_raw else topic_name
            _upsert_cached_ai_guidance(position, topic_name, topic_path_key_source, ai_guidance, model_provider='groq', model_name="llama-3.1-8b-instant")
            return jsonify({'ai_guidance': ai_guidance, 'message': 'AI guidance generated successfully'})
        except Exception as e:
            # Log the error for debugging
            error_msg = str(e)
            import traceback
            print(f"Groq API error: {error_msg}")
            print(traceback.format_exc())
            # Return the error so we can see what's wrong
            return jsonify({'error': f'Groq API error: {error_msg}. Check server logs for details.'}), 500
    
    # Try Google Gemini (good free tier: 60 req/min)
    gemini_key = os.getenv('GOOGLE_API_KEY')
    if gemini_key and genai:
        try:
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel('gemini-pro')
            full_prompt = f"You are an expert interview preparation coach specializing in technical roles. Provide comprehensive, interview-focused guidance with clear structure and practical examples.\n\n{prompt}"
            response = model.generate_content(
                full_prompt,
                generation_config={
                    'max_output_tokens': 400,
                    'temperature': 0.7,
                }
            )
            ai_guidance = response.text.strip()
            _save_ai_guidance(topic_id, ai_guidance)
            topic_path_key_source = f"{parent_path_raw} > {topic_name}" if parent_path_raw else topic_name
            _upsert_cached_ai_guidance(position, topic_name, topic_path_key_source, ai_guidance, model_provider='gemini', model_name='gemini-pro')
            return jsonify({'ai_guidance': ai_guidance, 'message': 'AI guidance generated successfully'})
        except Exception as e:
            # Fall through to error
            pass
    
    # No API keys configured or both failed
    groq_key = os.environ.get('GROQ_API_KEY') or os.getenv('GROQ_API_KEY')
    gemini_key = os.getenv('GOOGLE_API_KEY')
    
    error_msg = 'Failed to generate AI guidance.'
    if not groq_key and not gemini_key:
        error_msg = 'No AI API key configured. Set GROQ_API_KEY or GOOGLE_API_KEY environment variable.\n\nFree options:\n- Groq: https://console.groq.com (fast, generous free tier)\n- Google Gemini: https://makersuite.google.com/app/apikey (60 requests/min free)'
    elif groq_key and not Groq:
        error_msg = 'Groq library not available. Please reinstall: pip install groq'
    elif gemini_key and not genai:
        error_msg = 'Google Gemini library not available. Please reinstall: pip install google-generativeai'
    
    return jsonify({'error': error_msg}), 500

def _save_ai_guidance(topic_id, ai_guidance):
    """Helper function to save AI guidance to database"""
    conn = get_db()
    cursor = db_execute(conn, 'UPDATE topics SET ai_guidance = ? WHERE id = ?', (ai_guidance, topic_id))
    if USE_POSTGRESQL:
        cursor.close()
    conn.commit()
    conn.close()

def _save_ai_notes(topic_id, ai_notes):
    """Helper function to save AI study notes to database"""
    conn = get_db()
    cursor = db_execute(conn, 'UPDATE topics SET ai_notes = ? WHERE id = ?', (ai_notes, topic_id))
    if USE_POSTGRESQL:
        cursor.close()
    conn.commit()
    conn.close()

@app.route('/api/topics/<int:topic_id>/study-notes', methods=['POST'])
def generate_study_notes(topic_id):
    """
    Generate (or reuse) per-topic study notes compiled from AI guidance.
    Cache behavior:
    - If topics.ai_notes exists, return it unless force=1 / {"force": true}
    - Else try global study_notes_cache keyed by (position, topic, topic_path)
    """
    data = request.get_json(silent=True) or {}
    force = bool(data.get('force')) or str(request.args.get('force', '')).lower() in ('1', 'true', 'yes')

    conn = get_db()
    cursor = db_execute(conn, 'SELECT * FROM topics WHERE id = ?', (topic_id,))
    topic = db_fetchone(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    if not topic:
        conn.close()
        return jsonify({'error': 'Topic not found'}), 404

    cursor = db_execute(conn, 'SELECT * FROM interviews WHERE id = ?', (dict(topic)['interview_id'],))
    interview = db_fetchone(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    if not interview:
        conn.close()
        return jsonify({'error': 'Study material not found'}), 404
    conn.close()

    position = dict(interview).get('position', 'Data Scientist')
    topic_name = dict(topic).get('topic_name', '')
    category_name = dict(topic).get('category_name')
    parent_path_raw = category_name.strip() if isinstance(category_name, str) and category_name.strip() else None
    topic_path_key_source = f"{parent_path_raw} > {topic_name}" if parent_path_raw else topic_name

    existing_notes = dict(topic).get('ai_notes')
    if existing_notes and not force:
        return jsonify({'notes_markdown': existing_notes, 'message': 'Using cached study notes'})

    if not force:
        cached_notes = _get_cached_study_notes(position, topic_name, topic_path_key_source)
        if cached_notes:
            _save_ai_notes(topic_id, cached_notes)
            return jsonify({'notes_markdown': cached_notes, 'message': 'Using global cached study notes'})

    # We compile notes from existing guidance where possible
    existing_guidance = dict(topic).get('ai_guidance')
    user_material = dict(topic).get('notes') or ''
    if not existing_guidance:
        # Trigger guidance generation (respects global guidance cache unless forced)
        # We call the underlying logic by reusing the route function's behavior via a direct call.
        # Simpler: just instruct user to generate guidance first, but better UX is to generate it now.
        # We'll attempt to reuse global guidance cache here (same keys as guidance endpoint).
        cached_guidance = _get_cached_ai_guidance(position, topic_name, topic_path_key_source)
        if cached_guidance:
            existing_guidance = cached_guidance
            _save_ai_guidance(topic_id, cached_guidance)

    if not existing_guidance and not (os.getenv('GROQ_API_KEY') or os.getenv('GOOGLE_API_KEY') or os.environ.get('GROQ_API_KEY')):
        return jsonify({'error': 'No AI API key configured. Set GROQ_API_KEY or GOOGLE_API_KEY, or generate guidance first.'}), 400

    # Build topic path display for context
    parent_path_display = parent_path_raw.replace(' > ', ' → ') if parent_path_raw else None
    full_topic_path = f"{parent_path_display} → {topic_name}" if parent_path_display else topic_name

    # Admin-tunable: number of flashcards to generate
    try:
        flashcards_count = int(get_setting('flashcards_count', '15') or 15)
    except Exception:
        flashcards_count = 15

    prompt = f"""You are an expert interview preparation coach specializing in Data Scientist interviews.

You are compiling STUDY NOTES for one topic. The notes must be concise, structured, and easy to review quickly.

Topic path: {full_topic_path}

User-provided notes/material (may be empty, treat as authoritative if present):
{user_material}

Input guidance (may include extra detail):
{existing_guidance or ''}

Write study notes in Markdown with these sections (use these exact headings):
## Summary (5 bullets max)
## Key concepts
## Common interview questions (with brief answers)
## Flashcards (Q/A)
## Pitfalls & gotchas
## Mini cheat-sheet (syntax / patterns)
## Practice (3 tasks: easy/medium/hard)

Rules:
- Tailor to Data Scientist expectations (pandas/pyarrow examples ok; Spark only if relevant).
- Avoid fluff. Prefer concrete examples and decision tradeoffs.
- In **Flashcards (Q/A)**, produce {flashcards_count} cards ordered from EASY → HARD. Use bullets in exactly this format:
  - Q: ...
    A: ...
-    Difficulty: Easy|Medium|Hard
- Every card MUST include an answer (no blank A lines). If the question is ambiguous, write the most likely concise interview-style answer and note assumptions in 1 sentence.
- If the input guidance is missing something critical, infer reasonable details but keep it brief."""

    # Prefer Groq, then Gemini (similar to guidance)
    groq_key = os.environ.get('GROQ_API_KEY') or os.getenv('GROQ_API_KEY')
    if groq_key and Groq:
        try:
            client = Groq(api_key=groq_key)
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": "You are an expert interview preparation coach. You write crisp, well-structured study notes in Markdown. You are concise and practical."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=700,
                temperature=0.4
            )
            notes_markdown = response.choices[0].message.content.strip()
            _save_ai_notes(topic_id, notes_markdown)
            _upsert_cached_study_notes(position, topic_name, topic_path_key_source, notes_markdown, model_provider='groq', model_name="llama-3.1-8b-instant")
            return jsonify({'notes_markdown': notes_markdown, 'message': 'Study notes generated successfully'})
        except Exception as e:
            error_msg = str(e)
            import traceback
            print(f"Groq API error (study notes): {error_msg}")
            print(traceback.format_exc())
            return jsonify({'error': f'Groq API error: {error_msg}. Check server logs for details.'}), 500

    gemini_key = os.getenv('GOOGLE_API_KEY')
    if gemini_key and genai:
        try:
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel('gemini-pro')
            response = model.generate_content(
                prompt,
                generation_config={
                    'max_output_tokens': 700,
                    'temperature': 0.4,
                }
            )
            notes_markdown = response.text.strip()
            _save_ai_notes(topic_id, notes_markdown)
            _upsert_cached_study_notes(position, topic_name, topic_path_key_source, notes_markdown, model_provider='gemini', model_name='gemini-pro')
            return jsonify({'notes_markdown': notes_markdown, 'message': 'Study notes generated successfully'})
        except Exception:
            pass

    return jsonify({'error': 'Failed to generate study notes. Configure GROQ_API_KEY or GOOGLE_API_KEY.'}), 500

def load_default_topics():
    """Load default topics from topics.json file - supports recursive nesting"""
    def process_node(node, path_parts):
        """Recursively process a category/subcategory node"""
        topics_list = []
        node_name = node.get('name', '')
        current_path = path_parts + [node_name] if node_name else path_parts
        
        # Process subcategories first (if any)
        if 'subcategories' in node and node.get('subcategories'):
            # Process each subcategory recursively
            for subcat in node.get('subcategories', []):
                topics_list.extend(process_node(subcat, current_path))
        
        # Also process direct topics (if any) - this handles cases where a node has both subcategories and topics
        if 'topics' in node and node.get('topics'):
            for i, topic_name in enumerate(node.get('topics', [])):
                full_category = ' > '.join(current_path) if current_path else None
                topics_list.append({
                    'name': topic_name,
                    'category': full_category,
                    'priority': 'high' if i < 2 else 'medium'
                })
        
        return topics_list
    
    try:
        with open('topics.json', 'r') as f:
            data = json.load(f)
            topics = []
            
            # Process each category
            for category in data.get('categories', []):
                category_name = category.get('name', '')
                
                # Process recursively - this handles both subcategories and direct topics
                topics.extend(process_node(category, []))
            
            # Add uncategorized topics
            for topic_name in data.get('uncategorized_topics', []):
                topics.append({
                    'name': topic_name,
                    'category': None,
                    'priority': 'medium'
                })
            return topics
    except FileNotFoundError:
        # Fallback if file doesn't exist
        return []
    except json.JSONDecodeError:
        # Fallback if JSON is invalid
        return []

def generate_common_topics(position):
    """Generate common interview topics for a given position using AI"""
    # Default granular technical topics based on common data science interview requirements
    # Note: These are used as fallback if AI generation fails completely
    default_topics = [
            {'name': 'Python Data Structures (lists, dicts, sets, tuples)', 'priority': 'high', 'category': 'Core Programming'},
            {'name': 'Python Control Flow & Functions', 'priority': 'high', 'category': 'Core Programming'},
            {'name': 'List & Dict Comprehensions', 'priority': 'high', 'category': 'Core Programming'},
            {'name': 'Python OOP (classes, __init__, methods)', 'priority': 'medium', 'category': 'Core Programming'},
            {'name': 'groupby, agg, transform', 'priority': 'high', 'category': 'Data Manipulation & Analysis'},
            {'name': 'Merging/joining data', 'priority': 'high', 'category': 'Data Manipulation & Analysis'},
            {'name': 'Handling missing data', 'priority': 'high', 'category': 'Data Manipulation & Analysis'},
            {'name': 'Datetime operations', 'priority': 'medium', 'category': 'Data Manipulation & Analysis'},
            {'name': 'Vectorization vs loops', 'priority': 'medium', 'category': 'Data Manipulation & Analysis'},
            {'name': 'Performance awareness (when pandas breaks)', 'priority': 'medium', 'category': 'Data Manipulation & Analysis'},
            {'name': 'SQL SELECT, WHERE, JOIN', 'priority': 'high', 'category': 'SQL'},
            {'name': 'SQL GROUP BY, HAVING', 'priority': 'high', 'category': 'SQL'},
            {'name': 'SQL Window Functions', 'priority': 'high', 'category': 'SQL'},
            {'name': 'SQL Subqueries & CTEs', 'priority': 'medium', 'category': 'SQL'},
            {'name': 'Descriptive Statistics', 'priority': 'high', 'category': 'Statistics'},
            {'name': 'Probability Distributions', 'priority': 'high', 'category': 'Statistics'},
            {'name': 'Hypothesis Testing & p-values', 'priority': 'high', 'category': 'Statistics'},
            {'name': 'A/B Testing', 'priority': 'high', 'category': 'Statistics'},
            {'name': 'Linear & Logistic Regression', 'priority': 'high', 'category': 'Machine Learning'},
            {'name': 'Decision Trees', 'priority': 'high', 'category': 'Machine Learning'},
            {'name': 'Random Forests', 'priority': 'high', 'category': 'Machine Learning'},
            {'name': 'Gradient Boosting (XGBoost/LightGBM)', 'priority': 'high', 'category': 'Machine Learning'},
            {'name': 'Model Evaluation Metrics', 'priority': 'high', 'category': 'Machine Learning'}
    ]
    
    # First, try to load from topics.json
    json_topics = load_default_topics()
    print(f"Loaded {len(json_topics)} topics from topics.json")
    if json_topics:
        print(f"Sample topic: {json_topics[0] if json_topics else 'None'}")
    
    groq_key = os.environ.get('GROQ_API_KEY') or os.getenv('GROQ_API_KEY')
    
    if not groq_key or not Groq:
        # Fallback: return topics from JSON file, or hardcoded if JSON is empty
        if json_topics:
            print("Returning topics from topics.json (no API key)")
            return json_topics  # Return all topics from JSON
        
        # Fallback to hardcoded topics if JSON is empty
        topics_by_category = {}
        for topic in default_topics:
            category = topic.get('category', 'Other')
            if category not in topics_by_category:
                topics_by_category[category] = []
            topics_by_category[category].append({
                'name': topic['name'],
                'category': category,
                'priority': topic.get('priority', 'medium')
            })
        
        # Flatten back to list
        result = []
        for category, topics in topics_by_category.items():
            result.extend(topics)
        return result[:20]  # Return up to 20 topics
    
    try:
        client = Groq(api_key=groq_key)
        
        prompt = f"""You are an expert technical recruiter and interview coach. For a {position} position, generate a comprehensive list of interview topics organized by category.

Requirements:
- Focus on skills and concepts that are COMMONLY TESTED in real interviews
- Prioritize practical, hands-on skills over theoretical knowledge
- Include both fundamental concepts and commonly-asked advanced topics
- Each subtopic should be specific enough to study independently

Format your response EXACTLY as follows (use colons after category names):
CATEGORY_NAME:
- Specific subtopic 1
- Specific subtopic 2
- Specific subtopic 3

CATEGORY_NAME:
- Specific subtopic 1
- Specific subtopic 2

Provide 6-8 main categories (e.g., "Core Programming", "Data Manipulation & Analysis", "Machine Learning", "Statistics & Probability", "System Design", "SQL & Databases", etc.).
Each category should have 3-5 specific subtopics that are interview-relevant.

Be specific: Instead of "Algorithms", use "Sorting algorithms (quicksort, mergesort)" or "Graph traversal (BFS, DFS)". Instead of "Python", use "Python data structures (lists, dicts, sets)" or "List comprehensions and generators"."""

        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "You are an expert technical recruiter and interview coach with deep knowledge of what's actually tested in technical interviews. You provide comprehensive, well-organized topic lists that reflect real interview requirements. You prioritize practical skills and commonly-tested concepts."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=400,
            temperature=0.7
        )
        
        topics_text = response.choices[0].message.content.strip()
        # Parse the hierarchical response into categories and subtopics
        topics = []
        current_category = None
        lines = topics_text.split('\n')
        
        # Filter out instruction lines and find the actual content
        content_lines = []
        skip_patterns = ['format', 'example', 'provide', 'each category', 'each subtopic', 
                        'hierarchical', 'organized by', 'main categories', 'technical skills']
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Skip instruction lines
            if any(pattern in line.lower() for pattern in skip_patterns):
                continue
            # Skip lines that are too long (likely explanations)
            if len(line) > 150:
                continue
            content_lines.append(line)
        
        # Parse the filtered content
        for i, line in enumerate(content_lines):
            # Check if this is a category header
            is_category = False
            
            # Category indicators:
            # 1. Ends with colon
            # 2. Doesn't start with bullet/number and next line is a bullet
            if line.endswith(':'):
                is_category = True
            elif not line.startswith('-') and not line.startswith('•') and not line.startswith('*'):
                if line and not line[0].isdigit():
                    # Check if next non-empty line is a bullet point
                    if i + 1 < len(content_lines):
                        next_line = content_lines[i + 1].strip()
                        if next_line.startswith('-') or next_line.startswith('•') or next_line.startswith('*'):
                            is_category = True
            
            if is_category:
                # This is a category header
                current_category = line.rstrip(':').strip()
                # Remove asterisks and clean up
                current_category = current_category.rstrip('*').strip()
                # Validate: should be 2-80 characters, not too generic
                if (current_category and 2 <= len(current_category) <= 80 and 
                    current_category.lower() not in ['category', 'topic', 'skill', 'subject']):
                    # Category is valid, keep it
                    pass
                else:
                    current_category = None
            else:
                # This is a subtopic
                topic = line.lstrip('- •*0123456789. ').strip()
                topic = topic.rstrip('*').strip()
                # Only add if we have a valid category and topic
                if topic and len(topic) > 1 and current_category:
                    # Determine priority
                    category_topics = [t for t in topics if t.get('category') == current_category]
                    priority = 'high' if len(category_topics) < 2 else 'medium'
                    topics.append({
                        'name': topic,
                        'category': current_category,
                        'priority': priority
                    })
        
        # Ensure we have at least some topics
        if not topics:
            # Use topics from JSON file, or fallback to hardcoded
            topics = load_default_topics()
            if not topics:
                # Fallback to hardcoded topics
                for topic in default_topics:
                    topics.append({
                        'name': topic['name'],
                        'category': topic.get('category'),
                        'priority': topic.get('priority', 'medium')
                    })
        
        return topics
    
    except Exception as e:
        # Fallback on error - return granular default topics
        return default_topics[:15]

@app.route('/api/interviews/<int:interview_id>/study-plan', methods=['GET'])
def get_study_plan(interview_id):
    conn = get_db()
    cursor = db_execute(conn, 'SELECT * FROM interviews WHERE id = ?', (interview_id,))
    interview = db_fetchone(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    if not interview:
        conn.close()
        return jsonify({'error': 'Study material not found'}), 404
    
    cursor = db_execute(conn, 'SELECT * FROM topics WHERE interview_id = ? ORDER BY COALESCE(category_name, \'\'), priority DESC, topic_name ASC', 
                         (interview_id,))
    topics = db_fetchall(cursor)
    if USE_POSTGRESQL:
        cursor.close()
    conn.close()
    
    interview_dict = dict(interview)
    interview_date = interview_dict.get('interview_date')
    
    # Convert topics to list
    topics_list = []
    for topic in topics:
        topic_dict = dict(topic)
        if topic_dict.get('topic_name') is None:
            topic_dict['topic_name'] = 'Untitled Topic'
        topics_list.append(topic_dict)
    
    # Group topics by priority
    high_priority = [t for t in topics_list if t.get('priority') == 'high']
    medium_priority = [t for t in topics_list if t.get('priority') == 'medium']
    low_priority = [t for t in topics_list if t.get('priority') == 'low']
    
    return jsonify({
        'interview_date': interview_date,
        'days_until': None,
        'topics': {
            'high': high_priority,
            'medium': medium_priority,
            'low': low_priority,
            'all': topics_list
        },
        'total': len(topics_list)
    })

def generate_study_plan(topics, days_until):
    """Generate a study plan based on topics and days until interview"""
    plan = []
    
    if not topics:
        return plan
    
    # Sort topics by priority first, then by topic_name for consistency
    priority_order = {'high': 3, 'medium': 2, 'low': 1}
    sorted_topics = sorted(
        topics, 
        key=lambda x: (
            priority_order.get(x.get('priority', 'medium'), 2),  # Priority first
            x.get('topic_name', '').lower()  # Then alphabetically for consistency
        ),
        reverse=True
    )
    
    # Calculate distribution: spread topics evenly across available days
    # Reserve last day for review, so distribute across (days_until - 1) days
    study_days = max(1, days_until - 1)
    total_topics = len(sorted_topics)
    
    # Calculate topics per day, ensuring at least 1 topic per day
    topics_per_day = max(1, total_topics // study_days)
    remainder = total_topics % study_days
    
    current_date = datetime.now()
    topic_index = 0
    
    for day in range(study_days):
        if topic_index >= total_topics:
            break
        
        # Distribute remainder topics across first few days
        day_topic_count = topics_per_day + (1 if day < remainder else 0)
        
        day_date = current_date + timedelta(days=day)
        day_topics = sorted_topics[topic_index:topic_index + day_topic_count]
        topic_index += day_topic_count
        
        plan.append({
            'date': day_date.strftime('%Y-%m-%d'),
            'day_number': day + 1,
            'topics': [{'name': t.get('topic_name', 'Untitled Topic'), 'priority': t.get('priority', 'medium'), 'id': t.get('id')} for t in day_topics]
        })
    
    return plan

# Database initialization flag
_db_initialized = False

def ensure_db_initialized():
    """Ensure database is initialized (only runs once)"""
    global _db_initialized
    if _db_initialized:
        return
    
    try:
        # Try a simple query to check if core tables exist
        conn = get_db()
        cursor = db_execute(conn, "SELECT 1 FROM interviews LIMIT 1")
        if USE_POSTGRESQL:
            cursor.close()
        conn.close()
        # Core tables exist, but still run init_db() to apply any new migrations
        # (CREATE TABLE IF NOT EXISTS / ALTER TABLE ... ADD COLUMN ...).
        # Retry a few times in case the SQLite DB is briefly locked (Flask reloader).
        last_err = None
        for attempt in range(5):
            try:
                init_db()
                last_err = None
                break
            except sqlite3.OperationalError as e:
                last_err = e
                if 'locked' in str(e).lower():
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise
        if last_err is not None:
            raise last_err
        _db_initialized = True
        print("✅ Database tables already exist (migrations applied)")
    except Exception:
        # Tables don't exist, initialize them
        try:
            # Same retry logic for first-time init
            last_err = None
            for attempt in range(5):
                try:
                    init_db()
                    last_err = None
                    break
                except sqlite3.OperationalError as e:
                    last_err = e
                    if 'locked' in str(e).lower():
                        time.sleep(0.2 * (attempt + 1))
                        continue
                    raise
            if last_err is not None:
                raise last_err
            _db_initialized = True
            print("✅ Database initialized successfully")
        except Exception as e:
            print(f"⚠️  Failed to initialize database: {e}")
            import traceback
            traceback.print_exc()

# Initialize database on module load (works with gunicorn)
try:
    ensure_db_initialized()
except Exception as e:
    print(f"⚠️  Database initialization deferred: {e}")

# Ensure database is initialized before first request (for Railway/gunicorn)
@app.before_request
def before_request():
    """Ensure database is initialized before handling requests"""
    ensure_db_initialized()

if __name__ == '__main__':
    # Use PORT from environment variable (Railway) or default to 5006 for local development
    port = int(os.getenv('PORT', 5006))
    debug = os.getenv('RAILWAY_ENVIRONMENT') is None  # Only debug mode in local development
    app.run(debug=debug, host='0.0.0.0', port=port)

