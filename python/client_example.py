from flask import Flask, request, jsonify
import requests
import os
import asyncio
import threading
from cloudlink import client as cl_client

app = Flask(__name__)

# ============================
# Récupération de l’URL CloudLink depuis un endpoint
# ============================
AUTH_KEY = os.getenv("CLOUDLINK_KEY")
DISCOVERY_URL = os.getenv("DISCOVERY_URL", "https://mon-endpoint.com/get-server")

def get_cloudlink_url():
    try:
        resp = requests.post(DISCOVERY_URL, json={"cle": AUTH_KEY})
        data = resp.json()
        return data.get("web_socket_server")
    except Exception as e:
        print("❌ Erreur récupération CloudLink URL:", e)
        return None

# ============================
# Fonction générique : connecte → envoie → déconnecte
# ============================
async def cloudlink_action(action):
    url = get_cloudlink_url()
    if not url:
        return {"status": "error", "message": "Impossible de récupérer l’URL CloudLink"}

    client = cl_client()
    done = asyncio.Event()

    @client.on_connect
    async def on_connect():
        print("✅ Connecté à CloudLink:", url)
        await client.protocol.set_username("proxy_bot")
        await action(client)
        client.disconnect()

    @client.on_disconnect
    async def on_disconnect():
        print("❌ Déconnecté")
        done.set()

    threading.Thread(target=lambda: client.run(host=url), daemon=True).start()

    await done.wait()
    return {"status": "ok"}

# ============================
# Routes HTTP
# ============================
@app.route("/global-message", methods=["POST"])
def global_message():
    data = request.get_json()
    rooms = data.get("rooms", [])
    message = data.get("message", "")

    async def action(client):
        await client.protocol.send_gmsg(message, rooms=rooms)

    return jsonify(asyncio.run(cloudlink_action(action)))

@app.route("/private-message", methods=["POST"])
def private_message():
    data = request.get_json()
    username = data.get("username")
    room = data.get("room")
    message = data.get("message")

    async def action(client):
        await client.protocol.send_pmsg(username, room, message)

    return jsonify(asyncio.run(cloudlink_action(action)))

@app.route("/global-variable", methods=["POST"])
def global_variable():
    data = request.get_json()
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")

    async def action(client):
        await client.protocol.send_gvar(room, name, val)

    return jsonify(asyncio.run(cloudlink_action(action)))

@app.route("/private-variable", methods=["POST"])
def private_variable():
    data = request.get_json()
    username = data.get("username")
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")

    async def action(client):
        await client.protocol.send_pvar(username, room, name, val)

    return jsonify(asyncio.run(cloudlink_action(action)))

# ============================
# Lancement Flask
# ============================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)