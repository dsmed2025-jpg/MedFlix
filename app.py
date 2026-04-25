"""
MedFlix — Plataforma IPTV estilo Netflix/vouver.me
- Admin vincula lista M3U ou Xtream Codes a cada usuário
- Usuário faz login e vê SÓ os canais/filmes da lista dele
- Player HLS embutido na página
- Layout: sidebar de categorias + grid de cards + player fullscreen
"""
import os, re, json, time, gzip, sqlite3, threading, urllib.request, urllib.parse, base64
from pathlib import Path
from functools import wraps
from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, jsonify, Response)
from werkzeug.security import generate_password_hash, check_password_hash

# ─── App setup ───────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
if os.getenv("RENDER"):
    DB_PATH = Path("/opt/render/project/src/medflix.db")
else:
    DB_PATH = BASE_DIR / "medflix.db"
app = Flask(__name__)
app.config.update(
    SECRET_KEY               = os.getenv("SECRET_KEY", "medflix-2026-secret"),
    MAX_CONTENT_LENGTH       = 5 * 1024 * 1024,
    SESSION_COOKIE_SAMESITE  = "Lax",
    SESSION_COOKIE_HTTPONLY  = True,
)

# ─── Database ────────────────────────────────────────────────────────────────
def get_db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

_db_ready = False
@app.before_request
def ensure_db():
    global _db_ready
    if not _db_ready:
        try: _init_db()
        except Exception as e: app.logger.warning(f"DB: {e}")
        _db_ready = True

def _init_db():
    c = get_db(); cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        xtream_server TEXT DEFAULT '',
        xtream_user TEXT DEFAULT '',
        xtream_pass TEXT DEFAULT '',
        playlist_url TEXT DEFAULT '',
        access_token TEXT DEFAULT '',
        dias INTEGER DEFAULT 30,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    # migrate
    cols = {r[1] for r in cur.execute("PRAGMA table_info(users)")}
    for col, t in [("xtream_server","TEXT DEFAULT ''"),("xtream_user","TEXT DEFAULT ''"),
                   ("xtream_pass","TEXT DEFAULT ''"),("playlist_url","TEXT DEFAULT ''"),
                   ("access_token","TEXT DEFAULT ''"),("dias","INTEGER DEFAULT 30")]:
        if col not in cols: cur.execute(f"ALTER TABLE users ADD COLUMN {col} {t}")
    # channels cache per user
    cur.execute("""CREATE TABLE IF NOT EXISTS channels(
        user_id INTEGER NOT NULL,
        cat_id TEXT DEFAULT '',
        cat_name TEXT DEFAULT '',
        name TEXT NOT NULL,
        logo TEXT DEFAULT '',
        stream_url TEXT NOT NULL,
        ctype TEXT DEFAULT 'live',
        ext TEXT DEFAULT 'ts',
        updated_at INTEGER DEFAULT 0,
        PRIMARY KEY(user_id, stream_url))""")
    # default admin
    cur.execute("INSERT OR IGNORE INTO users(username,password_hash,is_admin) VALUES(?,?,1)",
                ("demo", generate_password_hash("demo123")))
    c.commit(); c.close()

# ─── Auth ────────────────────────────────────────────────────────────────────
def logged_in(): return "uid" in session

def me():
    """Always read from DB — never cache in session (avoids stale data after admin updates)."""
    if not logged_in(): return None
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    db.close()
    if not row: return None
    return dict(row)

def login_required(f):
    @wraps(f)
    def d(*a,**k):
        if not logged_in(): return redirect("/login")
        return f(*a,**k)
    return d

def admin_required(f):
    @wraps(f)
    def d(*a,**k):
        u = me()
        if not u or not u["is_admin"]: return redirect("/")
        return f(*a,**k)
    return d

# ─── M3U Parser ──────────────────────────────────────────────────────────────
UA = ("Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36")

_parse_cache = {}   # user_id → (channels_list, timestamp)
CACHE_TTL = 1800    # 30 min

# ── Genre mapping ──────────────────────────────────────────────────────────
GENRES = [
    ("live",    "📡 Ao Vivo"),
    ("filmes",  "🎬 Filmes"),
    ("series",  "📺 Séries"),
    ("infantil","🧒 Infantil"),
    ("esportes","⚽ Esportes"),
    ("noticias","📰 Notícias"),
    ("docs",    "🎥 Documentários"),
    ("adultos", "🔞 Adultos"),
    ("outros",  "📦 Outros"),
]
GENRE_KEYS = {g[0] for g in GENRES}

def _detect_genre(name, grp, url):
    """Classify a channel into one of our genres."""
    txt = (name + " " + grp).lower()
    url_l = url.lower()
    # Series/VOD by URL structure
    if "/series/" in url_l: return "series"
    if "/movie/"  in url_l: return "filmes"
    # By group-title keywords
    kw_map = [
        ("filmes",   ["filme","movie","vod","cinema","estreia","lançamento"]),
        ("series",   ["serie","séri","novela","temporada","episodio","episódio","season"]),
        ("infantil", ["infantil","kids","criança","cartoon","animação","animacao","disney","nickelodeon"]),
        ("esportes", ["sport","esporte","futebol","football","nba","nfl","ufc","mma","olympics","formula"]),
        ("noticias", ["notícia","noticia","news","jornal","cnn","globonews","band news"]),
        ("docs",     ["doc","document","discovery","national","history","biograph"]),
        ("adultos",  ["adulto","adult","xxx","erotic","sex","18+"]),
    ]
    for genre, keywords in kw_map:
        if any(k in txt for k in keywords):
            return genre
    # Live channels (default for non-VOD)
    if not any(url_l.endswith(x) for x in [".mp4",".mkv",".avi",".mov"]):
        return "live"
    return "filmes"

