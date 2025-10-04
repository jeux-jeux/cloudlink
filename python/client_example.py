# proxy.py
import os
import requests
import asyncio
import threading
import random
import traceback
from flask import Flask, request, jsonify
from cloudlink import client as cl_client
import websockets
from websockets.exceptions import InvalidStatusCode, InvalidHandshake

app = Flask(__name__)

# === Configuration (env) ===
DISCOVERY_URL = os.getenv("DISCOVERY_URL", "").strip()   # ex: https://example.com/get-server
CLOUDLINK_KEY = os.getenv("CLOUDLINK_KEY", "").strip()   # cle secrète à envoyer à DISCOVERY_URL

# Fallback interne pour Render (évite le 502)
FALLBACK_CLOUDLINK = os.getenv("FALLBACK_CLOUDLINK", "").strip() or "ws://127.0.0.1:3000/"

# === Helpers ===
def sanitize_ws_url(url: str) -> str:
    if not url:
        return url
    url = url.replace("\\", "").strip()
    if not (url.startswith("ws://") or url.startswith("wss://")):
        url = "ws://" + url.lstrip("/")
    url = url.rstrip("/") + "/"
    return url

def discover_cloudlink_url():
    if not DISCOVERY_URL or not CLOUDLINK_KEY:
        return sanitize_ws_url(FALLBACK_CLOUDLINK), None

    try:
        resp = requests.post(DISCOVERY_URL, json={"cle": CLOUDLINK_KEY}, timeout=6)
        resp.raise_for_status()
        j = resp.json()
        url = j.get("web_socket_server") or j.get("websocket") or j.get("url") or ""
        if not url:
            return sanitize_ws_url(FALLBACK_CLOUDLINK), "discovery response missing 'web_socket_server'"
        return sanitize_ws_url(url), None
    except Exception as e:
        return sanitize_ws_url(FALLBACK_CLOUDLINK), f"discovery error: {str(e)}"

def ws_handshake_test(url: str, extra_headers=None, timeout=6):
    async def _attempt():
        res = {"ok": False, "error": None, "exc_type": None, "status_code": None, "response_headers": None}
        try:
            async with websockets.connect(url, extra_headers=extra_headers, open_timeout=timeout) as ws:
                res["ok"] = True
                return res
        except InvalidStatusCode as e:
            res["exc_type"] = "InvalidStatusCode"
            res["error"] = str(e)
            res["status_code"] = getattr(e, "status_code", None)
            headers = getattr(e, "response_headers", None) or getattr(e, "headers", None)
            res["response_headers"] = dict(headers) if headers else None
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
        try: loop.close()
        except Exception: pass

# === Core CloudLink action ===
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
            try: await client.disconnect()
            except Exception: pass

    @client.on_disconnect
    async def _on_disconnect():
        finished.set()

    def run_client():
        try:
            client.run(host=ws_url)
        except Exception as e:
            result["error"] = str(e)
            traceback.print_exc()

    thread = threading.Thread(target=run_client, daemon=True)
    thread.start()
    await finished.wait()
    if result["ok"]:
        return {"status": "ok", "username": result.get("username")}
    else:
        return {"status": "error", "username": result.get("username"), "detail": result.get("error")}

def cloudlink_action(action_coro):
    url, err = discover_cloudlink_url()
    if not url:
        return {"status":"error","message":"discovery_failed","detail":err}

    # Handshake test sans headers (plus fiable sur Render)
    diag = ws_handshake_test(url, extra_headers=None, timeout=6)
    if not diag.get("ok"):
        return {"status":"error","message":"ws_handshake_failed","detail":diag}

    try:
        return asyncio.run(cloudlink_action_async(action_coro, url))
    except Exception as e:
        return {"status":"error","message":"internal_error","detail":str(e)}

# === Routes ===
@app.route("/debug-check", methods=["GET"])
def debug_check():
    url, err = discover_cloudlink_url()
    out = {"discovery_url": DISCOVERY_URL, "ok": bool(url), "ws_url": url or None, "detail": err}
    out["tests"] = {
        "default": ws_handshake_test(url, extra_headers=None) if url else None
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