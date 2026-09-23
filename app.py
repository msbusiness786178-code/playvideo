"""
app.py — PW MPD (MPEG-DASH) Proxy + Player for Render.com

Routes:
  GET /play?url=<mpd_url>&token=<auth_token>   — watchable player page
  GET /api/mpd/manifest?url=<mpd>&token=<tok>  — proxied + rewritten MPD manifest
  GET /api/mpd/seg?u=<b64token>                — segment / init / sub-manifest relay
  GET /api/mpd/key?u=<b64token>                — licence / key relay
  GET /health                                  — healthcheck for Render

URL FORMAT SUPPORTED:
  /play?url=https://cdn.../master.mpd&parentId=xxx&childId=yyy&videoId=zzz&token=JWT

  Extra params like parentId, childId, videoId are forwarded to CDN as-is
  but do NOT break the MPD URL — we extract `url` and `token` separately
  and pass everything else through correctly.
"""

import base64
import logging
import os
import re
import time
from urllib.parse import (
    urljoin, urlparse, parse_qsl, urlencode,
    urlunparse, unquote, quote
)
import xml.etree.ElementTree as ET

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

# ─── App ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────
UPSTREAM_TIMEOUT = 25
UPSTREAM_RETRIES = 4
CHUNK_SIZE       = 64 * 1024  # 64 KB chunks

# Mimic Chrome on Android — PW CDN checks UA
UPSTREAM_HEADERS = {
    "User-Agent"         : "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept"             : "*/*",
    "Accept-Language"    : "en-IN,en;q=0.9",
    "Referer"            : "https://www.pw.live/",
    "Origin"             : "https://www.pw.live",
    "sec-ch-ua"          : '"Chromium";v="124","Google Chrome";v="124"',
    "sec-ch-ua-mobile"   : "?1",
    "sec-ch-ua-platform" : '"Android"',
    "Sec-Fetch-Dest"     : "empty",
    "Sec-Fetch-Mode"     : "cors",
    "Sec-Fetch-Site"     : "cross-site",
    "Connection"         : "keep-alive",
}

NO_STORE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}

ET.register_namespace("",      "urn:mpeg:dash:schema:mpd:2011")
ET.register_namespace("cenc",  "urn:mpeg:cenc:2013")
ET.register_namespace("mspr",  "urn:microsoft:playready")


# ─── CORS — every single response ───────────────────────────────────────────
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"]   = "*"
    resp.headers["Access-Control-Allow-Methods"]  = "GET, HEAD, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"]  = "*"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Content-Type"
    resp.headers["Access-Control-Max-Age"]        = "86400"
    resp.headers["Timing-Allow-Origin"]           = "*"
    return resp

@app.route("/", methods=["OPTIONS"])
@app.route("/<path:p>", methods=["OPTIONS"])
def preflight(p=""):
    return Response("", 204)


# ─── URL/param extraction helpers ───────────────────────────────────────────

def parse_play_params(raw_qs: str) -> dict:
    """
    Safely parse query string where `url` value may contain unencoded
    `&` characters (e.g. CDN URL pasted raw with &parentId=... appended).

    Strategy:
      1. Find `url=` marker
      2. Read everything after it as the raw value
      3. Find `token=` inside that raw value (if present) — split there
      4. Everything between url= and token= is the CDN url (may include
         &parentId=, &childId=, &videoId= — these are forwarded to CDN)
      5. token= value goes to the end (no other keys after token expected)

    Returns dict with keys: url, token, extra_params
    """
    result = {"url": "", "token": "", "extra_params": {}}

    # Find url= position
    url_marker = "url="
    url_idx = raw_qs.find(url_marker)
    if url_idx == -1:
        return result

    after_url = raw_qs[url_idx + len(url_marker):]

    # Find token= inside the remaining string
    # token= will appear as either ?token= or &token= AFTER the MPD path
    # The MPD url ends at .mpd (with possible query string), then &token=
    token_pattern = re.search(r'[&?]token=', after_url)

    if token_pattern:
        cdn_raw  = after_url[:token_pattern.start()]
        tok_raw  = after_url[token_pattern.start() + len(token_pattern.group()):]
        # token value ends at next & that looks like a new key=value pair
        # (but token itself is a JWT with no & inside, so just take all)
        tok_next = re.search(r'&[a-zA-Z_]+=', tok_raw)
        token    = tok_raw[:tok_next.start()] if tok_next else tok_raw
        result["token"] = _safe_decode(token)
    else:
        cdn_raw = after_url

    result["url"] = _safe_decode(cdn_raw)
    return result


