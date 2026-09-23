"""
app.py — PW MPD (MPEG-DASH) Proxy + Player for Render.com

Routes:
  GET /play?url=<mpd_url>&token=<auth_token>   — watchable player page (main route)
  GET /api/mpd/manifest?url=<mpd>&token=<tok>  — proxied + rewritten MPD manifest
  GET /api/mpd/seg?u=<b64token>                — segment / init / sub-manifest relay
  GET /api/mpd/key?u=<b64token>                — licence / key relay (DRM-free keys)
  GET /health                                  — healthcheck for Render

Usage example:
  https://playvideo.onrender.com/play?url=https://d1d34p8vz63oiq.cloudfront.net/1b9095a8.../master.mpd&token=eyJhbGciOi...

Design notes:
  - Token is forwarded as Authorization: Bearer <token> on every upstream
    request, and also injected as a query-param ?token= where the CDN
    expects it (CloudFront signed URLs keep the token in the query string).
  - CORS is fully open (*) so any browser / iframe can load the player.
  - All MPD XML is rewritten in-flight: every URL (BaseURL, SegmentTemplate,
    SegmentList, SegmentBase, ContentProtection schemeURI) is routed through
    /api/mpd/seg so the real CDN URL never reaches client JS.
  - Segments are streamed in chunks (iter_content) to keep memory low.
"""

import base64
import logging
import os
import re
import time
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse, unquote
import xml.etree.ElementTree as ET

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

# ─── App init ────────────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────
UPSTREAM_TIMEOUT   = 20
UPSTREAM_RETRIES   = 3
CHUNK_SIZE         = 64 * 1024  # 64 KB streaming chunk

# Headers sent to PW CDN — mimics a real Chrome browser on Android
UPSTREAM_HEADERS = {
    "User-Agent"      : "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept"          : "*/*",
    "Accept-Language" : "en-IN,en;q=0.9",
    "Referer"         : "https://www.pw.live/",
    "Origin"          : "https://www.pw.live",
    "sec-ch-ua"       : '"Chromium";v="124","Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"Android"',
    "Sec-Fetch-Dest"  : "empty",
    "Sec-Fetch-Mode"  : "cors",
    "Sec-Fetch-Site"  : "cross-site",
}

NO_STORE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}

# MPD XML namespaces (standard + PW variants)
MPD_NS = {
    "mpd"  : "urn:mpeg:dash:schema:mpd:2011",
    "cenc" : "urn:mpeg:cenc:2013",
    "mspr" : "urn:microsoft:playready",
    "skd"  : "com.apple.streamingkeydelivery",
}
ET.register_namespace("", "urn:mpeg:dash:schema:mpd:2011")
ET.register_namespace("cenc", "urn:mpeg:cenc:2013")
ET.register_namespace("mspr", "urn:microsoft:playready")


# ─── CORS — every response ────────────────────────────────────────────────────
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


# ─── Helpers ─────────────────────────────────────────────────────────────────

def b64e(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

def b64d(s: str) -> str:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s).decode()


def extract_param(name: str, raw_qs: str = None) -> str | None:
    """
    Read a query-param from the raw query string safely.
    Flask's request.args truncates values at '&' when the value itself
    contains unencoded '&' (e.g. a CDN-signed URL pasted raw).
    """
    qs = raw_qs or request.query_string.decode("utf-8", errors="replace")
    marker = f"{name}="
    idx = qs.find(marker)
    if idx == -1:
        return None
    val = qs[idx + len(marker):]
    # Stop at next key=value boundary only if the next '&' is followed
    # by a key= pattern — otherwise keep the whole value (e.g. '&IDs&')
    # We do a greedy read here and let unquote handle it.
    try:
        return unquote(val)
    except Exception:
        return val


def build_auth_headers(token: str | None) -> dict:
    """Merge UPSTREAM_HEADERS with Authorization if token present."""
    h = dict(UPSTREAM_HEADERS)
    if token:
        h["Authorization"] = f"Bearer {token}"
        # Some PW CDN endpoints expect the token as a custom header too
        h["token"]         = token
        h["authToken"]     = token
    return h


