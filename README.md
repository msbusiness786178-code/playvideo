# PlayVideo — MPD/DASH Stream Proxy + Player

> Deploy on Render.com in 2 minutes. Stream PW DASH videos with auth token support.

---

## 🚀 Deploy to Render

1. Push this repo to GitHub
2. Go to [render.com](https://render.com) → **New → Web Service**
3. Connect your GitHub repo
4. Render auto-detects `Dockerfile` — click **Deploy**
5. Your public URL will be: `https://playvideo.onrender.com` (or similar)

---

## 📺 Usage

### Main Player URL

```
https://playvideo.onrender.com/play?url=MPD_URL&token=AUTH_TOKEN
```

**Example:**
```
https://playvideo.onrender.com/play?url=https://d1d34p8vz63oiq.cloudfront.net/1b9095a8-946a-48ed-9577-34473b42e810/master.mpd&IDs&token=eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...
```

### API Routes

| Route | Purpose |
|---|---|
| `GET /play?url=&token=` | **Main player page** (Shaka DASH player) |
| `GET /api/mpd/manifest?url=&token=` | Proxied + rewritten MPD XML |
| `GET /api/mpd/seg?u=<base64>` | Segment / init segment / sub-manifest relay |
| `GET /api/mpd/key?u=<base64>` | Key / licence relay |
| `GET /health` | Healthcheck for Render |
| `GET /` | Home page with URL input form |

---

## 🔧 How It Works

```
Browser ──── /play?url=MPD&token=TOK ────────────────► Flask (Render)
                                                            │
                                                   /api/mpd/manifest
                                                            │
                                                     Fetch MPD from CDN
                                                     Inject token header
                                                     Rewrite all URLs →
                                                     /api/mpd/seg?u=BASE64
                                                            │
Browser ◄──── Rewritten MPD XML ◄───────────────────────────┘
    │
Shaka Player fetches each segment:
    └── /api/mpd/seg?u=<base64(real_cdn_url_with_token)>
              │
              ├── Decode base64 → real CDN URL
              ├── Inject Authorization: Bearer token
              ├── Fetch from CloudFront/CDN
              └── Stream chunk back to browser
```

---

## 🔐 Token / Auth

- Token is passed as `?token=` in the player URL
- Injected as `Authorization: Bearer <token>` on every CDN request
- Also added as `?token=` query param on manifest/segment URLs
- Never exposed to client JS (all CDN requests go server-side)

---

## 🎮 Player Features

- **Shaka Player** (Google's production DASH player — same as YouTube)
- Auto quality (ABR) + manual quality selector
- Fullscreen + landscape lock on mobile
- "Go Live" button when behind live edge
- Elapsed time display
- Keyboard shortcuts: `Space/K` play/pause, `F` fullscreen, `←/→` seek, `↑/↓` volume, `M` mute
- Works on Chrome, Firefox, Edge, Safari

---

## 🐳 Local Run

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:8000
```

Or with Docker:
```bash
docker build -t playvideo .
docker run -p 8000:8000 playvideo
```
