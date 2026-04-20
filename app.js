/* ═══════════════════════════════════════════════════════════════
   PINGUIM v12 — app.js
   • Favoritos / Recentes
   • Progresso de assistir (salva, exibe modal, barra nos cards)
   • Cover system (lazy, TMDB fallback, placeholder)
═══════════════════════════════════════════════════════════════ */

const PINGUIM_FAVORITES_KEY = "pinguim:favorites";
const PINGUIM_RECENTS_KEY   = "pinguim:recents";
const PINGUIM_PROGRESS_KEY  = "pinguim:progress";

function pinguimReadList(key) {
    try { return JSON.parse(localStorage.getItem(key) || "[]"); } catch { return []; }
}
function pinguimWriteList(key, val) { localStorage.setItem(key, JSON.stringify(val)); }
function pinguimReadProgress() {
    try { return JSON.parse(localStorage.getItem(PINGUIM_PROGRESS_KEY) || "{}"); } catch { return {}; }
}
function pinguimSaveProgress(id, currentTime, duration) {
    if (!id || isNaN(currentTime) || currentTime < 5) return;
    var prog = pinguimReadProgress();
    prog[String(id)] = { t: Math.floor(currentTime), d: Math.floor(duration || 0), ts: Date.now() };
    localStorage.setItem(PINGUIM_PROGRESS_KEY, JSON.stringify(prog));
}
function pinguimGetProgress(id) {
    return pinguimReadProgress()[String(id)] || null;
}
function pinguimClearProgress(id) {
    var prog = pinguimReadProgress();
    delete prog[String(id)];
    localStorage.setItem(PINGUIM_PROGRESS_KEY, JSON.stringify(prog));
}
function pinguimFmtTime(s) {
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h > 0) return h + "h " + m + "m";
    return m + "m " + sec + "s";
}
function pinguimUpsert(list, entry, max) {
    var f = list.filter(function(i){ return String(i.id) !== String(entry.id); });
    f.unshift(entry);
    return f.slice(0, max);
}

/* ── Hydrate grids (recentes / favoritos) ── */
async function pinguimHydrateGrid(gridId, storageKey, emptyText) {
    var grid = document.getElementById(gridId);
    if (!grid) return;
    var stored = pinguimReadList(storageKey);
    if (!stored.length) { grid.innerHTML = '<div class="empty-state slim">' + emptyText + '</div>'; return; }
    var ids = stored.map(function(i){ return i.id; }).join(",");
    try {
        var r = await fetch(window.PINGUIM_API_ITEMS + "?ids=" + encodeURIComponent(ids));
        var p = await r.json();
        if (!p.items || !p.items.length) { grid.innerHTML = '<div class="empty-state slim">' + emptyText + '</div>'; return; }
        grid.innerHTML = p.items.map(function(item){
            var prog = pinguimGetProgress(item.id);
            var pct  = (prog && prog.d > 0) ? Math.min(100, Math.round((prog.t / prog.d) * 100)) : 0;
            var bar  = pct > 0 ? '<div class="card-progress-bar"><div class="card-progress-fill" style="width:' + pct + '%"></div></div>' : "";
            var capa = item.resolved_capa || item.cached_series_poster_url || item.cached_poster_url || (item.capa && !item.capa.includes("goo.gl") ? item.capa : "");
            var img  = capa
                ? '<div class="poster-img"><img src="' + capa + '" alt="' + item.titulo + '" loading="lazy">' + bar + '</div>'
                : '<div class="poster-no-img"><span class="pni-icon">🎬</span><span class="pni-title">' + item.titulo + '</span>' + bar + '</div>';
            return '<a class="poster-card" href="/assistir/' + item.id + '">' + img +
                '<div class="poster-overlay"><div class="poster-play"><svg viewBox="0 0 12 12"><polygon points="2,1 11,6 2,11"/></svg></div>' +
                '<div class="poster-title">' + item.titulo + '</div>' +
                '<div class="poster-sub">' + (item.subgrupo || item.genero || "") + '</div></div></a>';
        }).join("");
    } catch(e) { grid.innerHTML = '<div class="empty-state slim">' + emptyText + '</div>'; }
}

