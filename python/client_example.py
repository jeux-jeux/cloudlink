# proxy.py
import os
import requests
import asyncio
import threading
import random
import traceback
from flask import Flask, request, jsonify
from cloudlink import client as cl_client

# utilisé pour tester la handshake directement depuis Render
import websockets
from websockets.exceptions import InvalidStatusCode, InvalidHandshake

app = Flask(__name__)

# === Configuration (env) ===
DISCOVERY_URL = os.getenv("DISCOVERY_URL", "").strip()   # ex: https://example.com/get-server
CLOUDLINK_KEY = os.getenv("CLOUDLINK_KEY", "").strip()   # cle secrète à envoyer à DISCOVERY_URL

# Optional fixed fallback (dev). Leave empty in prod to force discovery.
FALLBACK_CLOUDLINK = os.getenv("FALLBACK_CLOUDLINK", "").strip()  # e.g. wss://cloudlink-server.onrender.com/

# === Helpers ===

def sanitize_ws_url(url: str) -> str:
    """Nettoie l'URL retournée par discovery.
       - Supprime backslashes éventuels
       - Ajoute 'wss://' si absent (si c'est nécessaire)
       - Assure un seul slash final (optionnel)
    """
    if not url:
        return url
    # Remove any stray backslashes
    url = url.replace("\\", "").strip()

    # If the value is a firebase URL or http(s) url used for storage, leave as-is (discovery might return multiple fields)
    # We only normalize if it looks like ws/wssecure or if it contains 'cloudlink' host.
    if not (url.startswith("ws://") or url.startswith("wss://")):
        # try to fix simple case where JSON returned 'wss://…' but escaping/formatting removed it:
        if url.startswith("://"):
            url = "wss" + url
        else:
            # if the returned string looks like a hostname without scheme, assume wss
            if url.startswith("cloudlink") or "onrender.com" in url or "onrender" in url:
                url = "wss://" + url.lstrip("/")
            # otherwise keep original (don't force incorrect scheme)
    # Normalize trailing slash: ensure either no trailing slash or exactly one (most ws libs accept both)
    url = url.rstrip("/") + "/"
    return url

def discover_cloudlink_url():
    """POST {cle: CLOUDLINK_KEY} to DISCOVERY_URL and return web_socket_server string."""
    # If DISCOVERY_URL not configured, fallback to env (if provided)
    if not DISCOVERY_URL:
        if FALLBACK_CLOUDLINK:
            return sanitize_ws_url(FALLBACK_CLOUDLINK), None
        return None, "DISCOVERY_URL not set"
    if not CLOUDLINK_KEY:
        return None, "CLOUDLINK_KEY not set"

    try:
        resp = requests.post(DISCOVERY_URL, json={"cle": CLOUDLINK_KEY}, timeout=8)
        resp.raise_for_status()
        j = resp.json()
        url = j.get("web_socket_server") or j.get("websocket") or j.get("url") or ""
        if not url:
            return None, f"discovery response missing 'web_socket_server' (got keys {list(j.keys())})"
        url = sanitize_ws_url(url)
        return url, None
    except Exception as e:
        return None, f"discovery error: {str(e)}"

# direct ws handshake test (useful to know exactly why server rejects)
def ws_handshake_test(url: str, extra_headers=None, timeout=6):
    """
    Attempt a websocket connection from this process and return diagnostic info.
    Runs in its own event loop to avoid interfering with Flask/Render loop.
    Returns a dict: {ok, error, exc_type, status_code, response_headers}
    """
    async def _attempt():
        res = {"ok": False, "error": None, "exc_type": None, "status_code": None, "response_headers": None}
        try:
            # websockets.connect accepts extra_headers as list of tuples or dict in recent versions
            async with websockets.connect(url, extra_headers=extra_headers, open_timeout=timeout) as ws:
                res["ok"] = True
                return res
        except InvalidStatusCode as e:
            res["exc_type"] = "InvalidStatusCode"
            res["error"] = str(e)
            # try to extract status_code / headers
            res["status_code"] = getattr(e, "status_code", None)
            headers = getattr(e, "response_headers", None) or getattr(e, "headers", None)
            try:
                res["response_headers"] = dict(headers) if headers else None
            except Exception:
                res["response_headers"] = str(headers)
            return res
        except InvalidHandshake as e:
            res["exc_type"] = "InvalidHandshake"
            res["error"] = str(e)
            return res
        except Exception as e:
            res["exc_type"] = type(e).__name__
            res["error"] = str(e)
            return res

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_attempt())
    finally:
        try:
            loop.close()
        except Exception:
            pass

