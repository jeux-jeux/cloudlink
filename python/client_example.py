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
import json
app = Flask(__name__)
app.logger.setLevel(logging.DEBUG)

# -------------------------
# Configuration
# -------------------------
# CLE attendue (pour authorisation des routes sending/checking)
CLE = os.getenv("CLE")
# URL du service d'auth/discovery (optionnel)
PROXY_AUTH_URL = os.getenv("PROXY")  # ex: https://proxy-authentification-v3.onrender.com/


USERNAME_TIMEOUT = int(os.getenv("USERNAME_TIMEOUT", "10"))
ACTION_TIMEOUT = int(os.getenv("ACTION_TIMEOUT", "15"))
TOTAL_ACTION_TIMEOUT = int(os.getenv("TOTAL_ACTION_TIMEOUT", "25"))

# Definition des headers
resp = requests.post(PROXY_AUTH_URL, json={"cle": CLE}, timeout=5 )
resp.raise_for_status()
j = resp.json()
url = j.get("web_socket_server")
cle_wbs = j.get("cle_wbs")
WS_EXTRA_HEADERS = [
    ("cle", cle_wbs),
    ("User-Agent", "proxy_render_manager")
]
# -------------------------
# Helpers
# -------------------------
def check_key(data: dict) -> bool:
    cle_received = (data or {}).get("cle")
    app.logger.debug(f"fetch_cloudlink_ws_url: requesting discovery from {PROXY_AUTH_URL}")
    resp = requests.post(PROXY_AUTH_URL, json={"cle": CLE}, timeout=5 )
    resp.raise_for_status()
    j = resp.json()
    level = j.get("level")
    if level == "code":
        cle_received = (data or {}).get("cle")
        if not cle_received:
            app.logger.debug("No expected CLE configured in env -> skipping check_key (open mode).")
            return False
      
        resp = requests.post(f"{PROXY_AUTH_URL}cle-ultra", json={"cle": cle_received}, timeout=5 )
        resp.raise_for_status()
        j = resp.json()
        access = j.get("access")
    
        if access == "false":
            resp = requests.post(f"{PROXY_AUTH_URL}cle-iphone", json={"cle": cle_received}, timeout=5 )
            resp.raise_for_status()
            j = resp.json()
            access = j.get("access")
        if access == "false":
            app.logger.warning("check_key: invalid or missing cle in request body.")
            ok = False
        else:
            ok = True
    elif level == "all":
        ok = True
    else:
        cle_received = (data or {}).get("cle")
        resp = requests.post(f"{PROXY_AUTH_URL}cle-ultra", json={"cle": cle_received}, timeout=5 )
        resp.raise_for_status()
        j = resp.json()
        access = j.get("access")
        if not access == "false":
            ok = True
        else:
            ok = False
    return ok

def fetch_cloudlink_ws_url():
    if PROXY_AUTH_URL:
        try:
            app.logger.debug(f"fetch_cloudlink_ws_url: requesting discovery from {PROXY_AUTH_URL}")
            resp = requests.post(PROXY_AUTH_URL, json={"cle": CLE}, timeout=5 )
            resp.raise_for_status()
            j = resp.json()
            url = j.get("web_socket_server")
            cle_wbs = j.get("cle_wbs")
            WS_EXTRA_HEADERS = [
                ("cle", cle_wbs),
                ("User-Agent", "proxy_render_manager")
            ]
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