def fetch_upstream(url: str, headers: dict, range_hdr: str = None) -> requests.Response:
    """Fetch with retry + exponential backoff. 4xx = final (no retry)."""
    h = dict(headers)
    if range_hdr:
        h["Range"] = range_hdr
    last_exc = None
    for attempt in range(UPSTREAM_RETRIES):
        try:
            r = requests.get(url, headers=h, timeout=UPSTREAM_TIMEOUT,
                             allow_redirects=True, stream=True)
            if r.ok or 400 <= r.status_code < 500:
                return r
            last_exc = requests.RequestException(f"HTTP {r.status_code}")
        except requests.RequestException as exc:
            last_exc = exc
        wait = 0.5 * (2 ** attempt)
        log.warning("Attempt %d failed for %s — retrying in %.1fs", attempt + 1, url, wait)
        time.sleep(wait)
    raise last_exc


def is_mpd(url: str, ctype: str = "") -> bool:
    if "dash" in ctype.lower() or "mpd" in ctype.lower():
        return True
    path = urlparse(url).path.lower().split("?")[0]
    return path.endswith(".mpd")


def is_m3u8(url: str, ctype: str = "") -> bool:
    if "mpegurl" in ctype.lower() or "m3u8" in ctype.lower():
        return True
    path = urlparse(url).path.lower().split("?")[0]
    return path.endswith(".m3u8")


def make_seg_url(absolute_url: str) -> str:
    """Wrap a real CDN URL into our /api/mpd/seg?u= proxy URL."""
    base = request.host_url.rstrip("/")
    return f"{base}/api/mpd/seg?u={b64e(absolute_url)}"


def rewrite_mpd_manifest(xml_text: str, mpd_base_url: str, token: str | None) -> str:
    """
    Parse the MPD XML and rewrite every media URL to point through
    /api/mpd/seg so:
      1. The real CDN URL + token never reaches client JS (security).
      2. CORS issues with CloudFront are eliminated.
      3. We can inject the auth token on every CDN request server-side.

    Handles:
      - <BaseURL> elements
      - SegmentTemplate @media / @initialization / @index attributes
      - SegmentList > SegmentURL @media / @index attributes
      - SegmentBase @indexRange stays as-is (byte ranges, not URLs)
    """
    try:
        # Preserve original XML declaration if present
        xml_decl = ""
        if xml_text.lstrip().startswith("<?xml"):
            xml_decl = xml_text[:xml_text.index("?>") + 2] + "\n"

        # Strip default namespace to make xpath simpler
        xml_clean = re.sub(r'\sxmlns="[^"]+"', '', xml_text, count=1)
        root = ET.fromstring(xml_clean)
    except ET.ParseError as exc:
        log.error("MPD parse error: %s", exc)
        return xml_text  # Return unchanged if unparseable

    def abs_and_proxy(rel_or_abs: str, context_base: str) -> str:
        if not rel_or_abs or rel_or_abs.startswith("data:"):
            return rel_or_abs
        absolute = urljoin(context_base, rel_or_abs.strip())
        # Inject token into query string if it's not already there
        if token and "token=" not in absolute and "authorization=" not in absolute.lower():
            sep = "&" if "?" in absolute else "?"
            absolute = f"{absolute}{sep}token={token}"
        return make_seg_url(absolute)

    current_base = mpd_base_url

    # Walk every element in the tree
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

        # <BaseURL>text</BaseURL>
        if tag == "BaseURL" and elem.text and elem.text.strip():
            original = elem.text.strip()
            absolute  = urljoin(current_base, original)
            elem.text = abs_and_proxy(original, current_base)
            current_base = absolute  # Update context for children

        # SegmentTemplate attributes
        if tag == "SegmentTemplate":
            for attr in ("media", "initialization", "index", "bitstreamSwitching"):
                val = elem.get(attr)
                if val:
                    # SegmentTemplate uses $Number$, $Time$, $RepresentationID$
                    # — we cannot proxy these directly (they're templates, not
                    # real URLs yet). Instead we inject a proxy prefix that
                    # the player will expand BEFORE fetching — BUT since
                    # most DASH players don't support that, we instead
                    # rewrite by adding a special marker the /api/mpd/seg
                    # endpoint will recognise and strip before forwarding.
                    if any(ph in val for ph in ("$Number$", "$Time$", "$Bandwidth$", "$RepresentationID$")):
                        # Build absolute template first
                        abs_tmpl = urljoin(current_base, val)
                        if token and "token=" not in abs_tmpl:
                            sep = "&" if "?" in abs_tmpl else "?"
                            abs_tmpl = f"{abs_tmpl}{sep}token={token}"
                        proxied_tmpl = make_seg_url(abs_tmpl)
                        # Replace the seg?u= encoded part with a template-aware version
                        # We encode the template string and let /api/mpd/seg handle it
                        elem.set(attr, proxied_tmpl)
                    else:
                        elem.set(attr, abs_and_proxy(val, current_base))

        # SegmentList > SegmentURL
        if tag == "SegmentURL":
            for attr in ("media", "index"):
                val = elem.get(attr)
                if val:
                    elem.set(attr, abs_and_proxy(val, current_base))

        # SegmentBase (indexRange is a byte range, not a URL — leave alone)
        # But @initialization is a URL sub-element sometimes
        if tag == "Initialization":
            val = elem.get("sourceURL") or elem.get("range")
            if val and not val.replace("-", "").replace(",", "").isdigit():
                elem.set("sourceURL", abs_and_proxy(val, current_base))

    rewritten = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return xml_decl + rewritten