def _safe_decode(s: str) -> str:
    try:
        return unquote(s)
    except Exception:
        return s


def b64e(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

def b64d(s: str) -> str:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s).decode()


def build_headers(token: str | None) -> dict:
    """Build upstream headers, injecting auth token if provided."""
    h = dict(UPSTREAM_HEADERS)
    if token:
        h["Authorization"] = f"Bearer {token}"
        h["token"]         = token      # some PW endpoints check this
        h["authToken"]     = token
    return h


def clean_mpd_url(raw_url: str) -> tuple[str, dict]:
    """
    Split a raw CDN url like:
      https://cdn.../master.mpd&parentId=xxx&childId=yyy&videoId=zzz

    into:
      base_url  = https://cdn.../master.mpd
      extra     = {parentId: xxx, childId: yyy, videoId: zzz}

    The & before parentId is NOT a valid query separator when there's no
    preceding ? — this is a PW-specific URL format quirk.
    """
    # Check if URL has proper query string
    parsed = urlparse(raw_url)

    if parsed.query:
        # Normal URL with ? — parse normally
        return raw_url, {}

    # No ? found — check if there are & params after the path
    # Pattern: https://cdn.../master.mpd&key=val&key2=val2
    amp_idx = raw_url.find("&")
    if amp_idx == -1:
        return raw_url, {}

    base = raw_url[:amp_idx]
    rest = raw_url[amp_idx + 1:]
    extra = dict(parse_qsl(rest, keep_blank_values=True))
    return base, extra


def fetch_upstream(url: str, headers: dict, range_hdr: str = None,
                   extra_params: dict = None) -> requests.Response:
    """Fetch with retry + exponential backoff. 4xx = no retry."""
    h = dict(headers)
    if range_hdr:
        h["Range"] = range_hdr

    # Attach extra params (parentId, childId, videoId) to the request
    params = extra_params or {}

    last_exc = None
    for attempt in range(UPSTREAM_RETRIES):
        try:
            r = requests.get(
                url, headers=h, params=params,
                timeout=UPSTREAM_TIMEOUT,
                allow_redirects=True, stream=True
            )
            log.info("Upstream %s → %d (attempt %d)", url[:70], r.status_code, attempt + 1)
            if r.ok or (400 <= r.status_code < 500):
                return r   # final — no retry on 4xx
            last_exc = requests.RequestException(f"HTTP {r.status_code}")
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("Upstream error attempt %d: %s", attempt + 1, exc)

        wait = 0.5 * (2 ** attempt)
        time.sleep(wait)

    raise last_exc


def is_mpd(url: str, ctype: str = "") -> bool:
    if "dash" in ctype.lower() or "mpd" in ctype.lower():
        return True
    return urlparse(url).path.lower().split("?")[0].endswith(".mpd")


def is_m3u8(url: str, ctype: str = "") -> bool:
    if "mpegurl" in ctype.lower() or "m3u8" in ctype.lower():
        return True
    return urlparse(url).path.lower().split("?")[0].endswith(".m3u8")


def make_seg_proxy(absolute_url: str) -> str:
    base = request.host_url.rstrip("/")
    return f"{base}/api/mpd/seg?u={b64e(absolute_url)}"


# ─── MPD XML rewriter ───────────────────────────────────────────────────────