/* ── Favoritar ── */
function pinguimSyncFavBtn() {
    var btn = document.querySelector(".favorite-toggle");
    if (!btn) return;
    var favs   = pinguimReadList(PINGUIM_FAVORITES_KEY);
    var active = favs.some(function(i){ return String(i.id) === String(btn.dataset.mediaId); });
    btn.classList.toggle("is-favorite", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
    btn.title = active ? "Remover dos favoritos" : "Favoritar";
}
function pinguimAttachFavBtn() {
    var btn = document.querySelector(".favorite-toggle");
    if (!btn) return;
    pinguimSyncFavBtn();
    btn.addEventListener("click", function() {
        var favs   = pinguimReadList(PINGUIM_FAVORITES_KEY);
        var id     = String(btn.dataset.mediaId);
        var exists = favs.some(function(i){ return String(i.id) === id; });
        var next   = exists
            ? favs.filter(function(i){ return String(i.id) !== id; })
            : pinguimUpsert(favs, { id: Number(id), titulo: btn.dataset.mediaTitle, capa: btn.dataset.mediaCover, genero: btn.dataset.mediaGenre, subgrupo: "" }, 24);
        pinguimWriteList(PINGUIM_FAVORITES_KEY, next);
        pinguimSyncFavBtn();
    });
}

/* ── Registrar recente ── */
function pinguimRegisterRecent() {
    var root = document.querySelector("[data-current-media-id]");
    if (!root) return;
    var recents = pinguimReadList(PINGUIM_RECENTS_KEY);
    var next = pinguimUpsert(recents, {
        id:        Number(root.dataset.currentMediaId),
        titulo:    root.dataset.currentMediaTitle     || "Mídia",
        capa:      root.dataset.currentMediaCover      || "",
        serie_capa: root.dataset.currentMediaSerieCover || root.dataset.currentMediaCover || "",
        genero:    root.dataset.currentMediaGenre      || "",
        subgrupo:  root.dataset.currentMediaSubgrupo   || "",
    }, 18);
    pinguimWriteList(PINGUIM_RECENTS_KEY, next);
}

function pinguimPersistRecentCache() {
    var root = document.querySelector("[data-current-media-id]");
    if (!root) return;
    fetch("/api/watch-cache", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            media_id: Number(root.dataset.currentMediaId || 0),
            titulo: root.dataset.currentMediaTitle || "",
            poster_url: root.dataset.currentMediaCover || "",
            series_poster_url: root.dataset.currentMediaSerieCover || root.dataset.currentMediaCover || "",
            genero: root.dataset.currentMediaGenre || "",
            subgrupo: root.dataset.currentMediaSubgrupo || ""
        })
    }).catch(function(){});
}

