# cloudlink_server_full.py
import os
import asyncio
import threading
import random
import traceback
from flask import Flask, request, jsonify
from cloudlink import server, client as cl_client
from cloudlink.server.protocols import clpv4, scratch

# ==============================
# 1) CLOUDLINK SERVER
# ==============================
class ExampleCallbacks:
    async def test1(self, client, message):
        print("Test1!")
        await asyncio.sleep(1)
        print("Test1 after one second!")

    async def test2(self, client, message):
        print("Test2!")
        await asyncio.sleep(1)
        print("Test2 after one second!")

    async def test3(self, client, message):
        print("Test3!")

class ExampleCommands:
    def __init__(self, srv, protocol):
        @srv.on_command(cmd="foobar", schema=protocol.schema)
        async def foobar(client, message):
            print("Foobar!")
            print("IP client:", protocol.get_client_ip(client))
            protocol.send_statuscode(client, protocol.statuscodes.ok, message)

class ExampleEvents:
    async def on_connect(self, client):
        print(f"Client {client.id} connected.")

    async def on_close(self, client):
        print(f"Client {client.id} disconnected.")

def start_cloudlink_server():
    srv = server()
    srv.logging.basicConfig(level=srv.logging.DEBUG)

    clpv4_protocol = clpv4(srv)
    scratch_protocol = scratch(srv)

    callbacks = ExampleCallbacks()
    commands = ExampleCommands(srv, clpv4_protocol)
    events = ExampleEvents()

    srv.bind_callback(cmd="handshake", schema=clpv4_protocol.schema, method=callbacks.test1)
    srv.bind_callback(cmd="handshake", schema=clpv4_protocol.schema, method=callbacks.test2)
    srv.bind_callback(cmd="foobar", schema=clpv4_protocol.schema, method=callbacks.test3)

    srv.bind_event(server.on_connect, events.on_connect)
    srv.bind_event(server.on_disconnect, events.on_close)

    host = "0.0.0.0"
    port_env = os.getenv("PORT")
    port = int(port_env) if port_env else 3000

    print(f"Starting CloudLink server on {host}:{port}")
    srv.run(ip=host, port=port)

# ==============================
# 2) FLASK HTTP SERVER
# ==============================
app = Flask(__name__)

# WebSocket URL (local vers CloudLink)
CLOUDLINK_WS_URL = os.getenv("CLOUDLINK_WS_URL", "ws://127.0.0.1:3000/")

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
            client.run(host=ws_url)
        except Exception as e:
            result["error"] = str(e)
            traceback.print_exc()

    thread = threading.Thread(target=run_client, daemon=True)
    thread.start()
    await finished.wait()
    return result if result["ok"] else {"status": "error", "username": result.get("username"), "detail": result.get("error")}

def cloudlink_action(action_coro):
    return asyncio.run(cloudlink_action_async(action_coro, CLOUDLINK_WS_URL))

# ---- ROUTES HTTP ----
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

@app.route("/_health", methods=["GET"])
def health():
    return jsonify({"status":"ok"})

@app.route("/")
def home():
    return "Serveur en ligne ✅"

# ==============================
# 3) LANCEMENT EN THREAD
# ==============================
def start_flask():
    port = int(os.getenv("PORT_HTTP", "5000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    # CloudLink sur thread séparé
    threading.Thread(target=start_cloudlink_server, daemon=True).start()
    # Flask principal
    start_flask()