def rewrite_mpd(xml_text: str, mpd_base_url: str, token: str | None) -> str:
    """
    Parse MPD XML and rewrite every media/segment URL through /api/mpd/seg
    so the real CDN URL + token never reaches the browser.
    Handles: BaseURL, SegmentTemplate, SegmentList>SegmentURL, Initialization.
    """
    try:
        xml_decl = ""
        if xml_text.lstrip().startswith("<?xml"):
            end = xml_text.index("?>") + 2
            xml_decl = xml_text[:end] + "\n"

        # Strip default namespace for simpler xpath
        xml_clean = re.sub(r'\sxmlns="[^"]+"', '', xml_text, count=1)
        root = ET.fromstring(xml_clean)
    except ET.ParseError as exc:
        log.error("MPD parse error: %s", exc)
        return xml_text

    def proxify(rel_or_abs: str, ctx_base: str) -> str:
        if not rel_or_abs or rel_or_abs.startswith("data:"):
            return rel_or_abs
        absolute = urljoin(ctx_base, rel_or_abs.strip())
        # Inject token into CDN URL if not already present
        if token:
            sep = "&" if "?" in absolute else "?"
            if "token=" not in absolute.lower():
                absolute += f"{sep}token={token}"
        return make_seg_proxy(absolute)

    def proxify_template(tmpl: str, ctx_base: str) -> str:
        """Handle SegmentTemplate strings like $Number$.ts or $RepresentationID$/seg$Number$.m4s"""
        if not tmpl:
            return tmpl
        absolute_tmpl = urljoin(ctx_base, tmpl)
        if token:
            sep = "&" if "?" in absolute_tmpl else "?"
            if "token=" not in absolute_tmpl.lower():
                absolute_tmpl += f"{sep}token={token}"
        # Encode the full template as base64 so /api/mpd/seg can decode +
        # substitute $Number$ etc. before fetching from CDN
        return make_seg_proxy(absolute_tmpl)

    current_base = mpd_base_url

    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

        if tag == "BaseURL" and elem.text and elem.text.strip():
            orig = elem.text.strip()
            absolute = urljoin(current_base, orig)
            elem.text = proxify(orig, current_base)
            current_base = absolute

        elif tag == "SegmentTemplate":
            for attr in ("media", "initialization", "index", "bitstreamSwitching"):
                val = elem.get(attr)
                if val:
                    if any(ph in val for ph in ("$Number$", "$Time$", "$Bandwidth$", "$RepresentationID$")):
                        elem.set(attr, proxify_template(val, current_base))
                    else:
                        elem.set(attr, proxify(val, current_base))

        elif tag == "SegmentURL":
            for attr in ("media", "index"):
                val = elem.get(attr)
                if val:
                    elem.set(attr, proxify(val, current_base))

        elif tag == "Initialization":
            val = elem.get("sourceURL")
            if val and not val.replace("-", "").replace(",", "").isdigit():
                elem.set("sourceURL", proxify(val, current_base))

    rewritten = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return xml_decl + rewritten