/* ════════════════════════════════════════════════════════════
   WATCH PROGRESS — salva posição + modal "Continuar"
════════════════════════════════════════════════════════════ */
function pinguimInitWatchProgress() {
    var root = document.querySelector("[data-current-media-id]");
    if (!root) return;
    var mediaId = Number(root.dataset.currentMediaId);
    if (!mediaId) return;
    var video = document.getElementById("player");
    if (!video) return; // modo iframe

    /* Salva a cada 5s */
    var _saveTimer = null;
    video.addEventListener("timeupdate", function() {
        if (_saveTimer) return;
        _saveTimer = setTimeout(function() {
            _saveTimer = null;
            if (video.currentTime > 5 && video.duration > 30) {
                pinguimSaveProgress(mediaId, video.currentTime, video.duration);
            }
        }, 5000);
    });

    /* Barra de progresso visual abaixo do player */
    var progressTrack = document.getElementById("player-progress-track");
    if (progressTrack) {
        video.addEventListener("timeupdate", function() {
            if (video.duration > 0) {
                progressTrack.style.width = ((video.currentTime / video.duration) * 100).toFixed(1) + "%";
            }
        });
    }

    /* Limpa ao terminar ou 95% */
    video.addEventListener("ended", function() { pinguimClearProgress(mediaId); });
    video.addEventListener("timeupdate", function() {
        if (video.duration > 0 && (video.currentTime / video.duration) > 0.95) {
            pinguimClearProgress(mediaId);
        }
    });

    /* Modal "Continuar de onde parou?" */
    var saved = pinguimGetProgress(mediaId);
    if (!saved || saved.t < 10) return;
    var pct = saved.d > 0 ? Math.round((saved.t / saved.d) * 100) : 0;

    var modal = document.createElement("div");
    modal.id = "resume-modal";
    modal.innerHTML =
        '<div class="resume-modal-box">' +
            '<div class="resume-modal-icon">▶</div>' +
            '<div class="resume-modal-info">' +
                '<strong>Continuar de onde parou?</strong>' +
                '<span>' + pinguimFmtTime(saved.t) + (pct > 0 ? ' assistido (' + pct + '%)' : '') + '</span>' +
            '</div>' +
            '<div class="resume-modal-actions">' +
                '<button id="btn-resume" class="resume-btn-primary">Continuar</button>' +
                '<button id="btn-restart" class="resume-btn-ghost">Do início</button>' +
            '</div>' +
            (pct > 0 ? '<div style="height:3px;background:linear-gradient(90deg,#e50914 ' + pct + '%,rgba(255,255,255,0.1) ' + pct + '%);border-radius:0 0 12px 12px;margin-top:8px"></div>' : '') +
        '</div>';
    document.body.appendChild(modal);

    function closeModal() {
        modal.classList.add("fade-out");
        setTimeout(function(){ if (modal.parentNode) modal.remove(); }, 300);
    }
    document.getElementById("btn-resume").addEventListener("click", function() {
        video.currentTime = saved.t;
        video.play().catch(function(){});
        closeModal();
    });
    document.getElementById("btn-restart").addEventListener("click", function() {
        pinguimClearProgress(mediaId);
        video.currentTime = 0;
        video.play().catch(function(){});
        closeModal();
    });
    setTimeout(closeModal, 12000);
}

/* ════════════════════════════════════════════════════════════
   SHELF "Continuar Assistindo" na home
════════════════════════════════════════════════════════════ */

var _seriesCoverCache = {}; // subgrupo → url | "" | "MISS"

function _isBadCover(url) {
    if (!url || url === "" || url === "undefined" || url === "null") return true;
    var dead = ["lgfp.one","goo.gl","t.co/","bit.ly","picsum","placehold","undefined","null"];
    return dead.some(function(d){ return url.indexOf(d) !== -1; });
}

async function _fetchSeriesCover(subgrupo) {
    if (!subgrupo) return "";
    if (_seriesCoverCache[subgrupo] !== undefined) return _seriesCoverCache[subgrupo];
    try {
        var r = await fetch("/api/series-cover?sg=" + encodeURIComponent(subgrupo));
        var d = await r.json();
        var url = (d && d.cover) ? d.cover : "";
        _seriesCoverCache[subgrupo] = url || "MISS";
        return url;
    } catch(e) {
        _seriesCoverCache[subgrupo] = "MISS";
        return "";
    }
}