def _fetch_url(url):
    """Download URL content with IPTV-compatible headers."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Connection": "keep-alive",
        "Pragma": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()
    try:    return raw.decode("utf-8")
    except: return raw.decode("latin-1", errors="ignore")

def _parse_m3u_text(text):
    """Parse M3U/M3U+ text → list of channel dicts with genre."""
    channels = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            logo = (re.search(r'tvg-logo="([^"]*)"',    line) or [None,""])[1].strip()
            grp  = (re.search(r'group-title="([^"]*)"', line) or [None,""])[1].strip()
            name = (re.search(r',(.+)$',               line) or [None,"Canal"])[1].strip()
            url  = ""
            j = i + 1
            while j < len(lines):
                cand = lines[j].strip()
                if cand and not cand.startswith("#"):
                    url = cand; i = j; break
                j += 1
            if url and name:
                u_low = url.lower()
                # Determine extension
                ext = "ts"
                if u_low.endswith(".m3u8"):   ext = "m3u8"
                elif u_low.endswith(".mp4"):   ext = "mp4"
                elif u_low.endswith(".mkv"):   ext = "mkv"
                elif "/movie/"  in u_low:      ext = "mp4"
                elif "/series/" in u_low:      ext = "mp4"
                genre = _detect_genre(name, grp, url)
                channels.append({
                    "genre":      genre,
                    "cat_name":   grp or genre,
                    "name":       name,
                    "logo":       logo,
                    "stream_url": url,
                    "ext":        ext,
                })
        i += 1
    return channels

def get_user_channels(user, force=False):
    """Return parsed channels for a user, using in-memory cache."""
    uid = user["id"]
    if not force and uid in _parse_cache:
        chans, ts = _parse_cache[uid]
        if time.time() - ts < CACHE_TTL:
            return chans

    srv  = (user.get("xtream_server") or "").strip().rstrip("/")
    xu   = (user.get("xtream_user")   or "").strip()
    xp   = (user.get("xtream_pass")   or "").strip()
    purl = (user.get("playlist_url")  or "").strip()

    channels = []
    error_msg = None

    # Option 1: Xtream Codes → build M3U URL
    if srv and xu and xp:
        if not srv.startswith(("http://","https://")):
            srv = "http://" + srv
        m3u_url = f"{srv}/get.php?username={xu}&password={xp}&type=m3u_plus&output=ts"
        try:
            app.logger.info(f"Fetching Xtream M3U for user {uid}: {m3u_url[:60]}...")
            text = _fetch_url(m3u_url)
            if "#EXTINF" in text:
                channels = _parse_m3u_text(text)
                app.logger.info(f"Parsed {len(channels)} channels for user {uid}")
            else:
                error_msg = f"Resposta inválida do servidor: {text[:200]}"
                app.logger.warning(error_msg)
        except Exception as e:
            error_msg = str(e)
            app.logger.warning(f"Xtream fetch error for user {uid}: {e}")

    # Option 2: Direct M3U URL
    if not channels and purl:
        try:
            app.logger.info(f"Fetching M3U URL for user {uid}: {purl[:60]}...")
            text = _fetch_url(purl)
            if "#EXTINF" in text:
                channels = _parse_m3u_text(text)
                app.logger.info(f"Parsed {len(channels)} channels (M3U URL) for user {uid}")
            else:
                app.logger.warning(f"M3U URL returned invalid data for user {uid}: {text[:200]}")
        except Exception as e:
            app.logger.warning(f"M3U URL fetch error for user {uid}: {e}")

    _parse_cache[uid] = (channels, time.time())
    if error_msg and not channels:
        _parse_cache[uid] = ([], time.time() - CACHE_TTL + 60)  # retry in 60s
    return channels

def get_genre_counts(channels):
    """Return dict of genre → count."""
    counts = {}
    for ch in channels:
        g = ch.get("genre","outros")
        counts[g] = counts.get(g, 0) + 1
    return counts

# ─── Background preload ───────────────────────────────────────────────────────
def _preload_user(user):
    try: get_user_channels(user, force=True)
    except: pass

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect("/login" if not logged_in() else "/tv")

@app.route("/login", methods=["GET","POST"])
def login():
    err = ""
    if request.method == "POST":
        u = request.form.get("username","").strip()
        p = request.form.get("password","").strip()
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        db.close()
        if row and check_password_hash(row["password_hash"], p):
            session.clear()
            session["uid"]      = row["id"]
            session["username"] = row["username"]
            # preload channels in background
            threading.Thread(target=_preload_user, args=(dict(row),), daemon=True).start()
            return redirect("/tv")
        err = "Usuário ou senha incorretos."
    return _page("login", LOGIN_HTML, err=err)

@app.route("/logout")
def logout():
    session.clear(); return redirect("/login")

@app.route("/login/token")
def login_token():
    t = request.args.get("t","").strip()
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE access_token=? AND access_token!=''", (t,)).fetchone()
    db.close()
    if not row: return redirect("/login")
    session.clear()
    session["uid"]      = row["id"]
    session["username"] = row["username"]
    threading.Thread(target=_preload_user, args=(dict(row),), daemon=True).start()
    return redirect("/tv")

@app.route("/tv")
@login_required
def tv():
    u = me()
    channels = get_user_channels(u)
    genre   = request.args.get("genre", "")
    subcat  = request.args.get("cat", "")
    q       = request.args.get("q","").strip().lower()

    # Filter by genre first, then subcat, then search
    filtered = channels
    if genre:
        filtered = [ch for ch in filtered if ch.get("genre") == genre]
    if subcat:
        filtered = [ch for ch in filtered if re.sub(r"[^a-z0-9]","",ch.get("cat_name","").lower())[:20] == subcat]
    if q:
        filtered = [ch for ch in filtered if q in ch["name"].lower()]

    # Subcats for this genre
    if genre:
        seen = {}
        for ch in [c for c in channels if c.get("genre")==genre]:
            cid = re.sub(r"[^a-z0-9]","", ch.get("cat_name","").lower())[:20]
            if cid not in seen: seen[cid] = ch.get("cat_name","")
        subcats = list(seen.items())
    else:
        subcats = []

    genre_counts = get_genre_counts(channels)
    # If user has credentials but no channels yet, trigger background load
    loading = False
    if not channels:
        srv  = (u.get("xtream_server") or "").strip()
        xu   = (u.get("xtream_user")   or "").strip()
        xp   = (u.get("xtream_pass")   or "").strip()
        purl = (u.get("playlist_url")  or "").strip()
        has_creds = (srv and xu and xp) or bool(purl)
        if has_creds:
            loading = True
            # Trigger load if not already cached
            if u["id"] not in _parse_cache:
                threading.Thread(target=_preload_user, args=(u,), daemon=True).start()
    else:
        has_creds = True
    return _page("tv", TV_HTML,
                 u=u, channels=filtered, has_list=bool(channels),
                 genre=genre, subcat=subcat, q=q,
                 subcats=subcats, loading=loading,
                 genres=GENRES, genre_counts=genre_counts)

@app.route("/watch")
@login_required
def watch():
    u = me()
    channels = get_user_channels(u)
    stream = request.args.get("s","")
    ch = next((c for c in channels if _url_id(c["stream_url"]) == stream), None)
    if not ch: return redirect("/tv")
    genre = request.args.get("genre", ch.get("genre",""))
    # Playlist = same genre (or same cat_name for better grouping)
    playlist = [c for c in channels if c.get("genre") == genre]
    if not playlist: playlist = channels
    genre_counts = get_genre_counts(channels)
    return _page("watch", WATCH_HTML,
                 u=u, ch=ch, playlist=playlist, genre=genre,
                 genres=GENRES, genre_counts=genre_counts)

@app.route("/api/reload")
@login_required
def api_reload():
    u = me()
    uid = u["id"]
    _parse_cache.pop(uid, None)
    threading.Thread(target=_preload_user, args=(u,), daemon=True).start()
    return jsonify({"ok": True, "uid": uid})

@app.route("/api/channels/count")
@login_required
def api_channels_count():
    """Quick endpoint to check if channels loaded yet."""
    u = me()
    chans = get_user_channels(u)
    return jsonify({"count": len(chans), "has_list": bool(chans)})

@app.route("/api/channels")
@login_required
def api_channels():
    u = me()
    channels = get_user_channels(u)
    q   = request.args.get("q","").strip().lower()
    cat = request.args.get("cat","")
    out = [ch for ch in channels
           if (not cat or ch["cat_id"] == cat) and
              (not q or q in ch["name"].lower())]
    return jsonify(out[:200])

# ─── ADMIN ───────────────────────────────────────────────────────────────────

@app.route("/setup", methods=["GET","POST"])
def setup():
    db = get_db()
    has = db.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]
    db.close()
    if has: return _simple("🔒","Setup já feito","Já existe um admin.","/login","Entrar")
    err = ""
    if request.method == "POST":
        u = request.form.get("u","").strip()
        p = request.form.get("p","").strip()
        code = request.form.get("code","").strip()
        if code != os.getenv("SETUP_CODE","medflix2026"): err="Código incorreto."
        elif not u or len(p)<6: err="Preencha usuário e senha (mín 6)."
        else:
            db = get_db()
            db.execute("INSERT OR REPLACE INTO users(username,password_hash,is_admin) VALUES(?,?,1)",
                       (u, generate_password_hash(p)))
            db.commit(); db.close()
            return _simple("✅","Admin criado!",f"Usuário '{u}' criado.","/login","Fazer login →")
    return _page("setup", SETUP_HTML, err=err)

@app.route("/admin/reset", methods=["GET","POST"])
def admin_reset():
    key = os.getenv("ADMIN_RESET_KEY","").strip()
    if not key: return _simple("🔒","Desativado","Defina ADMIN_RESET_KEY nas env vars.","/","Voltar")
    err = ""
    if request.method == "POST":
        if request.form.get("key","") != key: err="Chave incorreta."
        else:
            u = request.form.get("u","").strip()
            p = request.form.get("p","").strip()
            if not u or len(p)<6: err="Preencha todos os campos."
            else:
                import secrets as _sec
                tok = _sec.token_urlsafe(24)
                db = get_db()
                row = db.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()
                if row: db.execute("UPDATE users SET is_admin=1,password_hash=?,access_token=? WHERE id=?",
                                   (generate_password_hash(p), tok, row["id"]))
                else:   db.execute("INSERT INTO users(username,password_hash,is_admin,access_token) VALUES(?,?,1,?)",
                                   (u, generate_password_hash(p), tok))
                db.commit(); db.close()
                return _simple("✅","Acesso recuperado!",f"Admin '{u}' criado. Remova ADMIN_RESET_KEY após usar.","/login","Login →")
    return _page("reset", RESET_HTML, err=err)

@app.route("/admin", methods=["GET","POST"])
@admin_required
def admin():
    db = get_db()
    msg = err = ""
    if request.method == "POST":
        a = request.form.get("a","")
        try:
            import secrets as _sec
            if a == "create":
                tok = _sec.token_urlsafe(24)
                db.execute(
                    "INSERT INTO users(username,password_hash,dias,xtream_server,xtream_user,"
                    "xtream_pass,playlist_url,access_token) VALUES(?,?,?,?,?,?,?,?)",
                    (request.form.get("u","").strip(),
                     generate_password_hash(request.form.get("p","").strip()),
                     int(request.form.get("dias",30)),
                     request.form.get("srv","").strip(),
                     request.form.get("xu","").strip(),
                     request.form.get("xp","").strip(),
                     request.form.get("purl","").strip(),
                     tok))
                db.commit()
                msg = f"✅ Usuário criado. Token: {tok}"
            elif a == "update":
                uid = int(request.form.get("uid"))
                db.execute("UPDATE users SET xtream_server=?,xtream_user=?,xtream_pass=?,"
                           "playlist_url=?,dias=? WHERE id=?",
                    (request.form.get("srv","").strip(),
                     request.form.get("xu","").strip(),
                     request.form.get("xp","").strip(),
                     request.form.get("purl","").strip(),
                     int(request.form.get("dias",30)),
                     uid))
                db.commit()
                _parse_cache.pop(uid, None)   # force re-fetch channels
                # Preload in background so user sees content immediately after login
                db2 = get_db()
                updated_u = db2.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
                db2.close()
                if updated_u:
                    threading.Thread(target=_preload_user, args=(dict(updated_u),), daemon=True).start()
                msg = "✅ Usuário atualizado. Canais sendo carregados em background..."
            elif a == "token":
                uid = int(request.form.get("uid"))
                tok = _sec.token_urlsafe(24)
                db.execute("UPDATE users SET access_token=? WHERE id=?", (tok, uid))
                db.commit(); msg = f"✅ Token: {tok}"
            elif a == "pwd":
                uid = int(request.form.get("uid"))
                p = request.form.get("p","").strip()
                if len(p)<6: err="Senha mín 6 chars."
                else:
                    db.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (generate_password_hash(p), uid))
                    db.commit(); msg="✅ Senha alterada."
            elif a == "del":
                uid = int(request.form.get("uid"))
                db.execute("DELETE FROM users WHERE id=? AND is_admin=0", (uid,))
                db.commit(); msg="✅ Excluído."
        except Exception as e: err=str(e)
    users = db.execute("SELECT * FROM users ORDER BY id").fetchall()
    db.close()
    return _page("admin", ADMIN_HTML, users=users, msg=msg, err=err, me=me())

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _url_id(url):
    return base64.urlsafe_b64encode(url.encode()).decode()[:32]

def _page(name, tmpl, **ctx):
    return render_template_string(BASE_HTML.replace("%%CONTENT%%", tmpl), **ctx)

def _simple(icon, title, msg, link, ltext):
    return render_template_string(SIMPLE_HTML, icon=icon, title=title,
                                  msg=msg, link=link, ltext=ltext)

# ─── HTML Templates ──────────────────────────────────────────────────────────

CSS = r"""
:root{
  --bg:#09090f;--bg2:#111118;--bg3:#1a1a24;--bg4:#22222e;
  --border:rgba(255,255,255,.07);--text:#eeeef5;--muted:#666;
  --accent:#e8183a;--accent2:#ff5533;--gold:#f5c518;
  --r:10px;--font:'Segoe UI',system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:var(--font);
     min-height:100vh;overflow-x:hidden;-webkit-tap-highlight-color:transparent}
