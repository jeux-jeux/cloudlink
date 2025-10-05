# proxy_http.py
import os
import asyncio
import threading
import random
import traceback
import functools
from urllib.parse import urlsplit
from flask import Flask, request, jsonify
from cloudlink import client as cl_client
import websockets

app = Flask(__name__)

# -------------------------
# Configuration
# -------------------------
CLOUDLINK_WS_URL = os.getenv("CLOUDLINK_WS_URL", "wss://cloudlink-server.onrender.com/")
# Headers à injecter si besoin (TurboWarp-like)
WS_EXTRA_HEADERS = [
    ("Origin", "tw-editor://."),
    ("User-Agent", "turbowarp-desktop/1.14.4")
]

# -------------------------
# Helpers
# -------------------------
def sanitize_ws_url(url: str) -> str:
    """Nettoie et formate l’URL WebSocket retournée par discovery ou env."""
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
    # assure exactement un slash final
    url = url.rstrip("/") + "/"
    return url


def ws_handshake_test_sync(url: str, extra_headers=None, timeout=6):
    """
    Teste rapidement un handshake websocket depuis le même container (boucle dédiée).
    Retourne dict {ok, exc, status_code, response_headers}.
    """
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
        try:
            loop.close()
        except Exception:
            pass


# -------------------------
# Core CloudLink runner (corrigé pour éviter blocage)
# -------------------------
async def cloudlink_action_async(action_coro, ws_url):
    """
    Connecte un client CloudLink, exécute action_coro(client, username) (async),
    puis se déconnecte proprement. Attend la déconnexion en utilisant threading.Event
    pour ne pas mélanger les boucles asyncio.
    """
    client = cl_client()

    # Event thread-safe utilisé pour signaler la fin (set depuis le thread du client).
    finished_thread = threading.Event()

    # Conteneur résultat
    result = {"ok": False, "error": None, "username": None}

    @client.on_connect
    async def _on_connect():
        try:
            # username aléatoire pour la session
            username = str(random.randint(100_000_000, 999_999_999))
            result["username"] = username

            # set_username est une coroutine → await obligatoire
            await client.protocol.set_username(username)

            # action_coro est async (défini dans les routes).
            # IMPORTANT: action_coro doit utiliser client.send_packet(...) SANS await.
            await action_coro(client, username)

            result["ok"] = True
        except Exception as e:
            result["error"] = str(e)
            traceback.print_exc()
        finally:
            # demande déconnexion propre (await car coroutine)
            try:
                await client.disconnect()
            except Exception:
                pass

    @client.on_disconnect
    async def _on_disconnect():
        # Cet async callback est exécuté dans la boucle du client (autre thread).
        # On peut appeler set() sur threading.Event depuis n'importe quel thread.
        finished_thread.set()

    # run client.run(host=...) dans un thread séparé (client gère sa propre boucle asyncio)
    def run_client():
        try:
            # monkeypatch websockets.connect si on veut ajouter headers
            try:
                client.ws.connect = functools.partial(websockets.connect, extra_headers=WS_EXTRA_HEADERS)
            except Exception:
                # si ça échoue, on continue quand même sans headers
                pass

            client.run(host=ws_url)
        except Exception as e:
            # capture erreur pour renvoyer au caller et s'assurer que l'attente se réveille
            result["error"] = str(e)
            result["traceback"] = traceback.format_exc()
            try:
                finished_thread.set()
            except Exception:
                pass

    thread = threading.Thread(target=run_client, daemon=True)
    thread.start()

    # Attendre que finished_thread soit set (en attendant dans l'event-loop asyncio de façon non-bloquante)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, finished_thread.wait)
    except Exception as e:
        # fallback: si l'attente échoue, retourne erreur
        return {"status": "error", "detail": f"waiting for client thread failed: {e}"}

    # Retour comme avant
    if result.get("ok"):
        return {"status": "ok", "username": result.get("username")}
    else:
        # si erreur remplie, retournons-la pour debug (trace si existante)
        out = {"status": "error", "username": result.get("username"), "detail": result.get("error")}
        if result.get("traceback"):
            out["trace"] = result.get("traceback")
        return out


def cloudlink_action(action_coro):
    """
    Wrapper synchrone appelé depuis les routes Flask.
    Sanitize URL, run async flow (dans asyncio.run) et retourne dict résultat.
    """
    raw = os.getenv("CLOUDLINK_WS_URL", CLOUDLINK_WS_URL)
    ws_url = sanitize_ws_url(raw)
    try:
        return asyncio.run(cloudlink_action_async(action_coro, ws_url))
    except Exception as e:
        return {"status": "error", "message": "internal_error", "detail": str(e)}


# -------------------------
# Routes principales (4)
# -------------------------
@app.route("/global-message", methods=["POST"])
def route_global_message():
    data = request.get_json(force=True, silent=True) or {}
    rooms = data.get("rooms")
    message = data.get("message")
    if not isinstance(rooms, list) or not message:
        return jsonify({"status": "error", "message": "rooms (list) and message required"}), 400

    # action_coro : coroutine qui utilisera client.send_packet(...) (SANS await)
    async def action(client, username):
        client.send_packet({"cmd": "gmsg", "val": message, "rooms": rooms})

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
        client.send_packet({"cmd": "pmsg", "val": message, "id": username_target, "room": room})

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
        client.send_packet({"cmd": "gvar", "name": name, "val": val, "room": room})

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
        client.send_packet({"cmd": "pvar", "name": name, "val": val, "room": room, "id": username_target})

    return jsonify(cloudlink_action(action))


# -------------------------
# Health & debug
# -------------------------
@app.route("/_health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/", methods=["GET"])
def home():
    return "Serveur HTTP en ligne ✅"


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
    """
    Lance un client CloudLink réel (dans un thread), capture erreur/trace si échec.
    Utile pour voir le handshake + rejet 502 côté CloudLink.
    """
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