async function pinguimBuildContinuarShelf() {
    var track = document.getElementById("shelf-continuar");
    var wrap  = document.getElementById("shelf-continuar-wrap");
    if (!track || !wrap) return;

    var progress = pinguimReadProgress();
    var recents  = pinguimReadList(PINGUIM_RECENTS_KEY);
    var items    = recents.filter(function(r) {
        var p = progress[String(r.id)];
        return p && p.t > 10;
    });

    if (!items.length) { wrap.style.display = "none"; return; }
    wrap.style.display = "";

    var cacheMap = {};
    try {
        var ids = items.map(function(i){ return i.id; }).join(",");
        var cr = await fetch("/api/watch-cache?ids=" + encodeURIComponent(ids));
        var cp = await cr.json();
        (cp.items || []).forEach(function(entry) { cacheMap[String(entry.media_id)] = entry; });
    } catch(e) {}

    // Pre-fetch canonical covers for all series items that need it
    var coverPromises = items.map(async function(item) {
        var cached = cacheMap[String(item.id)] || {};
        if (!_isBadCover(cached.series_poster_url || "")) {
            item._resolved_cover = cached.series_poster_url;
            return;
        }
        if (!_isBadCover(cached.poster_url || "")) {
            item._resolved_cover = cached.poster_url;
            return;
        }
        // Priority: 1) stored serie_capa (good), 2) API lookup by subgrupo, 3) episode capa
        if (item.subgrupo && (_isBadCover(item.serie_capa) || !item.serie_capa)) {
            var fetched = await _fetchSeriesCover(item.subgrupo);
            if (fetched && fetched !== "MISS") {
                item._resolved_cover = fetched;
                return;
            }
        }
        // Use stored serie_capa if valid, else fall back to episode capa
        item._resolved_cover = (!_isBadCover(item.serie_capa) ? item.serie_capa : null)
                             || (!_isBadCover(item.capa)      ? item.capa      : null)
                             || "";
    });
    await Promise.all(coverPromises);

    function getEpisodeLabel(title) {
        var txt = title || "";
        var match = txt.match(/[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})/) || txt.match(/(\d{1,2})x(\d{1,3})/i);
        if (match) {
            return "S" + String(match[1]).padStart(2, "0") + " E" + String(match[2]).padStart(2, "0");
        }
        return "";
    }

    track.innerHTML = items.map(function(item) {
        var p   = progress[String(item.id)];
        var pct = p.d > 0 ? Math.min(100, Math.round((p.t / p.d) * 100)) : 0;
        var capa = item._resolved_cover || "";
        var serieNome = item.subgrupo || item.titulo || "";
        var episodeLabel = getEpisodeLabel(item.titulo);
        var imgHtml = capa
            ? '<img src="' + capa + '" alt="' + (item.titulo||"") + '" loading="lazy" style="object-fit:cover;width:100%;height:100%" onerror="this.parentElement.innerHTML=\'<span style=&quot;font-size:2rem;display:flex;align-items:center;justify-content:center;height:100%&quot;>🎬</span>\'">'
            : '<span style="font-size:2rem;display:flex;align-items:center;justify-content:center;height:100%">🎬</span>';
        var bar = pct > 0
            ? '<div class="card-progress-bar"><div class="card-progress-fill" style="width:' + pct + '%"></div></div>'
            : '';
        var timeLabel = pinguimFmtTime(p.t) + (pct > 0 ? ' · ' + pct + '%' : '');
        var badge = episodeLabel ? '<div class="continue-episode-badge">' + episodeLabel + '</div>' : '';
        return '<a class="poster-card" href="/assistir/' + item.id + '">' +
            '<div class="poster-img continuar-card-img" style="background:#111;overflow:hidden">' + imgHtml + badge + bar + '</div>' +
            '<div class="poster-overlay">' +
                '<div class="poster-play"><svg viewBox="0 0 12 12"><polygon points="2,1 11,6 2,11"/></svg></div>' +
                '<div class="poster-title">' + serieNome.slice(0, 25) + '</div>' +
                '<div class="poster-sub">' + (episodeLabel ? episodeLabel + ' · ' : '') + '⏱ ' + timeLabel + '</div>' +
            '</div></a>';
    }).join("");
}

function clearAllProgress() {
    localStorage.removeItem(PINGUIM_PROGRESS_KEY);
    var wrap = document.getElementById("shelf-continuar-wrap");
    if (wrap) wrap.style.display = "none";
}

/* ════════════════════════════════════════════════════════════
   INICIALIZAÇÃO — script está no fim do body, DOM já pronto
════════════════════════════════════════════════════════════ */
pinguimHydrateGrid("recent-grid",   PINGUIM_RECENTS_KEY,   "Abra alguns itens para montar sua faixa de recentes.");
pinguimHydrateGrid("favorite-grid", PINGUIM_FAVORITES_KEY, "Marque favoritos no player para vê-los aqui.");
pinguimAttachFavBtn();
pinguimRegisterRecent();
pinguimPersistRecentCache();
pinguimInitWatchProgress();
pinguimBuildContinuarShelf(); // async — fetches series covers from API when needed


/* ═══════════════════════════════════════════════════════════════
   COVER SYSTEM — lazy load + TMDB fallback + placeholder
═══════════════════════════════════════════════════════════════ */

var DEAD_DOMAINS = ["lgfp.one","goo.gl","t.co","bit.ly","tinyurl.com","picsum.photos","placehold","undefined","null"];

