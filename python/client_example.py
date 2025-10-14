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
import logging
import inspect
import requests

app = Flask(__name__)
app.logger.setLevel(logging.DEBUG)

# -------------------------
# Configuration
# -------------------------
# CLE attendue (pour authorisation des routes sending/checking)
ENV_KEY_NAME = "CLE"
# URL du service d'auth/discovery (optionnel)
PROXY_AUTH_URL = os.getenv("PROXY")  # ex: https://proxy-authentification-v3.onrender.com/

WS_EXTRA_HEADERS = [
    ("Origin", "https://cloudlink-manager.onrender.com/"),
    ("User-Agent", "proxy_render_manager")
]

USERNAME_TIMEOUT = int(os.getenv("USERNAME_TIMEOUT", "10"))
ACTION_TIMEOUT = int(os.getenv("ACTION_TIMEOUT", "15"))
TOTAL_ACTION_TIMEOUT = int(os.getenv("TOTAL_ACTION_TIMEOUT", "25"))

# -------------------------
# Helpers
# -------------------------
def check_key(data: dict) -> bool:
    """Vérifie que le corps JSON contient une clé 'cle' valide."""
    expected = os.getenv(ENV_KEY_NAME)
    # Si aucune clé attendue configurée, autorise (pratique pour tests)
    if not expected:
        app.logger.debug("No expected CLE configured in env -> skipping check_key (open mode).")
        return True
    received = (data or {}).get("cle")
    ok = (received == expected)
    if not ok:
        app.logger.warning("check_key: invalid or missing cle in request body.")
    return ok

def fetch_cloudlink_ws_url():
    if PROXY_AUTH_URL:
        try:
            app.logger.debug(f"fetch_cloudlink_ws_url: requesting discovery from {PROXY_AUTH_URL}")
            resp = requests.get(PROXY_AUTH_URL, timeout=5, headers={"Origin": "https://cloudlink-manager.onrender.com"})
            resp.raise_for_status()
            j = resp.json()
            url = j.get("web_socket_server") or j.get("websocket") or j.get("web_socket_url") or j.get("url")
            if url:
                app.logger.info(f"fetch_cloudlink_ws_url: discovered websocket url: {url}")
                return url
            else:
                app.logger.warning("fetch_cloudlink_ws_url: discovery returned JSON but no WS field found.")
        except Exception as e:
            app.logger.exception(f"fetch_cloudlink_ws_url: discovery request failed: {e}")

    # Si discovery échoue, retourne None au lieu d’un fallback
    app.logger.error("fetch_cloudlink_ws_url: discovery failed and no fallback configured")
    return None


def sanitize_ws_url(url: str) -> str | None:
    """
    Nettoie l'URL retournée par discovery.
    - convertit http(s) -> ws(s)
    - ajoute scheme si absent (wss par défaut)
    - normalise slash final
    Retourne None si l'URL est manifestement invalide.
    """
    if not url or not isinstance(url, str):
        return None
    url = url.replace("\\", "").strip()
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    # si pas de scheme, ajouter wss://
    parts = urlsplit(url)
    if not parts.scheme:
        url = "wss://" + url.lstrip("/")
    # n'accepter que ws:// ou wss://
    if not (url.startswith("ws://") or url.startswith("wss://")):
        return None
    # s'assurer qu'il y a au moins un host
    parsed = urlsplit(url)
    if not parsed.hostname:
        return None
    # normaliser trailing slash
    return url.rstrip("/") + "/"

