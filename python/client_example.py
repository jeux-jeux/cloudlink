# proxy.py
import os
import requests
import asyncio
import threading
import random
import traceback
from flask import Flask, request, jsonify
from cloudlink import client as cl_client

app = Flask(__name__)

# === Configuration (env) ===
# REQUIRED:
DISCOVERY_URL = os.getenv("DISCOVERY_URL", "").strip()   # ex: https://example.com/get-server
CLOUDLINK_KEY = os.getenv("CLOUDLINK_KEY", "").strip()   # cle secrète à envoyer à DISCOVERY_URL

# Optional: keep it empty — we generate a username each request
# PROXY_USERNAME_FIXED = os.getenv("PROXY_USERNAME", "").strip()

# === Helpers ===
def discover_cloudlink_url():
    """POST {cle: CLOUDLINK_KEY} to DISCOVERY_URL and return web_socket_server string."""
    if not DISCOVERY_URL or not CLOUDLINK_KEY:
        return None, "DISCOVERY_URL or CLOUDLINK_KEY not set"
    try:
        resp = requests.post(DISCOVERY_URL, json={"cle": CLOUDLINK_KEY}, timeout=8)
        resp.raise_for_status()
        j = resp.json()
        url = j.get("web_socket_server")
        if not url:
            return None, "discovery response missing 'web_socket_server'"
        return url, None
    except Exception as e:
        return None, f"discovery error: {str(e)}"

# === Core: connect -> do action -> disconnect (no timeout) ===
async def cloudlink_action_async(action_coro):
    """
    action_coro: async function(client, username)
    No external timeout — waits until disconnect occurs.
    Returns dict with status and username (and error detail on failure).
    """
    url, err = discover_cloudlink_url()
    if not url:
        return {"status": "error", "message": "discovery_failed", "detail": err}

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
                client.disconnect()
            except Exception:
                pass

    @client.on_disconnect
    async def _on_disconnect():
        finished.set()

    # run the cloudlink client (blocking) in a background thread
    thread = threading.Thread(target=lambda: client.run(host=url), daemon=True)
    thread.start()

    # WAIT INDEFINITELY for disconnect (user requested no timeout)
    await finished.wait()

    if result["ok"]:
        return {"status": "ok", "username": result.get("username")}
    else:
        return {"status": "error", "username": result.get("username"), "detail": result.get("error")}

def cloudlink_action(action_coro):
    return asyncio.run(cloudlink_action_async(action_coro))

# === Routes ===

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
# simple health
@app.route("/_health", methods=["GET"])
def health():
    return jsonify({"status":"ok"})

# === Launch ===
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)