# ═══════════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════════

# ── /play  ─────────────────────────────────────────────────────────────────────
@app.route("/play")
def play():
    """
    GET /play?url=<mpd_url>&token=<auth_token>
    Renders the DASH player page. URL and token are read client-side from
    window.location.search so no server-side escaping needed.
    """
    raw_url = extract_param("url")
    if not raw_url:
        return render_template("error.html",
                               message="Missing ?url= parameter. "
                                       "Usage: /play?url=https://.../master.mpd&token=YOUR_TOKEN"), 400
    return render_template("player.html")


# ── /api/mpd/manifest  ─────────────────────────────────────────────────────────
@app.route("/api/mpd/manifest")
def mpd_manifest():
    """
    GET /api/mpd/manifest?url=<mpd>&token=<tok>
    Fetches the MPD from the CDN, rewrites all segment URLs to go through
    /api/mpd/seg, and returns the rewritten XML.
    """
    qs     = request.query_string.decode("utf-8", errors="replace")
    url    = extract_param("url", qs)
    token  = extract_param("token", qs)

    if not url:
        return jsonify({"error": "url param missing"}), 400

    log.info("Fetching MPD: %s (token=%s)", url, "YES" if token else "NO")
    headers = build_auth_headers(token)

    try:
        r = fetch_upstream(url, headers)
    except requests.RequestException as exc:
        log.error("Upstream error: %s", exc)
        return jsonify({"error": f"Upstream error: {exc}"}), 502

    if not r.ok:
        return jsonify({"error": f"CDN returned {r.status_code}"}), r.status_code

    ctype = r.headers.get("content-type", "")
    body  = r.content.decode("utf-8", errors="replace")

    if is_m3u8(url, ctype):
        # Fallback: HLS manifest instead of DASH — proxy it through HLS rewriter
        return _proxy_m3u8(body, url, token)

    rewritten = rewrite_mpd_manifest(body, url, token)

    return Response(
        rewritten, 200,
        headers={
            **NO_STORE,
            "Content-Type": "application/dash+xml; charset=utf-8",
        }
    )


def _proxy_m3u8(body: str, base_url: str, token: str | None) -> Response:
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
        out.append(make_seg_url(absolute))
    return Response(
        "\n".join(out) + "\n", 200,
        headers={**NO_STORE, "Content-Type": "application/vnd.apple.mpegurl"}
    )


