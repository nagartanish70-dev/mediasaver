"""
MediaSaver by Tanish Nagar — Runs on your PC
Receives URLs, downloads media using yt-dlp,
stores them on your PC, and serves the files back to the iPhone.
Supports: Instagram Reels, YouTube videos, Twitter/X videos.
"""

import io
import json
import os
import re
import plistlib
import socket
import sqlite3
import hashlib
import secrets
import subprocess
import threading
import time
import uuid as _uuid_mod
from datetime import datetime
from flask import Flask, request, jsonify, send_file, abort, Response

try:
    import yt_dlp
except ImportError:
    raise SystemExit("yt-dlp is not installed. Run: pip install yt-dlp")

import shutil as _shutil

def _find_ffmpeg() -> str:
    """Return the absolute path to ffmpeg, checking local dir first, then PATH."""
    _here = os.path.dirname(os.path.abspath(__file__))
    if os.name == "nt":
        local = os.path.join(_here, "ffmpeg.exe")
        if os.path.isfile(local):
            return local
    else:
        local = os.path.join(_here, "ffmpeg")
        if os.path.isfile(local):
            return local
    found = _shutil.which("ffmpeg")
    return found or "ffmpeg"

# ─── Cloudflare Tunnel ───────────────────────────────────────────────────────────

_public_url: str = ""   # set once cloudflared reports its URL

# In-memory job store for async downloads  {job_id: {status, title, filepath, error}}
_jobs: dict = {}
_jobs_lock = threading.Lock()

# Short-lived cache: POST /stream pre-extracts info so GET can start ffmpeg instantly
_stream_cache: dict = {}  # {cache_key: (timestamp, info)}
_stream_cache_lock = threading.Lock()

# ─── PC ↔ Render bridge ──────────────────────────────────────────────────────
# On Render: stores the PC's tunnel URL + last heartbeat timestamp.
# On PC: sends heartbeat to Render every 2 minutes.
_pc_tunnel_url: str = ""
_pc_last_seen: float = 0.0
_pc_bridge_lock = threading.Lock()

import urllib.request as _urlreq

def _pc_is_online() -> str:
    """Return the PC tunnel URL if PC is online, else empty string."""
    with _pc_bridge_lock:
        if (time.time() - _pc_last_seen) < 300 and _pc_tunnel_url:
            return _pc_tunnel_url
    return ""

def _forward_to_pc(path: str, query_string: str, method: str = "GET",
                   body: bytes = b"", content_type: str = "") -> tuple:
    """
    Forward a request to the PC's tunnel.  Returns (response_body: bytes, status: int, content_type: str)
    or raises on failure so the caller can fall back to local handling.
    """
    pc = _pc_is_online()
    if not pc:
        raise ConnectionError("PC offline")
    url = f"{pc}/{path.lstrip('/')}"
    if query_string:
        url += f"?{query_string}"
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    req = _urlreq.Request(url, data=body if body else None, headers=headers, method=method)
    resp = _urlreq.urlopen(req, timeout=120)
    return resp.read(), resp.status, resp.headers.get("Content-Type", "application/json")

# ─── Rate limiting ────────────────────────────────────────────────────────────
_rate_data: dict = {}   # {ip: [timestamp, ...]}
_rate_lock = threading.Lock()

def _is_rate_limited(ip: str, max_req: int = 20, window: int = 3600) -> bool:
    """Return True if ip has exceeded max_req requests within window seconds."""
    now = time.time()
    with _rate_lock:
        ts = [t for t in _rate_data.get(ip, []) if now - t < window]
        if len(ts) >= max_req:
            return True
        ts.append(now)
        _rate_data[ip] = ts
    return False

def _client_ip() -> str:
    """Real IP, respecting CF-Connecting-IP / X-Forwarded-For from Cloudflare."""
    return (request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr
            or "unknown")

def _start_cloudflared():
    global _public_url
    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloudflared.exe")
    if not os.path.isfile(exe):
        print("  [tunnel] cloudflared.exe not found — skipping public URL")
        return

    _here      = os.path.dirname(os.path.abspath(__file__))
    config_yml = os.path.join(_here, "tunnel.yml")
    url_file   = os.path.join(_here, "tunnel_url.txt")

    # ── Named tunnel (stable URL, no timeout, no random domain) ──────────
    if os.path.isfile(config_yml):
        # Read public hostname from YAML config (ingress[0].hostname)
        try:
            import yaml
            with open(config_yml, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            hostname = cfg.get("ingress", [{}])[0].get("hostname", "")
            if hostname:
                _public_url = f"https://{hostname}"
                print(f"  [tunnel] Named tunnel → {_public_url}")
                with open(url_file, "w") as f:
                    f.write(_public_url)
        except Exception as e:
            print(f"  [tunnel] Could not read tunnel.yml: {e}")

        proc = subprocess.Popen(
            [exe, "tunnel", "--config", config_yml, "--no-autoupdate", "run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return

    # ── Quick tunnel (random URL, 90s timeout — fallback) ────────────────
    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    for line in iter(proc.stdout.readline, ""):
        m = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", line)
        if m:
            _public_url = m.group(0)
            print(f"  Public URL (iPhone): {_public_url}")
            print(f"  (Quick tunnel — URL changes on restart. See tunnel_url.txt)")
            with open(url_file, "w") as f:
                f.write(_public_url)

            break

# ─── Local IP helper ─────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the machine's LAN IP (best-effort)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

# ─── iOS Shortcut generator ───────────────────────────────────────────────────

def _uid() -> str:
    return str(_uuid_mod.uuid4()).upper()

def _plain(s: str) -> dict:
    """Bare text token (no variable attachments)."""
    return {"Value": {"attachmentsByRange": {}, "string": s},
            "WFSerializationType": "WFTextTokenString"}

def _out_tok(out_uuid: str, out_name: str = "") -> dict:
    """Reference a previous action's output by UUID."""
    return {"Value": {"OutputName": out_name, "OutputUUID": out_uuid,
                      "Type": "ActionOutput"},
            "WFSerializationType": "WFTextTokenAttachment"}

def _concat(*parts) -> dict:
    """Build a WFTextTokenString from str and attachment-dict parts."""
    s = ""
    attachments: dict = {}
    for p in parts:
        if isinstance(p, str):
            s += p
        else:
            attachments[f"{{{len(s)}, 1}}"] = p["Value"]
            s += "\ufffc"
    return {"Value": {"attachmentsByRange": attachments, "string": s},
            "WFSerializationType": "WFTextTokenString"}

def _dict_field_value(*pairs) -> dict:
    """Build a WFDictionaryFieldValue from (key_str, value_token) pairs."""
    items = [{"WFItemType": 0, "WFKey": _plain(k), "WFValue": v}
             for k, v in pairs]
    return {"Value": {"WFDictionaryFieldValueItems": items},
            "WFSerializationType": "WFDictionaryFieldValue"}

def build_shortcut_plist(pc_ip: str, port: int, api_key: str) -> bytes:
    """Generate a .shortcut plist file pre-configured for this server."""
    dl_url    = f"http://{pc_ip}:{port}/download"
    file_base = f"http://{pc_ip}:{port}"

    u_post  = _uid()   # POST → server JSON dict
    u_stat  = _uid()   # "status" string
    u_grp   = _uid()   # if/end-if grouping
    u_path  = _uid()   # "file_path" string
    u_file  = _uid()   # downloaded video bytes
    u_title = _uid()   # "title" string

    # Shortcut Input (the shared URL from the Share Sheet)
    si = {"Value": {"Type": "Variable", "VariableName": "Shortcut Input"},
          "WFSerializationType": "WFTextTokenAttachment"}

    actions = [
        # ── 1. POST media URL to server ──────────────────────────────────────
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
            "WFWorkflowActionParameters": {
                "UUID": u_post,
                "CustomOutputName": "ServerResponse",
                "WFHTTPMethod": "POST",
                "WFHTTPBodyType": "JSON",
                "WFURL": _plain(dl_url),
                "WFHTTPHeaders": _dict_field_value(
                    ("Content-Type", _plain("application/json")),
                    ("X-API-Key",    _plain(api_key))
                ),
                "WFJSONValues": _dict_field_value(
                    ("url", _concat(si))
                )
            }
        },
        # ── 2. Extract "status" ───────────────────────────────────────────────
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.getvalueforkey",
            "WFWorkflowActionParameters": {
                "UUID": u_stat,
                "CustomOutputName": "DownloadStatus",
                "WFInput": _out_tok(u_post, "ServerResponse"),
                "WFDictionaryKey": _plain("status")
            }
        },
        # ── 3. If status is not "success" ─────────────────────────────────────
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
            "WFWorkflowActionParameters": {
                "UUID": _uid(),
                "GroupingIdentifier": u_grp,
                "WFControlFlowMode": 0,
                "WFCondition": 5,
                "WFConditionalActionString": "success",
                "WFInput": {
                    "Type": "Variable",
                    "Variable": _out_tok(u_stat, "DownloadStatus")
                }
            }
        },
        # ── 4. Show error alert ───────────────────────────────────────────────
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.alert",
            "WFWorkflowActionParameters": {
                "WFAlertActionMessage": _concat(_out_tok(u_post, "ServerResponse")),
                "WFAlertActionTitle": "MediaSaver Error",
                "WFAlertActionCancelButtonShown": False
            }
        },
        # ── 5. Exit Shortcut ──────────────────────────────────────────────────
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.exit",
            "WFWorkflowActionParameters": {}
        },
        # ── 6. End If ─────────────────────────────────────────────────────────
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
            "WFWorkflowActionParameters": {
                "UUID": _uid(),
                "GroupingIdentifier": u_grp,
                "WFControlFlowMode": 2
            }
        },
        # ── 7. Extract "file_path" ────────────────────────────────────────────
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.getvalueforkey",
            "WFWorkflowActionParameters": {
                "UUID": u_path,
                "CustomOutputName": "FilePath",
                "WFInput": _out_tok(u_post, "ServerResponse"),
                "WFDictionaryKey": _plain("file_path")
            }
        },
        # ── 8. Download video file ────────────────────────────────────────────
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
            "WFWorkflowActionParameters": {
                "UUID": u_file,
                "CustomOutputName": "VideoFile",
                "WFHTTPMethod": "GET",
                "WFURL": _concat(file_base, _out_tok(u_path, "FilePath"),
                                 f"?api_key={api_key}")
            }
        },
        # ── 9. Save to Photos ─────────────────────────────────────────────────
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.savephotosalbum",
            "WFWorkflowActionParameters": {
                "WFInput": _out_tok(u_file, "VideoFile"),
                "WFAlbumActionAlbum": "MediaSaver"
            }
        },
        # ── 10. Extract title ─────────────────────────────────────────────────
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.getvalueforkey",
            "WFWorkflowActionParameters": {
                "UUID": u_title,
                "CustomOutputName": "VideoTitle",
                "WFInput": _out_tok(u_post, "ServerResponse"),
                "WFDictionaryKey": _plain("title")
            }
        },
        # ── 11. Show notification ─────────────────────────────────────────────
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.shownotification",
            "WFWorkflowActionParameters": {
                "WFNotificationActionBody": _concat(_out_tok(u_title, "VideoTitle")),
                "WFNotificationActionTitle": "Saved to Gallery ✓",
                "WFNotificationActionPlaySound": False
            }
        }
    ]

    data = {
        "WFWorkflowClientVersion": "2600.1",
        "WFWorkflowClientRelease": "26.0",
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowIcon": {
            "WFWorkflowIconGlyphNumber": 59511,
            "WFWorkflowIconStartColor": -1070804992
        },
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": [
            "WFURLContentItem",
            "WFStringContentItem"
        ],
        "WFWorkflowMinimumClientVersion": 1,
        "WFWorkflowMinimumClientVersionString": "1",
        "WFWorkflowTypes": [
            "WFReceivesCurrentWebPageParameters",
            "WFSiriTypes",
            "NCWidget"
        ],
        "WFWorkflowActions": actions
    }

    buf = io.BytesIO()
    plistlib.dump(data, buf, fmt=plistlib.FMT_BINARY)
    return buf.getvalue()


