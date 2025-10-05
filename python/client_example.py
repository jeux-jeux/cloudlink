import os
import asyncio
import threading
import random
import traceback
import functools
from urllib.parse import urlsplit
from flask import Flask, request, jsonify
from cloudlink import client as cl_client
import websockets
import logging
import time
import requests

app = Flask(__name__)
app.logger.setLevel(logging.DEBUG)

# -------------------------
# Configuration
# -------------------------
def check_key(data):
    """Vérifie que le corps JSON contient une clé 'cle' valide."""
    expected = os.getenv("CLE")
    received = data.get("cle")
    if not expected or received != expected:
        return False
    return True

def fetch_cloudlink_ws_url():
    try:
        proxy_auth = os.getenv("PROXY")
        response = requests.get(proxy_auth, timeout=5)
        response.raise_for_status()
        data = response.json()
        url = data.get("web_socket_server")
        if not url:
            print("⚠️ Aucun champ 'web_socket_server' trouvé dans la réponse.")
            return "Pas de json dans la reponse du proxy authentification !"
        print(f"✅ web_socket_url obtenu : {url}")
        return url
    except Exception as e:
        print(f"⚠️ Impossible de récupérer l'URL depuis le proxy : {e}")
        return "error : immpossible to get cloudlink_url"

# En-têtes WebSocket
WS_EXTRA_HEADERS = [
    ("Origin", "https://cloudlink-manager.onrender.com/"),
    ("User-Agent", "turbowarp-desktop/1.14.4")
]

# Timeouts (en secondes)
USERNAME_TIMEOUT = 5
ACTION_TIMEOUT = 6
TOTAL_ACTION_TIMEOUT = 10

# -------------------------
# Helpers
# -------------------------
def sanitize_ws_url(url: str) -> str:
    if not url:
        return url
    url = url.replace("\\", "").strip()
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    parts = urlsplit(url)
    if not parts.scheme:
        url = "wss://" + url.lstrip("/")
    url = url.rstrip("/") + "/"
    return url

def ws_handshake_test_sync(url: str, extra_headers=None, timeout=6):
    async def _attempt():
        out = {"ok": False, "exc": None, "status_code": None, "response_headers": None}
        try:
            async with websockets.connect(url, extra_headers=extra_headers, open_timeout=timeout) as ws:
                out["ok"] = True
                return out
        except Exception as e:
            out["exc"] = repr(e)
            out["status_code"] = getattr(e, "status_code", None)
            headers = getattr(e, "response_headers", None) or getattr(e, "headers", None)
            try:
                out["response_headers"] = dict(headers) if headers else None
            except Exception:
                out["response_headers"] = str(headers)
            return out

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_attempt())
    finally:
        try:
            loop.close()
        except Exception:
            pass

# -------------------------
# Core CloudLink runner (avec timeouts)
# -------------------------
async def cloudlink_action_async(action_coro, ws_url, total_timeout=TOTAL_ACTION_TIMEOUT):
    app.logger.debug("proxy: cloudlink_action_async start")
    client = cl_client()
    finished_thread = threading.Event()
    result = {"ok": False, "error": None, "username": None, "trace": None}

    @client.on_connect
    async def _on_connect():
        app.logger.debug("proxy: on_connect called inside client loop")
        try:
            username = str(random.randint(100_000_000, 999_999_999))
            result["username"] = username
            await asyncio.wait_for(client.protocol.set_username(username), timeout=USERNAME_TIMEOUT)
            await asyncio.wait_for(action_coro(client, username), timeout=ACTION_TIMEOUT)
            await asyncio.sleep(0.15)
            result["ok"] = True
        except Exception as e:
            result["error"] = str(e)
            result["trace"] = traceback.format_exc()
            app.logger.exception("proxy: exception in client on_connect")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @client.on_disconnect
    async def _on_disconnect():
        finished_thread.set()

    def run_client():
        try:
            client.ws.connect = functools.partial(websockets.connect, extra_headers=WS_EXTRA_HEADERS)
            client.run(host=ws_url)
            finished_thread.set()
        except Exception as e:
            result["error"] = str(e)
            result["trace"] = traceback.format_exc()
            finished_thread.set()

    thread = threading.Thread(target=run_client, daemon=True)
    thread.start()
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(None, finished_thread.wait), timeout=total_timeout)
    except asyncio.TimeoutError:
        return {"status": "error", "username": result.get("username"), "detail": "timeout waiting for disconnect"}
    if result.get("ok"):
        return {"status": "ok", "username": result.get("username")}
    else:
        return {"status": "error", "username": result.get("username"), "detail": result.get("error")}

