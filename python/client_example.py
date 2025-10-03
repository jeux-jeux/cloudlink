# python/client_example.py
import os
import threading
import random
import traceback
from functools import partial

from flask import Flask, request, jsonify
from cloudlink import client as cl_client
import websockets  # utilisé pour wrapper la connect et ajouter des headers

app = Flask(__name__)

# === CONFIGURATION FIXE ===
CLOUDLINK_URL = "wss://cloudlink-server.onrender.com/"

# Origin + User-Agent que TurboWarp envoie — on les injectera dans le handshake.
WS_EXTRA_HEADERS = {
    "Origin": "tw-editor://.",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) turbowarp-desktop/1.14.4 "
        "Chrome/136.0.7103.149 Electron/36.4.0 Safari/537.36"
    ),
}


def cloudlink_action(action_coro):
    """
    Synchronous wrapper called from Flask routes.
    It starts a thread which runs the cloudlink client and waits for completion.
    Returns the result dict produced by the client callbacks.
    """

    finished = threading.Event()
    result = {"ok": False, "error": None, "username": None}

    def run_client_thread():
        """
        This function runs in a separate thread. It instantiates the cloudlink client,
        monkeypatches the websockets.connect used internally to include extra headers,
        registers connect/disconnect handlers and then calls client.run(host=...).
        """

        client = cl_client()

        # Monkeypatch the underlying websocket connect function to include extra headers.
        # The client uses self.ws.connect(host), where self.ws is the websockets module.
        # We replace client.ws.connect with a partial that always passes extra_headers.
        try:
            client.ws.connect = partial(websockets.connect, extra_headers=WS_EXTRA_HEADERS)
        except Exception:
            # If for any reason monkeypatching fails, continue without it (we'll log later)
            pass

        @client.on_connect
        async def _on_connect():
            try:
                username = str(random.randint(100_000_000, 999_999_999))
                result["username"] = username

                # set username via protocol (cloudlink client protocol call)
                await client.protocol.set_username(username)

                # call the user-provided action (async) inside the client's loop
                await action_coro(client, username)

                result["ok"] = True
            except Exception as e:
                result["error"] = str(e)
                traceback.print_exc()
            finally:
                # attempt graceful disconnect
                try:
                    await client.disconnect()
                except Exception:
                    pass

        @client.on_disconnect
        async def _on_disconnect():
            # Signal the main thread that the client is finished
            finished.set()

        # Run the client (this will create and run its own asyncio loop inside this thread)
        try:
            client.run(host=CLOUDLINK_URL)
        except Exception as e:
            # If connection fails (ex: HTTP 502), capture error and set finished
            result["error"] = str(e)
            traceback.print_exc()
            finished.set()

    # Start the thread and wait (no asyncio.run in main thread)
    thread = threading.Thread(target=run_client_thread, daemon=True)
    thread.start()

    # WAIT here until the thread signals finished.set()
    # This blocks the Flask request thread until the CloudLink action completes.
    # If you want a timeout, replace finished.wait() with finished.wait(timeout_seconds)
    finished.wait()

    # Build response similar to previous behaviour
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


# === Launch ===
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
