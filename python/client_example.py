# proxy.py
import os
import requests
import asyncio
import threading
import random
import traceback
import json
from flask import Flask, request, jsonify
from cloudlink import client as cl_client

app = Flask(__name__)

# === Configuration (env) ===
# REQUIRED:
DISCOVERY_URL = os.getenv("DISCOVERY_URL", "").strip()   # ex: https://example.com/get-server
CLOUDLINK_KEY = os.getenv("CLOUDLINK_KEY", "").strip()   # cle secrète à envoyer à DISCOVERY_URL

# Timeout pour la découverte et pour la connexion Cloudlink (en secondes)
DISCOVERY_TIMEOUT = 8
CONNECTION_TIMEOUT = int(os.getenv("CONNECTION_TIMEOUT", "10"))

def discover_cloudlink_url():
    """
    POST {cle: CLOUDLINK_KEY} to DISCOVERY_URL and return web_socket_server string.
    Nettoie les backslashes et supprime le slash final.
    """
    if not DISCOVERY_URL or not CLOUDLINK_KEY:
        return None, "DISCOVERY_URL or CLOUDLINK_KEY not set"
    try:
        resp = requests.post(DISCOVERY_URL, json={"cle": CLOUDLINK_KEY}, timeout=DISCOVERY_TIMEOUT)
        resp.raise_for_status()

        # essayer de parser le JSON
        try:
            j = resp.json()
        except ValueError:
            # si le serveur a renvoyé un JSON double-encodé (ou texte), tenter un second parse
            text = resp.text
            try:
                j = json.loads(text)
            except Exception:
                return None, f"discovery error: invalid json: {text[:500]}"

        # Si la réponse est une string JSON encodée, parser à nouveau
        if isinstance(j, str):
            try:
                j = json.loads(j)
            except Exception:
                return None, f"discovery error: double-encoded json: {j[:500]}"

        url = j.get("web_socket_server")
        if not url:
            return None, "discovery response missing 'web_socket_server'"

        # Nettoyage : supprimer tous les backslashes (éventuels) et forcer PAS de slash final
        if isinstance(url, str):
            url = url.replace("\\", "")
            url = url.rstrip("/")

        return url, None
    except Exception as e:
        return None, f"discovery error: {str(e)}"

# === Core: connect -> do action -> disconnect (with a connection timeout) ===
async def cloudlink_action_async(action_coro):
    """
    action_coro: async function(client, username)
    Waits until disconnect occurs or until CONNECTION_TIMEOUT seconds
    Returns dict with status and username (and error detail on failure).
    """
    url, err = discover_cloudlink_url()
    if not url:
        return {"status": "error", "message": "discovery_failed", "detail": err}

    print(f"[proxy] Tentative de connexion Cloudlink sur: {url}")

    client = cl_client()
    finished = asyncio.Event()
    result = {"ok": False, "error": None, "username": None}

    @client.on_connect
    async def _on_connect():
        print("[proxy] ✅ on_connect called")
        try:
            # generate random 9-digit username for this session
            username = str(random.randint(100_000_000, 999_999_999))
            result["username"] = username

            await client.protocol.set_username(username)
            # perform user action (send gmsg / pmsg / gvar / pvar)
            await action_coro(client, username)
            result["ok"] = True
            print("[proxy] action executed successfully")
        except Exception as e:
            result["error"] = str(e)
            traceback.print_exc()
        finally:
            try:
                client.disconnect()
                print("[proxy] client.disconnect() called")
            except Exception:
                pass

    @client.on_disconnect
    async def _on_disconnect():
        print("[proxy] on_disconnect called")
        finished.set()

    # run the cloudlink client (blocking) in a background thread
    thread = threading.Thread(target=lambda: client.run(host=url), daemon=True)
    thread.start()

    # WAIT for disconnect but with timeout to avoid hanging requests forever
    try:
        print(f"[proxy] ⏳ En attente de déconnexion (timeout={CONNECTION_TIMEOUT}s)...")
        await asyncio.wait_for(finished.wait(), timeout=CONNECTION_TIMEOUT)
        print("[proxy] ✔️ Déconnexion reçue avant timeout")
    except asyncio.TimeoutError:
        # Timeout : on considère que la connexion n'a pas été établie correctement
        result["error"] = "connection_timeout"
        print("[proxy] ⛔ Timeout de connexion Cloudlink — on restart/stoppe le client")
        try:
            client.disconnect()
        except Exception:
            pass

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