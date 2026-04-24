"""
MedFlix — Plataforma IPTV com Xtream Codes API
Cada usuário faz login com suas credenciais do servidor IPTV.
Não precisa importar M3U — tudo carrega via API em tempo real.
"""
import os, re, json, time, sqlite3, threading, urllib.request, urllib.parse
from pathlib import Path
from functools import wraps
from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, jsonify, Response, stream_with_context)
from werkzeug.security import generate_password_hash, check_password_hash

# ── Config ─────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
DB_PATH    = BASE_DIR / "medflix.db"
app = Flask(__name__)
app.config["SECRET_KEY"]          = os.getenv("SECRET_KEY", "medflix-secret-2026")
app.config["MAX_CONTENT_LENGTH"]  = 10 * 1024 * 1024
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ── Database ────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

_db_ready = False
@app.before_request
def ensure_db():
    global _db_ready
    if not _db_ready:
        try:
            _init_db()
        except Exception as e:
            app.logger.warning(f"DB init warning: {e}")
        _db_ready = True

def _init_db():
    conn = get_db(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        email         TEXT DEFAULT '',
        is_admin      INTEGER DEFAULT 0,
        xtream_server TEXT DEFAULT '',
        xtream_user   TEXT DEFAULT '',
        xtream_pass   TEXT DEFAULT '',
        playlist_url  TEXT DEFAULT '',
        dias_acesso   INTEGER DEFAULT 30,
        access_token  TEXT DEFAULT '',
        created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    # Migrate
    existing = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    for col, typedef in [
        ("xtream_server", "TEXT DEFAULT ''"),
        ("xtream_user",   "TEXT DEFAULT ''"),
        ("xtream_pass",   "TEXT DEFAULT ''"),
        ("playlist_url",  "TEXT DEFAULT ''"),
        ("access_token",  "TEXT DEFAULT ''"),
        ("is_admin",      "INTEGER DEFAULT 0"),
        ("dias_acesso",   "INTEGER DEFAULT 30"),
    ]:
        if col not in existing:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
    # Demo admin
    c.execute("INSERT OR IGNORE INTO users (username,password_hash,is_admin) VALUES (?,?,1)",
              ("admin", generate_password_hash("admin123")))
    conn.commit(); conn.close()

# ── Auth helpers ────────────────────────────────────────────────────────────
def logged_in():
    return "user_id" in session

def current_user():
    if not logged_in(): return None
    cached = session.get("_u")
    if cached: return cached
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    if not u: return None
    d = dict(u)
    session["_u"] = d
    return d

def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not logged_in(): return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        u = current_user()
        if not u or not u.get("is_admin"): return redirect(url_for("home"))
        return f(*args, **kwargs)
    return decorated

# ── Xtream Codes API ─────────────────────────────────────────────────────────
IPTV_UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")

_xtream_cache = {}  # (user_id, endpoint) → (data, timestamp)
_m3u_cache = {}     # user_id → (parsed_data, timestamp)
CACHE_TTL = 3600    # 1 hour

def parse_m3u(m3u_content):
    """Parse M3U playlist and return structured list."""
    if not m3u_content:
        return [], {}, {}, []
    
    lines = m3u_content.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    all_items = []
    current_info = {}
    live_cats = {}
    vod_cats = {}
    series_cats = {}
    
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#EXTM3U'):
            continue
        
        if line.startswith('#EXTINF:'):
            info = line[8:] if line.startswith('#EXTINF:-1') else line[7:]
            parts = info.split(',', 1)
            attrs = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else 'Sem nome'
            
            current_info = {"name": name}
            
            tvg_match = re.search(r'tvg-name="([^"]*)"', attrs)
            if tvg_match:
                current_info["name"] = tvg_match.group(1)
            
            logo_match = re.search(r'tvg-logo="([^"]*)"', attrs)
            if logo_match:
                current_info["stream_icon"] = logo_match.group(1)
            
            group_match = re.search(r'group-title="([^"]*)"', attrs)
            if group_match:
                current_info["category_name"] = group_match.group(1)
            else:
                current_info["category_name"] = "Geral"
            
        elif line.startswith('#'):
            continue
        else:
            if line and current_info.get("name") and current_info["name"] != 'Sem nome':
                stream_url = line
                stream_id = abs(hash(stream_url)) % (10**12)
                cat_name = current_info.get("category_name", "Geral")
                cat_id = cat_name.replace(" ", "_").lower()
                
                item = {
                    "name": current_info.get("name", "Sem nome"),
                    "stream_id": stream_id,
                    "stream_icon": current_info.get("stream_icon", ""),
                    "category_name": cat_name,
                    "category_id": cat_id,
                    "_url": stream_url,
                }
                all_items.append(item)
                
                if cat_name not in live_cats:
                    live_cats[cat_name] = {"category_id": cat_id, "category_name": cat_name}
                if cat_name not in vod_cats:
                    vod_cats[cat_name] = {"category_id": cat_id, "category_name": cat_name}
            
            current_info = {}
    
    return all_items, live_cats, vod_cats, series_cats

def get_m3u_streams(u):
    """Get parsed M3U streams for user, with caching."""
    playlist_url = (u.get("playlist_url") or "").strip()
    if not playlist_url:
        return {"items": [], "live": [], "vod": [], "series": [], "live_categories": [], "vod_categories": [], "series_categories": []}
    
    cache_key = u["id"]
    if cache_key in _m3u_cache:
        data, ts = _m3u_cache[cache_key]
        if time.time() - ts < CACHE_TTL:
            return data
    
    try:
        req = urllib.request.Request(playlist_url, headers={"User-Agent": IPTV_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            content = r.read().decode("utf-8", "ignore")
        
        all_items, live_cats, vod_cats, series_cats = parse_m3u(content)
        
        result = {
            "items": all_items,
            "live": all_items,
            "vod": all_items,
            "series": [],
            "live_categories": list(live_cats.values()),
            "vod_categories": list(vod_cats.values()),
            "series_categories": [],
        }
        
        _m3u_cache[cache_key] = (result, time.time())
        return result
    except Exception as e:
        app.logger.warning(f"M3U parse error: {e}")
        return {"items": [], "live": [], "vod": [], "series": [], "live_categories": [], "vod_categories": [], "series_categories": []}

def m3u_get_stream_url(u, stream_id):
    """Get stream URL from parsed M3U data by stream_id."""
    streams = get_m3u_streams(u)
    for item in streams.get("items", []):
        if str(item.get("stream_id")) == str(stream_id):
            return item.get("_url")
    return None

def xtream_request(server, username, password, endpoint, params=None, user_id=None):
    """Make a request to Xtream Codes API with caching."""
    if not server or not username or not password:
        return None
    
    # Normalize server URL
    server = server.rstrip("/")
    if not server.startswith(("http://","https://")):
        server = "http://" + server
    
    cache_key = (user_id, endpoint, str(params))
    if cache_key in _xtream_cache:
        data, ts = _xtream_cache[cache_key]
        if time.time() - ts < CACHE_TTL:
            return data
    
    base_params = {"username": username, "password": password}
    if params:
        base_params.update(params)
    
    url = f"{server}/player_api.php?{urllib.parse.urlencode(base_params)}"
    if endpoint:
        url += f"&action={endpoint}"
    
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": IPTV_UA,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        _xtream_cache[cache_key] = (data, time.time())
        return data
    except Exception as e:
        app.logger.warning(f"Xtream API error: {e}")
        return None

def get_user_xtream(u):
    """Get Xtream credentials from user dict."""
    server   = (u.get("xtream_server") or "").strip()
    username = (u.get("xtream_user")   or "").strip()
    password = (u.get("xtream_pass")   or "").strip()
    return server, username, password

def xtream_get_stream_url(u, stream_id, stream_type="live", ext="ts"):
    """Build the direct stream URL for a given stream."""
    server, username, password = get_user_xtream(u)
    if not server: return None
    server = server.rstrip("/")
    if not server.startswith(("http://","https://")):
        server = "http://" + server
    if stream_type == "live":
        return f"{server}/{username}/{password}/{stream_id}.{ext}"
    elif stream_type == "vod":
        return f"{server}/movie/{username}/{password}/{stream_id}.{ext}"
    elif stream_type == "series":
        return f"{server}/series/{username}/{password}/{stream_id}.{ext}"
    return None

# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if logged_in(): return redirect(url_for("home"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","").strip()
        conn = get_db()
        u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if u and check_password_hash(u["password_hash"], password):
            session.clear()
            session["user_id"] = u["id"]
            session["username"] = u["username"]
            return redirect(url_for("home"))
        error = "Usuário ou senha incorretos."
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/home")
@require_login
def home():
    u = current_user()
    server, username, password = get_user_xtream(u)
    has_xtream = bool(server and username and password)
    has_m3u = bool((u.get("playlist_url") or "").strip())
    has_content = has_xtream or has_m3u
    
    live_cats = vod_cats = series_cats = None
    account_info = None
    
    if has_xtream:
        info = xtream_request(server, username, password, None, user_id=u["id"])
        if info:
            account_info = info.get("user_info", {})
        live_cats   = xtream_request(server, username, password, "get_live_categories",   user_id=u["id"]) or []
        vod_cats    = xtream_request(server, username, password, "get_vod_categories",    user_id=u["id"]) or []
        series_cats = xtream_request(server, username, password, "get_series_categories", user_id=u["id"]) or []
    elif has_m3u:
        m3u_data = get_m3u_streams(u)
        live_cats   = m3u_data.get("live_categories", [])
        vod_cats    = m3u_data.get("vod_categories", [])
        series_cats = []
    
    return render_template_string(HOME_HTML,
        u=u, has_xtream=has_xtream, has_m3u=has_m3u, has_content=has_content,
        live_cats=live_cats, vod_cats=vod_cats, series_cats=series_cats,
        account_info=account_info)

@app.route("/browse/<content_type>")
@require_login
def browse(content_type):
    """Browse live/vod/series by category."""
    u = current_user()
    server, username, password = get_user_xtream(u)
    playlist_url = (u.get("playlist_url") or "").strip()
    category_id = request.args.get("cat", "")
    page = int(request.args.get("p", 1))
    per_page = 40
    
    items = []
    category_name = request.args.get("cat_name", content_type.title())
    
    if server and username and password:
        if content_type == "live":
            action = "get_live_streams"
        elif content_type == "vod":
            action = "get_vod_streams"
        else:
            action = "get_series"
        
        params = {}
        if category_id:
            params["category_id"] = category_id
        
        items = xtream_request(server, username, password, action, params, user_id=u["id"]) or []
    elif playlist_url:
        m3u_data = get_m3u_streams(u)
        items = m3u_data.get("items", [])
    
    if category_id and (server and username and password or playlist_url):
        items = [i for i in items if i.get("category_id", "").replace(" ", "_").lower() == category_id]
    
    total = len(items)
    start = (page-1)*per_page
    items_page = items[start:start+per_page]
    total_pages = max(1, (total + per_page - 1) // per_page)
    
    return render_template_string(BROWSE_HTML,
        u=u, items=items_page, content_type=content_type,
        category_name=category_name, category_id=category_id,
        page=page, total_pages=total_pages, total=total)

@app.route("/watch/<content_type>/<stream_id>")
@require_login
def watch(content_type, stream_id):
    """Watch a live/vod/series stream."""
    u = current_user()
    server, username, password = get_user_xtream(u)
    
    title = request.args.get("title", "")
    cover = request.args.get("cover", "")
    cat_id = request.args.get("cat", "")
    
    stream_url = ""
    info = {}
    episodes = []
    
    if server and username and password:
        if content_type == "live":
            stream_url = xtream_get_stream_url(u, stream_id, "live", "ts")
        elif content_type == "vod":
            stream_url = xtream_get_stream_url(u, stream_id, "vod", "mp4")
            vod_info = xtream_request(server, username, password, "get_vod_info",
                                      {"vod_id": stream_id}, user_id=u["id"])
            if vod_info:
                info = vod_info.get("info", {})
                title = title or info.get("name","")
                cover = cover or info.get("cover_big") or info.get("movie_image","")
        elif content_type == "series":
            series_info = xtream_request(server, username, password, "get_series_info",
                                         {"series_id": stream_id}, user_id=u["id"])
            if series_info:
                info = series_info.get("info", {})
                title = title or info.get("name","")
                cover = cover or info.get("cover","")
                for season_num, season_eps in (series_info.get("episodes") or {}).items():
                    for ep in (season_eps or []):
                        ep["_season"] = season_num
                        episodes.append(ep)
                if episodes:
                    first = episodes[0]
                    stream_id = first.get("id", stream_id)
                    stream_url = xtream_get_stream_url(u, stream_id, "series",
                                                       first.get("container_extension","mp4"))
    else:
        stream_url = m3u_get_stream_url(u, stream_id)
        if not title:
            title = request.args.get("name", "Stream")
    
    return render_template_string(WATCH_HTML,
        u=u, stream_url=stream_url, title=title, cover=cover,
        content_type=content_type, stream_id=stream_id,
        info=info, episodes=episodes, cat_id=cat_id)

@app.route("/watch/series/<series_id>/ep/<ep_id>")
@require_login  
def watch_episode(series_id, ep_id):
    u = current_user()
    ext = request.args.get("ext","mp4")
    stream_url = xtream_get_stream_url(u, ep_id, "series", ext)
    title = request.args.get("title","")
    cover = request.args.get("cover","")
    return render_template_string(WATCH_HTML,
        u=u, stream_url=stream_url, title=title, cover=cover,
        content_type="series", stream_id=ep_id,
        info={}, episodes=[], cat_id="")

@app.route("/search")
@require_login
def search():
    u = current_user()
    q = request.args.get("q","").strip().lower()
    server, username, password = get_user_xtream(u)
    results = []
    
    if q and server:
        for action, ctype in [
            ("get_live_streams","live"),
            ("get_vod_streams","vod"),
            ("get_series","series")
        ]:
            items = xtream_request(server, username, password, action, user_id=u["id"]) or []
            for item in items:
                name = (item.get("name") or item.get("title","")).lower()
                if q in name:
                    item["_type"] = ctype
                    results.append(item)
                    if len(results) >= 60:
                        break
            if len(results) >= 60:
                break
    
    return render_template_string(SEARCH_HTML, u=u, q=q, results=results)

# ── Admin routes ────────────────────────────────────────────────────────────

@app.route("/setup", methods=["GET","POST"])
def setup():
    conn = get_db()
    has_admin = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]
    
    if has_admin:
        conn.close()
        return render_template_string(SIMPLE_PAGE,
            title="Setup já concluído",
            icon="🔒",
            msg="Já existe um administrador. Esta rota está desativada.",
            link="/", link_text="← Voltar")
    
    error = ""
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","").strip()
        code     = request.form.get("code","").strip()
        setup_code = os.getenv("SETUP_CODE","medflix2026")
        
        if code != setup_code:
            error = "Código incorreto."
        elif not username or len(password) < 6:
            error = "Usuário obrigatório e senha mínimo 6 caracteres."
        else:
            conn.execute("INSERT OR REPLACE INTO users (username,password_hash,is_admin) VALUES (?,?,1)",
                         (username, generate_password_hash(password)))
            conn.commit(); conn.close()
            return render_template_string(SIMPLE_PAGE,
                title="Admin criado!", icon="✅",
                msg=f"Usuário '{username}' criado como administrador.",
                link="/login", link_text="Fazer login →")
    conn.close()
    return render_template_string(SETUP_HTML, error=error)

@app.route("/admin/reset", methods=["GET","POST"])
def admin_reset():
    reset_key = os.getenv("ADMIN_RESET_KEY","").strip()
    if not reset_key:
        return render_template_string(SIMPLE_PAGE,
            title="Desativado", icon="🔒",
            msg="Defina ADMIN_RESET_KEY nas variáveis de ambiente para ativar.",
            link="/", link_text="← Voltar")
    
    error = ""
    if request.method == "POST":
        if request.form.get("key","") != reset_key:
            error = "Chave incorreta."
        else:
            username = request.form.get("username","").strip()
            password = request.form.get("password","").strip()
            if not username or len(password) < 6:
                error = "Preencha todos os campos (senha mín. 6 chars)."
            else:
                import secrets
                token = secrets.token_urlsafe(24)
                conn = get_db()
                u = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
                if u:
                    conn.execute("UPDATE users SET is_admin=1,password_hash=?,access_token=? WHERE id=?",
                                 (generate_password_hash(password), token, u["id"]))
                else:
                    conn.execute("INSERT INTO users (username,password_hash,is_admin,access_token) VALUES (?,?,1,?)",
                                 (username, generate_password_hash(password), token))
                conn.commit(); conn.close()
                return render_template_string(SIMPLE_PAGE,
                    title="Acesso recuperado!", icon="✅",
                    msg=f"Admin '{username}' criado. Remova ADMIN_RESET_KEY após usar.",
                    link="/login", link_text="Fazer login →")
    return render_template_string(RESET_HTML, error=error)

@app.route("/admin", methods=["GET","POST"])
@require_admin
def admin():
    """Admin panel: manage users and their Xtream credentials."""
    conn = get_db()
    msg = error = ""
    
    if request.method == "POST":
        action = request.form.get("action","")
        try:
            import secrets
            if action == "create":
                uname    = request.form.get("username","").strip()
                pwd      = request.form.get("password","").strip()
                email    = request.form.get("email","").strip()
                dias     = int(request.form.get("dias",30))
                srv      = request.form.get("xtream_server","").strip()
                xu       = request.form.get("xtream_user","").strip()
                xp       = request.form.get("xtream_pass","").strip()
                playlist = request.form.get("playlist_url","").strip()
                token    = secrets.token_urlsafe(24)
                conn.execute(
                    "INSERT INTO users (username,password_hash,email,dias_acesso,"
                    "xtream_server,xtream_user,xtream_pass,playlist_url,access_token) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (uname, generate_password_hash(pwd), email, dias, srv, xu, xp, playlist, token)
                )
                conn.commit()
                msg = f"✅ Usuário '{uname}' criado. Token: {token}"
            
            elif action == "update":
                uid  = int(request.form.get("uid"))
                conn.execute(
                    "UPDATE users SET email=?,dias_acesso=?,xtream_server=?,"
                    "xtream_user=?,xtream_pass=?,playlist_url=? WHERE id=?",
                    (request.form.get("email",""),
                     int(request.form.get("dias",30)),
                     request.form.get("xtream_server","").strip(),
                     request.form.get("xtream_user","").strip(),
                     request.form.get("xtream_pass","").strip(),
                     request.form.get("playlist_url","").strip(),
                     uid)
                )
                conn.commit()
                msg = "✅ Usuário atualizado."
            
            elif action == "gen_token":
                uid   = int(request.form.get("uid"))
                token = secrets.token_urlsafe(24)
                conn.execute("UPDATE users SET access_token=? WHERE id=?", (token, uid))
                conn.commit()
                msg = f"✅ Novo token: {token}"
            
            elif action == "delete":
                uid = int(request.form.get("uid"))
                conn.execute("DELETE FROM users WHERE id=? AND is_admin=0", (uid,))
                conn.commit()
                msg = "✅ Usuário excluído."
            
            elif action == "reset_password":
                uid = int(request.form.get("uid"))
                pwd = request.form.get("new_password","").strip()
                if len(pwd) >= 6:
                    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                                 (generate_password_hash(pwd), uid))
                    conn.commit()
                    msg = "✅ Senha alterada."
                else:
                    error = "Senha mínimo 6 caracteres."
        except Exception as e:
            error = str(e)
    
    users = conn.execute(
        "SELECT id,username,email,is_admin,dias_acesso,xtream_server,"
        "xtream_user,xtream_pass,playlist_url,access_token,created_at "
        "FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return render_template_string(ADMIN_HTML, users=users, msg=msg, error=error, u=current_user())

@app.route("/login/token")
def login_token():
    """Auto-login via token URL."""
    token = request.args.get("t","").strip()
    if not token: return redirect(url_for("login"))
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE access_token=? AND access_token!=''", (token,)).fetchone()
    conn.close()
    if not u: return redirect(url_for("login"))
    session.clear()
    session["user_id"]  = u["id"]
    session["username"] = u["username"]
    return redirect(url_for("home"))

@app.route("/api/me")
@require_login
def api_me():
    u = current_user()
    server, username, password = get_user_xtream(u)
    xtream_ok = False
    exp_date = ""
    if server and username and password:
        info = xtream_request(server, username, password, None, user_id=u["id"])
        if info and info.get("user_info"):
            xtream_ok = True
            exp_date = info["user_info"].get("exp_date","")
    return jsonify({
        "id": u["id"], "username": u["username"],
        "has_xtream": xtream_ok, "exp_date": exp_date,
    })

@app.route("/api/clear-cache")
@require_login
def api_clear_cache():
    u = current_user()
    keys_to_del = [k for k in _xtream_cache if k[0] == u["id"]]
    for k in keys_to_del:
        del _xtream_cache[k]
    if u["id"] in _m3u_cache:
        del _m3u_cache[u["id"]]
    session.pop("_u", None)
    return jsonify({"ok": True, "cleared": len(keys_to_del)})

# ── Templates ───────────────────────────────────────────────────────────────

_CSS = """
:root {
  --bg: #08080f;
  --bg2: #0f0f1a;
  --bg3: #161625;
  --border: rgba(255,255,255,.08);
  --text: #e8e8f0;
  --muted: #888;
  --accent: #e63950;
  --accent2: #ff6b35;
  --gold: #f5c518;
  --radius: 12px;
  --font: 'Segoe UI', system-ui, sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;
     -webkit-tap-highlight-color:transparent}

/* Header */
.header{position:sticky;top:0;z-index:100;background:rgba(8,8,15,.92);
        backdrop-filter:blur(20px);border-bottom:1px solid var(--border);
        padding:.6rem 1.2rem;display:flex;align-items:center;gap:.8rem;flex-wrap:wrap}
.logo{font-size:1.3rem;font-weight:800;background:linear-gradient(135deg,var(--accent),var(--accent2));
      -webkit-background-clip:text;-webkit-text-fill-color:transparent;text-decoration:none;
      flex-shrink:0;letter-spacing:-.5px}
.header-search{flex:1;min-width:140px;max-width:340px}
.header-search input{width:100%;background:var(--bg3);border:1px solid var(--border);
  border-radius:24px;padding:.4rem 1rem;color:var(--text);font-size:.85rem;outline:none}
.header-search input:focus{border-color:var(--accent)}
.nav-links{display:flex;gap:.2rem;flex-wrap:wrap}
.nav-link{color:var(--muted);text-decoration:none;padding:.35rem .7rem;border-radius:8px;
          font-size:.82rem;transition:all .15s}
.nav-link:hover,.nav-link.active{background:var(--bg3);color:var(--text)}
.user-chip{margin-left:auto;display:flex;align-items:center;gap:.5rem;flex-shrink:0}
.user-chip span{font-size:.8rem;color:var(--muted);display:none}
@media(min-width:480px){.user-chip span{display:inline}}
.btn-sm{background:var(--bg3);border:1px solid var(--border);color:var(--text);
        border-radius:8px;padding:.35rem .8rem;font-size:.78rem;cursor:pointer;
        text-decoration:none;transition:all .15s;white-space:nowrap}
.btn-sm:hover{border-color:var(--accent);color:var(--accent)}
.btn-accent{background:var(--accent);border-color:var(--accent);color:#fff}
.btn-accent:hover{opacity:.85;color:#fff}

/* Cards grid */
.page{padding:1rem;max-width:1400px;margin:0 auto}
.section{margin-bottom:2rem}
.section-title{font-size:1rem;font-weight:700;margin-bottom:.8rem;
               display:flex;align-items:center;gap:.5rem;color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:.7rem}
@media(max-width:480px){.grid{grid-template-columns:repeat(3,1fr);gap:.5rem}}

.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);
      overflow:hidden;cursor:pointer;transition:transform .15s,border-color .15s;
      text-decoration:none;color:var(--text);display:block;position:relative}
.card:hover{transform:translateY(-3px);border-color:rgba(230,57,80,.4)}
.card:active{transform:scale(.97)}
.card-img{width:100%;aspect-ratio:2/3;object-fit:cover;background:var(--bg3);
           display:block;transition:opacity .2s}
.card-img.live{aspect-ratio:16/9}
.card-body{padding:.5rem .6rem .6rem}
.card-title{font-size:.75rem;font-weight:600;line-height:1.3;
            overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;
            -webkit-box-orient:vertical}
.card-meta{font-size:.68rem;color:var(--muted);margin-top:.2rem}
.card-badge{position:absolute;top:.4rem;right:.4rem;background:var(--accent);
            color:#fff;font-size:.6rem;font-weight:700;padding:.15rem .4rem;
            border-radius:4px;text-transform:uppercase}
.card-badge.live{background:#22c55e;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}

/* Category pills */
.cats{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:1rem}
.cat-pill{background:var(--bg3);border:1px solid var(--border);border-radius:20px;
          padding:.3rem .8rem;font-size:.78rem;cursor:pointer;text-decoration:none;
          color:var(--muted);transition:all .15s}
.cat-pill:hover,.cat-pill.active{background:var(--accent);border-color:var(--accent);color:#fff}

/* Player */
.player-wrap{background:#000;border-radius:var(--radius);overflow:hidden;
             position:relative;aspect-ratio:16/9;width:100%}
.player-wrap video{width:100%;height:100%;display:block}
.watch-layout{display:grid;grid-template-columns:1fr;gap:1rem;max-width:1200px;margin:0 auto;padding:1rem}
@media(min-width:900px){.watch-layout{grid-template-columns:1fr 320px}}
.watch-info h1{font-size:1.2rem;margin-bottom:.5rem}
.watch-desc{color:var(--muted);font-size:.85rem;line-height:1.5;margin:.5rem 0}
.ep-list{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);
         overflow-y:auto;max-height:500px}
.ep-item{padding:.6rem .8rem;border-bottom:1px solid var(--border);cursor:pointer;
         font-size:.82rem;display:flex;gap:.5rem;align-items:center;text-decoration:none;color:var(--text)}
.ep-item:hover{background:var(--bg3)}
.ep-item.active{border-left:3px solid var(--accent);background:rgba(230,57,80,.06)}

/* Forms */
.form-wrap{max-width:480px;margin:2rem auto;padding:1rem}
.card-form{background:var(--bg2);border:1px solid var(--border);border-radius:16px;padding:2rem}
.form-title{font-size:1.2rem;font-weight:700;margin-bottom:1.2rem;text-align:center}
.form-group{margin-bottom:.9rem}
label{font-size:.75rem;color:var(--muted);display:block;margin-bottom:.25rem}
input,select,textarea{width:100%;background:var(--bg);border:1px solid var(--border);
  border-radius:8px;padding:.55rem .8rem;color:var(--text);font-size:.9rem;outline:none}
input:focus,select:focus{border-color:var(--accent)}
.btn-full{width:100%;background:var(--accent);color:#fff;border:none;border-radius:8px;
          padding:.7rem;font-size:1rem;font-weight:700;cursor:pointer;margin-top:.5rem}
.btn-full:hover{opacity:.88}
.alert{padding:.6rem .9rem;border-radius:8px;font-size:.85rem;margin-bottom:1rem}
.alert-error{background:rgba(230,57,80,.1);border:1px solid var(--accent);color:#ffa0b0}
.alert-success{background:rgba(34,197,94,.1);border:1px solid #22c55e;color:#a0ffb0}
.form-link{text-align:center;margin-top:1rem;font-size:.82rem;color:var(--muted)}
.form-link a{color:var(--accent);text-decoration:none}

/* Admin table */
.table-wrap{overflow-x:auto;border-radius:var(--radius);border:1px solid var(--border)}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{background:var(--bg3);padding:.6rem .8rem;text-align:left;font-weight:600;
   color:var(--muted);border-bottom:1px solid var(--border)}
td{padding:.55rem .8rem;border-bottom:1px solid var(--border);vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.02)}
.badge{display:inline-block;padding:.15rem .45rem;border-radius:4px;font-size:.65rem;font-weight:700}
.badge-admin{background:rgba(245,197,24,.15);color:var(--gold)}
.badge-ok{background:rgba(34,197,94,.15);color:#22c55e}
.badge-no{background:rgba(230,57,80,.1);color:var(--accent)}

/* Home hero */
.hero{background:linear-gradient(135deg,var(--bg2),var(--bg3));border:1px solid var(--border);
      border-radius:16px;padding:1.5rem;margin-bottom:1.5rem;display:flex;
      align-items:center;gap:1.2rem;flex-wrap:wrap}
.hero-icon{font-size:3rem;flex-shrink:0}
.hero h2{font-size:1.1rem;font-weight:700;margin-bottom:.3rem}
.hero p{color:var(--muted);font-size:.85rem;line-height:1.5}
.hero-actions{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.8rem}

/* No content */
.empty{text-align:center;padding:3rem 1rem;color:var(--muted)}
.empty .icon{font-size:3rem;margin-bottom:.8rem}

/* Pagination */
.pagination{display:flex;gap:.3rem;justify-content:center;margin-top:1.5rem;flex-wrap:wrap}
.page-btn{background:var(--bg3);border:1px solid var(--border);color:var(--muted);
          padding:.35rem .7rem;border-radius:6px;font-size:.8rem;text-decoration:none}
.page-btn.active,.page-btn:hover{background:var(--accent);border-color:var(--accent);color:#fff}

/* Loading */
.spinner{width:36px;height:36px;border:3px solid var(--border);border-top-color:var(--accent);
         border-radius:50%;animation:spin .8s linear infinite;margin:2rem auto}
@keyframes spin{to{transform:rotate(360deg)}}

/* Mobile */
@media(max-width:640px){
  .header{padding:.5rem .8rem;gap:.5rem}
  .nav-links{display:none}
  .page{padding:.7rem}
  .watch-layout{padding:.7rem}
}
"""

_HEAD = lambda title: f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="theme-color" content="#08080f">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>{title} — MedFlix</title>
<style>{_CSS}</style>
</head>
<body>
"""

_HEADER = """
<header class="header">
  <a class="logo" href="/home">🎬 MedFlix</a>
  <form class="header-search" action="/search" method="get">
    <input name="q" placeholder="Buscar filmes, séries, canais..." value="{{ request.args.get('q','') }}" autocomplete="off">
  </form>
  <nav class="nav-links">
    <a class="nav-link" href="/browse/live">📡 Ao Vivo</a>
    <a class="nav-link" href="/browse/vod">🎬 Filmes</a>
    <a class="nav-link" href="/browse/series">📺 Séries</a>
  </nav>
  <div class="user-chip">
    <span>{{ u.username if u else '' }}</span>
    {% if u and u.is_admin %}<a class="btn-sm" href="/admin">⚙️ Admin</a>{% endif %}
    <a class="btn-sm" href="/logout">Sair</a>
  </div>
</header>
"""

LOGIN_HTML = _HEAD("Login") + """
<div class="form-wrap">
  <div class="card-form">
    <div style="text-align:center;margin-bottom:1.5rem">
      <div style="font-size:3rem">🎬</div>
      <div style="font-size:1.4rem;font-weight:800;background:linear-gradient(135deg,#e63950,#ff6b35);
                  -webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-top:.3rem">
        MedFlix
      </div>
      <div style="color:var(--muted);font-size:.82rem;margin-top:.3rem">Sua biblioteca digital personalizada</div>
    </div>
    {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
    <form method="post">
      <div class="form-group">
        <label>USUÁRIO</label>
        <input name="username" placeholder="seu usuário" required autocomplete="username" autofocus>
      </div>
      <div class="form-group">
        <label>SENHA</label>
        <input name="password" type="password" placeholder="••••••••" required autocomplete="current-password">
      </div>
      <button class="btn-full" type="submit">Entrar</button>
    </form>
    <div class="form-link" style="margin-top:1.5rem;font-size:.75rem;opacity:.5">
      Não tem acesso? Entre em contato com o administrador.
    </div>
  </div>
</div>
</body></html>"""

HOME_HTML = _HEAD("Início") + _HEADER + """
<div class="page">
{% if not has_content %}
  <div class="hero">
    <div class="hero-icon">📡</div>
    <div style="flex:1">
      <h2>Configure sua lista de canais</h2>
      <p>Sua conta ainda não tem um servidor IPTV ou URL M3U vinculado.<br>
         Entre em contato com o administrador para vincular sua assinatura.</p>
      {% if u and u.is_admin %}
      <div class="hero-actions">
        <a class="btn-sm btn-accent" href="/admin">⚙️ Configurar no painel admin</a>
      </div>
      {% endif %}
    </div>
  </div>
{% else %}
  {% if account_info %}
  <div style="background:var(--bg2);border:1px solid var(--border);border-radius:12px;
              padding:.8rem 1.2rem;margin-bottom:1.5rem;display:flex;align-items:center;
              gap:1rem;flex-wrap:wrap;font-size:.82rem">
    <span style="color:var(--muted)">📡 Servidor conectado</span>
    <span class="badge badge-ok">✓ Online</span>
    {% if account_info.get('exp_date') %}
    <span style="color:var(--muted)">Validade: <strong style="color:var(--text)">
      {{ account_info.exp_date }}</strong></span>
    {% endif %}
    {% if account_info.get('max_connections') %}
    <span style="color:var(--muted)">Conexões: {{ account_info.get('active_cons',0) }}/{{ account_info.max_connections }}</span>
    {% endif %}
    <a class="btn-sm" href="/api/clear-cache" style="margin-left:auto">🔄 Atualizar</a>
  </div>
  {% endif %}

  <div class="section">
    <div class="section-title">📡 Ao Vivo
      <a class="btn-sm" href="/browse/live" style="margin-left:auto;font-size:.75rem">Ver todos</a>
    </div>
    <div class="cats">
      {% for cat in (live_cats or [])[:12] %}
      <a class="cat-pill" href="/browse/live?cat={{ cat.category_id }}&cat_name={{ cat.category_name|urlencode }}">
        {{ cat.category_name }}
      </a>
      {% endfor %}
    </div>
  </div>

  <div class="section">
    <div class="section-title">🎬 Filmes
      <a class="btn-sm" href="/browse/vod" style="margin-left:auto;font-size:.75rem">Ver todos</a>
    </div>
    <div class="cats">
      {% for cat in (vod_cats or [])[:12] %}
      <a class="cat-pill" href="/browse/vod?cat={{ cat.category_id }}&cat_name={{ cat.category_name|urlencode }}">
        {{ cat.category_name }}
      </a>
      {% endfor %}
    </div>
  </div>

  <div class="section">
    <div class="section-title">📺 Séries
      <a class="btn-sm" href="/browse/series" style="margin-left:auto;font-size:.75rem">Ver todos</a>
    </div>
    <div class="cats">
      {% for cat in (series_cats or [])[:12] %}
      <a class="cat-pill" href="/browse/series?cat={{ cat.category_id }}&cat_name={{ cat.category_name|urlencode }}">
        {{ cat.category_name }}
      </a>
      {% endfor %}
    </div>
  </div>
{% endif %}
</div>
</body></html>"""

BROWSE_HTML = _HEAD("{{ category_name }}") + _HEADER + """
<div class="page">
  <div style="display:flex;align-items:center;gap:.8rem;margin-bottom:1rem;flex-wrap:wrap">
    <h1 style="font-size:1.1rem">
      {{ '📡' if content_type=='live' else ('🎬' if content_type=='vod' else '📺') }}
      {{ category_name }}
    </h1>
    <span style="color:var(--muted);font-size:.82rem">{{ total }} itens</span>
    <a href="/browse/{{ content_type }}" class="btn-sm" style="margin-left:auto">Todas as categorias</a>
  </div>

  {% if not items %}
    <div class="empty"><div class="icon">📭</div><p>Nenhum item encontrado.</p></div>
  {% else %}
    <div class="grid">
    {% for item in items %}
      {% set name = item.get('name') or item.get('title','') %}
      {% set cover = item.get('stream_icon') or item.get('cover') or '' %}
      {% set sid = item.get('stream_id') or item.get('series_id') or '' %}
      {% set ext = item.get('container_extension','ts') %}
      <a class="card" href="/watch/{{ content_type }}/{{ sid }}?title={{ name|urlencode }}&cover={{ cover|urlencode }}&cat={{ category_id }}">
        <img class="card-img {{ 'live' if content_type=='live' else '' }}"
             src="{{ cover }}" alt="{{ name }}"
             loading="lazy"
             onerror="this.src='';this.style.background='#161625'">
        {% if content_type=='live' %}<span class="card-badge live">AO VIVO</span>{% endif %}
        <div class="card-body">
          <div class="card-title">{{ name }}</div>
          {% if item.get('rating') %}<div class="card-meta">⭐ {{ item.rating }}</div>{% endif %}
          {% if item.get('releaseDate') or item.get('year') %}
          <div class="card-meta">{{ item.get('releaseDate') or item.get('year','') }}</div>
          {% endif %}
        </div>
      </a>
    {% endfor %}
    </div>

    {% if total_pages > 1 %}
    <div class="pagination">
      {% if page > 1 %}
      <a class="page-btn" href="?cat={{ category_id }}&cat_name={{ category_name|urlencode }}&p={{ page-1 }}">‹ Anterior</a>
      {% endif %}
      {% for pg in range([1,page-2]|max, [total_pages+1,page+3]|min) %}
      <a class="page-btn {{ 'active' if pg==page else '' }}" href="?cat={{ category_id }}&cat_name={{ category_name|urlencode }}&p={{ pg }}">{{ pg }}</a>
      {% endfor %}
      {% if page < total_pages %}
      <a class="page-btn" href="?cat={{ category_id }}&cat_name={{ category_name|urlencode }}&p={{ page+1 }}">Próximo ›</a>
      {% endif %}
    </div>
    {% endif %}
  {% endif %}
</div>
</body></html>"""

WATCH_HTML = _HEAD("{{ title or 'Assistir' }}") + _HEADER + """
<div class="watch-layout">
  <div>
    <div class="player-wrap">
      {% if stream_url %}
      <video id="player" controls autoplay playsinline>
        <source src="{{ stream_url }}" type="{{ 'video/mp4' if content_type=='vod' else 'application/x-mpegURL' }}">
        Seu navegador não suporta este formato.
      </video>
      {% else %}
      <div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted)">
        <div style="text-align:center"><div style="font-size:2rem">⚠️</div><p>Stream não disponível</p></div>
      </div>
      {% endif %}
    </div>

    {% if stream_url and content_type == 'live' %}
    <div style="margin-top:.5rem;display:flex;gap:.5rem;flex-wrap:wrap">
      <button onclick="switchExt('ts')" class="btn-sm">TS</button>
      <button onclick="switchExt('m3u8')" class="btn-sm">M3U8</button>
      <span style="color:var(--muted);font-size:.78rem;align-self:center;margin-left:auto">
        Se não carregar, tente outro formato
      </span>
    </div>
    {% endif %}

    <div style="margin-top:1rem">
      <h1 style="font-size:1.1rem;margin-bottom:.4rem">{{ title }}</h1>
      {% if info.get('description') or info.get('plot') %}
      <p class="watch-desc">{{ info.get('description') or info.get('plot','') }}</p>
      {% endif %}
      {% if info.get('genre') %}
      <div style="font-size:.78rem;color:var(--muted);margin-top:.3rem">🎭 {{ info.genre }}</div>
      {% endif %}
      {% if info.get('director') %}
      <div style="font-size:.78rem;color:var(--muted)">🎬 {{ info.director }}</div>
      {% endif %}
      {% if info.get('cast') %}
      <div style="font-size:.78rem;color:var(--muted)">👥 {{ info.cast[:100] }}</div>
      {% endif %}
    </div>
  </div>

  {% if episodes %}
  <div>
    <div style="font-weight:600;margin-bottom:.5rem;font-size:.9rem">📋 Episódios</div>
    <div class="ep-list">
      {% for ep in episodes %}
      {% set ep_id = ep.get('id','') %}
      {% set ep_ext = ep.get('container_extension','mp4') %}
      {% set ep_title = ep.get('title','') or ('Ep. ' ~ ep.get('episode_num','')) %}
      <a class="ep-item {{ 'active' if ep_id|string == stream_id|string else '' }}"
         href="/watch/series/{{ ep_id }}/ep/{{ ep_id }}?ext={{ ep_ext }}&title={{ ep_title|urlencode }}&cover={{ cover|urlencode }}">
        <span style="color:var(--muted);flex-shrink:0">S{{ ep._season }}E{{ ep.get('episode_num','') }}</span>
        <span>{{ ep_title }}</span>
      </a>
      {% endfor %}
    </div>
  </div>
  {% endif %}
</div>

<script>
// HLS support via hls.js CDN
var video = document.getElementById('player');
var streamUrl = {{ (stream_url or '')|tojson }};

if(video && streamUrl) {
  function tryHls(url) {
    if(typeof Hls !== 'undefined' && Hls.isSupported()) {
      var hls = new Hls({maxBufferLength:30, enableWorker:false});
      hls.loadSource(url);
      hls.attachMedia(video);
    } else if(video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url;
    }
  }
  
  var isHls = streamUrl.includes('.m3u8') || streamUrl.includes('/live/') || streamUrl.endsWith('.ts');
  if(isHls) {
    var script = document.createElement('script');
    script.src = 'https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.4.12/hls.min.js';
    script.onload = function() { tryHls(streamUrl); };
    document.head.appendChild(script);
  }
}

function switchExt(ext) {
  var base = streamUrl.substring(0, streamUrl.lastIndexOf('.'));
  var newUrl = base + '.' + ext;
  if(video) { video.pause(); video.src = newUrl; video.load(); video.play().catch(function(){}); }
}
</script>
</body></html>"""

SEARCH_HTML = _HEAD("Buscar") + _HEADER + """
<div class="page">
  <form action="/search" method="get" style="margin-bottom:1rem">
    <div style="display:flex;gap:.5rem;max-width:500px">
      <input name="q" value="{{ q }}" placeholder="Buscar..." style="flex:1" autofocus>
      <button class="btn-sm btn-accent" type="submit">Buscar</button>
    </div>
  </form>

  {% if q and not results %}
  <div class="empty"><div class="icon">🔍</div><p>Nenhum resultado para "{{ q }}".</p></div>
  {% elif results %}
  <p style="color:var(--muted);font-size:.82rem;margin-bottom:.8rem">{{ results|length }} resultados para "{{ q }}"</p>
  <div class="grid">
    {% for item in results %}
    {% set name = item.get('name') or item.get('title','') %}
    {% set cover = item.get('stream_icon') or item.get('cover') or '' %}
    {% set sid = item.get('stream_id') or item.get('series_id') or '' %}
    {% set ctype = item.get('_type','vod') %}
    <a class="card" href="/watch/{{ ctype }}/{{ sid }}?title={{ name|urlencode }}&cover={{ cover|urlencode }}">
      <img class="card-img" src="{{ cover }}" alt="{{ name }}" loading="lazy"
           onerror="this.src='';this.style.background='#161625'">
      <div class="card-badge">{{ ctype|upper }}</div>
      <div class="card-body">
        <div class="card-title">{{ name }}</div>
      </div>
    </a>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty"><div class="icon">🔍</div><p>Digite algo para buscar em toda sua biblioteca.</p></div>
  {% endif %}
</div>
</body></html>"""

ADMIN_HTML = _HEAD("Admin") + _HEADER + """
<div class="page">
  <h1 style="font-size:1.2rem;margin-bottom:1.2rem">⚙️ Painel Admin — Gerenciar Usuários</h1>

  {% if msg %}<div class="alert alert-success">{{ msg }}</div>{% endif %}
  {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}

  <!-- Create User -->
  <div style="background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:1.2rem;margin-bottom:1.5rem">
    <h2 style="font-size:.95rem;margin-bottom:1rem">➕ Criar Novo Usuário</h2>
    <form method="post">
      <input type="hidden" name="action" value="create">
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:.6rem;margin-bottom:.8rem">
        <div><label>Usuário *</label><input name="username" required placeholder="joao123"></div>
        <div><label>Senha *</label><input name="password" type="password" required placeholder="mín 6 chars"></div>
        <div><label>E-mail</label><input name="email" type="email" placeholder="joao@email.com"></div>
        <div><label>Dias de acesso</label><input name="dias" type="number" value="30" min="1"></div>
      </div>
      <div style="background:rgba(230,57,80,.05);border:1px solid rgba(230,57,80,.15);border-radius:8px;padding:.8rem;margin-bottom:.8rem">
        <p style="font-size:.75rem;color:var(--muted);margin-bottom:.6rem">
          🔑 <strong style="color:var(--text)">Credenciais Xtream Codes</strong> — preencha para vincular a assinatura IPTV do usuário
        </p>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:.6rem">
          <div><label>Servidor IPTV</label><input name="xtream_server" placeholder="http://fastp150.com"></div>
          <div><label>Usuário IPTV</label><input name="xtream_user" placeholder="dcs17966"></div>
          <div><label>Senha IPTV</label><input name="xtream_pass" placeholder="29665wru"></div>
          <div><label>URL M3U (alternativa)</label><input name="playlist_url" placeholder="http://...m3u"></div>
        </div>
      </div>
      <button class="btn-sm btn-accent" type="submit">Criar Usuário</button>
    </form>
  </div>

  <!-- Users table -->
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>#</th><th>Usuário</th><th>Servidor IPTV</th>
          <th>Validade</th><th>Token de Acesso</th><th>Ações</th>
        </tr>
      </thead>
      <tbody>
        {% for user in users %}
        <tr>
          <td>{{ user.id }}</td>
          <td>
            <strong>{{ user.username }}</strong>
            {% if user.is_admin %}<span class="badge badge-admin">ADMIN</span>{% endif %}
            {% if user.email %}<br><span style="color:var(--muted);font-size:.72rem">{{ user.email }}</span>{% endif %}
          </td>
          <td style="font-size:.72rem">
            {% if user.xtream_server %}
              <span class="badge badge-ok">✓ Xtream</span><br>
              <span style="color:var(--muted)">{{ user.xtream_server[:30] }}</span><br>
              <span style="color:var(--muted)">{{ user.xtream_user }}/{{ user.xtream_pass }}</span>
            {% elif user.playlist_url %}
              <span class="badge" style="background:rgba(59,130,246,.15);color:#60a5fa">M3U</span><br>
              <span style="color:var(--muted)">{{ user.playlist_url[:40] }}</span>
            {% else %}
              <span class="badge badge-no">Sem lista</span>
            {% endif %}
          </td>
          <td style="font-size:.72rem;color:var(--muted)">
            {{ user.dias_acesso }} dias<br>
            {{ user.created_at[:10] if user.created_at else '' }}
          </td>
          <td style="font-size:.68rem;word-break:break-all;max-width:140px">
            {% if user.access_token %}
            <code style="color:var(--gold)">{{ user.access_token[:20] }}...</code><br>
            <a href="/login/token?t={{ user.access_token }}" style="color:var(--accent);font-size:.65rem" target="_blank">🔗 Link de acesso</a>
            {% else %}—{% endif %}
          </td>
          <td>
            <div style="display:flex;flex-direction:column;gap:.3rem">
              <!-- Edit form -->
              <button class="btn-sm" onclick="toggleEdit({{ user.id }})">✏️ Editar</button>
              <form method="post" style="display:inline">
                <input type="hidden" name="action" value="gen_token">
                <input type="hidden" name="uid" value="{{ user.id }}">
                <button class="btn-sm" type="submit">🔑 Token</button>
              </form>
              {% if not user.is_admin %}
              <form method="post" onsubmit="return confirm('Excluir {{ user.username }}?')">
                <input type="hidden" name="action" value="delete">
                <input type="hidden" name="uid" value="{{ user.id }}">
                <button class="btn-sm" type="submit" style="color:var(--accent)">🗑</button>
              </form>
              {% endif %}
            </div>

            <!-- Inline edit form (hidden) -->
            <div id="edit-{{ user.id }}" style="display:none;margin-top:.5rem">
              <form method="post">
                <input type="hidden" name="action" value="update">
                <input type="hidden" name="uid" value="{{ user.id }}">
                <div style="display:flex;flex-direction:column;gap:.3rem">
                  <input name="xtream_server" value="{{ user.xtream_server or '' }}" placeholder="Servidor Xtream" style="font-size:.75rem">
                  <input name="xtream_user" value="{{ user.xtream_user or '' }}" placeholder="Usuário Xtream" style="font-size:.75rem">
                  <input name="xtream_pass" value="{{ user.xtream_pass or '' }}" placeholder="Senha Xtream" style="font-size:.75rem">
                  <input name="playlist_url" value="{{ user.playlist_url or '' }}" placeholder="URL M3U (alternativa)" style="font-size:.75rem">
                  <input name="email" value="{{ user.email or '' }}" placeholder="E-mail" style="font-size:.75rem">
                  <input name="dias" type="number" value="{{ user.dias_acesso }}" placeholder="Dias" style="font-size:.75rem">
                  <button class="btn-sm btn-accent" type="submit">💾 Salvar</button>
                </div>
              </form>
            </div>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div style="background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:1rem;margin-top:1.5rem;font-size:.82rem;color:var(--muted)">
    <strong style="color:var(--text)">🔗 Como compartilhar acesso com um cliente:</strong><br><br>
    1. Crie o usuário com as credenciais Xtream dele<br>
    2. Gere um token com o botão 🔑<br>
    3. Envie o link: <code style="color:var(--gold)">https://medflix.onrender.com/login/token?t=TOKEN</code><br>
    4. O cliente clica no link e já entra direto no site com os canais dele
  </div>
</div>

<script>
function toggleEdit(id) {
  var el = document.getElementById('edit-'+id);
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}
</script>
</body></html>"""

SETUP_HTML = _HEAD("Setup") + """
<div class="form-wrap">
  <div class="card-form">
    <div class="form-title">🚀 Setup Inicial</div>
    <p style="color:var(--muted);font-size:.82rem;text-align:center;margin-bottom:1.2rem">
      Crie o primeiro administrador do sistema
    </p>
    {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
    <div style="background:rgba(245,197,24,.05);border:1px solid rgba(245,197,24,.2);
                border-radius:8px;padding:.7rem;margin-bottom:1rem;font-size:.78rem;color:var(--muted)">
      Código padrão: <code style="color:var(--gold)">medflix2026</code>
      — ou defina a env var <code>SETUP_CODE</code>
    </div>
    <form method="post">
      <div class="form-group"><label>Usuário</label><input name="username" required placeholder="admin"></div>
      <div class="form-group"><label>Senha (mín. 6 caracteres)</label><input name="password" type="password" required></div>
      <div class="form-group"><label>Código de ativação</label><input name="code" type="password" required placeholder="medflix2026"></div>
      <button class="btn-full" type="submit">Criar Administrador</button>
    </form>
  </div>
</div>
</body></html>"""

RESET_HTML = _HEAD("Recuperar Acesso") + """
<div class="form-wrap">
  <div class="card-form">
    <div class="form-title">🔧 Recuperar Acesso Admin</div>
    {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
    <form method="post">
      <div class="form-group"><label>Chave (ADMIN_RESET_KEY)</label><input name="key" type="password" required></div>
      <div class="form-group"><label>Usuário admin</label><input name="username" required></div>
      <div class="form-group"><label>Nova senha</label><input name="password" type="password" required></div>
      <button class="btn-full" type="submit">Recuperar</button>
    </form>
  </div>
</div>
</body></html>"""

SIMPLE_PAGE = """<!DOCTYPE html><html><head><meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{{ title }}</title>
<style>body{background:#08080f;color:#e8e8f0;font-family:system-ui;display:flex;
align-items:center;justify-content:center;min-height:100vh;padding:1rem}
.box{background:#0f0f1a;border:1px solid rgba(255,255,255,.1);border-radius:16px;
padding:2rem;max-width:420px;width:100%;text-align:center}
h2{margin:.5rem 0;color:#fff} p{color:#888;font-size:.9rem;margin:.5rem 0}
a{display:inline-block;margin-top:1rem;background:#e63950;color:#fff;padding:.6rem 1.4rem;
border-radius:8px;text-decoration:none;font-weight:700}</style></head>
<body><div class='box'>
<div style='font-size:2.5rem'>{{ icon }}</div>
<h2>{{ title }}</h2><p>{{ msg }}</p>
<a href='{{ link }}'>{{ link_text }}</a>
</div></body></html>"""

# ── Run ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
