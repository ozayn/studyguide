from flask import Flask, render_template, request, jsonify, session, redirect
from datetime import datetime, timedelta
from functools import wraps
import sqlite3
import os
import json
import time

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
    ADMIN_EMAILS = os.getenv('ADMIN_EMAILS', '').split(',')
    
    # OAuth scopes
    SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/userinfo.profile']
    
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
    updated_at = datetime.utcnow().isoformat()
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
    updated_at = datetime.utcnow().isoformat()

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
    updated_at = datetime.utcnow().isoformat()

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
    if not GOOGLE_OAUTH_AVAILABLE or not ADMIN_EMAILS:
        return True  # Allow access if OAuth not configured
    return email.strip().lower() in [admin.strip().lower() for admin in ADMIN_EMAILS if admin.strip()]

def login_required(f):
    """Decorator to require Google OAuth login for admin routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # For local development only, bypass OAuth if running on localhost
        is_local = (request.host.startswith('localhost') or 
                   request.host.startswith('127.0.0.1') or
                   request.host.startswith('10.'))
        
        if is_local:
            print("DEBUG: Local development detected, bypassing OAuth")
            return f(*args, **kwargs)
        
        if not GOOGLE_OAUTH_AVAILABLE:
            return f(*args, **kwargs)  # Allow access if OAuth not configured
        
        # Check if user is logged in
        if ('user_email' not in session or 
            not session.get('user_email') or 
            'credentials' not in session):
            print("DEBUG: No valid session found, redirecting to login")
            return redirect('/auth/login')
        
        # Check if user is admin
        if not is_admin_email(session['user_email']):
            return jsonify({'error': f'Access denied for {session["user_email"]}. Contact administrator.'}), 403
        
        print(f"DEBUG: Authenticated user: {session['user_email']}")
        return f(*args, **kwargs)
    return decorated_function

@app.route('/auth/login')
def auth_login():
    """Initiate Google OAuth login"""
    if not GOOGLE_OAUTH_AVAILABLE:
        return redirect('/admin')  # Skip auth if not configured
    
    # For local development, redirect directly to admin
    is_local = (request.host.startswith('localhost') or 
               request.host.startswith('127.0.0.1') or
               request.host.startswith('10.'))
    
    if is_local:
        return redirect('/admin')
    
    try:
        # Create flow with proper configuration
        flow = Flow.from_client_config(CLIENT_CONFIG, SCOPES)
        
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
            include_granted_scopes='true'
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
    if not GOOGLE_OAUTH_AVAILABLE:
        return redirect('/admin')
    
    try:
        # Create flow
        flow = Flow.from_client_config(CLIENT_CONFIG, SCOPES)
        
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
        session['credentials'] = credentials.to_json()
        
        return redirect('/admin')
        
    except Exception as e:
        print(f"OAuth callback error: {e}")
        return f"OAuth callback error: {e}", 500

@app.route('/auth/logout')
def auth_logout():
    """Logout user"""
    # Clear all session data
    session.clear()
    
    if 'credentials' in session:
        del session['credentials']
    if 'user_email' in session:
        del session['user_email']
    if 'user_name' in session:
        del session['user_name']
    if 'state' in session:
        del session['state']
    
    session.permanent = False
    return redirect('/')

@app.route('/admin')
@login_required
def admin():
    return render_template('admin.html', session=session)

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

