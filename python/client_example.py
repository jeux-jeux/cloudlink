# proxy.py
import os
import requests
import asyncio
import threading
import random
from flask import Flask, request, jsonify
from cloudlink import client as cl_client

app = Flask(__name__)

# ---------- Configuration via env ----------
CLOUDLINK_OVERRIDE_URL = os.getenv("CLOUDLINK_OVERRIDE_URL", "").strip()
DISCOVERY_URL = os.getenv("DISCOVERY_URL", "").strip()
CLOUDLINK_KEY = os.getenv("CLOUDLINK_KEY", "").strip()
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "").strip()  # optional fixed username
ACTION_TIMEOUT = float(os.getenv("ACTION_TIMEOUT", "15.0"))

# ---------- Utilities ----------
def discover_url():
    if not DISCOVERY_URL or not CLOUDLINK_KEY:
        return None
    try:
        resp = requests.post(DISCOVERY_URL, json={"cle": CLOUDLINK_KEY}, timeout=6)
        resp.raise_for_status()
        j = resp.json()
        return j.get("web_socket_server")
    except Exception as e:
        print("Discovery failed:", e)
        return None

def choose_cloudlink_url():
    if CLOUDLINK_OVERRIDE_URL:
        print("Using override CloudLink URL:", CLOUDLINK_OVERRIDE_URL)
        return CLOUDLINK_OVERRIDE_URL
    url = discover_url()
    if url:
        print("Discovered CloudLink URL:", url)
        return url
    return None

# ---------- Core: connect -> do action -> disconnect ----------
async def cloudlink_action_async(action_coro):
    """
    action_coro: async function(client, username) that performs the protocol action(s).
    Returns dict result (includes username used).
    """
    url = choose_cloudlink_url()
    if not url:
        return {"status": "error", "message": "No CloudLink URL available (set CLOUDLINK_OVERRIDE_URL or DISCOVERY_URL+CLOUDLINK_KEY)"}

    client = cl_client()
    finished = asyncio.Event()
    result_holder = {"ok": False, "error": None, "username": None}

    @client.on_connect
    async def _on_connect():
        try:
            # Determine username: use fixed PROXY_USERNAME if set, otherwise random 9-digit number
            if PROXY_USERNAME:
                username = PROXY_USERNAME
            else:
                username = str(random.randint(100_000_000, 999_999_999))
            result_holder["username"] = username

            await client.protocol.set_username(username)
            # execute provided coroutine (it receives the client as argument)
            await action_coro(client, username)
            result_holder["ok"] = True
        except Exception as e:
            print("Error during action:", e)
            result_holder["error"] = str(e)
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    @client.on_disconnect
    async def _on_disconnect():
        finished.set()

    # Run the CloudLink client in background thread
    thread = threading.Thread(target=lambda: client.run(host=url), daemon=True)
    thread.start()

    # Wait for completion or timeout
    try:
        await asyncio.wait_for(finished.wait(), timeout=ACTION_TIMEOUT)
    except asyncio.TimeoutError:
        return {"status": "error", "message": "Timeout waiting for CloudLink action/disconnect"}

    if result_holder["ok"]:
        return {"status": "ok", "username": result_holder["username"]}
    else:
        return {"status": "error", "message": result_holder["error"] or "unknown", "username": result_holder.get("username")}

def cloudlink_action(action_coro):
    return asyncio.run(cloudlink_action_async(action_coro))

# ---------- Routes HTTP ----------
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

# ---------- Start ----------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)