def proxy_m3u8(body: str, base_url: str, token: str | None) -> Response:
    """Minimal HLS rewriter for fallback HLS manifests."""
    out = []
    for line in body.splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            out.append(line)
            continue
        absolute = urljoin(base_url, t)
        if token and "token=" not in absolute:
            sep = "&" if "?" in absolute else "?"
            absolute += f"{sep}token={token}"
        out.append(make_seg_proxy(absolute))
    return Response(
        "\n".join(out) + "\n", 200,
        headers={**NO_STORE, "Content-Type": "application/vnd.apple.mpegurl"}
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/play")
def play():
    """
    GET /play?url=MPD_URL&token=JWT
    Also supports:
    GET /play?url=https://cdn.../master.mpd&parentId=xxx&childId=yyy&videoId=zzz&token=JWT
    """
    raw_qs = request.query_string.decode("utf-8", errors="replace")
    params = parse_play_params(raw_qs)

    if not params["url"]:
        return render_template("error.html",
            message="Missing ?url= parameter. Usage: /play?url=https://.../master.mpd&token=TOKEN"), 400

    return render_template("player.html")


@app.route("/api/mpd/manifest")
def mpd_manifest():
    """
    GET /api/mpd/manifest?url=MPD_URL&token=JWT&parentId=...
    Fetches MPD, rewrites all segment URLs, returns rewritten XML.
    """
    raw_qs = request.query_string.decode("utf-8", errors="replace")
    params = parse_play_params(raw_qs)

    url   = params["url"]
    token = params["token"]

    if not url:
        return jsonify({"error": "url param missing"}), 400

    # Split CDN base URL from extra params (parentId, childId, videoId)
    cdn_url, extra_params = clean_mpd_url(url)

    log.info("Fetching MPD: %s | token=%s | extra=%s",
             cdn_url[:80], "YES" if token else "NO", extra_params)

    headers = build_headers(token)

    try:
        r = fetch_upstream(cdn_url, headers, extra_params=extra_params)
    except requests.RequestException as exc:
        log.error("MPD upstream error: %s", exc)
        return jsonify({"error": f"Cannot reach CDN: {exc}"}), 502

    if not r.ok:
        body_preview = r.text[:300] if r.text else ""
        log.error("CDN returned %d for %s: %s", r.status_code, cdn_url[:80], body_preview)
        return jsonify({
            "error": f"CDN returned HTTP {r.status_code}",
            "hint": "Token may be expired or URL is wrong",
            "cdn_url": cdn_url[:120]
        }), r.status_code

    ctype = r.headers.get("content-type", "")
    body  = r.content.decode("utf-8", errors="replace")

    if is_m3u8(cdn_url, ctype):
        return proxy_m3u8(body, cdn_url, token)

    rewritten = rewrite_mpd(body, cdn_url, token)
    return Response(rewritten, 200, headers={
        **NO_STORE,
        "Content-Type": "application/dash+xml; charset=utf-8",
    })


@app.route("/api/mpd/seg")
def mpd_seg():
    """
    GET /api/mpd/seg?u=<base64url_encoded_cdn_url>
    Relay any segment, init segment, or sub-manifest.
    Streams in 64 KB chunks.
    """
    tok_b64 = request.args.get("u")
    if not tok_b64:
        return jsonify({"error": "Missing u param"}), 400

    try:
        cdn_url = b64d(tok_b64)
        parsed  = urlparse(cdn_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Bad scheme")
    except Exception:
        return jsonify({"error": "Invalid segment token"}), 400

    # Extract token embedded in the URL during rewrite
    qs_dict = dict(parse_qsl(parsed.query, keep_blank_values=True))
    token   = qs_dict.get("token") or qs_dict.get("authorization") or qs_dict.get("authToken")

    headers   = build_headers(token)
    range_hdr = request.headers.get("Range")

    log.debug("Seg relay: %s", cdn_url[:80])

    try:
        r = fetch_upstream(cdn_url, headers, range_hdr)
    except requests.RequestException as exc:
        log.error("Seg upstream error: %s", exc)
        return jsonify({"error": f"Upstream error: {exc}"}), 502

    if not r.ok:
        return jsonify({"error": f"CDN returned {r.status_code}"}), r.status_code

    ctype = r.headers.get("content-type", "")

    # Sub-manifest — rewrite before returning
    if is_mpd(cdn_url, ctype):
        body      = r.content.decode("utf-8", errors="replace")
        rewritten = rewrite_mpd(body, cdn_url, token)
        return Response(rewritten, 200, headers={
            **NO_STORE, "Content-Type": "application/dash+xml; charset=utf-8"
        })

    if is_m3u8(cdn_url, ctype):
        body = r.content.decode("utf-8", errors="replace")
        return proxy_m3u8(body, cdn_url, token)

    # Binary segment — stream in chunks
    resp_headers = {
        "Content-Type"  : ctype or "video/mp4",
        "Cache-Control" : "public, max-age=30",
        "Accept-Ranges" : "bytes",
    }
    if r.headers.get("Content-Length"):
        resp_headers["Content-Length"] = r.headers["Content-Length"]
    if r.headers.get("Content-Range"):
        resp_headers["Content-Range"]  = r.headers["Content-Range"]

    status = 206 if r.status_code == 206 else 200

    def generate():
        try:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    yield chunk
        except Exception as exc:
            log.error("Stream chunk error: %s", exc)

    return Response(
        stream_with_context(generate()),
        status=status, headers=resp_headers
    )


@app.route("/api/mpd/key")
def mpd_key():
    """GET /api/mpd/key?u=<b64_key_url> — Key/licence relay."""
    tok_b64 = request.args.get("u")
    if not tok_b64:
        return jsonify({"error": "Missing u param"}), 400
    try:
        key_url = b64d(tok_b64)
        if urlparse(key_url).scheme not in ("http", "https"):
            raise ValueError
    except Exception:
        return jsonify({"error": "Invalid key token"}), 400

    qs_dict = dict(parse_qsl(urlparse(key_url).query, keep_blank_values=True))
    token   = qs_dict.get("token")
    headers = build_headers(token)

    try:
        r = fetch_upstream(key_url, headers)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502

    return Response(r.content, r.status_code, headers={
        "Content-Type"  : r.headers.get("Content-Type", "application/octet-stream"),
        "Cache-Control" : "no-store",
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "playvideo-mpd-proxy"})


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