def ws_handshake_test_sync(url: str, extra_headers=None, timeout=6):
    """
    Test de handshake websocket exécuté dans une boucle propre.
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
# Core CloudLink runner (avec timeouts & protections)
# -------------------------

async def action(client, username):
    """
    Envoie get_userlist et récupère la prochaine réponse 'ulist' via le callback
    d'événements du client (on_message / on_packet). Évite d'appeler ws.recv()
    directement pour ne pas entrer en conflit avec la boucle de réception interne.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    # Normalise la room en str
    room_str = str(room)

    # Handler qui acceptera soit un dict déjà parsé, soit une chaîne JSON
    def make_handler():
        def _handler(*args, **kwargs):
            # Le client peut appeler le handler avec (packet) ou (client, packet)
            pkt = None
            if len(args) == 1:
                pkt = args[0]
            elif len(args) >= 2:
                pkt = args[1]
            elif "packet" in kwargs:
                pkt = kwargs["packet"]

            # Si on reçoit une chaîne, tenter de parser JSON
            if isinstance(pkt, str):
                try:
                    import json as _json
                    pkt = _json.loads(pkt)
                except Exception:
                    return

            # Vérifier que c'est bien le packet attendu
            try:
                if isinstance(pkt, dict) and pkt.get("cmd") == "ulist" and str(pkt.get("rooms")) == room_str:
                    if not fut.done():
                        fut.set_result(pkt.get("val", []))
            except Exception:
                # ne pas faire planter le handler
                pass

        return _handler

    handler = make_handler()
    registered = False
    unregister_possible = []
    # Tentatives d'enregistrement (ordre : on_message, on_packet)
    try:
        if hasattr(client, "on_message"):
            # API similaire à @client.on_message (méthode d'enregistrement)
            client.on_message(handler)
            registered = True
            unregister_possible.append("on_message")
    except Exception:
        registered = False

    if not registered:
        try:
            if hasattr(client, "on_packet"):
                client.on_packet(handler)
                registered = True
                unregister_possible.append("on_packet")
        except Exception:
            registered = False

    if not registered:
        # Aucun hook dispo : on ne peut pas lire sans conflit de recv
        raise RuntimeError(
            "Client library doesn't expose on_message/on_packet to capture incoming packets. "
            "Can't wait for ulist without concurrent recv."
        )

    # Envoie la commande au serveur
    client.send_packet({"cmd": "get_userlist", "room": room_str})

    # Attendre la réponse via le futur mis par le handler
    try:
        users = await asyncio.wait_for(fut, timeout=4.0)
        return users
    except asyncio.TimeoutError:
        raise RuntimeError("Timeout waiting for ulist response from server")
    finally:
        # Tentative de "désinscription" si la lib fournit une API d'off :
        # on essaye des noms courants (si présents).
        for off_name in ("off_message", "remove_on_message", "remove_listener", "off_packet", "remove_handler"):
            if hasattr(client, off_name):
                try:
                    getattr(client, off_name)(handler)
                except Exception:
                    pass



def cloudlink_action(action_coro):
    raw = fetch_cloudlink_ws_url()
    ws_url = sanitize_ws_url(raw)
    if not ws_url:
        app.logger.error(f"cloudlink_action: invalid websocket url from discovery: raw={raw!r}")
        return {"status": "error", "message": "invalid_ws_url", "detail": str(raw)}
    app.logger.info(f"cloudlink_action: using websocket {ws_url}")
    try:
        return asyncio.run(cloudlink_action_async(action_coro, ws_url))
    except Exception as e:
        app.logger.exception("cloudlink_action: asyncio.run raised")
        return {"status": "error", "message": "internal_error", "detail": str(e)}



# -------------------------
# Routes sending (requièrent 'cle' dans body)
# -------------------------
@app.route("/sending/global-message", methods=["POST"])
def route_global_message():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403

    rooms = data.get("rooms")
    message = data.get("message")

    if not isinstance(rooms, list) or not message:
        return jsonify({"status": "error", "message": "rooms (list) and message required"}), 400

    async def action(client, username):
        # DEBUG log pour vérifier
        print("DEBUG proxy -> send_packet:", {"cmd": "gmsg", "val": message, "rooms": rooms})
        client.send_packet({"cmd": "link", "val": rooms})
        await asyncio.sleep(0.15)
        # Envoyer le message global directement
        client.send_packet({"cmd": "gmsg", "val": message, "rooms": rooms})   
    result = cloudlink_action(action)
    status = 200 if result.get("status") == "ok" else 500
    return jsonify(result), status