def cloudlink_action(action_coro):
    raw = fetch_cloudlink_ws_url()
    ws_url = sanitize_ws_url(raw)
    try:
        return asyncio.run(cloudlink_action_async(action_coro, ws_url))
    except Exception as e:
        return {"status": "error", "message": "internal_error", "detail": str(e)}

# -------------------------
# Routes principales
# -------------------------
@app.route("/sending/global-message", methods=["POST"])
def route_global_message():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    rooms = data.get("rooms")
    message = data.get("message")
    if not isinstance(rooms, list) or not message:
        return jsonify({"status": "error", "message": "rooms (list) and message required"}), 400
    async def action(client, username):
        client.send_packet({"cmd": "gmsg", "val": message, "rooms": rooms})
    return jsonify(cloudlink_action(action))

@app.route("/sending/private-message", methods=["POST"])
def route_private_message():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    username_target = data.get("username")
    room = data.get("room")
    message = data.get("message")
    if not username_target or not room or not message:
        return jsonify({"status": "error", "message": "username, room and message required"}), 400
    async def action(client, username):
        client.send_packet({"cmd": "pmsg", "val": message, "id": username_target, "room": room})
    return jsonify(cloudlink_action(action))

@app.route("/sending/global-variable", methods=["POST"])
def route_global_variable():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")
    if not room or name is None:
        return jsonify({"status": "error", "message": "room and name required"}), 400
    async def action(client, username):
        client.send_packet({"cmd": "gvar", "name": name, "val": val, "room": room})
    return jsonify(cloudlink_action(action))

@app.route("/sending/private-variable", methods=["POST"])
def route_private_variable():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    username_target = data.get("username")
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")
    if not username_target or not room or name is None:
        return jsonify({"status": "error", "message": "username, room and name required"}), 400
    async def action(client, username):
        client.send_packet({"cmd": "pvar", "name": name, "val": val, "room": room, "id": username_target})
    return jsonify(cloudlink_action(action))

@app.route("/deleter", methods=["POST"])
def route_kick_client():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    room = data.get("room")
    targets = data.get("targets")
    if not room or not isinstance(targets, list) or not targets:
        return jsonify({"status": "error", "message": "room and targets (list) required"}), 400
    secret = os.getenv("ADMIN_SECRET", "").strip()
    async def action(client, username):
        payload = {"cmd": "kick", "room": room, "targets": targets}
        if secret:
            payload["secret"] = secret
        client.send_packet(payload)
    return jsonify(cloudlink_action(action))

# -------------------------
# Health & Debug
# -------------------------
@app.route("/checking/health", methods=["POST"])
def health():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    return jsonify({"status": "ok"})

@app.route("/checking", methods=["POST"])
def home():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    return "Serveur HTTP en ligne ✅"

@app.route("/checking/handshake", methods=["POST"])
def debug_handshake():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    raw = fetch_cloudlink_ws_url()
    url = sanitize_ws_url(raw)
    tests = {
        "default": ws_handshake_test_sync(url),
        "origin": ws_handshake_test_sync(url, extra_headers=[("Origin", "https://cloudlink-manager.onrender.com/")]),
        "origin+ua": ws_handshake_test_sync(url, extra_headers=WS_EXTRA_HEADERS)
    }
    return jsonify({"ws_url": url, "tests": tests})

@app.route("/checking/connect-client", methods=["POST"])
def debug_connect_client():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    raw = fetch_cloudlink_ws_url()
    ws_url = sanitize_ws_url(raw)
    timeout = int(request.args.get("timeout", str(TOTAL_ACTION_TIMEOUT)))

    result = {"ok": False, "error": None, "trace": None}
    def run_client_and_capture():
        client = cl_client()
        finished_flag = threading.Event()

        @client.on_connect
        async def _on_connect():
            try:
                username = str(random.randint(100_000_000, 999_999_999))
                await client.protocol.set_username(username)
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
            finished_flag.set()

        try:
            client.ws.connect = functools.partial(websockets.connect, extra_headers=WS_EXTRA_HEADERS)
            client.run(host=ws_url)
        except Exception as e:
            result["error"] = str(e)
            result["trace"] = traceback.format_exc()
            finished_flag.set()

    thread = threading.Thread(target=run_client_and_capture, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        return jsonify({"status": "timeout", "detail": f"Client still alive after {timeout}s", "result": result})
    else:
        return jsonify({"status": "finished", "result": result})

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.logger.info(f"proxy: starting app on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
