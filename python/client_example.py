import os
import asyncio
import threading
import random
import traceback
from flask import Flask, request, jsonify
from cloudlink import client as cl_client

app = Flask(__name__)

# === CONFIGURATION FIXE ===
CLOUDLINK_URL = "wss://cloudlink-server.onrender.com/"

# === Core: connect -> do action -> disconnect ===
async def cloudlink_action_async(action_coro):
    client = cl_client()
    finished = asyncio.Event()
    result = {"ok": False, "error": None, "username": None}

    @client.on_connect
    async def _on_connect():
        try:
            # username aléatoire
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

    # Lancement du client cloudlink avec asyncio et options WebSocket
    async def start_client():
        await client.connect(
            host=CLOUDLINK_URL,
            websocket_options={
                "extra_headers": {
                    "Origin": "tw-editor://.",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "turbowarp-desktop/1.14.4 Chrome/136.0.7103.149 "
                                  "Electron/36.4.0 Safari/537.36"
                }
            }
        )
        await finished.wait()

    asyncio.run(start_client())

    if result["ok"]:
        return {"status": "ok", "username": result.get("username")}
    else:
        return {"status": "error", "username": result.get("username"), "detail": result.get("error")}

def cloudlink_action(action_coro):
    return asyncio.run(cloudlink_action_async(action_coro))

# === ROUTES ===

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
    return "Serveur Cloudlink-sender en ligne ✅"

@app.route("/_health", methods=["GET"])
def health():
    return jsonify({"status":"ok"})

# === Launch ===
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