@app.route("/sending/private-message", methods=["POST"])
def route_private_message():
    data = request.get_json(force=True, silent=True) or {}

    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403

    username_target = data.get("username")
    rooms = data.get("rooms") or data.get("room")
    message = data.get("message")

    if not username_target:
        return jsonify({"status": "error", "message": "username required"}), 400

    # Normaliser rooms en liste
    if isinstance(rooms, str) or isinstance(rooms, int):
        rooms = [str(rooms)]
    elif rooms is None:
        return jsonify({"status": "error", "message": "room(s) required"}), 400
    elif not isinstance(rooms, list):
        return jsonify({"status": "error", "message": "room(s) required"}), 400

    if not message:
        return jsonify({"status": "error", "message": "message required"}), 400

    async def action(client, username):
        # s'abonner d'abord aux rooms
        client.send_packet({"cmd": "link", "val": rooms})
        await asyncio.sleep(0.15)

        # envoyer un pmsg par room (schema serveur attend 'room' ou 'rooms')
        for room in rooms:
            client.send_packet({
                "cmd": "pmsg",
                "val": message,
                "id": username_target,
                "room": str(room)
            })

    result = cloudlink_action(action)
    status = 200 if result.get("status") == "ok" else 500
    return jsonify(result), status

@app.route("/sending/global-variable", methods=["POST"])
def route_global_variable():
    data = request.get_json(force=True, silent=True) or {}

    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403

    rooms = data.get("rooms") or data.get("room")
    name = data.get("name")
    val = data.get("val")

    if name is None:
        return jsonify({"status": "error", "message": "name required"}), 400

    # Normaliser rooms en liste
    if isinstance(rooms, (str, int)):
        rooms = [str(rooms)]
    elif rooms is None:
        return jsonify({"status": "error", "message": "room(s) required"}), 400
    elif not isinstance(rooms, list):
        return jsonify({"status": "error", "message": "room(s) must be a list or string"}), 400

    async def action(client, username):
        # s'abonner d'abord aux rooms comme les autres routes
        client.send_packet({"cmd": "link", "val": rooms})
        await asyncio.sleep(0.15)

        # envoyer une seule commande gvar avec la clé "rooms" (liste) — cohérent avec gmsg
        client.send_packet({
            "cmd": "gvar",
            "name": name,
            "val": val,
            "rooms": rooms
        })

    result = cloudlink_action(action)
    status = 200 if result.get("status") == "ok" else 500
    return jsonify(result), status


@app.route("/sending/private-variable", methods=["POST"])
def route_private_variable():
    data = request.get_json(force=True, silent=True) or {}

    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403

    username_target = data.get("username")
    rooms = data.get("rooms") or data.get("room")
    name = data.get("name")
    val = data.get("val")

    if not username_target:
        return jsonify({"status": "error", "message": "username required"}), 400
    if name is None:
        return jsonify({"status": "error", "message": "name required"}), 400

    # Normaliser rooms en liste
    if isinstance(rooms, (str, int)):
        rooms = [str(rooms)]
    elif rooms is None:
        return jsonify({"status": "error", "message": "room(s) required"}), 400
    elif not isinstance(rooms, list):
        return jsonify({"status": "error", "message": "room(s) must be a list or string"}), 400

    async def action(client, username):
        # s'abonner d'abord aux rooms
        client.send_packet({"cmd": "link", "val": rooms})
        await asyncio.sleep(0.15)

        # envoyer une seule commande pvar avec "rooms" (liste)
        client.send_packet({
            "cmd": "pvar",
            "name": name,
            "val": val,
            "rooms": rooms,
            "id": username_target
        })

    result = cloudlink_action(action)
    status = 200 if result.get("status") == "ok" else 500
    return jsonify(result), status