a{color:inherit;text-decoration:none}
img{display:block}

/* ── TOPBAR ── */
.topbar{
  position:fixed;top:0;left:0;right:0;z-index:200;
  background:rgba(9,9,15,.95);backdrop-filter:blur(16px);
  border-bottom:1px solid var(--border);
  height:56px;display:flex;align-items:center;padding:0 1rem;gap:.8rem;
}
.logo{font-size:1.4rem;font-weight:900;letter-spacing:-1px;flex-shrink:0;
      background:linear-gradient(135deg,var(--accent),var(--accent2));
      -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.topbar-search{flex:1;max-width:320px;position:relative}
.topbar-search input{
  width:100%;background:var(--bg3);border:1px solid var(--border);
  border-radius:20px;padding:.35rem 1rem .35rem 2.2rem;
  color:var(--text);font-size:.82rem;outline:none;
}
.topbar-search input:focus{border-color:var(--accent)}
.topbar-search::before{content:"🔍";position:absolute;left:.7rem;top:50%;
  transform:translateY(-50%);font-size:.75rem;pointer-events:none}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:.5rem}
.topbar-user{font-size:.78rem;color:var(--muted);display:none}
@media(min-width:500px){.topbar-user{display:block}}
.tbtn{
  background:var(--bg3);border:1px solid var(--border);color:var(--text);
  border-radius:8px;padding:.3rem .7rem;font-size:.75rem;cursor:pointer;
  white-space:nowrap;transition:border-color .15s;
}
.tbtn:hover{border-color:var(--accent);color:var(--accent)}
.tbtn-accent{background:var(--accent);border-color:var(--accent);color:#fff}
.tbtn-accent:hover{opacity:.85;color:#fff}

/* ── LAYOUT ── */
.shell{display:flex;padding-top:56px;min-height:100vh}

/* ── SIDEBAR ── */
.sidebar{
  width:220px;flex-shrink:0;
  background:var(--bg2);border-right:1px solid var(--border);
  position:sticky;top:56px;height:calc(100vh - 56px);
  overflow-y:auto;padding:.5rem 0;
  scrollbar-width:thin;scrollbar-color:var(--bg4) transparent;
}
.sidebar::-webkit-scrollbar{width:4px}
.sidebar::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:2px}
.sidebar-head{
  padding:.5rem 1rem .4rem;font-size:.68rem;font-weight:700;
  color:var(--muted);text-transform:uppercase;letter-spacing:.08em;
}
.cat-item{
  display:block;padding:.48rem 1rem;font-size:.82rem;color:var(--muted);
  cursor:pointer;transition:all .12s;border-left:3px solid transparent;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.cat-item:hover{background:var(--bg3);color:var(--text)}
.cat-item.active{background:rgba(232,24,58,.08);color:var(--accent);
                  border-left-color:var(--accent);font-weight:600}

/* ── MAIN ── */
.main{flex:1;min-width:0;padding:1rem}

/* ── CARDS GRID ── */
.grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
  gap:.65rem;
}
@media(max-width:500px){
  .grid{grid-template-columns:repeat(3,1fr);gap:.4rem}
}

.card{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--r);overflow:hidden;cursor:pointer;
  transition:transform .15s,border-color .15s,box-shadow .15s;
}
.card:hover{transform:translateY(-4px);border-color:rgba(232,24,58,.4);
             box-shadow:0 8px 24px rgba(0,0,0,.4)}
