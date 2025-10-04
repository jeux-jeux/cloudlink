# proxy_http.py
import os
import asyncio
import threading
import random
import traceback
import functools
import time
from flask import Flask, request, jsonify
from cloudlink import client as cl_client
import websockets
from urllib.parse import urlsplit

app = Flask(__name__)

# URL du serveur CloudLink
CLOUDLINK_WS_URL = os.getenv("CLOUDLINK_WS_URL", "wss://cloudlink-server.onrender.com/")

# En-têtes WebSocket (comme TurboWarp)
WS_EXTRA_HEADERS = [
    ("Origin", "tw-editor://."),
    ("User-Agent", "turbowarp-desktop/1.14.4")
]

# -------------------------
# Helpers
# -------------------------
def sanitize_ws_url(url: str) -> str:
    """Nettoie et formate l’URL WebSocket"""
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
    """Teste un handshake WebSocket simple"""
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
        loop.close()

# -------------------------
# CloudLink client runner
# -------------------------
async def cloudlink_action_async(action_coro, ws_url):
    client = cl_client()
    finished = asyncio.Event()
    result = {"ok": False, "error": None, "username": None}

    @client.on_connect
    async def _on_connect():
        try:
            username = str(random.randint(100_000_000, 999_999_999))
            result["username"] = username
            await client.protocol.set_username(username)
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

    def run_client():
        try:
            try:
                client.ws.connect = functools.partial(websockets.connect, extra_headers=WS_EXTRA_HEADERS)
            except Exception as e:
                print("Warning: failed to monkeypatch client.ws.connect ->", e)
            client.run(host=ws_url)
        except Exception as e:
            result["error"] = str(e)
            traceback.print_exc()
            finished.set()

    thread = threading.Thread(target=run_client, daemon=True)
    thread.start()

    await finished.wait()
    if result["ok"]:
        return {"status": "ok", "username": result.get("username")}
    else:
        return {"status": "error", "username": result.get("username"), "detail": result.get("error")}


def cloudlink_action(action_coro):
    raw = os.getenv("CLOUDLINK_WS_URL", CLOUDLINK_WS_URL)
    ws_url = sanitize_ws_url(raw)
    try:
        return asyncio.run(cloudlink_action_async(action_coro, ws_url))
    except Exception as e:
        return {"status": "error", "message": "internal_error", "detail": str(e)}

# -------------------------
# HTTP routes
# -------------------------

@app.route("/global-message", methods=["POST"])
def global_message():
    data = request.get_json(force=True, silent=True) or {}
    rooms = data.get("rooms")
    message = data.get("message")
    if not isinstance(rooms, list) or not message:
        return jsonify({"status": "error", "message": "rooms (list) and message required"}), 400

    async def action(client, username):
        await client.send_packet({"cmd": "gmsg", "val": message, "rooms": rooms})

    return jsonify(cloudlink_action(action))


@app.route("/private-message", methods=["POST"])
def private_message():
    data = request.get_json(force=True, silent=True) or {}
    username_target = data.get("username")
    room = data.get("room")
    message = data.get("message")
    if not username_target or not room or not message:
        return jsonify({"status": "error", "message": "username, room and message required"}), 400

    async def action(client, username):
        await client.send_packet({"cmd": "pmsg", "val": message, "uid": username_target, "room": room})

    return jsonify(cloudlink_action(action))


@app.route("/global-variable", methods=["POST"])
def global_variable():
    data = request.get_json(force=True, silent=True) or {}
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")
    if not room or name is None:
        return jsonify({"status": "error", "message": "room and name required"}), 400

    async def action(client, username):
        await client.send_packet({"cmd": "gvar", "name": name, "val": val, "room": room})

    return jsonify(cloudlink_action(action))


@app.route("/private-variable", methods=["POST"])
def private_variable():
    data = request.get_json(force=True, silent=True) or {}
    username_target = data.get("username")
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")
    if not username_target or not room or name is None:
        return jsonify({"status": "error", "message": "username, room and name required"}), 400

    async def action(client, username):
        await client.send_packet({"cmd": "pvar", "name": name, "val": val, "room": room, "uid": username_target})

    return jsonify(cloudlink_action(action))


@app.route("/_health")
def health():
    return jsonify({"status": "ok"})


@app.route("/")
def home():
    return "Serveur HTTP en ligne ✅"

# -------------------------
# Diagnostic endpoints
# -------------------------
@app.route("/debug-handshake", methods=["GET"])
def debug_handshake():
    raw = os.getenv("CLOUDLINK_WS_URL", CLOUDLINK_WS_URL)
    url = sanitize_ws_url(raw)
    tests = {
        "default": ws_handshake_test_sync(url, extra_headers=None),
        "origin": ws_handshake_test_sync(url, extra_headers=[("Origin", "tw-editor://.")]),
        "origin+ua": ws_handshake_test_sync(url, extra_headers=[("Origin", "tw-editor://."), ("User-Agent", "turbowarp-desktop/1.14.4")])
    }
    return jsonify({"ws_url": url, "tests": tests})


@app.route("/debug-connect-client", methods=["POST"])
def debug_connect_client():
    raw = os.getenv("CLOUDLINK_WS_URL", CLOUDLINK_WS_URL)
    ws_url = sanitize_ws_url(raw)
    timeout = int(request.args.get("timeout", "8"))

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
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
