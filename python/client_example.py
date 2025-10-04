# python/client_example.py
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

def cloudlink_action(action_coro):
    """
    Exécute une action CloudLink dans un thread séparé et attend sa fin.
    """
    finished = threading.Event()
    result = {"ok": False, "error": None, "username": None}

    async def run_client():
        client = cl_client()

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
                finished.set()

        @client.on_disconnect
        async def _on_disconnect():
            finished.set()

        try:
            # Connexion directe sans headers personnalisés
            await client.__run__(CLOUDLINK_URL)
        except Exception as e:
            result["error"] = str(e)
            traceback.print_exc()
            finished.set()

    def thread_target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_client())

    # Lancer dans un thread pour éviter asyncio.run dans un event loop existant
    thread = threading.Thread(target=thread_target, daemon=True)
    thread.start()
    finished.wait()

    if result["ok"]:
        return {"status": "ok", "username": result.get("username")}
    else:
        return {"status": "error", "username": result.get("username"), "detail": result.get("error")}


# === ROUTES ===

@app.route("/global-message", methods=["POST"])
def route_global_message():
    data = request.get_json(force=True, silent=True) or {}
    rooms = data.get("rooms")
    message = data.get("message")
    if not isinstance(rooms, list) or not message:
        return jsonify({"status": "error", "message": "rooms (list) and message required"}), 400

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
        return jsonify({"status": "error", "message": "username, room and message required"}), 400

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
        return jsonify({"status": "error", "message": "room and name required"}), 400

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
        return jsonify({"status": "error", "message": "username, room and name required"}), 400

    async def action(client, username):
        await client.protocol.send_pvar(username_target, room, name, val)

    return jsonify(cloudlink_action(action))


@app.route("/")
def home():
    return "Serveur Cloudlink-sender en ligne ✅"


@app.route("/_health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