function isDeadUrl(src) {
    if (!src || src.trim() === "" || src === "undefined" || src === "null") return true;
    return DEAD_DOMAINS.some(function(d){ return src.includes(d); });
}
function markLoaded(img) {
    img.classList.add("loaded");
    removeLoadingSkeleton(img);
    img.style.opacity = "1";
}
function showLoadingSkeleton(img) {
    var wrap = img.closest(".poster-img");
    if (wrap && !wrap.querySelector(".cover-skeleton")) {
        img.style.opacity = "0";
        var sk = document.createElement("div");
        sk.className = "cover-skeleton";
        sk.style.cssText = "position:absolute;inset:0;background:linear-gradient(90deg,#1a1a2e 25%,#2a2a4e 50%,#1a1a2e 75%);background-size:200% 100%;animation:skeleton-shimmer 1.4s infinite;border-radius:inherit;";
        wrap.style.position = "relative";
        wrap.appendChild(sk);
    }
}
function removeLoadingSkeleton(img) {
    var wrap = img.closest(".poster-img");
    if (wrap) {
        wrap.querySelectorAll(".cover-skeleton").forEach(function(el){ el.remove(); });
        img.style.opacity = "1";
    }
}
function showRichPlaceholder(img, titulo) {
    var wrap = img.closest(".poster-img");
    if (!wrap) return;
    var displayTitle = titulo || img.alt || "Sem título";
    var hue  = (displayTitle.charCodeAt(0) * 37 + (displayTitle.charCodeAt(1) || 0) * 13) % 360;
    var hue2 = (hue + 40) % 360;
    wrap.outerHTML = '<div class="poster-no-img" style="background:linear-gradient(160deg,hsl(' + hue + ',40%,12%) 0%,hsl(' + hue2 + ',35%,18%) 100%)">' +
        '<span class="pni-icon">🎬</span><span class="pni-title">' + displayTitle.slice(0,60) + '</span></div>';
}
function fixBrokenCover(img) {
    var card = img.closest(".poster-card") || img.closest(".poster-img");
    if (!card || card.dataset.coverFixed) return;
    card.dataset.coverFixed = "1";
    var titulo    = img.dataset.titulo || img.alt || "";
    var link      = img.dataset.link   || card.dataset.link || "";
    var mediaType = img.dataset.media  || card.dataset.media || "movie";
    if (!titulo) { showRichPlaceholder(img, ""); return; }
    showLoadingSkeleton(img);
    fetch("/api/cover?t=" + encodeURIComponent(titulo) + "&m=" + mediaType + "&link=" + encodeURIComponent(link))
        .then(function(r){ return r.json(); })
        .then(function(data) {
            if (data.url) {
                img.onload  = function(){ markLoaded(img); };
                img.onerror = function(){ removeLoadingSkeleton(img); showRichPlaceholder(img, titulo); };
                img.src = data.url;
            } else { removeLoadingSkeleton(img); showRichPlaceholder(img, titulo); }
        })
        .catch(function(){ removeLoadingSkeleton(img); showRichPlaceholder(img, titulo); });
}

var _coverObserver = null;
function getCoverObserver() {
    if (_coverObserver) return _coverObserver;
    _coverObserver = new IntersectionObserver(function(entries) {
        entries.forEach(function(entry) {
            if (!entry.isIntersecting) return;
            var img = entry.target;
            _coverObserver.unobserve(img);
            if (isDeadUrl(img.getAttribute("src"))) fixBrokenCover(img);
        });
    }, { rootMargin: "200px" });
    return _coverObserver;
}

(function() {
    if (!document.getElementById("skeleton-style")) {
        var s = document.createElement("style");
        s.id = "skeleton-style";
        s.textContent = "@keyframes skeleton-shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}";
        document.head.appendChild(s);
    }
    var obs = getCoverObserver();
    document.querySelectorAll(".poster-img img").forEach(function(img) {
        if (isDeadUrl(img.src) || isDeadUrl(img.getAttribute("src"))) {
            obs.observe(img);
        } else {
            img.addEventListener("error", function() {
                showLoadingSkeleton(this);
                fixBrokenCover(this);
            }, { once: true });
        }
    });
})();