async def cloudlink_action_async(action_coro, ws_url, total_timeout=TOTAL_ACTION_TIMEOUT):
    app.logger.debug(f"cloudlink_action_async: start host={ws_url}")
    client = cl_client()
    finished_thread = threading.Event()
    result = {"ok": False, "error": None, "username": None, "trace": None}

    @client.on_connect
    async def _on_connect():
        app.logger.debug("cloudlink_action_async: on_connect called")
        try:
            username = str(random.randint(100_000_000, 999_999_999))
            result["username"] = username

            # set_username (avec timeout)
            await asyncio.wait_for(client.protocol.set_username(username), timeout=USERNAME_TIMEOUT)

            # run action (avec timeout)
            await asyncio.wait_for(action_coro(client, username), timeout=ACTION_TIMEOUT)

            # petite pause pour laisser les messages partir
            await asyncio.sleep(0.15)

            # tenter un disconnect propre (safe)
            try:
                disconnect_fn = getattr(client, "disconnect", None)
                if disconnect_fn is not None:
                    maybe = disconnect_fn()
                    if asyncio.iscoroutine(maybe):
                        await maybe
                else:
                    # fallback try close()
                    close_fn = getattr(client, "close", None)
                    if close_fn is not None:
                        maybe = close_fn()
                        if asyncio.iscoroutine(maybe):
                            await maybe
            except Exception:
                app.logger.exception("cloudlink_action_async: disconnect failed (ignored)")

            result["ok"] = True
            app.logger.debug("cloudlink_action_async: action completed OK")
        except Exception as e:
            result["error"] = str(e)
            result["trace"] = traceback.format_exc()
            app.logger.exception("cloudlink_action_async: exception inside on_connect")
        finally:
            try:
                finished_thread.set()
            except Exception:
                pass

    @client.on_disconnect
    async def _on_disconnect():
        app.logger.debug("cloudlink_action_async: on_disconnect -> set finished_thread")
        try:
            finished_thread.set()
        except Exception:
            pass

    def run_client():
        try:
            try:
                client.ws.connect = functools.partial(websockets.connect, extra_headers=WS_EXTRA_HEADERS)
            except Exception as e:
                app.logger.warning(f"cloudlink_action_async: monkeypatch client.ws.connect failed: {e}")

            app.logger.debug(f"cloudlink_action_async: client.run(host={ws_url}) starting")
            client.run(host=ws_url)
            app.logger.debug("cloudlink_action_async: client.run returned")
            try:
                finished_thread.set()
            except Exception:
                pass
        except Exception as e:
            result["error"] = str(e)
            result["trace"] = traceback.format_exc()
            app.logger.exception("cloudlink_action_async: exception in run_client")
            try:
                finished_thread.set()
            except Exception:
                pass

    thread = threading.Thread(target=run_client, daemon=True)
    thread.start()

    # Wait with overall timeout
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(None, finished_thread.wait), timeout=total_timeout)
    except asyncio.TimeoutError:
        app.logger.warning("cloudlink_action_async: timeout waiting for client to finish")
        out = {"status": "error", "username": result.get("username"), "detail": "timeout waiting for disconnect"}
        if result.get("trace"):
            out["trace"] = result.get("trace")
        return out

    if result.get("ok"):
        return {"status": "ok", "username": result.get("username")}
    else:
        out = {"status": "error", "username": result.get("username"), "detail": result.get("error")}
        if result.get("trace"):
            out["trace"] = result.get("trace")
        return out


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


# --- route Flask (proxy) : retourne les rooms non vides ---
@app.route("/get/lists", methods=["POST"])
def route_list_rooms():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403

    # Optionnel : timeout en secondes (paramètre query ?timeout=5)
    timeout = float(request.args.get("timeout", "5.0"))

    async def _task():
        raw = fetch_cloudlink_ws_url()
        ws_url = sanitize_ws_url(raw)
        if not ws_url:
            return {"status": "error", "message": "invalid_ws_url", "detail": str(raw)}

        try:
            async with websockets.connect(ws_url, extra_headers=WS_EXTRA_HEADERS, open_timeout=5) as ws:
                app.logger.debug(f"route_list_rooms: connected to {ws_url}, requesting rooms list")

                # envoyer la commande get_rooms (server doit implémenter ce handler)
                await ws.send(json.dumps({"cmd": "get_rooms"}))

                # attendre la réponse rooms_list
                end = asyncio.get_event_loop().time() + timeout
                while True:
                    remaining = end - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        return {"status": "error", "message": "timeout waiting for rooms_list"}
                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        return {"status": "error", "message": "timeout waiting for rooms_list"}

                    try:
                        msg = json.loads(raw_msg)
                    except Exception:
                        continue

                    if msg.get("cmd") == "rooms_list":
                        rooms = msg.get("val", [])
                        # normaliser en liste de strings
                        if isinstance(rooms, (list, tuple, set)):
                            rooms = [str(r) for r in rooms]
                        elif isinstance(rooms, dict):
                            rooms = list(rooms.keys())
                        else:
                            rooms = []

                        return {"status": "ok", "rooms": rooms}

                # unreachable
        except Exception as e:
            app.logger.exception("route_list_rooms: websocket error")
            return {"status": "error", "message": "ws_error", "detail": str(e)}

    result = asyncio.run(_task())
    status = 200 if result.get("status") == "ok" else 500
    return jsonify(result), status