.card:active{transform:scale(.97)}
.card-thumb{
  width:100%;aspect-ratio:16/9;object-fit:cover;
  background:var(--bg3);
}
.card-thumb.poster{aspect-ratio:2/3}
.card-body{padding:.45rem .6rem .55rem}
.card-name{
  font-size:.72rem;font-weight:600;line-height:1.3;
  overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;
}
.card-sub{font-size:.65rem;color:var(--muted);margin-top:.15rem}
.live-dot{
  display:inline-block;width:6px;height:6px;background:#22c55e;
  border-radius:50%;margin-right:4px;
  animation:blink 1.5s ease-in-out infinite;
}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

/* ── PLAYER PAGE ── */
.watch-shell{
  display:grid;grid-template-columns:1fr;gap:0;
  padding-top:56px;height:100vh;overflow:hidden;
}
@media(min-width:900px){
  .watch-shell{grid-template-columns:1fr 280px}
}
.player-area{
  display:flex;flex-direction:column;background:#000;
  height:calc(100vh - 56px);
}
.player-wrap{
  flex:1;position:relative;background:#000;
  display:flex;align-items:center;justify-content:center;
}
.player-wrap video{width:100%;height:100%;display:block;background:#000}
.player-bar{
  background:var(--bg2);border-top:1px solid var(--border);
  padding:.5rem .8rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;
}
.player-title{font-size:.82rem;font-weight:600;flex:1;min-width:0;
               white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.player-cat{font-size:.72rem;color:var(--muted)}
.pl-list{
  border-left:1px solid var(--border);background:var(--bg2);
  overflow-y:auto;height:calc(100vh - 56px);
}
.pl-item{
  padding:.55rem .8rem;border-bottom:1px solid var(--border);
  cursor:pointer;display:flex;align-items:center;gap:.6rem;
  transition:background .1s;
}
.pl-item:hover{background:var(--bg3)}
.pl-item.active{background:rgba(232,24,58,.1);border-left:3px solid var(--accent)}
.pl-thumb{
  width:56px;height:32px;object-fit:cover;flex-shrink:0;
  border-radius:4px;background:var(--bg4);
}
.pl-name{font-size:.75rem;font-weight:500;
          overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;
          -webkit-box-orient:vertical;line-height:1.3}

/* ── NO LIST banner ── */
.no-list{
  background:var(--bg2);border:1px solid var(--border);border-radius:12px;
  padding:2rem;text-align:center;max-width:500px;margin:2rem auto;
}
.no-list .icon{font-size:3rem;margin-bottom:.8rem}
.no-list h2{font-size:1.1rem;margin-bottom:.4rem}
.no-list p{color:var(--muted);font-size:.85rem;line-height:1.5}

/* ── LOADING OVERLAY ── */
.loading{
  position:fixed;inset:0;background:rgba(9,9,15,.85);
  display:flex;align-items:center;justify-content:center;
  z-index:999;flex-direction:column;gap:1rem;
}
.spinner{
  width:40px;height:40px;border:3px solid var(--border);
  border-top-color:var(--accent);border-radius:50%;
  animation:spin .8s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── FORMS / ADMIN ── */
.page-wrap{max-width:1200px;margin:0 auto;padding:1rem}
.card-form{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:14px;padding:1.5rem;max-width:440px;margin:2rem auto;
}
.form-title{font-size:1.1rem;font-weight:700;margin-bottom:1.2rem;text-align:center}
.fg{margin-bottom:.8rem}
label{font-size:.73rem;color:var(--muted);display:block;margin-bottom:.2rem}
input,select{
  width:100%;background:var(--bg);border:1px solid var(--border);
  border-radius:7px;padding:.5rem .75rem;color:var(--text);
  font-size:.88rem;outline:none;
}
input:focus,select:focus{border-color:var(--accent)}
.btn{
  background:var(--accent);color:#fff;border:none;border-radius:8px;
  padding:.6rem 1.2rem;font-size:.88rem;font-weight:700;cursor:pointer;
}
.btn:hover{opacity:.88}
.btn-full{width:100%;padding:.65rem;font-size:.95rem;margin-top:.3rem}
.btn-ghost{background:var(--bg3);color:var(--text);border:1px solid var(--border)}
.btn-ghost:hover{border-color:var(--accent)}
.alert{padding:.55rem .85rem;border-radius:7px;font-size:.82rem;margin-bottom:.9rem}
.alert-ok{background:rgba(34,197,94,.1);border:1px solid #22c55e;color:#86efac}
.alert-err{background:rgba(232,24,58,.1);border:1px solid var(--accent);color:#fca5a5}

/* ── ADMIN TABLE ── */
.tbl-wrap{overflow-x:auto;border-radius:10px;border:1px solid var(--border);margin-top:1rem}
table{width:100%;border-collapse:collapse;font-size:.78rem}
th{background:var(--bg3);padding:.55rem .8rem;text-align:left;color:var(--muted);
   font-weight:600;border-bottom:1px solid var(--border)}
td{padding:.5rem .8rem;border-bottom:1px solid var(--border);vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.015)}
.badge{display:inline-block;padding:.12rem .4rem;border-radius:4px;
       font-size:.62rem;font-weight:700}
.b-admin{background:rgba(245,197,24,.15);color:var(--gold)}
.b-ok{background:rgba(34,197,94,.12);color:#4ade80}
.b-no{background:rgba(232,24,58,.1);color:var(--accent)}
.inline-form{display:inline}
.row-actions{display:flex;gap:.3rem;flex-wrap:wrap}
.edit-panel{display:none;background:var(--bg3);border-radius:8px;
            padding:.8rem;margin-top:.5rem}

/* ── MOBILE SIDEBAR TOGGLE ── */
.mob-toggle{display:none;background:none;border:none;font-size:1.3rem;
             cursor:pointer;color:var(--text);padding:.1rem .2rem}
@media(max-width:768px){
  .mob-toggle{display:block}
  .sidebar{
    position:fixed;top:56px;left:0;bottom:0;z-index:150;
    width:240px;transform:translateX(-100%);transition:transform .22s;
  }
  .sidebar.open{transform:none}
  .mob-overlay{
    display:none;position:fixed;inset:0;top:56px;
    background:rgba(0,0,0,.55);z-index:149;
  }
  .mob-overlay.open{display:block}
}
"""

BASE_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="theme-color" content="#09090f">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>MedFlix</title>
<style>""" + CSS + """</style>
</head>
<body>
%%CONTENT%%
<script>
// Mobile sidebar
function toggleSidebar(){
  var s=document.getElementById('sidebar');
  var o=document.getElementById('mob-overlay');
  if(s){s.classList.toggle('open'); if(o) o.classList.toggle('open');}
}
// Close on overlay click
var ov=document.getElementById('mob-overlay');
if(ov) ov.addEventListener('click',function(){
  document.getElementById('sidebar').classList.remove('open');
  ov.classList.remove('open');
});
</script>
</body>
</html>"""

# ── TV (main browsing page) ──
TV_HTML = """
<div class="topbar">
  <button class="mob-toggle" onclick="toggleSidebar()">☰</button>
  <a class="logo" href="/tv">🎬MedFlix</a>
  <form class="topbar-search" action="/tv" method="get">
    {% if genre %}<input type="hidden" name="genre" value="{{ genre }}">{% endif %}
    <input name="q" value="{{ q }}" placeholder="Buscar..." autocomplete="off">
  </form>
  <div class="topbar-right">
    <span class="topbar-user">{{ u.username }}</span>
    {% if u.is_admin %}<a class="tbtn" href="/admin">⚙️ Admin</a>{% endif %}
    <a class="tbtn" id="reload-btn" href="#" onclick="reloadList(event)">🔄</a>
    <a class="tbtn" href="/logout">Sair</a>
  </div>
</div>
<div class="mob-overlay" id="mob-overlay"></div>

<div class="shell">
  <!-- SIDEBAR -->
  <nav class="sidebar" id="sidebar">
    <div class="sidebar-head">Gêneros</div>
    <a class="cat-item {{ 'active' if not genre else '' }}" href="/tv">
      🏠 Todos <span style="float:right;font-size:.65rem;color:var(--muted)">{{ channels|length if not genre else (genre_counts.values()|sum) }}</span>
    </a>
    {% for gid, gname in genres %}
    {% if genre_counts.get(gid, 0) > 0 %}
    <a class="cat-item {{ 'active' if genre==gid else '' }}" href="/tv?genre={{ gid }}">
      {{ gname }}
      <span style="float:right;font-size:.65rem;color:var(--muted)">{{ genre_counts.get(gid,0) }}</span>
    </a>
    {% endif %}
    {% endfor %}

    {% if subcats %}
    <div class="sidebar-head" style="margin-top:.8rem">Categorias</div>
    <a class="cat-item {{ 'active' if not subcat else '' }}"
       href="/tv?genre={{ genre }}">Todas</a>
    {% for cid, cname in subcats %}
    <a class="cat-item {{ 'active' if subcat==cid else '' }}"
       href="/tv?genre={{ genre }}&cat={{ cid }}">{{ cname }}</a>
    {% endfor %}
    {% endif %}
  </nav>

  <!-- MAIN -->
  <main class="main">
    {% if not has_list %}
    {% if loading %}
    <!-- Has credentials but channels not loaded yet — show spinner with auto-reload -->
    <div class="no-list" id="loading-box">
      <div class="spinner" style="margin:0 auto 1rem"></div>
      <h2>Carregando sua lista...</h2>
      <p style="color:var(--muted)">Estamos buscando seus canais e filmes.<br>
         Isso pode levar até 1 minuto na primeira vez.</p>
      <div style="margin-top:1rem;font-size:.78rem;color:var(--muted)" id="load-count">Aguarde...</div>
    </div>
    <script>
    // Poll until channels are loaded, then reload the page
    var attempts = 0;
    function checkChannels(){
      fetch('/api/channels/count').then(function(r){return r.json();}).then(function(d){
        document.getElementById('load-count').textContent =
          d.count > 0 ? d.count + ' itens carregados...' : 'Buscando canais...';
        if(d.count > 0){
          setTimeout(function(){ location.reload(); }, 800);
        } else {
          attempts++;
          setTimeout(checkChannels, attempts < 10 ? 2000 : 4000);
        }
      }).catch(function(){ setTimeout(checkChannels, 3000); });
    }
    checkChannels();
    </script>
    {% else %}
    <div class="no-list">
      <div class="icon">📡</div>
      <h2>Lista não configurada</h2>
      <p>Sua conta ainda não tem uma lista IPTV vinculada.<br>
         Entre em contato com o administrador.</p>
      {% if u.is_admin %}
      <a href="/admin" class="btn" style="display:inline-block;margin-top:1rem">⚙️ Configurar agora</a>
      {% endif %}
    </div>
    {% endif %}

    {% elif not channels %}
    <div class="no-list">
      <div class="icon">🔍</div>
      <h2>Sem resultados</h2>
      <p>Tente outra categoria ou busca.</p>
      <a href="/tv" style="color:var(--accent);font-size:.85rem">← Voltar ao início</a>
    </div>

    {% else %}
    {% if genre or q %}
    <div style="margin-bottom:.8rem;font-size:.82rem;color:var(--muted);display:flex;align-items:center;gap:.8rem">
      <span>{{ channels|length }} itens</span>
      {% if genre %}<a href="/tv" style="color:var(--accent)">✕ Limpar filtro</a>{% endif %}
    </div>
    {% endif %}

    <div class="grid" id="grid">
      {% for ch in channels %}
      {% set sid = ch.stream_url | b64id %}
      <a class="card" href="/watch?s={{ sid }}&genre={{ ch.genre }}">
        <img class="card-thumb {{ 'poster' if ch.genre in ['filmes','series','infantil','docs'] else '' }}"
             src="{{ ch.logo }}" alt="{{ ch.name }}" loading="lazy"
             onerror="this.src='';this.style.background='#1a1a24'">
        <div class="card-body">
          <div class="card-name">
            {% if ch.genre=='live' %}<span class="live-dot"></span>{% endif %}
            {{ ch.name }}
          </div>
          {% if ch.cat_name %}
          <div class="card-sub">{{ ch.cat_name }}</div>
          {% endif %}
        </div>
      </a>
      {% endfor %}
    </div>
    {% endif %}
  </main>
</div>

<script>
function reloadList(e) {
  e.preventDefault();
  var btn = document.getElementById('reload-btn');
  btn.textContent = '⏳';
  fetch('/api/reload').then(function(){ location.reload(); });
}
</script>
"""

# ── WATCH page ──
WATCH_HTML = """
<div class="topbar">
  <button class="mob-toggle" onclick="togglePlaylist()">☰</button>
  <a class="logo" href="/tv">🎬MedFlix</a>
  <span class="topbar-user" style="flex:1;padding-left:.5rem;font-size:.82rem;
        color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
    {{ ch.name }}
  </span>
  <div class="topbar-right">
    <a class="tbtn" href="/tv?genre={{ genre }}">← Voltar</a>
    <a class="tbtn" href="/logout">Sair</a>
  </div>
</div>

<div class="watch-shell">
  <div class="player-area">
    <div class="player-wrap">
      <video id="vid" autoplay playsinline controls
             style="max-height:calc(100vh - 56px - 52px);width:100%"></video>
    </div>
    <div class="player-bar">
      <div>
        <div class="player-title">{{ ch.name }}</div>
        <div class="player-cat">{{ ch.cat_name }}</div>
      </div>
      <div style="margin-left:auto;display:flex;gap:.4rem;flex-wrap:wrap">
        {% if ch.ctype=='live' %}
        <button class="tbtn" onclick="switchExt('ts')">TS</button>
        <button class="tbtn" onclick="switchExt('m3u8')">M3U8</button>
        {% endif %}
        <button class="tbtn" onclick="toggleFull()">⛶</button>
      </div>
    </div>
  </div>

  <div class="pl-list" id="pl-list">
    {% for item in playlist %}
    {% set sid = item.stream_url | b64id %}
    <div class="pl-item {{ 'active' if item.stream_url == ch.stream_url else '' }}"
         onclick="playItem('{{ item.stream_url | e }}','{{ item.name | e }}','{{ item.ext }}')">
      <img class="pl-thumb" src="{{ item.logo }}" alt="" loading="lazy"
           onerror="this.src='';this.style.background='#22222e'">
      <div class="pl-name">
        {% if item.ctype=='live' %}<span class="live-dot"></span>{% endif %}
        {{ item.name }}
      </div>
    </a>
    {% endfor %}
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.4.12/hls.min.js"></script>
<script>
var vid = document.getElementById('vid');
var currentUrl = {{ ch.stream_url | tojson }};
var currentExt = {{ ch.ext | tojson }};
var hls = null;

function loadStream(url, ext) {
  if(hls){ hls.destroy(); hls = null; }
  vid.pause();

  var isHls = url.includes('.m3u8') || ext === 'm3u8' ||
              url.includes('/live/') || ext === 'ts';

  if(isHls && typeof Hls !== 'undefined' && Hls.isSupported()){
    hls = new Hls({
      maxBufferLength: 30,
      maxMaxBufferLength: 60,
      enableWorker: false,
      lowLatencyMode: true,
    });
    hls.loadSource(url);
    hls.attachMedia(vid);
    hls.on(Hls.Events.MANIFEST_PARSED, function(){ vid.play().catch(function(){}); });
    hls.on(Hls.Events.ERROR, function(e, d){
      if(d.fatal && d.type === Hls.ErrorTypes.NETWORK_ERROR){
        setTimeout(function(){ hls.startLoad(); }, 2000);
      }
    });
  } else if(vid.canPlayType('application/vnd.apple.mpegurl') && isHls){
    vid.src = url;
    vid.load();
    vid.play().catch(function(){});
  } else {
    vid.src = url;
    vid.load();
    vid.play().catch(function(){});
  }
}

function playItem(url, name, ext){
  currentUrl = url; currentExt = ext;
  // Update active class
  document.querySelectorAll('.pl-item').forEach(function(el){el.classList.remove('active')});
  event.currentTarget.classList.add('active');
  // Update title
  document.querySelector('.player-title').textContent = name;
  loadStream(url, ext);
}

function switchExt(ext){
  var base = currentUrl.lastIndexOf('.') > currentUrl.lastIndexOf('/') ?
             currentUrl.substring(0, currentUrl.lastIndexOf('.')) : currentUrl;
  var newUrl = base + '.' + ext;
  currentExt = ext; currentUrl = newUrl;
  loadStream(newUrl, ext);
}

function toggleFull(){
  var el = document.querySelector('.watch-shell');
  if(!document.fullscreenElement) el.requestFullscreen().catch(function(){
    vid.requestFullscreen().catch(function(){});
  });
  else document.exitFullscreen();
}

function togglePlaylist(){
  var pl = document.getElementById('pl-list');
  pl.style.display = pl.style.display === 'none' ? '' : 'none';
}

// Initial load
loadStream(currentUrl, currentExt);

// Scroll active item into view
var active = document.querySelector('.pl-item.active');
if(active) active.scrollIntoView({block:'center'});
</script>
"""

LOGIN_HTML = """
<div class="card-form" style="margin-top:3rem">
  <div style="text-align:center;margin-bottom:1.5rem">
    <div style="font-size:2.8rem">🎬</div>
    <div style="font-size:1.5rem;font-weight:900;letter-spacing:-1px;
                background:linear-gradient(135deg,#e8183a,#ff5533);
                -webkit-background-clip:text;-webkit-text-fill-color:transparent">
      MedFlix
    </div>
    <div style="color:var(--muted);font-size:.8rem;margin-top:.2rem">Sua biblioteca digital</div>
  </div>
  {% if err %}<div class="alert alert-err">{{ err }}</div>{% endif %}
  <form method="post">
    <div class="fg"><label>USUÁRIO</label>
      <input name="username" placeholder="seu usuário" required autofocus autocomplete="username"></div>
    <div class="fg"><label>SENHA</label>
      <input name="password" type="password" placeholder="••••••••" required autocomplete="current-password"></div>
    <button class="btn btn-full" type="submit">Entrar</button>
  </form>
  <div style="text-align:center;margin-top:1.2rem;font-size:.73rem;color:var(--muted)">
    Não tem acesso? Fale com o administrador.
  </div>
</div>
"""

ADMIN_HTML = """
<div class="topbar">
  <a class="logo" href="/tv">🎬MedFlix</a>
  <span style="color:var(--muted);font-size:.82rem;margin-left:.5rem">/ Admin</span>
  <div class="topbar-right">
    <a class="tbtn" href="/tv">← Player</a>
    <a class="tbtn" href="/logout">Sair</a>
  </div>
</div>
<div style="padding-top:56px">
<div class="page-wrap">
  <h1 style="font-size:1.2rem;margin:1rem 0">⚙️ Painel Admin</h1>

  {% if msg %}<div class="alert alert-ok">{{ msg }}</div>{% endif %}
  {% if err %}<div class="alert alert-err">{{ err }}</div>{% endif %}

  <!-- CREATE USER -->
  <div style="background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:1.2rem;margin-bottom:1.5rem">
    <h2 style="font-size:.95rem;margin-bottom:1rem;color:var(--text)">➕ Criar usuário + vincular lista</h2>
    <form method="post">
      <input type="hidden" name="a" value="create">
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:.6rem;margin-bottom:.8rem">
        <div><label>Usuário *</label><input name="u" required placeholder="joao123"></div>
        <div><label>Senha *</label><input name="p" type="password" required placeholder="mín 6"></div>
        <div><label>Dias de acesso</label><input name="dias" type="number" value="30" min="1"></div>
      </div>

      <div style="background:rgba(232,24,58,.05);border:1px solid rgba(232,24,58,.2);
                  border-radius:8px;padding:.9rem;margin-bottom:.8rem">
        <p style="font-size:.75rem;color:var(--muted);margin-bottom:.7rem">
          🔑 <strong style="color:var(--text)">Vincular assinatura IPTV</strong>
          — Xtream Codes <em>ou</em> URL M3U direto
        </p>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:.6rem">
          <div>
            <label>Servidor Xtream (ex: http://fastp150.com)</label>
            <input name="srv" placeholder="http://fastp150.com">
          </div>
          <div>
            <label>Usuário Xtream</label>
            <input name="xu" placeholder="dcs17966">
          </div>
          <div>
            <label>Senha Xtream</label>
            <input name="xp" placeholder="29665wru">
          </div>
          <div>
            <label>— OU — URL M3U direta</label>
            <input name="purl" placeholder="http://servidor.com/lista.m3u">
          </div>
        </div>
      </div>
      <button class="btn" type="submit">Criar Usuário</button>
    </form>
  </div>

  <!-- USERS TABLE -->
  <div class="tbl-wrap">
    <table>
      <thead>
        <tr>
          <th>#</th><th>Usuário</th><th>Lista IPTV vinculada</th>
          <th>Validade</th><th>Link de acesso</th><th>Ações</th>
        </tr>
      </thead>
      <tbody>
        {% for u in users %}
        <tr>
          <td style="color:var(--muted)">{{ u.id }}</td>
          <td>
            <strong>{{ u.username }}</strong>
            {% if u.is_admin %}<span class="badge b-admin">ADMIN</span>{% endif %}
          </td>
          <td>
            {% if u.xtream_server %}
              <span class="badge b-ok">✓ Xtream</span><br>
              <span style="color:var(--muted);font-size:.7rem">
                {{ u.xtream_server[:35] }}<br>
                {{ u.xtream_user }} / {{ u.xtream_pass }}
              </span>
            {% elif u.playlist_url %}
              <span class="badge" style="background:rgba(59,130,246,.12);color:#60a5fa">M3U</span><br>
              <span style="color:var(--muted);font-size:.7rem">{{ u.playlist_url[:50] }}</span>
            {% else %}
              <span class="badge b-no">Sem lista</span>
            {% endif %}
          </td>
          <td style="color:var(--muted);font-size:.72rem">
            {{ u.dias }} dias<br>{{ (u.created_at or '')[:10] }}
          </td>
          <td style="font-size:.68rem;max-width:160px;word-break:break-all">
            {% if u.access_token %}
            <code style="color:var(--gold);font-size:.65rem">{{ u.access_token[:22] }}…</code><br>
            <a href="/login/token?t={{ u.access_token }}" target="_blank"
               style="color:var(--accent)">🔗 Link direto</a>
            {% else %}—{% endif %}
          </td>
          <td>
            <div class="row-actions">
              <button class="tbtn" onclick="toggleEdit({{ u.id }})">✏️</button>
              <form class="inline-form" method="post">
                <input type="hidden" name="a" value="token">
                <input type="hidden" name="uid" value="{{ u.id }}">
                <button class="tbtn" type="submit">🔑</button>
              </form>
              {% if not u.is_admin %}
              <form class="inline-form" method="post"
                    onsubmit="return confirm('Excluir {{ u.username }}?')">
                <input type="hidden" name="a" value="del">
                <input type="hidden" name="uid" value="{{ u.id }}">
                <button class="tbtn" type="submit" style="color:var(--accent)">🗑</button>
              </form>
              {% endif %}
            </div>

            <div class="edit-panel" id="ep-{{ u.id }}">
              <form method="post">
                <input type="hidden" name="a" value="update">
                <input type="hidden" name="uid" value="{{ u.id }}">
                <div style="display:flex;flex-direction:column;gap:.35rem">
                  <input name="srv"  value="{{ u.xtream_server or '' }}" placeholder="Servidor Xtream" style="font-size:.75rem">
                  <input name="xu"   value="{{ u.xtream_user or '' }}"   placeholder="Usuário Xtream"  style="font-size:.75rem">
                  <input name="xp"   value="{{ u.xtream_pass or '' }}"   placeholder="Senha Xtream"    style="font-size:.75rem">
                  <input name="purl" value="{{ u.playlist_url or '' }}"  placeholder="URL M3U"         style="font-size:.75rem">
                  <input name="dias" type="number" value="{{ u.dias }}"  placeholder="Dias"            style="font-size:.75rem">
                  <button class="btn" type="submit" style="font-size:.78rem;padding:.4rem">💾 Salvar</button>
                </div>
              </form>
            </div>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div style="background:var(--bg2);border:1px solid var(--border);border-radius:10px;
              padding:1rem;margin-top:1.5rem;font-size:.8rem;color:var(--muted);line-height:1.7">
    <strong style="color:var(--text)">📋 Como funciona o vínculo:</strong><br>
    1. Crie o usuário e preencha as credenciais Xtream do cliente<br>
    2. Clique em 🔑 para gerar o token de acesso<br>
    3. Copie o link <em>"🔗 Link direto"</em> e envie para o cliente<br>
    4. O cliente clica no link → faz login automático → vê os canais <strong>só dele</strong>
  </div>
</div>
</div>
<script>
function toggleEdit(id){
  var p=document.getElementById('ep-'+id);
  p.style.display=p.style.display==='none'||p.style.display===''?'block':'none';
}
</script>
"""

SETUP_HTML = """
<div class="card-form" style="margin-top:3rem">
  <div class="form-title">🚀 Setup Inicial</div>
  <div style="background:rgba(245,197,24,.05);border:1px solid rgba(245,197,24,.2);
              border-radius:7px;padding:.6rem .8rem;margin-bottom:1rem;font-size:.75rem;color:var(--muted)">
    Código padrão: <code style="color:var(--gold)">medflix2026</code>
    — ou defina a env var <code>SETUP_CODE</code>
  </div>
  {% if err %}<div class="alert alert-err">{{ err }}</div>{% endif %}
  <form method="post">
    <div class="fg"><label>Usuário admin</label><input name="u" required placeholder="admin"></div>
    <div class="fg"><label>Senha (mín 6)</label><input name="p" type="password" required></div>
    <div class="fg"><label>Código de ativação</label><input name="code" type="password" required placeholder="medflix2026"></div>
    <button class="btn btn-full" type="submit">Criar Admin</button>
  </form>
</div>
"""

RESET_HTML = """
<div class="card-form" style="margin-top:3rem">
  <div class="form-title">🔧 Recuperar Acesso Admin</div>
  {% if err %}<div class="alert alert-err">{{ err }}</div>{% endif %}
  <form method="post">
    <div class="fg"><label>Chave (ADMIN_RESET_KEY)</label><input name="key" type="password" required></div>
    <div class="fg"><label>Usuário</label><input name="u" required></div>
    <div class="fg"><label>Nova senha</label><input name="p" type="password" required></div>
    <button class="btn btn-full" type="submit">Recuperar</button>
  </form>
</div>
"""

SIMPLE_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{background:#09090f;color:#eee;font-family:system-ui;
  display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1rem}
.box{background:#111118;border:1px solid rgba(255,255,255,.08);border-radius:14px;
  padding:2rem;max-width:400px;width:100%;text-align:center}
h2{margin:.4rem 0;font-size:1.1rem}p{color:#666;font-size:.85rem;margin:.4rem 0}
a{display:inline-block;margin-top:1rem;background:#e8183a;color:#fff;
  padding:.55rem 1.3rem;border-radius:8px;text-decoration:none;font-weight:700}
</style></head><body><div class="box">
<div style="font-size:2.5rem">{{ icon }}</div>
<h2>{{ title }}</h2><p>{{ msg }}</p>
<a href="{{ link }}">{{ ltext }}</a>
</div></body></html>"""

# ─── Template filter ─────────────────────────────────────────────────────────
import base64 as _b64
@app.template_filter("b64id")
def b64id_filter(url):
    return _b64.urlsafe_b64encode(url.encode()).decode()[:32]

# ─── Run ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