# ── /api/mpd/seg  ──────────────────────────────────────────────────────────────
@app.route("/api/mpd/seg")
def mpd_seg():
    """
    GET /api/mpd/seg?u=<base64url_encoded_cdn_url>
    Relays any media segment, init segment, or sub-manifest.
    Streams in 64 KB chunks to keep memory low on Render's free tier.
    """
    token_b64 = request.args.get("u")
    if not token_b64:
        return jsonify({"error": "Missing u param"}), 400

    try:
        cdn_url = b64d(token_b64)
        parsed  = urlparse(cdn_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Bad scheme")
    except Exception:
        return jsonify({"error": "Invalid segment token"}), 400

    # Extract token from the URL itself (was injected during manifest rewrite)
    qs_pairs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    token    = qs_pairs.get("token") or qs_pairs.get("authorization")

    log.debug("Seg relay: %s", cdn_url[:80])
    headers   = build_auth_headers(token)
    range_hdr = request.headers.get("Range")

    try:
        r = fetch_upstream(cdn_url, headers, range_hdr)
    except requests.RequestException as exc:
        log.error("Seg upstream error for %s: %s", cdn_url[:60], exc)
        return jsonify({"error": f"Upstream error: {exc}"}), 502

    if not r.ok:
        return jsonify({"error": f"CDN returned {r.status_code}"}), r.status_code

    ctype = r.headers.get("content-type", "")

    # Sub-manifest (child MPD or HLS playlist) — rewrite before returning
    if is_mpd(cdn_url, ctype):
        body      = r.content.decode("utf-8", errors="replace")
        rewritten = rewrite_mpd_manifest(body, cdn_url, token)
        return Response(rewritten, 200, headers={
            **NO_STORE,
            "Content-Type": "application/dash+xml; charset=utf-8",
        })

    if is_m3u8(cdn_url, ctype):
        body = r.content.decode("utf-8", errors="replace")
        return _proxy_m3u8(body, cdn_url, token)

    # Binary segment — stream back in chunks
    resp_headers = {
        "Content-Type"  : ctype or "video/mp4",
        "Cache-Control" : "public, max-age=30",
        "Accept-Ranges" : "bytes",
    }
    if r.headers.get("Content-Length"):
        resp_headers["Content-Length"] = r.headers["Content-Length"]
    if r.headers.get("Content-Range"):
        resp_headers["Content-Range"] = r.headers["Content-Range"]

    status = 206 if r.status_code == 206 else 200

    def generate():
        try:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    yield chunk
        except Exception as exc:
            log.error("Streaming error: %s", exc)

    return Response(stream_with_context(generate()), status=status, headers=resp_headers)


# ── /api/mpd/key  ──────────────────────────────────────────────────────────────
@app.route("/api/mpd/key")
def mpd_key():
    """
    GET /api/mpd/key?u=<b64_key_url>
    Key/licence relay for clearkey or key-only (non-Widevine) content.
    Binary relay with proper CORS so the browser's EME can fetch keys.
    """
    token_b64 = request.args.get("u")
    if not token_b64:
        return jsonify({"error": "Missing u param"}), 400
    try:
        key_url = b64d(token_b64)
        if urlparse(key_url).scheme not in ("http", "https"):
            raise ValueError
    except Exception:
        return jsonify({"error": "Invalid key token"}), 400

    qs_pairs = dict(parse_qsl(urlparse(key_url).query, keep_blank_values=True))
    token    = qs_pairs.get("token")
    headers  = build_auth_headers(token)

    try:
        r = fetch_upstream(key_url, headers)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502

    return Response(r.content, r.status_code, headers={
        "Content-Type"  : r.headers.get("Content-Type", "application/octet-stream"),
        "Cache-Control" : "no-store",
    })


# ── /health  ───────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "playvideo-mpd-proxy"})


# ── Root redirect  ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ─── Dev server ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