def build_shortcut_plist_url(base_url: str, api_key: str) -> bytes:
    """Build shortcut plist using a base URL (local IP used inside shortcut actions)."""
    return build_shortcut_plist(_local_ip(), PORT, api_key)

# ─── Configuration ────────────────────────────────────────────────────────────

SAVE_DIR    = os.environ.get("SAVE_DIR", os.path.join(os.path.expanduser("~"), "Downloads", "MediaVault"))
DB_PATH     = os.path.join(SAVE_DIR, "vault.db")
PORT        = int(os.environ.get("PORT", 8765))
API_KEY_FILE = os.path.join(SAVE_DIR, "api_key.txt")
RENDER_URL  = os.environ.get("RENDER_URL", "").rstrip("/")  # e.g. https://mediasaver.onrender.com
IS_RENDER   = bool(os.environ.get("RENDER", ""))           # Render sets this automatically

# On PC: if RENDER_URL not set, default to the known Render deployment
if not IS_RENDER and not RENDER_URL:
    RENDER_URL = "https://mediasaver.onrender.com"

os.makedirs(SAVE_DIR, exist_ok=True)

# ─── YouTube bot-detection bypass ────────────────────────────────────────────
# Using the iOS/Android player clients avoids YouTube's bot detection entirely —
# no cookies needed. Cookies are kept as an extra layer if provided via env var.
_YT_COOKIES_FILE: str = ""

def _init_yt_cookies():
    """
    Initialise YouTube cookie file for yt-dlp.
    Priority: YOUTUBE_COOKIES env var → yt_cookies.txt next to server.py.
    """
    global _YT_COOKIES_FILE
    # 1) env var (e.g. set in Render dashboard)
    cookie_data = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookie_data:
        path = os.path.join("/tmp" if os.name != "nt" else os.environ.get("TEMP", "C:\\Temp"), "yt_cookies.txt")
        try:
            with open(path, "w") as f:
                f.write(cookie_data)
            _YT_COOKIES_FILE = path
            print(f"  [cookies] loaded from YOUTUBE_COOKIES env var → {path}")
            return
        except Exception:
            pass
    # 2) file next to server.py (bundled in Docker image)
    _here = os.path.dirname(os.path.abspath(__file__))
    local_cookie = os.path.join(_here, "yt_cookies.txt")
    if os.path.isfile(local_cookie):
        _YT_COOKIES_FILE = local_cookie
        print(f"  [cookies] loaded from {local_cookie}")

_init_yt_cookies()

def _yt_opts() -> dict:
    """
    Return yt-dlp options for YouTube extraction.
    Do NOT force player_client — yt-dlp's defaults handle client selection
    and format negotiation correctly on both local and datacenter IPs.
    """
    opts = {"js_runtimes": {"node": {}}}
    if _YT_COOKIES_FILE and os.path.isfile(_YT_COOKIES_FILE):
        opts["cookiefile"] = _YT_COOKIES_FILE
    return opts

def _cookie_opts() -> dict:
    """Return yt-dlp cookie opts (kept for non-YouTube use)."""
    if _YT_COOKIES_FILE and os.path.isfile(_YT_COOKIES_FILE):
        return {"cookiefile": _YT_COOKIES_FILE}
    return {}

# ─── API Key management ───────────────────────────────────────────────────────

def load_or_create_api_key() -> str:
    # On Render (or any cloud): use environment variable
    env_key = os.environ.get("API_KEY", "")
    if env_key:
        return env_key
    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            return f.read().strip()
    key = secrets.token_hex(24)
    try:
        with open(API_KEY_FILE, "w") as f:
            f.write(key)
    except Exception:
        pass
    return key

API_KEY = load_or_create_api_key()