# === Core: connect -> do action -> disconnect (no timeout) ===
async def cloudlink_action_async(action_coro, ws_url):
    """
    action_coro: async function(client, username)
    ws_url: sanitized websocket url to connect to
    """
    client = cl_client()
    finished = asyncio.Event()
    result = {"ok": False, "error": None, "username": None}

    @client.on_connect
    async def _on_connect():
        try:
            # generate random 9-digit username for this session
            username = str(random.randint(100_000_000, 999_999_999))
            result["username"] = username

            await client.protocol.set_username(username)
            # perform user action (send gmsg / pmsg / gvar / pvar)
            await action_coro(client, username)
            result["ok"] = True
        except Exception as e:
            result["error"] = str(e)
            traceback.print_exc()
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @client.on_disconnect
    async def _on_disconnect():
        finished.set()

    # run the cloudlink client (blocking) in a background thread
    def run_client():
        try:
            # The client.run method expects a host like "wss://host/". We pass ws_url.
            client.run(host=ws_url)
        except Exception as e:
            # capture errors in result so main thread can return them
            result["error"] = str(e)
            traceback.print_exc()

    thread = threading.Thread(target=run_client, daemon=True)
    thread.start()

    # WAIT INDEFINITELY for disconnect (user requested no timeout)
    await finished.wait()

    if result["ok"]:
        return {"status": "ok", "username": result.get("username")}
    else:
        return {"status": "error", "username": result.get("username"), "detail": result.get("error")}

def cloudlink_action(action_coro):
    """
    Flask-friendly wrapper:
     - discovers ws url
     - does a quick handshake test and returns detailed error if handshake fails
     - if handshake ok, runs the action and returns result
    """
    url, err = discover_cloudlink_url()
    if not url:
        return {"status": "error", "message": "discovery_failed", "detail": err}

    # quick diagnostic handshake (from this same Render instance)
    diag = ws_handshake_test(url, extra_headers=None, timeout=6)
    if not diag.get("ok"):
        # attach diag details to response so you see why the server rejects (502 etc)
        return {"status": "error", "message": "ws_handshake_failed", "detail": diag}

    # run real cloudlink action (this will create a thread and run the client)
    try:
        return asyncio.run(cloudlink_action_async(action_coro, url))
    except Exception as e:
        return {"status": "error", "message": "internal_error", "detail": str(e)}

# === Routes ===

@app.route("/debug-check", methods=["GET"])
def debug_check():
    """Return discovery URL and handshake diagnostics (useful to debug 502s)."""
    url, err = discover_cloudlink_url()
    out = {"discovery_url": DISCOVERY_URL, "ok": False, "detail": None}
    if not url:
        out["detail"] = err
        return jsonify(out)
    out["ok"] = True
    out["ws_url"] = url

    # run handshake tests: default + origin + ua
    out["tests"] = {
        "default": ws_handshake_test(url, extra_headers=None),
        "origin": ws_handshake_test(url, extra_headers=[("Origin", "tw-editor://.")]),
        "origin+ua": ws_handshake_test(url, extra_headers=[
            ("Origin", "tw-editor://."),
            ("User-Agent", "turbowarp-desktop/1.14.4")
        ])
    }
    return jsonify(out)

@app.route("/global-message", methods=["POST"])
def route_global_message():
    data = request.get_json(force=True, silent=True) or {}
    rooms = data.get("rooms")
    message = data.get("message")
    if not isinstance(rooms, list) or not message:
        return jsonify({"status":"error","message":"rooms (list) and message required"}), 400

    async def action(client, username):
        await client.protocol.send_gmsg(message, rooms=rooms)

    return jsonify(cloudlink_action(action))

@app.route("/private-message", methods=["POST"])
def route_private_message():
    data = request.get_json(force=True, silent=True) or {}
    username_target = data.get("username")
    room = data.get("room")
    message = data.get("message")
    if not username_target or not room or not message:
        return jsonify({"status":"error","message":"username, room and message required"}), 400

    async def action(client, username):
        await client.protocol.send_pmsg(username_target, room, message)

    return jsonify(cloudlink_action(action))

@app.route("/global-variable", methods=["POST"])
def route_global_variable():
    data = request.get_json(force=True, silent=True) or {}
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")
    if not room or name is None:
        return jsonify({"status":"error","message":"room and name required"}), 400

    async def action(client, username):
        await client.protocol.send_gvar(room, name, val)

    return jsonify(cloudlink_action(action))

@app.route("/private-variable", methods=["POST"])
def route_private_variable():
    data = request.get_json(force=True, silent=True) or {}
    username_target = data.get("username")
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")
    if not username_target or not room or name is None:
        return jsonify({"status":"error","message":"username, room and name required"}), 400

    async def action(client, username):
        await client.protocol.send_pvar(username_target, room, name, val)

    return jsonify(cloudlink_action(action))

@app.route("/")
def home():
    return "Serveur en ligne ✅"

@app.route("/_health", methods=["GET"])
def health():
    return jsonify({"status":"ok"})

# === Launch ===
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
