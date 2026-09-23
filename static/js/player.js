(function () {
    "use strict";

    /* ── DOM ── */
    var video       = document.getElementById("video");
    var shell       = document.getElementById("shell");
    var statusBox   = document.getElementById("status");
    var statusText  = document.getElementById("statusText");
    var errDetail   = document.getElementById("errDetail");
    var qualBadge   = document.getElementById("qualBadge");
    var elapsedBadge= document.getElementById("elapsedBadge");
    var qualBtn     = document.getElementById("qualBtn");
    var qualPanel   = document.getElementById("qualPanel");
    var qualList    = document.getElementById("qualList");
    var goLiveBtn   = document.getElementById("goLiveBtn");
    var fsBtn       = document.getElementById("fsBtn");

    /* ── Read ?url= and ?token= from the page URL ── */
    var qs      = window.location.search;
    var rawUrl  = getParam(qs, "url");
    var token   = getParam(qs, "token");

    function getParam(search, name) {
        var marker = name + "=";
        var idx    = search.indexOf(marker);
        if (idx === -1) return "";
        var raw = search.slice(idx + marker.length);
        try { return decodeURIComponent(raw); } catch(e) { return raw; }
    }

    /* ── Status helpers ── */
    function showLoading(msg) {
        statusBox.className  = "status";
        statusBox.style.display = "flex";
        statusText.textContent  = msg || "Loading...";
        errDetail.textContent   = "";
    }
    function showError(msg, detail) {
        statusBox.className  = "status error";
        statusBox.style.display = "flex";
        statusText.textContent  = "✕ " + msg;
        errDetail.textContent   = detail || "";
    }
    function hideStatus() {
        statusBox.style.display = "none";
    }

    /* ── Time formatter ── */
    function fmt(s) {
        s = Math.floor(s || 0);
        var h = Math.floor(s / 3600);
        var m = Math.floor((s % 3600) / 60);
        var sec = s % 60;
        return (h ? h + ":" : "") +
               String(m).padStart(2, "0") + ":" +
               String(sec).padStart(2, "0");
    }

    /* ── Guard: url required ── */
    if (!rawUrl) {
        showError(
            "URL missing!",
            "Usage: /play?url=https://.../master.mpd&token=YOUR_TOKEN"
        );
        return;
    }

    /* ── Build the manifest URL (routed through our proxy) ── */
    var origin    = window.location.origin;
    var manifUrl  = origin + "/api/mpd/manifest?url=" +
                    encodeURIComponent(rawUrl) +
                    (token ? "&token=" + encodeURIComponent(token) : "");

    showLoading("Initialising player...");

    /* ═════════════════════════════════════════════════════════════════════
       Shaka Player setup
       ═════════════════════════════════════════════════════════════════════ */
    var player = null;
    var tracks  = [];
    var started = false;
    var elapsedInterval = null;

    /* Install built-in polyfills (EME, fetch, etc.) */
    shaka.polyfill.installAll();

    if (!shaka.Player.isBrowserSupported()) {
        showError(
            "Browser not supported",
            "Please use Chrome, Edge, or Firefox."
        );
        return;
    }

    player = new shaka.Player(video);

    /* ── Network request filter — inject auth token on every CDN request
          that goes through our proxy (the proxy already handles this, but
          this also covers any requests Shaka issues directly, e.g. EME
          license requests if ever needed) ── */
    player.getNetworkingEngine().registerRequestFilter(function (type, req) {
        if (token) {
            req.headers["Authorization"] = "Bearer " + token;
        }
        // Ensure CORS credentials not sent (our proxy is open)
        req.allowCrossSiteCredentials = false;
    });

    /* ── Shaka configuration ── */
    player.configure({
        streaming: {
            lowLatencyMode        : false,
            rebufferingGoal       : 3,
            bufferingGoal         : 30,
            bufferBehind          : 60,
            retryParameters: {
                maxAttempts         : 5,
                baseDelay           : 500,
                backoffFactor       : 1.5,
                fuzzFactor          : 0.5,
                timeout             : 20000,
            },
            // Prefer higher quality automatically
            useNativeHlsOnSafari  : false,
        },
        abr: {
            enabled               : true,
            defaultBandwidthEstimate: 2000000, // start at 2 Mbps
            switchInterval        : 8,         // seconds between ABR switches
            bandwidthUpgradeTarget: 0.85,
            bandwidthDowngradeTarget: 0.95,
            restrictions: {
                minHeight           : 360,
            },
        },
        manifest: {
            dash: {
                ignoreMinBufferTime : true,
                autoCorrectDrift    : true,
            },
            retryParameters: {
                maxAttempts         : 5,
                baseDelay           : 500,
                backoffFactor       : 1.5,
                fuzzFactor          : 0.5,
                timeout             : 20000,
            },
        },
    });

    /* ── Error handler ── */
    player.addEventListener("error", function (evt) {
        var err  = evt.detail;
        var code = err && err.code ? err.code : "?";
        var cat  = err && err.category ? err.category : "?";
        console.error("Shaka error", err);

        var msg    = "Playback error (code " + code + ")";
        var detail = "";

        if (code === 1001 || code === 1002) {
            msg    = "Network error — stream unreachable";
            detail = "CDN link may have expired or your connection dropped.";
        } else if (code === 1003) {
            msg    = "Timeout fetching stream";
            detail = "Server is too slow or the URL is invalid.";
        } else if (code === 2000 || code === 2006) {
            msg    = "Manifest error — cannot parse MPD";
            detail = "The MPD URL may be wrong or the CDN returned an error.";
        } else if (code === 4001) {
            msg    = "DRM / key error";
            detail = "Token may be expired or DRM licence unavailable.";
        } else if (code === 3000 || code === 3001) {
            msg    = "Decryption failed";
            detail = "Token is invalid or the content is Widevine-protected.";
        }

        showError(msg, detail);
    });

    /* ── Adaptation (quality change) ── */
    player.addEventListener("adaptation", updateQualBadge);
    player.addEventListener("trackschanged", function () {
        buildQualityMenu();
        updateQualBadge();
    });

    /* ── Load the manifest ── */
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
            console.error("Load failed", err);
            var code = err && err.code ? err.code : "?";
            showError(
                "Failed to load stream",
                "Error code: " + code + ". Check the URL/token and try again."
            );
        });

    /* ── Autoplay ── */
    function attemptAutoplay() {
        video.play().catch(function () {
            video.muted = true;
            video.play().catch(function () {});
        });
    }

    /* ── Video events ── */
    video.addEventListener("playing", function () {
        if (started) hideStatus();
    });
    video.addEventListener("waiting", function () {
        if (started) showLoading("Buffering...");
    });
    video.addEventListener("stalled", function () {
        if (started) showLoading("Stalled — reconnecting...");
    });
    video.addEventListener("error", function () {
        showError("Video element error — try refreshing.");
    });
    video.addEventListener("click", function () {
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

            // Auto option
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
                items[i].classList.toggle("active", !abrOn && tracks[i-1] && tracks[i-1].id === (active && active.id));
            }
        } catch(e) {}
    }

    function closeQualPanel() { qualPanel.classList.remove("open"); }

    qualBtn.addEventListener("click", function(e) {
        e.stopPropagation();
        qualPanel.classList.toggle("open");
    });
    document.addEventListener("click", function() { closeQualPanel(); });
    qualPanel.addEventListener("click", function(e){ e.stopPropagation(); });

    /* ── Elapsed timer + "Go Live" ── */
    function startElapsedTimer() {
        if (elapsedInterval) return;
        elapsedBadge.classList.add("show");
        elapsedInterval = setInterval(function () {
            try {
                var prs = player.getStats();
                if (prs && typeof prs.playTime === "number") {
                    elapsedBadge.textContent = fmt(prs.playTime);
                } else if (!video.paused) {
                    elapsedBadge.textContent = fmt(video.currentTime);
                }
            } catch(e) {}

            // "Go Live" button: show if user is >10s behind live edge
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

    /* ── Fullscreen + landscape lock ── */
    fsBtn.addEventListener("click", function () {
        var req = shell.requestFullscreen  ||
                  shell.webkitRequestFullscreen ||
                  shell.mozRequestFullScreen;
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
            try {
                if (screen.orientation && screen.orientation.unlock)
                    screen.orientation.unlock();
            } catch(e) {}
            fsBtn.textContent = "⛶";
        } else {
            fsBtn.textContent = "✕";
        }
    });

    /* ── Keyboard shortcuts ── */
    document.addEventListener("keydown", function(e) {
        switch(e.key) {
            case " ":
            case "k":
                e.preventDefault();
                if (video.paused) video.play().catch(function(){});
                else video.pause();
                break;
            case "f":
            case "F":
                fsBtn.click();
                break;
            case "ArrowRight":
                video.currentTime = Math.min(video.currentTime + 10, video.duration || Infinity);
                break;
            case "ArrowLeft":
                video.currentTime = Math.max(video.currentTime - 10, 0);
                break;
            case "ArrowUp":
                video.volume = Math.min(video.volume + 0.1, 1);
                break;
            case "ArrowDown":
                video.volume = Math.max(video.volume - 0.1, 0);
                break;
            case "m":
            case "M":
                video.muted = !video.muted;
                break;
        }
    });

})();