# ─── Database ─────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            url          TEXT    NOT NULL,
            platform     TEXT,
            title        TEXT,
            filename     TEXT,
            filepath     TEXT,
            status       TEXT,
            downloaded_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def log_download(url: str, platform: str, title: str,
                 filename: str, filepath: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO downloads (url,platform,title,filename,filepath,status,downloaded_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (url, platform, title, filename, filepath, status, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    url_lower = url.lower()
    if "instagram.com" in url_lower:
        return "instagram"
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    if "twitter.com" in url_lower or "x.com" in url_lower:
        return "twitter"
    return "unknown"

def require_api_key():
    """Abort with 401 if the request does not carry the correct API key."""
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if not key or not secrets.compare_digest(key, API_KEY):
        abort(401, description="Invalid or missing API key.")

# ─── Flask app ────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.after_request
def _add_cors(response):
    """Allow Render frontend to call PC tunnel APIs (cross-origin fetch)."""
    origin = request.headers.get("Origin", "")
    if origin and ("onrender.com" in origin or "localhost" in origin or "127.0.0.1" in origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.before_request
def _handle_preflight():
    """Auto-respond to CORS preflight OPTIONS requests."""
    if request.method == "OPTIONS":
        resp = Response("", status=204)
        origin = request.headers.get("Origin", "")
        if origin and ("onrender.com" in origin or "localhost" in origin or "127.0.0.1" in origin):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Access-Control-Max-Age"] = "3600"
        return resp

@app.route("/status", methods=["GET"])
def status():
    """Health-check — no auth needed so the Shortcut can ping first."""
    import shutil
    node_path = shutil.which("node")
    node_ver = ""
    if node_path:
        try:
            node_ver = subprocess.check_output([node_path, "--version"], timeout=5).decode().strip()
        except Exception:
            node_ver = "error"
    return jsonify({
        "status": "running",
        "instance": "render" if IS_RENDER else "pc",
        "save_dir": SAVE_DIR,
        "tunnel_url": _public_url,
        "yt_dlp_version": yt_dlp.version.__version__,
        "node": node_ver or "not found",
        "node_path": node_path or "none",
        "yt_cookies": bool(_YT_COOKIES_FILE),
        "ffmpeg": _find_ffmpeg(),
        "pc_online": (time.time() - _pc_last_seen) < 300 if IS_RENDER else None,
        "pc_tunnel": _pc_tunnel_url if IS_RENDER else _public_url,
    })


# ─── PC ↔ Render bridge endpoints ────────────────────────────────────────────

@app.route("/_pc_heartbeat", methods=["POST"])
def pc_heartbeat():
    """PC sends its tunnel URL to Render every 2 minutes."""
    global _pc_tunnel_url, _pc_last_seen
    key = request.args.get("k", "")
    if not key or not secrets.compare_digest(key, API_KEY):
        return jsonify({"error": "bad key"}), 401
    data = request.get_json(silent=True) or {}
    tunnel = (data.get("tunnel_url") or "").strip().rstrip("/")
    if not tunnel:
        return jsonify({"error": "missing tunnel_url"}), 400
    with _pc_bridge_lock:
        _pc_tunnel_url = tunnel
        _pc_last_seen = time.time()
    return jsonify({"ok": True})


@app.route("/_pc_status", methods=["GET"])
def pc_status():
    """Frontend checks if PC is online. No auth needed (returns minimal info)."""
    with _pc_bridge_lock:
        online = (time.time() - _pc_last_seen) < 300  # 5 min threshold
        tunnel = _pc_tunnel_url if online else ""
    return jsonify({"pc_online": online, "pc_tunnel": tunnel})


@app.route("/_proxy_test", methods=["GET"])
def proxy_test():
    """Debug: test if Render can reach PC's tunnel."""
    key = request.args.get("k", "")
    if not key or not secrets.compare_digest(key, API_KEY):
        return jsonify({"error": "bad key"}), 401
    pc = _pc_is_online()
    if not pc:
        return jsonify({"error": "PC offline", "tunnel": _pc_tunnel_url,
                        "last_seen": time.time() - _pc_last_seen})
    try:
        data, status, ct = _forward_to_pc("status", "", "GET")
        return jsonify({"ok": True, "pc_tunnel": pc, "pc_status": json.loads(data)})
    except Exception as e:
        return jsonify({"error": str(e), "type": type(e).__name__, "pc_tunnel": pc})


@app.route("/debug_formats", methods=["GET"])
def debug_formats():
    """Debug: try multiple player clients and report which work on this instance."""
    key = request.args.get("k", "")
    if not key or not secrets.compare_digest(key, API_KEY):
        abort(401)
    url = request.args.get("url", "https://www.youtube.com/watch?v=O77bYeW5ZJw")

    # Quick test: process=True but with permissive format
    quick = {}
    try:
        # First try with process=True and a very permissive format
        for fmt_sel in ["bestvideo*+bestaudio*/best*", "worst", None]:
            try:
                opts = {"quiet": True, "no_warnings": True, **_yt_opts()}
                if fmt_sel:
                    opts["format"] = fmt_sel
                ydl = yt_dlp.YoutubeDL(opts)
                info = ydl.extract_info(url, download=False)
                fmts = info.get("formats") or []
                with_url = [f for f in fmts if f.get("url")]
                vid_only = [f for f in with_url if f.get("vcodec") not in ("none", None, "") and f.get("acodec") in ("none", None, "")]
                aud_only = [f for f in with_url if f.get("acodec") not in ("none", None, "") and f.get("vcodec") in ("none", None, "")]
                muxed    = [f for f in with_url if f.get("vcodec") not in ("none", None, "") and f.get("acodec") not in ("none", None, "")]
                quick = {
                    "ok": True, "format_used": fmt_sel or "default",
                    "total": len(fmts), "with_url": len(with_url),
                    "vid_only": len(vid_only), "aud_only": len(aud_only), "muxed": len(muxed),
                    "title": info.get("title", "?"),
                    "sample_fmts": [
                        {"id": f.get("format_id"), "ext": f.get("ext"),
                         "vcodec": f.get("vcodec"), "acodec": f.get("acodec"),
                         "height": f.get("height"), "has_url": bool(f.get("url"))}
                        for f in fmts[:10]
                    ]
                }
                break
            except Exception as e:
                quick = {"ok": False, "format_tried": fmt_sel, "error": str(e)[:300]}
                continue
    except Exception as e:
        quick = {"ok": False, "error": str(e)[:300]}

    return jsonify({"raw_extract": quick})


@app.route("/save", methods=["GET", "POST"])
def save_media_get():
    """
    Async download endpoint — returns a job_id immediately so mobile browsers
    don't timeout. Poll /job/<id> for result.
    """
    key = request.args.get("k", "")
    if not key or not secrets.compare_digest(key, API_KEY):
        return jsonify({"error": "bad key"}), 401

    if request.method == "POST":
        json_body = request.get_json(silent=True)
        if json_body and "url" in json_body:
            url = str(json_body["url"]).strip()
        else:
            url = (request.get_data(as_text=True) or "").strip()
    else:
        url = request.args.get("url", "").strip()

    if not url:
        return jsonify({"error": "no url"}), 400

    job_id = str(_uuid_mod.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "title": "", "filepath": "", "error": ""}

    def _run():
        platform = detect_platform(url)
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        out_dir  = os.path.join(SAVE_DIR, platform)
        os.makedirs(out_dir, exist_ok=True)
        _ffmpeg = _find_ffmpeg()
        ydl_opts = {
            "outtmpl"             : os.path.join(out_dir, f"%(title).60s_{url_hash}.%(ext)s"),
            "format"              : "bestvideo+bestaudio/bestvideo*+bestaudio*/best",
            "merge_output_format" : "mp4",
            "ffmpeg_location"     : _ffmpeg,
            "quiet"               : True,
            "no_warnings"         : True,
            "http_headers"        : {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
                )
            },
            **_yt_opts(),
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info     = ydl.extract_info(url, download=True)
                title    = info.get("title", "Unknown")
                filepath = ydl.prepare_filename(info)
                if not os.path.exists(filepath):
                    filepath = os.path.splitext(filepath)[0] + ".mp4"
            log_download(url, platform, title, os.path.basename(filepath), filepath, "success")
            with _jobs_lock:
                _jobs[job_id].update({"status": "done", "title": title, "filepath": filepath})
        except Exception as exc:
            msg = str(exc)
            log_download(url, platform, "", "", "", f"error: {msg}")
            with _jobs_lock:
                _jobs[job_id].update({"status": "error", "error": msg})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/job/<job_id>", methods=["GET"])
def job_status(job_id):
    """Poll this after calling /save to get download progress."""
    key = request.args.get("k", "")
    if not key or not secrets.compare_digest(key, API_KEY):
        return jsonify({"error": "bad key"}), 401
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    resp = {"status": job["status"], "title": job["title"], "error": job["error"]}
    if job["status"] == "done" and job["filepath"]:
        # Provide a URL to download the file to the phone
        fname = os.path.basename(job["filepath"])
        import urllib.parse
        resp["download_url"] = "/dl/" + urllib.parse.quote(fname)
    return jsonify(resp)


@app.route("/dl/<path:filename>", methods=["GET"])
def serve_download(filename):
    """Stream a downloaded video file so the phone can save it too."""
    key = request.args.get("k", "")
    if not key or not secrets.compare_digest(key, API_KEY):
        abort(401)
    # Search all platform subdirs
    for sub in ["", "instagram", "youtube", "twitter", "unknown"]:
        candidate = os.path.join(SAVE_DIR, sub, filename) if sub else os.path.join(SAVE_DIR, filename)
        if os.path.isfile(candidate):
            return send_file(candidate, as_attachment=True,
                             download_name=filename, mimetype="video/mp4")
    abort(404)


@app.route("/qualities", methods=["GET", "POST"])
def get_qualities():
    """
    For YouTube URLs: return all available video resolutions (including DASH).
    Each option carries a yt-dlp format selector string for use with /stream.
    """
    key = request.args.get("k", "")
    if not key or not secrets.compare_digest(key, API_KEY):
        return jsonify({"error": "bad key"}), 401

    # On Render: try forwarding to PC for home-IP quality list
    if IS_RENDER and _pc_is_online():
        try:
            body = request.get_data()
            ct = request.content_type or ""
            data, status, rct = _forward_to_pc(
                "qualities", request.query_string.decode(), request.method, body, ct
            )
            return Response(data, status=status, content_type=rct)
        except Exception:
            pass  # fall through to local handling

    if request.method == "POST":
        json_body = request.get_json(silent=True)
        url = (json_body or {}).get("url", "") or (request.get_data(as_text=True) or "")
    else:
        url = request.args.get("url", "")
    url = url.strip()

    if detect_platform(url) != "youtube":
        return jsonify({"youtube": False})

    try:
        ydl_opts = {"quiet": True, "no_warnings": True, **_yt_opts()}
        info = None
        for fmt_sel in ["bestvideo+bestaudio/best", "bestvideo*+bestaudio*", "best", None]:
            try:
                opts = {**ydl_opts}
                if fmt_sel is not None:
                    opts["format"] = fmt_sel
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                break
            except Exception:
                continue
        if info is None:
            raise ValueError("all format selectors failed")
    except Exception as _e:
        return jsonify({"error": str(_e)}), 422

    try:
        title = info.get("title", "")
        fmts  = info.get("formats", [])

        # Best audio — prefer m4a/AAC (Safari-native), then highest bitrate
        audio_fmts = [
            f for f in fmts
            if f.get("vcodec") in ("none", None, "")
            and f.get("acodec") not in ("none", None, "")
            and f.get("url")
        ]
        best_audio = sorted(
            audio_fmts,
            key=lambda f: (1 if "mp4a" in (f.get("acodec") or "") else 0, f.get("abr") or 0),
            reverse=True,
        )[0] if audio_fmts else None

        # Separate muxed (video+audio, single URL — fast, no ffmpeg) from
        # DASH-only (video-only, needs ffmpeg merge — slow, YouTube throttles).
        # Only include H.264 (avc) muxed — VP9/HEVC don't play on all devices.
        muxed_fmts = [
            f for f in fmts
            if f.get("vcodec") not in ("none", None, "")
            and f.get("acodec") not in ("none", None, "")
            and f.get("height") and f.get("url")
            and ("avc" in (f.get("vcodec") or "") or "avc" in (f.get("ext") or ""))
        ]
        # Fallback: include all muxed if no H.264 ones found
        if not muxed_fmts:
            muxed_fmts = [
                f for f in fmts
                if f.get("vcodec") not in ("none", None, "")
                and f.get("acodec") not in ("none", None, "")
                and f.get("height") and f.get("url")
            ]
        dash_fmts = [
            f for f in fmts
            if f.get("vcodec") not in ("none", None, "")
            and f.get("acodec") in ("none", None, "")
            and f.get("height") and f.get("url")
        ]

        # Build best muxed per height — prefer H.264, then highest tbr
        muxed_by_h = {}
        for f in muxed_fmts:
            h = f["height"]
            cur = muxed_by_h.get(h)
            is_h264  = "avc" in (f.get("vcodec") or "")
            cur_h264 = "avc" in (cur.get("vcodec") or "") if cur else False
            if (not cur
                    or (is_h264 and not cur_h264)
                    or (is_h264 == cur_h264 and (f.get("tbr") or 0) > (cur.get("tbr") or 0))):
                muxed_by_h[h] = f

        # Build best DASH per height — prefer H.264, then highest tbr
        dash_by_h = {}
        for f in dash_fmts:
            h = f["height"]
            cur = dash_by_h.get(h)
            is_h264  = "avc" in (f.get("vcodec") or "")
            cur_h264 = "avc" in (cur.get("vcodec") or "") if cur else False
            if (not cur
                    or (is_h264 and not cur_h264)
                    or (is_h264 == cur_h264 and (f.get("tbr") or 0) > (cur.get("tbr") or 0))):
                dash_by_h[h] = f

        _ffmpeg_q = _find_ffmpeg()
        has_ffmpeg = os.path.isfile(_ffmpeg_q)

        duration = info.get("duration") or 0

        def _size_label(fmt, extra_fmt=None):
            """Return a human-readable size string from filesize or tbr*duration."""
            sz = fmt.get("filesize") or fmt.get("filesize_approx")
            if not sz and duration:
                tbr = (fmt.get("tbr") or 0)
                if extra_fmt:
                    tbr += (extra_fmt.get("tbr") or 0)
                if tbr:
                    sz = int(tbr * 1000 / 8 * duration)
            if sz and sz > 0:
                return f" ~{sz/1024/1024:.0f} MB"
            return ""

        # Merge: prefer muxed (instant, full-speed download) over DASH at same height
        all_heights = sorted(set(list(muxed_by_h.keys()) + list(dash_by_h.keys())), reverse=True)
        options = []
        for h in all_heights:
            mf = muxed_by_h.get(h)
            df = dash_by_h.get(h)

            if mf:
                sz_str = _size_label(mf)
                label = f"{h}p{sz_str}"
                # Fallback chain: numeric id → avc1+m4a → avc+any audio → any muxed at height → best overall
                fmt_selector = (f"{mf['format_id']}"
                                f"/bestvideo[vcodec^=avc1][height<={h}]+bestaudio[ext=m4a]"
                                f"/bestvideo[vcodec^=avc][height<={h}]+bestaudio"
                                f"/bestvideo[height<={h}]+bestaudio"
                                f"/best[height<={h}]/best")
                options.append({"format_id": fmt_selector, "label": label, "height": h})
            elif df and has_ffmpeg and best_audio:
                sz_str = _size_label(df, best_audio)
                label = f"{h}p{sz_str} (via server)"
                # Fallback chain: numeric ids → avc1+m4a → avc+any audio → any muxed at height → best overall
                fmt_selector = (f"{df['format_id']}+{best_audio['format_id']}"
                                f"/bestvideo[vcodec^=avc1][height<={h}]+bestaudio[ext=m4a]"
                                f"/bestvideo[vcodec^=avc][height<={h}]+bestaudio"
                                f"/bestvideo[height<={h}]+bestaudio"
                                f"/best[height<={h}]/best")
                options.append({"format_id": fmt_selector, "label": label, "height": h})

        if not options:
            options.append({"format_id": "18", "label": "360p", "height": 360})

        # Cache full info — POST /stream reuses it so no second yt-dlp call needed
        qk = hashlib.md5(f"{url}\xff{time.time()}".encode()).hexdigest()
        with _stream_cache_lock:
            _stream_cache[qk] = (time.time(), info)

        return jsonify({"youtube": True, "title": title, "options": options, "qk": qk})

    except Exception as exc:
        return jsonify({"error": str(exc)}), 422


@app.route("/stream", methods=["GET", "POST"])
def stream_to_phone():
    """
    GET  – streams/proxies video bytes to phone. Supports Range requests.
    POST – extracts CDN URL, caches it, returns {proxy_url} or {cdn_url}.
    """
    if request.method == "POST" and _is_rate_limited(_client_ip()):
        return jsonify({"error": "Too many requests. Try again later."}), 429
    key = request.args.get("k", "")
    if not key or not secrets.compare_digest(key, API_KEY):
        abort(401)

    # On Render: proxy POST /stream to PC so cache keys + download stay on PC
    if IS_RENDER and request.method == "POST" and _pc_is_online():
        try:
            pc_url = _pc_is_online()
            body = request.get_data()
            ct = request.content_type or ""
            data, status, rct = _forward_to_pc(
                "stream", request.query_string.decode(), "POST", body, ct
            )
            # Rewrite relative proxy_url to point to PC's tunnel
            if status == 200 and b"proxy_url" in data:
                import json as _json
                j = _json.loads(data)
                if "proxy_url" in j and j["proxy_url"].startswith("/"):
                    j["proxy_url"] = pc_url + j["proxy_url"]
                data = _json.dumps(j).encode()
            return Response(data, status=status, content_type=rct)
        except Exception:
            pass  # fall through to local handling

    _ffmpeg = _find_ffmpeg()

    import urllib.request as _urlreq

    # ════════════════════════════════════════════════════════════════════════
    # GET  — browser navigates here to actually download the video
    # ════════════════════════════════════════════════════════════════════════
    if request.method == "GET":
        url_param = request.args.get("url", "").strip()
        ck_param  = request.args.get("ck", "").strip()

        # ── proxy (Instagram / Twitter / YouTube CDN) ── ck only, no url ───
        if ck_param and not url_param:
            with _stream_cache_lock:
                entry = _stream_cache.pop(ck_param, None)
            if not entry or (time.time() - entry[0]) > 1800:  # 30-min window
                abort(410)
            cd           = entry[1]
            cdn_url      = cd["cdn_url"]
            title_p      = cd.get("title", "video")
            cdn_hdrs     = cd.get("http_headers", {})
            safe_fn_p    = re.sub(r'[^\w .-]', '_', title_p)[:60].strip() + ".mp4"

            # Forward Range header so iOS can resume interrupted downloads
            range_hdr = request.headers.get("Range")
            req_hdrs_p = {k: v for k, v in cdn_hdrs.items()
                          if k.lower() not in ("accept-encoding", "host")}
            if range_hdr:
                req_hdrs_p["Range"] = range_hdr
            req_p = _urlreq.Request(cdn_url, headers=req_hdrs_p)

            resp_obj_p  = _urlreq.urlopen(req_p, timeout=60)
            http_status = resp_obj_p.status  # 200 or 206

            def gen_proxy():
                try:
                    while True:
                        chunk = resp_obj_p.read(262144)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    resp_obj_p.close()

            resp_headers_p = {
                "Content-Disposition": f'attachment; filename="{safe_fn_p}"',
                "Accept-Ranges": "bytes",
            }
            for h in ("Content-Length", "Content-Range", "Content-Type"):
                v = resp_obj_p.headers.get(h)
                if v:
                    resp_headers_p[h] = v
            return Response(gen_proxy(), status=http_status, mimetype="video/mp4",
                            headers=resp_headers_p)

        # ── YouTube: pipe yt-dlp stdout directly to phone ────────────────
        # yt-dlp with --throttled-rate forces anti-throttle range downloads
        # even when writing to stdout. Phone receives bytes immediately.
        url       = url_param
        # NOTE: URL query strings decode '+' as space (x-www-form-urlencoded).
        # yt-dlp format selectors use '+' to merge streams (e.g. bestvideo+bestaudio).
        # Undo the space-decode so the selector is valid.
        format_id = request.args.get("format_id", "").replace(" ", "+").strip()
        cache_key = ck_param
        if not url:
            return jsonify({"error": "no url"}), 400
        if not format_id:
            format_id = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"

        # Get title from cache (set by POST) for filename
        title = "video"
        if cache_key:
            with _stream_cache_lock:
                entry = _stream_cache.pop(cache_key, None)
                if entry and (time.time() - entry[0]) < 1800:
                    title = entry[1].get("title", "video")

        safe_fn  = re.sub(r'[^\w .-]', '_', title)[:60].strip() + ".mp4"

        # Extract CDN URLs via yt-dlp (Node.js installed → n-parameter solved → no throttle)
        # Extract WITHOUT format filter first, then select formats manually.
        # This avoids "format not available" errors entirely.
        info_pipe = None
        try:
            ydl_opts_pipe = {"quiet": True, "no_warnings": True, **_yt_opts()}
            for fmt_sel in ["bestvideo+bestaudio/best", "bestvideo*+bestaudio*", "best", None]:
                try:
                    opts = {**ydl_opts_pipe}
                    if fmt_sel is not None:
                        opts["format"] = fmt_sel
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info_pipe = ydl.extract_info(url, download=False)
                    break
                except Exception:
                    continue
            if info_pipe is None:
                raise ValueError("all format selectors failed")
        except Exception as e:
            return jsonify({"error": f"extract failed: {e}"}), 422

        # Manually pick best video + audio from available formats
        all_fmts_s = info_pipe.get("formats") or []
        video_only = [f for f in all_fmts_s
                      if f.get("vcodec") not in ("none", None, "")
                      and f.get("acodec") in ("none", None, "")
                      and f.get("url")]
        audio_only = [f for f in all_fmts_s
                      if f.get("acodec") not in ("none", None, "")
                      and f.get("vcodec") in ("none", None, "")
                      and f.get("url")]
        # Prefer H.264 video, then highest height/tbr
        video_only.sort(key=lambda f: (
            1 if "avc" in (f.get("vcodec") or "") else 0,
            f.get("height") or 0, f.get("tbr") or 0), reverse=True)
        # Prefer m4a audio, then highest abr
        audio_only.sort(key=lambda f: (
            1 if "mp4a" in (f.get("acodec") or "") else 0,
            f.get("abr") or 0), reverse=True)

        try:
            if video_only and audio_only:
                video_url  = video_only[0]["url"]
                audio_url  = audio_only[0]["url"]
                video_hdrs = video_only[0].get("http_headers") or {}
                audio_hdrs = audio_only[0].get("http_headers") or {}
            elif info_pipe.get("url"):
                video_url  = info_pipe["url"]
                audio_url  = None
                video_hdrs = info_pipe.get("http_headers") or {}
                audio_hdrs = {}
            else:
                # Last resort: any format with a URL
                any_fmt = [f for f in all_fmts_s if f.get("url")]
                if any_fmt:
                    video_url  = any_fmt[-1]["url"]
                    audio_url  = None
                    video_hdrs = any_fmt[-1].get("http_headers") or {}
                    audio_hdrs = {}
                else:
                    raise ValueError("no url in extracted info")
        except Exception as e:
            return jsonify({"error": f"extract failed: {e}"}), 422

        def _hdr_str(hdrs):
            return "".join(
                f"{k}: {v}\r\n"
                for k, v in hdrs.items()
                if k.lower() not in ("accept-encoding", "content-length")
            )

        ffmpeg_cmd = [_ffmpeg, "-y"]
        h = _hdr_str(video_hdrs)
        if h: ffmpeg_cmd += ["-headers", h]
        ffmpeg_cmd += ["-i", video_url]
        if audio_url:
            h2 = _hdr_str(audio_hdrs)
            if h2: ffmpeg_cmd += ["-headers", h2]
            ffmpeg_cmd += ["-i", audio_url]
        ffmpeg_cmd += [
            "-c:v", "copy", "-c:a", "copy",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]

        def generate_ffmpeg():
            proc = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                while True:
                    chunk = proc.stdout.read(262144)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
                except Exception:
                    pass

        return Response(
            generate_ffmpeg(),
            mimetype="video/mp4",
            headers={"Content-Disposition": f'attachment; filename="{safe_fn}"'},
        )

    # ════════════════════════════════════════════════════════════════════════
    # POST — extract info, cache it, return URL the browser navigates to
    # ════════════════════════════════════════════════════════════════════════
    json_body = request.get_json(silent=True)
    url       = (json_body or {}).get("url", "") or (request.get_data(as_text=True) or "")
    format_id = (json_body or {}).get("format_id", "") or ""
    url = url.strip()
    if not url:
        return jsonify({"error": "no url"}), 400

    platform = detect_platform(url)

    if platform == "youtube":
        # ── YouTube: always pipe through ffmpeg (DASH merge) ──
        # Redirect to GET /stream which extracts without format filter
        from urllib.parse import urlencode
        pipe_params = {"k": key, "url": url}
        return jsonify({"youtube": True,
                        "proxy_url": "/stream?" + urlencode(pipe_params)})

    # ── Instagram / Twitter: extract CDN URL, cache it, return proxy URL ───────
    try:
        ydl_opts_info = {
            "quiet": True, "no_warnings": True,
            # Force H.264+AAC — universal compatibility on all devices
            "format": ("best[ext=mp4][vcodec^=avc1][acodec^=mp4a]"
                       "/best[ext=mp4][vcodec^=avc][acodec^=mp4a]"
                       "/best[ext=mp4][vcodec!*=none][acodec!*=none]"
                       "/best[vcodec!*=none][acodec!*=none]/best"),
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
                )
            },
            **_yt_opts(),
        }
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(url, download=False)
            title_s      = info.get("title", "video")

            direct_url = None
            cdn_hdrs   = {}

            # Strategy 1: find best muxed format with both video+audio AND a direct URL
            if info.get("formats"):
                candidates = [
                    f for f in info["formats"]
                    if (f.get("url") or f.get("webpage_url"))
                    and f.get("vcodec") not in ("none", None, "")
                    and f.get("acodec") not in ("none", None, "")
                ]
                if candidates:
                    chosen = max(candidates, key=lambda f: f.get("tbr") or f.get("quality") or 0)
                    direct_url = chosen.get("url")
                    cdn_hdrs = chosen.get("http_headers") or {}

            # Strategy 2: check info["url"] directly (some extractors put it here)
            if not direct_url:
                direct_url = info.get("url")
                cdn_hdrs = info.get("http_headers") or {}

            # Strategy 3: check requested_formats (yt-dlp resolved format)
            if not direct_url and info.get("requested_formats"):
                for rf in info["requested_formats"]:
                    if rf.get("url"):
                        direct_url = rf["url"]
                        cdn_hdrs = rf.get("http_headers") or {}
                        break

            # Strategy 4: any format with a url at all
            if not direct_url and info.get("formats"):
                for f in reversed(info["formats"]):  # reversed = highest quality first
                    if f.get("url"):
                        direct_url = f["url"]
                        cdn_hdrs = f.get("http_headers") or {}
                        break
    except Exception as exc:
        return jsonify({"error": str(exc)}), 422

    if not direct_url:
        return jsonify({"error": "Could not extract video URL"}), 422

    # Store CDN URL in cache — GET /stream?ck=... will proxy it to the phone
    ck_s = hashlib.md5(f"{url}\xff{time.time()}".encode()).hexdigest()
    with _stream_cache_lock:
        _stream_cache[ck_s] = (time.time(), {
            "cdn_url": direct_url,
            "title": title_s,
            "platform": platform,
            "http_headers": cdn_hdrs,
            "source_url": url,
        })

    from urllib.parse import urlencode
    proxy_params = {"k": key, "ck": ck_s}
    # Return cdn_url directly — phone downloads straight from CDN, no proxy hop
    return jsonify({"cdn_url": direct_url, "proxy_url": "/stream?" + urlencode(proxy_params)})


@app.route("/getlink", methods=["GET", "POST"])
def getlink():
    """
    savefrom.net-style: extract direct CDN URL, return as plain text.
    Shortcut step 1: POST {"url": "..."} → get back the CDN URL as text.
    Shortcut step 2: Get Contents of URL on that CDN URL → video bytes.
    Shortcut step 3: Save to Photo Album.
    Also fires off background PC save.
    """
    key = request.args.get("k", "")
    if not key or not secrets.compare_digest(key, API_KEY):
        abort(401)
    if _is_rate_limited(_client_ip()):
        return "Too many requests. Try again later.", 429

    # Extract URL from POST JSON body
    url = ""
    if request.method == "POST":
        jb = request.get_json(silent=True)
        url = (jb or {}).get("url", "")
        if not url:
            body = request.get_data(as_text=True).strip()
            m = re.search(r'https?://\S+', body)
            if m:
                url = m.group(0)
    if not url:
        url = request.args.get("url", "").strip()
    if not url:
        return "no url", 400

    platform = detect_platform(url)
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    out_dir  = os.path.join(SAVE_DIR, platform)
    os.makedirs(out_dir, exist_ok=True)
    _ffmpeg = _find_ffmpeg()

    print(f"  [getlink] extracting CDN URL for: {url}")

    if platform == "youtube":
        # ── YouTube: DASH video+audio → merge with ffmpeg ────────────────────
        _gl_opts = {
            "quiet"      : True,
            "no_warnings": True,
            **_yt_opts(),
        }

        # Try format selectors from most to least restrictive.
        # Datacenter IPs may only get DASH formats (no muxed), so "best" alone can fail.
        info = None
        for fmt_sel in [
            "bestvideo+bestaudio/bestvideo*+bestaudio*/best",
            "bestvideo*+bestaudio*",
            "best",
            None,  # no format key at all — let yt-dlp decide
        ]:
            try:
                opts = {**_gl_opts}
                if fmt_sel is not None:
                    opts["format"] = fmt_sel
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                break  # success
            except Exception as _e:
                last_err = _e
                continue
        if info is None:
            return str(last_err), 422
        try:

            title = info.get("title", "video")
            safe_fn_gl = re.sub(r'[^\w .-]', '_', title)[:60].strip() + ".mp4"
            print(f"  [getlink] streaming bytes for: {title}")

            # Manually select best video (<=720p, prefer H.264) + best audio (prefer m4a)
            all_fmts = info.get("formats") or []
            video_fmts = [f for f in all_fmts
                          if f.get("vcodec") not in ("none", None, "")
                          and f.get("acodec") in ("none", None, "")
                          and f.get("height") and f.get("height") <= 720
                          and f.get("url")]
            audio_fmts = [f for f in all_fmts
                          if f.get("acodec") not in ("none", None, "")
                          and f.get("vcodec") in ("none", None, "")
                          and f.get("url")]
            muxed_fmts = [f for f in all_fmts
                          if f.get("vcodec") not in ("none", None, "")
                          and f.get("acodec") not in ("none", None, "")
                          and f.get("url")]

            # Pick best video: prefer avc1, then highest tbr
            video_fmts.sort(key=lambda f: (
                1 if "avc" in (f.get("vcodec") or "") else 0,
                f.get("height") or 0,
                f.get("tbr") or 0
            ), reverse=True)
            # Pick best audio: prefer m4a/mp4a, then highest abr
            audio_fmts.sort(key=lambda f: (
                1 if "mp4a" in (f.get("acodec") or "") else 0,
                f.get("abr") or 0
            ), reverse=True)

            best_video = video_fmts[0] if video_fmts else None
            best_audio = audio_fmts[0] if audio_fmts else None

            def _hdr_str_gl(hdrs):
                return "".join(
                    f"{k}: {v}\r\n"
                    for k, v in hdrs.items()
                    if k.lower() not in ("accept-encoding", "content-length")
                )

            if best_video and best_audio:
                # DASH: merge video + audio with ffmpeg
                video_url  = best_video["url"]
                audio_url  = best_audio["url"]
                video_hdrs = best_video.get("http_headers") or {}
                audio_hdrs = best_audio.get("http_headers") or {}

                ffmpeg_cmd_gl = [_ffmpeg, "-y"]
                hv = _hdr_str_gl(video_hdrs)
                if hv: ffmpeg_cmd_gl += ["-headers", hv]
                ffmpeg_cmd_gl += ["-i", video_url]
                ha = _hdr_str_gl(audio_hdrs)
                if ha: ffmpeg_cmd_gl += ["-headers", ha]
                ffmpeg_cmd_gl += ["-i", audio_url,
                    "-c:v", "copy", "-c:a", "copy",
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                    "-f", "mp4", "pipe:1"]

                def _gen_ffmpeg_gl():
                    proc = subprocess.Popen(
                        ffmpeg_cmd_gl,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    try:
                        while True:
                            chunk = proc.stdout.read(262144)
                            if not chunk:
                                break
                            yield chunk
                    finally:
                        try:
                            if proc.poll() is None:
                                proc.kill()
                            proc.wait()
                        except Exception:
                            pass

                return Response(_gen_ffmpeg_gl(), mimetype="video/mp4",
                                headers={"Content-Disposition": f'attachment; filename="{safe_fn_gl}"'})

            # Fallback: best muxed format (e.g. format 18 / 360p)
            best_muxed = None
            if muxed_fmts:
                muxed_fmts.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
                best_muxed = muxed_fmts[0]
            direct_url  = best_muxed["url"] if best_muxed else (info.get("url") or None)
            cdn_headers = (best_muxed.get("http_headers") or {}) if best_muxed else (info.get("http_headers") or {})
            if not direct_url:
                # Debug info for troubleshooting
                n_all = len(all_fmts)
                n_url = len([f for f in all_fmts if f.get("url")])
                return f"could not extract youtube url (formats={n_all}, with_url={n_url})", 422

        except Exception as exc:
            return str(exc), 422

    else:
        # ── Instagram / Twitter: get muxed CDN URL and proxy it ──────────────
        ydl_opts_extract = {
            "quiet"        : True,
            "no_warnings"  : True,
            "format"       : ("best[ext=mp4][vcodec^=avc1][acodec^=mp4a]"
                              "/best[ext=mp4][vcodec^=avc][acodec^=mp4a]"
                              "/best[ext=mp4][vcodec!*=none][acodec!*=none]"
                              "/best[vcodec!*=none][acodec!*=none]/best"),
            "http_headers" : {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.0 Mobile/15E148 Safari/604.1"
                )
            },
            **_cookie_opts(),
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts_extract) as ydl:
                info = ydl.extract_info(url, download=False)

            title = info.get("title", "video")
            safe_fn_gl = re.sub(r'[^\w .-]', '_', title)[:60].strip() + ".mp4"
            print(f"  [getlink] streaming bytes for: {title}")

            direct_url  = info.get("url")
            cdn_headers = info.get("http_headers") or {}

            if not direct_url and info.get("requested_formats"):
                for rf in info["requested_formats"]:
                    if rf.get("url"):
                        direct_url  = rf["url"]
                        cdn_headers = rf.get("http_headers") or {}
                        break

            if not direct_url and info.get("formats"):
                h264 = [f for f in info["formats"]
                        if f.get("url")
                        and (f.get("vcodec") or "").startswith("avc")
                        and f.get("acodec") not in ("none", None, "")]
                if h264:
                    chosen      = max(h264, key=lambda f: f.get("tbr") or 0)
                    direct_url  = chosen["url"]
                    cdn_headers = chosen.get("http_headers") or {}

            if not direct_url:
                return "could not extract url", 422

        except Exception as exc:
            return str(exc), 422

    # ── Stream bytes to caller ────────────────────────────────────────────────
    import urllib.request as _urlreq2
    req_hdrs = {k: v for k, v in (cdn_headers or {}).items()
                if k.lower() not in ("accept-encoding", "host")}
    req_gl = _urlreq2.Request(direct_url, headers=req_hdrs)
    try:
        resp_gl = _urlreq2.urlopen(req_gl, timeout=120)
    except Exception as exc:
        return str(exc), 422

    def _gen_gl():
        try:
            while True:
                chunk = resp_gl.read(262144)
                if not chunk:
                    break
                yield chunk
        finally:
            resp_gl.close()

    resp_headers_gl = {
        "Content-Disposition": f'attachment; filename="{safe_fn_gl}"',
        "Accept-Ranges": "bytes",
    }
    for h in ("Content-Length", "Content-Type"):
        v = resp_gl.headers.get(h)
        if v:
            resp_headers_gl[h] = v
    return Response(_gen_gl(), status=200, mimetype="video/mp4",
                    headers=resp_headers_gl)


@app.route("/quicksave", methods=["GET", "POST"])
def quicksave():
    """
    Single endpoint for iOS Shortcut.
    Downloads the video via yt-dlp, saves to PC disk, returns video bytes.
    Shortcut: Get Contents of URL → Save to Photo Album
    """
    key = request.args.get("k", "")
    if not key or not secrets.compare_digest(key, API_KEY):
        abort(401)
    if _is_rate_limited(_client_ip()):
        return jsonify({"error": "Too many requests. Try again later."}), 429

    # Debug: log the full raw request for troubleshooting
    raw_qs = request.query_string.decode("utf-8", errors="replace")
    raw_url = request.url
    print(f"  [quicksave] full URL: {raw_url}")
    print(f"  [quicksave] query string: {raw_qs}")
    print(f"  [quicksave] method: {request.method}")
    if request.data:
        print(f"  [quicksave] body: {request.get_data(as_text=True)[:500]}")

    # Extract URL: try raw query string first (avoids &/? in video URL breaking parsing)
    url = ""
    marker = "&url="
    idx = raw_qs.find(marker)
    if idx != -1:
        url = raw_qs[idx + len(marker):]
    # Also try "url=" at start of query string (if url is the first param)
    if not url:
        marker2 = "url="
        if raw_qs.startswith(marker2):
            url = raw_qs[len(marker2):]
        else:
            idx2 = raw_qs.find("&" + marker2)
            if idx2 != -1:
                url = raw_qs[idx2 + 1 + len(marker2):]
    # Fallback: POST JSON body or form body
    if not url and request.method == "POST":
        jb = request.get_json(silent=True)
        url = (jb or {}).get("url", "")
        if not url:
            url = request.form.get("url", "")
        if not url:
            # Maybe the entire body IS the URL (plain text)
            body = request.get_data(as_text=True).strip()
            if body.startswith("http"):
                url = body
            else:
                m = re.search(r'https?://\S+', body)
                if m:
                    url = m.group(0)
    # Fallback: standard query param
    if not url:
        url = request.args.get("url", "")
    url = url.strip()

    # If URL is percent-encoded, decode it
    from urllib.parse import unquote
    if "%3A" in url or "%2F" in url:
        url = unquote(url)

    # Try to extract a URL from text (shortcut might send "Look at this https://...")
    if url and not url.startswith("http"):
        m = re.search(r'https?://\S+', url)
        if m:
            url = m.group(0)
        else:
            url = ""

    print(f"  [quicksave] extracted url: {url}")

    if not url:
        return jsonify({"error": "no url"}), 400

    platform = detect_platform(url)
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    out_dir  = os.path.join(SAVE_DIR, platform)
    os.makedirs(out_dir, exist_ok=True)

    _ffmpeg = _find_ffmpeg()

    # ── Extract direct CDN URL (no download) ─────────────────────────────
    # Like savefrom.net: get the direct video link, send it to phone.
    # Phone downloads directly from CDN = instant start, max speed.
    ydl_opts_extract = {
        "quiet"        : True,
        "no_warnings"  : True,
        # Force H.264+AAC — plays on every device without re-encoding
        "format"       : ("best[ext=mp4][vcodec^=avc1][acodec^=mp4a]"
                          "/best[ext=mp4][vcodec^=avc][acodec^=mp4a]"
                          "/best[ext=mp4][vcodec!*=none][acodec!*=none]"
                          "/best[vcodec!*=none][acodec!*=none]/best"),
        "http_headers" : {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            )
        },
        **_cookie_opts(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts_extract) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get("title", "video")

        # Find the direct CDN URL (muxed format preferred)
        direct_url = None
        cdn_headers = {}

        # Strategy 1: best muxed candidate with both video+audio
        if info.get("formats"):
            candidates = [
                f for f in info["formats"]
                if f.get("url")
                and f.get("vcodec") not in ("none", None, "")
                and f.get("acodec") not in ("none", None, "")
            ]
            if candidates:
                chosen = max(candidates, key=lambda f: f.get("tbr") or f.get("quality") or 0)
                direct_url = chosen["url"]
                cdn_headers = chosen.get("http_headers") or {}

        # Strategy 2: info["url"] (some extractors)
        if not direct_url and info.get("url"):
            direct_url = info["url"]
            cdn_headers = info.get("http_headers") or {}

        # Strategy 3: requested_formats
        if not direct_url and info.get("requested_formats"):
            for rf in info["requested_formats"]:
                if rf.get("url"):
                    direct_url = rf["url"]
                    cdn_headers = rf.get("http_headers") or {}
                    break

        # Strategy 4: any format with a url
        if not direct_url and info.get("formats"):
            for f in reversed(info["formats"]):
                if f.get("url"):
                    direct_url = f["url"]
                    cdn_headers = f.get("http_headers") or {}
                    break

        if not direct_url:
            return jsonify({"error": "Could not extract direct video URL"}), 422

        print(f"  [quicksave] got direct CDN URL for: {title}")

        # ── Redirect phone directly to CDN ───────────────────────────────
        # Phone downloads at full CDN speed, no PC bottleneck.
        from flask import redirect
        return redirect(direct_url)

    except Exception as exc:
        return jsonify({"error": str(exc)}), 422


@app.route("/download", methods=["POST"])
def download_media():
    """
    Body (JSON):  { "url": "<media_url>" }
    Header:       X-API-Key: <your key>
    Returns JSON: { "status": "success", "title": "...", "platform": "...",
                    "filename": "...", "file_path": "/file/<name>" }
    """
    require_api_key()

    data = request.get_json(silent=True)
    if not data or "url" not in data:
        return jsonify({"error": "No URL provided"}), 400

    url      = data["url"].strip()
    platform = detect_platform(url)

    # Unique sub-folder keeps filenames collision-free
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    out_dir  = os.path.join(SAVE_DIR, platform)
    os.makedirs(out_dir, exist_ok=True)

    _ffmpeg = _find_ffmpeg()

    ydl_opts = {
        "outtmpl"             : os.path.join(out_dir, f"%(title).60s_{url_hash}.%(ext)s"),
        "format"              : "bestvideo+bestaudio/best",
        "merge_output_format" : "mp4",
        "ffmpeg_location"     : _ffmpeg,
        "quiet"               : True,
        "no_warnings"         : True,
        "http_headers"        : {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            )
        },
        **_cookie_opts(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info     = ydl.extract_info(url, download=True)
            title    = info.get("title", "Unknown")
            filepath = ydl.prepare_filename(info)

            # yt-dlp may change extension after merging
            if not os.path.exists(filepath):
                filepath = os.path.splitext(filepath)[0] + ".mp4"

            filename = os.path.relpath(filepath, SAVE_DIR).replace("\\", "/")

        log_download(url, platform, title, os.path.basename(filepath), filepath, "success")

        return jsonify({
            "status"    : "success",
            "title"     : title,
            "platform"  : platform,
            "filename"  : os.path.basename(filepath),
            "file_path" : f"/file/{filename}",
        })

    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        log_download(url, platform, "", "", "", f"error: {msg}")
        return jsonify({"error": msg}), 422
    except Exception as exc:
        msg = str(exc)
        log_download(url, platform, "", "", "", f"error: {msg}")
        return jsonify({"error": msg}), 500


@app.route("/file/<path:filename>", methods=["GET"])
def serve_file(filename: str):
    """Serve a downloaded file back to the iPhone for gallery saving."""
    require_api_key()

    # Sanitise path — prevent directory traversal
    safe_path = os.path.normpath(os.path.join(SAVE_DIR, filename))
    if not safe_path.startswith(os.path.normpath(SAVE_DIR)):
        abort(400, description="Invalid file path.")

    if not os.path.isfile(safe_path):
        return jsonify({"error": "File not found"}), 404

    return send_file(safe_path, mimetype="video/mp4", as_attachment=False)


@app.route("/", methods=["GET"])
def home():
    ip  = _local_ip()
    key = API_KEY
    # Relative URL works whether served via cloudflare tunnel or local IP
    save_api = f"/save?k={key}"

    tunnel_banner = ""
    if not _public_url:
        tunnel_banner = """
  <div style="background:#fff8e1;border-left:4px solid #ff9500;border-radius:10px;
              padding:10px 14px;font-size:.83em;color:#6e6e73;width:100%;max-width:430px">
    &#9203; MediaSaver tunnel connecting&hellip; <a onclick="location.reload()" href="#"
    style="color:#007aff;text-decoration:none;font-weight:600">Refresh</a> in a few seconds.
  </div>"""

    return Response(f"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="MediaSaver">
  <title>MediaSaver</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,Helvetica,sans-serif;background:#f2f2f7;
          display:flex;flex-direction:column;align-items:center;
          padding:28px 16px 56px;color:#1c1c1e;gap:14px}}
    .card{{background:#fff;border-radius:18px;padding:20px 18px;width:100%;max-width:430px;
           box-shadow:0 2px 14px rgba(0,0,0,.07)}}
    h1{{font-size:1.45em;text-align:center}}
    .sub{{font-size:.86em;color:#6e6e73;text-align:center;margin-top:4px}}
    textarea{{width:100%;height:90px;border:1.5px solid #d1d1d6;border-radius:12px;
              padding:10px 12px;font-size:1em;font-family:inherit;resize:none;
              outline:none;margin:12px 0 10px;color:#1c1c1e}}
    textarea:focus{{border-color:#007aff}}
    .btn{{display:block;width:100%;color:#fff;text-align:center;padding:14px;
          border:none;border-radius:14px;font-size:1.05em;font-weight:700;
          cursor:pointer;text-decoration:none;font-family:inherit}}
    .btn-green{{background:#34c759}} .btn-blue{{background:#007aff}}
    .btn:active{{opacity:.82}}
    #result{{margin-top:12px;padding:10px 12px;border-radius:10px;font-size:.9em;
             display:none;word-break:break-word;line-height:1.5}}
    .ok{{background:#edfaf1;color:#1a7a3a;border:1px solid #b7e4c7}}
    .err{{background:#ffeef0;color:#b22;border:1px solid #f5c6cb}}
    .loading{{background:#f0f4ff;color:#007aff;border:1px solid #c5d5f7}}
    .row{{display:flex;gap:11px;align-items:flex-start;margin:9px 0}}
    .num{{background:#007aff;color:#fff;border-radius:50%;min-width:26px;height:26px;
          display:flex;align-items:center;justify-content:center;
          font-size:.79em;font-weight:700;flex-shrink:0;margin-top:1px}}
    .row p{{font-size:.86em;line-height:1.5}}
    small{{color:#6e6e73;font-size:.8em}}
    hr{{border:none;border-top:1px solid #e5e5ea;margin:14px 0}}
  </style>
</head><body>
{tunnel_banner}
  <!-- HEADER -->
  <div style="text-align:center;padding-top:4px">
    <div style="font-size:2.6em">&#127909;</div>
    <h1>MediaSaver</h1>
    <p class="sub">by Tanish Nagar</p>
    <p class="sub" style="margin-top:2px">Save Instagram Reels, YouTube &amp; Twitter/X videos</p>
  </div>

  <!-- PASTE & SAVE CARD -->
  <div class="card">
    <p style="font-weight:700;font-size:.95em">&#128203; Paste video URL</p>
    <textarea id="vurl" placeholder="Paste Instagram / YouTube / Twitter URL here&hellip;"
              autocorrect="off" autocapitalize="none" spellcheck="false"></textarea>

    <button class="btn btn-green" onclick="streamVideo()">
      &#11015; Save to Device
    </button>

    <div id="result"></div>
  </div>

  <!-- HOW TO USE -->
  <div class="card">
    <p style="font-weight:700;font-size:.93em;margin-bottom:6px">How to use</p>
    <div class="row"><div class="num">1</div><p>Open any Reel, YouTube video or Twitter/X video</p></div>
    <div class="row"><div class="num">2</div><p>Tap <b>Share</b> &rarr; <b>Copy Link</b></p></div>
    <div class="row"><div class="num">3</div><p>Come back here, paste &amp; tap <b>Download to Phone</b></p></div>
    <hr>
    <p style="font-size:.82em;color:#6e6e73">&#128161; Safari Share &rarr; <b>Add to Home Screen</b> for one-tap access.</p>
  </div>

  <div style="font-size:.72em;color:#c7c7cc;text-align:center">
    MediaSaver&trade; by Tanish Nagar
  </div>
  <div id="backend-badge" style="font-size:.75em;text-align:center;margin-top:4px;color:#8e8e93">
    checking backend&hellip;
  </div>

  <script>
  const KEY = '{key}';

  // Check if PC is online (informational badge only — routing is server-side)
  async function checkPC() {{
    const badge = document.getElementById('backend-badge');
    try {{
      const r = await fetch('/_pc_status', {{signal: AbortSignal.timeout(5000)}});
      const d = await r.json();
      if (d.pc_online) {{
        badge.innerHTML = '&#9989; Connected to your PC';
        badge.style.color = '#34c759';
      }} else {{
        badge.innerHTML = '&#9898; Using cloud server (PC offline)';
        badge.style.color = '#8e8e93';
      }}
    }} catch(e) {{
      badge.innerHTML = '&#9898; Using cloud server';
      badge.style.color = '#8e8e93';
    }}
  }}

  function setRes(cls, html) {{
    const r = document.getElementById('result');
    r.className = cls; r.style.display = 'block'; r.innerHTML = html;
  }}

  function isYouTube(url) {{
    return /youtube[.]com|youtu[.]be/i.test(url);
  }}

  async function streamVideo(format_id, qk) {{
    const url = document.getElementById('vurl').value.trim();
    if (!url) {{ document.getElementById('vurl').focus(); return; }}

    // YouTube with no quality chosen → fetch quality list first
    if (isYouTube(url) && !format_id) {{
      setRes('loading', '&#9203; Fetching available qualities&hellip;');
      try {{
        const r = await fetch('/qualities?k=' + KEY, {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{url}})
        }});
        const data = await r.json();
        if (data.error) throw new Error(data.error);
        if (data.youtube && data.options && data.options.length) {{
          const btns = data.options.map(o =>
            `<button onclick="streamVideo('${{o.format_id}}','${{data.qk}}')" style="
              display:inline-block;margin:4px 3px;padding:9px 16px;
              background:#007aff;color:#fff;border:none;border-radius:10px;
              font-size:.92em;font-weight:600;cursor:pointer;
            ">${{o.label}}</button>`
          ).join('');
          setRes('loading',
            `<b>Choose quality:</b><br><span style="font-size:.85em;color:#3c3c43">${{data.title}}</span>
            <div style="margin-top:10px;line-height:2.4">${{btns}}</div>`);
          return;
        }}
      }} catch(e) {{
        setRes('err', '&#10060; ' + (e.message || 'Could not fetch qualities'));
        return;
      }}
    }}

    // YouTube with quality chosen → POST (instant), navigate to stream URL
    if (isYouTube(url)) {{
      setRes('loading', '&#9203; Preparing download&hellip;');
      try {{
        const body = {{url}};
        if (format_id) body.format_id = format_id;
        if (qk) body.qk = qk;
        const r = await fetch('/stream?k=' + KEY, {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(body)
        }});
        if (!r.ok) throw new Error(await r.text());
        const j = await r.json();
        if (j.error) throw new Error(j.error);
        document.getElementById('vurl').value = '';
        setRes('ok', '&#9989; Starting download&hellip;');
        window.location.href = j.proxy_url || j.stream_url;
      }} catch(e) {{
        setRes('err', '&#10060; ' + (e.message || 'Failed to start download'));
      }}
      return;
    }}

    // Instagram / Twitter → proxy through server
    setRes('loading', '&#9203; Getting video link&hellip;');
    try {{
      const r = await fetch('/stream?k=' + KEY, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{url}})
      }});
      if (!r.ok) throw new Error(await r.text());
      const j = await r.json();
      if (j.error) throw new Error(j.error);
      document.getElementById('vurl').value = '';
      setRes('ok', '&#9989; Starting download&hellip;');
      window.location.href = j.proxy_url;
    }} catch(e) {{
      setRes('err', '&#10060; ' + (e.message || 'Could not get video link'));
    }}
  }}

  document.addEventListener('DOMContentLoaded', () => {{
    checkPC();  // check if PC is online on page load
    setInterval(checkPC, 120000);  // re-check every 2 minutes
    document.getElementById('vurl').addEventListener('keydown', e => {{
      if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); streamVideo(); }}
    }});
  }});
  </script>
</body></html>""", mimetype="text/html")

@app.route("/get-shortcut", methods=["GET"])
def get_shortcut_public():
    """
    Public endpoint fetched by the Shortcuts deep link.
    Uses the public tunnel URL so the shortcut talks to the PC from anywhere.
    """
    base = _public_url if _public_url else f"http://{_local_ip()}:{PORT}"
    plist_bytes = build_shortcut_plist_url(base, API_KEY)
    return Response(
        plist_bytes,
        status=200,
        mimetype="application/x-shortcuts",
        headers={
            "Content-Disposition": 'attachment; filename="MediaSaver.shortcut"',
            "Content-Length": str(len(plist_bytes)),
            "Cache-Control": "no-store",
        }
    )


@app.route("/install", methods=["GET"])
def install_shortcut():
    """Legacy direct-download (fallback, requires API key)."""
    require_api_key()
    plist_bytes = build_shortcut_plist(_local_ip(), PORT, API_KEY)
    return Response(
        plist_bytes,
        status=200,
        mimetype="application/x-shortcuts",
        headers={"Content-Disposition": 'attachment; filename="MediaSaver.shortcut"'}
    )


@app.route("/history", methods=["GET"])
def history():
    """Return the last 100 downloads from the database."""
    require_api_key()
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.execute(
        "SELECT id,url,platform,title,filename,status,downloaded_at "
        "FROM downloads ORDER BY downloaded_at DESC LIMIT 100"
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    return jsonify(rows)


# ─── Entry point ──────────────────────────────────────────────────────────────

def _self_ping_loop():
    """Keep Render free tier awake by pinging our own URL every 4 minutes."""
    if not RENDER_URL:
        return
    import urllib.request as _ur
    time.sleep(60)  # wait for server to fully start
    while True:
        try:
            _ur.urlopen(f"{RENDER_URL}/status", timeout=10)
        except Exception:
            pass
        time.sleep(240)  # 4 minutes


def _pc_heartbeat_loop():
    """PC sends its tunnel URL to Render every 2 minutes so Render knows PC is online."""
    if IS_RENDER or not RENDER_URL:
        return
    import urllib.request as _ur
    time.sleep(30)  # wait for tunnel to establish
    while True:
        tunnel = _public_url
        if tunnel:
            try:
                payload = json.dumps({"tunnel_url": tunnel}).encode()
                req = _ur.Request(
                    f"{RENDER_URL}/_pc_heartbeat?k={API_KEY}",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                _ur.urlopen(req, timeout=15)
            except Exception:
                pass
        time.sleep(120)  # 2 minutes


if __name__ == "__main__":
    init_db()

    # Start cloudflared tunnel in background thread
    t = threading.Thread(target=_start_cloudflared, daemon=True)
    t.start()

    # Keep Render free tier alive (only runs on Render itself)
    if IS_RENDER and RENDER_URL:
        threading.Thread(target=_self_ping_loop, daemon=True).start()

    # PC → Render heartbeat (tells Render our tunnel URL so it can proxy)
    if not IS_RENDER and RENDER_URL:
        threading.Thread(target=_pc_heartbeat_loop, daemon=True).start()

    print("=" * 56)
    print("  MediaSaver by Tanish Nagar")
    print("=" * 56)
    print(f"  Save directory : {SAVE_DIR}")
    print(f"  Port           : {PORT}")
    print(f"  API Key        : {API_KEY}")
    print()
    print(f"  Local install  : http://{_local_ip()}:{PORT}")
    print("  Public URL     : starting tunnel... (wait ~10 sec)")
    print()
    print("  Open the Local or Public URL in Safari on iPhone.")
    print("=" * 56)

    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT,
          threads=64,                # 64 worker threads (handles 64 concurrent downloads)
          channel_timeout=300,       # 5 min timeout per connection (large videos)
          recv_bytes=262144,         # 256 KB recv buffer
          send_bytes=262144,         # 256 KB send buffer
          connection_limit=2000,     # max simultaneous TCP connections
          cleanup_interval=10000,    # cleanup stale connections every 10s
          ident="MediaSaver",        # server header
    )