@app.route("/room/users", methods=["POST"])
def route_get_userlist():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403

    room = data.get("room")
    if not room:
        return jsonify({"status": "error", "message": "room required"}), 400

    async def action(client, username):
        """
        Envoie get_userlist et attend le packet ulist correspondant.
        Retourne la liste d'utilisateurs (list) ou lance une exception si timeout.
        """
        # Envoie la commande serveur
        client.send_packet({"cmd": "get_userlist", "room": str(room)})

        # On va tenter de lire directement un message websocket (JSON) du client temporaire.
        # ATTENTION: adapte `client.client` si dans ton cloudlink client le ws se nomme différemment.
        ws = getattr(client, "client", None) or getattr(client, "_client", None) or getattr(client, "ws", None)
        if ws is None:
            # Si on ne trouve pas le websocket interne, on ne peut pas lire : retourne erreur
            raise RuntimeError("internal client websocket not accessible (attribute name may differ)")

        # on attend la prochaine trame texte du websocket (timeout)
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
        except Exception as e:
            raise RuntimeError(f"no response from server (timeout or recv error): {e}")

        # tenter parser JSON
        try:
            import ujson as _ujson
        except Exception:
            import json as _ujson

        try:
            packet = _ujson.loads(raw)
        except Exception:
            raise RuntimeError("received non-json or malformed packet")

        # vérifier qu'on a bien ulist et room attendu
        if packet.get("cmd") == "ulist" and packet.get("rooms") == str(room):
            return packet.get("val", [])
        else:
            # si on a un statuscode OK + ulist manquant, renvoyer statue + vide
            return {"note": "unexpected packet", "packet": packet}

    # Exécute l'action via cloudlink_action
    result = cloudlink_action(action)
    # si la fonction a retourné payload, c'est notre liste
    if result.get("status") == "ok" and "payload" in result:
        return jsonify({"status": "ok", "usernames": result["payload"]})
    else:
        # retourne l'erreur remontée par cloudlink_action
        status = 200 if result.get("status") == "ok" else 500
        return jsonify(result), status


@app.route("/room/deleter", methods=["POST"])
def route_kick_client():
    data = request.get_json(force=True, silent=True) or {}

    # Vérifie la clé d'API (fonction que tu as déjà)
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403

    # Récupération des paramètres
    room = data.get("room")
    targets = data.get("targets") or data.get("target")
    if isinstance(targets, str):
        targets = [targets]

    # Validation simple
    if not room or not isinstance(targets, list) or not targets:
        return jsonify({"status": "error", "message": "room and targets (list or single) required"}), 400

    # Secret admin (optionnel) : envoyé dans le payload si présent en env
    secret = os.getenv("ADMIN_SECRET", "").strip()

    # Action envoyée au client CloudLink (cloudlink_action doit gérer l'exécution asynchrone)
    async def action(client, username):
        # Envoie une commande delete_user par cible (un par un)
        for t in targets:
            payload = {"cmd": "delete_user", "id": t}
            if secret:
                payload["secret"] = secret
            # Utilise send_packet pour envoyer au client CloudLink
            try:
                client.send_packet(payload)
            except Exception:
                # Si client a une autre API, tu peux tenter server.send_packet ou client.send
                try:
                    client.send(payload)
                except Exception:
                    # log si besoin (server.logger si accessible)
                    pass

    # cloudlink_action doit retourner quelque chose (résultat / statut)
    result = cloudlink_action(action)
    return jsonify(result)


# -------------------------
# Health & Debug (some routes accept key, some not)
# -------------------------
@app.route("/checking/health", methods=["POST"])
def health():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    return jsonify({"status": "ok"})


@app.route("/checking/handshake", methods=["POST"])
def debug_handshake():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    raw = fetch_cloudlink_ws_url()
    url = sanitize_ws_url(raw)
    tests = {
        "default": ws_handshake_test_sync(url, extra_headers=None) if url else {"ok": False, "exc": "invalid url"},
        "origin": ws_handshake_test_sync(url, extra_headers=[("Origin", "https://cloudlink-manager.onrender.com/")]) if url else {"ok": False, "exc": "invalid url"},
        "origin+ua": ws_handshake_test_sync(url, extra_headers=WS_EXTRA_HEADERS) if url else {"ok": False, "exc": "invalid url"}
    }
    return jsonify({"ws_url": url, "tests": tests})


@app.route("/checking/connect-client", methods=["POST"])
def debug_connect_client():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403
    raw = fetch_cloudlink_ws_url()
    ws_url = sanitize_ws_url(raw)
    if not ws_url:
        return jsonify({"status": "error", "message": "invalid_ws_url", "detail": str(raw)}), 500
    timeout = int(request.args.get("timeout", str(TOTAL_ACTION_TIMEOUT)))

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
        app.logger.warning("debug_connect_client: client thread timed out")
        return jsonify({"status": "timeout", "detail": f"Client still alive after {timeout}s", "result": result})
    else:
        return jsonify({"status": "finished", "result": result})

# -------------------------
# Entrée principale
# -------------------------
@app.route("/", methods=["GET"])
def index():
    return "Proxy HTTP Cloudlink en ligne ✅"

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.logger.info(f"proxy: starting app on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
