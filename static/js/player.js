(function () {
    "use strict";

    /* ── DOM ── */
    var video        = document.getElementById("video");
    var shell        = document.getElementById("shell");
    var statusBox    = document.getElementById("status");
    var statusText   = document.getElementById("statusText");
    var errDetail    = document.getElementById("errDetail");
    var qualBadge    = document.getElementById("qualBadge");
    var elapsedBadge = document.getElementById("elapsedBadge");
    var qualBtn      = document.getElementById("qualBtn");
    var qualPanel    = document.getElementById("qualPanel");
    var qualList     = document.getElementById("qualList");
    var goLiveBtn    = document.getElementById("goLiveBtn");
    var fsBtn        = document.getElementById("fsBtn");

    /* ────────────────────────────────────────────────────────────────────
       Read ?url= and ?token= from the page URL.

       Problem: The user may paste a URL like:
         /play?url=https://cdn.../master.mpd&parentId=xxx&childId=yyy&token=JWT

       Standard URLSearchParams.get("url") would only return
       "https://cdn.../master.mpd" (stops at first &) — dropping parentId etc.

       We need the FULL url value INCLUDING &parentId=... up until &token=.
       Then we pass everything verbatim to /api/mpd/manifest which handles
       the correct splitting server-side.

       Strategy:
         1. Read raw window.location.search
         2. Extract everything after `url=` as rawUrl
         3. Extract token from `token=` (last param)
         4. Build manifest URL = /api/mpd/manifest?url=<rawUrl>&token=<token>
            where rawUrl is re-encoded properly
    ─────────────────────────────────────────────────────────────────────── */

    var rawSearch = window.location.search; // e.g. ?url=https://...mpd&parentId=xxx&token=JWT

    function extractRawParam(search, name) {
        var marker = (search.indexOf("?") === 0 ? "" : "") + name + "=";
        // Search from after the ? 
        var haystack = search.indexOf("?") === 0 ? search.slice(1) : search;
        var idx = haystack.indexOf(marker);
        if (idx === -1) return "";
        return haystack.slice(idx + marker.length);
    }

    function parsePlayerParams(search) {
        // Remove leading ?
        var qs = search.indexOf("?") === 0 ? search.slice(1) : search;

        var urlMarker   = "url=";
        var tokenMarker = "token=";

        var urlIdx   = qs.indexOf(urlMarker);
        var tokenIdx = qs.lastIndexOf(tokenMarker); // token is usually last

        if (urlIdx === -1) return { url: "", token: "" };

        var afterUrl = qs.slice(urlIdx + urlMarker.length);

        // Find &token= inside afterUrl
        var tokenInUrl = afterUrl.match(/[&?]token=/);
        var rawUrl, rawToken;

        if (tokenInUrl) {
            rawUrl   = afterUrl.slice(0, tokenInUrl.index);
            rawToken = afterUrl.slice(tokenInUrl.index + tokenInUrl[0].length);
            // Token ends at next & followed by key=
            var nextKey = rawToken.match(/&[a-zA-Z_]+=./);
            if (nextKey) rawToken = rawToken.slice(0, nextKey.index);
        } else if (tokenIdx !== -1 && tokenIdx > urlIdx) {
            // token= is a separate top-level param
            rawUrl   = qs.slice(urlIdx + urlMarker.length, tokenIdx > 0 ? qs.lastIndexOf("&token=") : qs.length);
            rawToken = qs.slice(tokenIdx + tokenMarker.length);
        } else {
            rawUrl   = afterUrl;
            rawToken = "";
        }

        var safeUrl = rawUrl;
        var safeToken = rawToken;
        try { safeUrl   = decodeURIComponent(rawUrl);   } catch(e) {}
        try { safeToken = decodeURIComponent(rawToken); } catch(e) {}

        return { url: safeUrl, token: safeToken };
    }

    var params = parsePlayerParams(rawSearch);
    var rawUrl = params.url;
    var token  = params.token;

    /* ── Status helpers ── */
    function showLoading(msg) {
        statusBox.className       = "status";
        statusBox.style.display   = "flex";
        statusText.textContent    = msg || "Loading...";
        errDetail.textContent     = "";
    }
    function showError(msg, detail) {
        statusBox.className       = "status error";
        statusBox.style.display   = "flex";
        statusText.textContent    = "✕ " + msg;
        errDetail.textContent     = detail || "";
    }
    function hideStatus() {
        statusBox.style.display = "none";
    }

    function fmt(s) {
        s = Math.floor(s || 0);
        var h   = Math.floor(s / 3600);
        var m   = Math.floor((s % 3600) / 60);
        var sec = s % 60;
        return (h ? h + ":" : "") +
               String(m).padStart(2, "0") + ":" +
               String(sec).padStart(2, "0");
    }

    if (!rawUrl) {
        showError("URL missing!", "Usage: /play?url=https://.../master.mpd&token=YOUR_TOKEN");
        return;
    }

    /* ── Build manifest proxy URL ──
       We send the raw url + token to the server. Server does the proper
       splitting of CDN url vs extra params (parentId, childId, videoId). ── */
    var manifUrl = window.location.origin +
                   "/api/mpd/manifest?url=" + encodeURIComponent(rawUrl) +
                   (token ? "&token=" + encodeURIComponent(token) : "");

    showLoading("Initialising player...");

    /* ═══════════════════════════════════════════════════════════════════
       Shaka Player
    ═══════════════════════════════════════════════════════════════════ */
    var player  = null;
    var tracks  = [];
    var started = false;
    var elapsedInterval = null;

    shaka.polyfill.installAll();

    if (!shaka.Player.isBrowserSupported()) {
        showError("Browser not supported", "Please use Chrome, Edge, or Firefox.");
        return;
    }

    player = new shaka.Player(video);

    /* Network filter — inject auth on any direct request Shaka makes */
    player.getNetworkingEngine().registerRequestFilter(function (type, req) {
        if (token) {
            req.headers["Authorization"] = "Bearer " + token;
        }
        req.allowCrossSiteCredentials = false;
    });

    /* Shaka config */
    player.configure({
        streaming: {
            lowLatencyMode  : false,
            rebufferingGoal : 3,
            bufferingGoal   : 30,
            bufferBehind    : 60,
            retryParameters : {
                maxAttempts : 5,
                baseDelay   : 1000,
                backoffFactor: 1.5,
                fuzzFactor  : 0.5,
                timeout     : 30000,
            },
        },
        abr: {
            enabled                 : true,
            defaultBandwidthEstimate: 2000000,
            switchInterval          : 8,
            bandwidthUpgradeTarget  : 0.85,
            bandwidthDowngradeTarget: 0.95,
        },
        manifest: {
            dash: {
                ignoreMinBufferTime : true,
                autoCorrectDrift    : true,
            },
            retryParameters: {
                maxAttempts : 5,
                baseDelay   : 1000,
                backoffFactor: 1.5,
                fuzzFactor  : 0.5,
                timeout     : 30000,
            },
        },
    });

    /* ── Error handler ── */
    player.addEventListener("error", function (evt) {
        var err  = evt.detail;
        var code = err && err.code ? err.code : "?";
        console.error("Shaka error", err);

        var msg = "Playback error (code " + code + ")";
        var detail = "";

        if (code === 1001 || code === 1002) {
            msg    = "Network error — CDN unreachable";
            detail = "Link may have expired, or server is waking up (Render free tier cold start). Try refreshing in 30s.";
        } else if (code === 1003) {
            msg    = "Request timed out";
            detail = "CDN is too slow or URL is invalid.";
        } else if (code === 2000 || code === 2006) {
            msg    = "Cannot parse manifest";
            detail = "MPD URL may be wrong or CDN returned an error page.";
        } else if (code === 4001) {
            msg    = "DRM / Key error";
            detail = "Token expired or content requires Widevine (not supported in free proxy).";
        }

        showError(msg, detail);
    });

    player.addEventListener("adaptation",    updateQualBadge);
    player.addEventListener("trackschanged", function () {
        buildQualityMenu();
        updateQualBadge();
    });

    /* ── Load ── */
    showLoading("Fetching manifest...");

    player.load(manifUrl)
        .then(function () {
            started = true;
            hideStatus();
            qualBadge.classList.add("show");
            buildQualityMenu();
            updateQualBadge();
            startElapsedTimer();
            attemptAutoplay();
        })
        .catch(function (err) {
            console.error("Shaka load failed", err);
            var code = err && err.code ? err.code : "?";
            showError(
                "Failed to load stream",
                "Code: " + code + ". Check URL/token. If Render just woke up, try refreshing."
            );
        });

    function attemptAutoplay() {
        video.play().catch(function () {
            video.muted = true;
            video.play().catch(function () {});
        });
    }

    /* ── Video events ── */
    video.addEventListener("playing", function () { if (started) hideStatus(); });
    video.addEventListener("waiting", function () { if (started) showLoading("Buffering..."); });
    video.addEventListener("stalled", function () { if (started) showLoading("Stalled — reconnecting..."); });
    video.addEventListener("error",   function () { showError("Video element error — try refreshing."); });
    video.addEventListener("click",   function () {
        video.muted = false;
        if (video.paused) video.play().catch(function(){});
        else video.pause();
    });

    /* ── Quality badge ── */
    function updateQualBadge() {
        try {
            var t = player.getVariantTracks().find(function(t){ return t.active; });
            if (t) {
                var label = t.height ? t.height + "p" : "AUTO";
                if (t.bandwidth) label += " · " + Math.round(t.bandwidth / 1000) + "k";
                qualBadge.textContent = label;
            }
        } catch(e) {}
    }

    /* ── Quality menu ── */
    function buildQualityMenu() {
        try {
            tracks = player.getVariantTracks();
            tracks.sort(function(a,b){ return (b.height||0) - (a.height||0); });
            qualList.innerHTML = "";

            var autoEl = document.createElement("div");
            autoEl.className = "q-item" + (player.getConfiguration().abr.enabled ? " active" : "");
            autoEl.innerHTML = '<span class="q-dot"></span>Auto (ABR)';
            autoEl.onclick = function() {
                player.configure({ abr: { enabled: true } });
                closeQualPanel();
                updateQualBadge();
                renderActiveState();
            };
            qualList.appendChild(autoEl);

            tracks.forEach(function(t) {
                var label = (t.height ? t.height + "p" : "?");
                if (t.bandwidth) label += " · " + Math.round(t.bandwidth / 1000) + "k";
                var el = document.createElement("div");
                el.className = "q-item" + (t.active && !player.getConfiguration().abr.enabled ? " active" : "");
                el.innerHTML = '<span class="q-dot"></span>' + label;
                el.onclick = (function(track) {
                    return function() {
                        player.configure({ abr: { enabled: false } });
                        player.selectVariantTrack(track, true);
                        closeQualPanel();
                        updateQualBadge();
                        renderActiveState();
                    };
                })(t);
                qualList.appendChild(el);
            });
        } catch(e) { console.warn("Quality menu error:", e); }
    }

    function renderActiveState() {
        var abrOn = player.getConfiguration().abr.enabled;
        var items = qualList.querySelectorAll(".q-item");
        if (!items.length) return;
        items[0].classList.toggle("active", abrOn);
        try {
            var active = player.getVariantTracks().find(function(t){ return t.active; });
            for (var i = 1; i < items.length; i++) {
                items[i].classList.toggle("active", !abrOn && tracks[i-1] && active && tracks[i-1].id === active.id);
            }
        } catch(e) {}
    }

    function closeQualPanel() { qualPanel.classList.remove("open"); }

    qualBtn.addEventListener("click", function(e) {
        e.stopPropagation();
        qualPanel.classList.toggle("open");
    });
    document.addEventListener("click", function() { closeQualPanel(); });
    qualPanel.addEventListener("click", function(e) { e.stopPropagation(); });

    /* ── Elapsed + Go Live ── */
    function startElapsedTimer() {
        if (elapsedInterval) return;
        elapsedBadge.classList.add("show");
        elapsedInterval = setInterval(function () {
            try {
                var st = player.getStats();
                if (st && typeof st.playTime === "number") {
                    elapsedBadge.textContent = fmt(st.playTime);
                } else if (!video.paused) {
                    elapsedBadge.textContent = fmt(video.currentTime);
                }
            } catch(e) {}

            try {
                var seekRange = player.seekRange();
                if (seekRange && seekRange.end > 0) {
                    var behind = seekRange.end - video.currentTime;
                    goLiveBtn.classList.toggle("show", behind > 10);
                }
            } catch(e) {}
        }, 800);
    }

    goLiveBtn.addEventListener("click", function () {
        try {
            var seekRange = player.seekRange();
            if (seekRange && seekRange.end) {
                video.currentTime = seekRange.end - 2;
                video.play().catch(function(){});
            }
        } catch(e) {}
    });

    /* ── Fullscreen ── */
    fsBtn.addEventListener("click", function () {
        var req = shell.requestFullscreen || shell.webkitRequestFullscreen || shell.mozRequestFullScreen;
        if (req) {
            var p = req.call(shell);
            var lock = function() {
                if (screen.orientation && screen.orientation.lock) {
                    screen.orientation.lock("landscape").catch(function(){});
                }
            };
            if (p && p.then) p.then(lock).catch(function(){});
            else lock();
        } else if (video.webkitEnterFullscreen) {
            video.webkitEnterFullscreen();
        }
    });

    document.addEventListener("fullscreenchange", function () {
        if (!document.fullscreenElement) {
            try { if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock(); } catch(e) {}
            fsBtn.textContent = "⛶";
        } else {
            fsBtn.textContent = "✕";
        }
    });

    /* ── Keyboard shortcuts ── */
    document.addEventListener("keydown", function(e) {
        switch(e.key) {
            case " ": case "k":
                e.preventDefault();
                if (video.paused) video.play().catch(function(){}); else video.pause();
                break;
            case "f": case "F": fsBtn.click(); break;
            case "ArrowRight": video.currentTime = Math.min(video.currentTime + 10, video.duration || Infinity); break;
            case "ArrowLeft":  video.currentTime = Math.max(video.currentTime - 10, 0); break;
            case "ArrowUp":    video.volume = Math.min(video.volume + 0.1, 1); break;
            case "ArrowDown":  video.volume = Math.max(video.volume - 0.1, 0); break;
            case "m": case "M": video.muted = !video.muted; break;
        }
    });

})();