@app.route("/get/users", methods=["POST"])
def route_get_userlist():
    data = request.get_json(force=True, silent=True) or {}
    if not check_key(data):
        return jsonify({"status": "error", "message": "clé invalide"}), 403

    room = data.get("room")
    if room is None:
        return jsonify({"status": "error", "message": "paramètre 'room' manquant"}), 400

    # Normaliser la room : str/int -> str, [one] -> str ; refuser listes >1
    if isinstance(room, (list, tuple, set)):
        if len(room) == 0:
            return jsonify({"status": "error", "message": "room vide"}), 400
        if len(room) > 1:
            return jsonify({"status": "error", "message": "une seule room attendue"}), 400
        room_val = str(next(iter(room)))
    elif isinstance(room, (str, int)):
        room_val = str(room)
    else:
        return jsonify({"status": "error", "message": "room invalide"}), 400

    async def _task():
        raw = fetch_cloudlink_ws_url()
        ws_url = sanitize_ws_url(raw)
        if not ws_url:
            return {"status": "error", "message": "invalid_ws_url", "detail": str(raw)}

        # headers définis globalement : WS_EXTRA_HEADERS
        try:
            # ouvrir connexion websocket brute (séparée du cl_client)
            async with websockets.connect(ws_url, extra_headers=WS_EXTRA_HEADERS, open_timeout=5) as ws:
                app.logger.debug(f"route_get_userlist: connected to {ws_url}, linking to {room_val!r}")

                # envoyer link en STRING (très important)
                await ws.send(json.dumps({"cmd": "link", "val": room_val}))
                # laisser le serveur traiter l'abonnement
                await asyncio.sleep(0.18)

                # demander la userlist pour la room (server enverra un 'ulist')
                await ws.send(json.dumps({"cmd": "get_userlist", "room": room_val}))

                proxy_snowflake = None
                # on lit jusqu'à timeout pour récupérer 'ulist'
                end_time = asyncio.get_event_loop().time() + 5.0
                while True:
                    timeout = end_time - asyncio.get_event_loop().time()
                    if timeout <= 0:
                        return {"status": "error", "message": "timeout waiting for ulist"}
                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        return {"status": "error", "message": "timeout waiting for ulist"}

                    try:
                        msg = json.loads(raw_msg)
                    except Exception:
                        # ignorer non-json
                        continue

                    # capture client object packet pour retrouver le snowflake du proxy
                    if msg.get("cmd") == "client_obj":
                        proxy_snowflake = msg.get("val", {}).get("id") or proxy_snowflake
                        continue

                    # réponse avec la liste d'utilisateurs attendue
                    if msg.get("cmd") == "ulist":
                        ulist = msg.get("val", {})
                        # si le serveur renvoie une liste, convertir en dict si possible
                        if isinstance(ulist, list):
                            # tenter de normaliser en dict : si éléments sont {username: {...}} -> fusionner
                            norm = {}
                            for item in ulist:
                                if isinstance(item, dict):
                                    # cas attendu : {"username": X} ou {"id":...}
                                    if "username" in item:
                                        key = item["username"]
                                        norm[key] = item
                                    elif "id" in item:
                                        norm[item["id"]] = item
                                # ignorer autres formats
                            ulist = norm

                        # retirer la propre entrée du proxy si présente (comparaison sur snowflake)
                        if proxy_snowflake and isinstance(ulist, dict):
                            to_remove = []
                            for k, v in ulist.items():
                                try:
                                    if v.get("id") == proxy_snowflake:
                                        to_remove.append(k)
                                except Exception:
                                    pass
                            for k in to_remove:
                                ulist.pop(k, None)

                        return {"status": "ok", "room": room_val, "users": ulist}

                # fin while
        except Exception as e:
            app.logger.exception("route_get_userlist: websocket error")
            return {"status": "error", "message": "ws_error", "detail": str(e)}

    result = asyncio.run(_task())
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
