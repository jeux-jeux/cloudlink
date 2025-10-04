# proxy_http.py
import os
import asyncio
import threading
import random
import traceback
import functools
import time
from flask import Flask, request, jsonify
from cloudlink import client as cl_client
import websockets

app = Flask(__name__)

# URL du serveur CloudLink (doit être wss:// si CloudLink public via HTTPS)
CLOUDLINK_WS_URL = os.getenv("CLOUDLINK_WS_URL", "wss://cloudlink-server.onrender.com/")

# Headers à injecter (TurboWarp-like). Tu peux modifier/ajouter si nécessaire.
WS_EXTRA_HEADERS = [
    ("Origin", "tw-editor://."),
    ("User-Agent", "turbowarp-desktop/1.14.4")
]


# -------------------------
# Helpers
# -------------------------
# import au sommet du fichier si nécessaire
from urllib.parse import urlsplit

def sanitize_ws_url(url: str) -> str:
    """
    Nettoyage robuste de l'URL WebSocket :
     - supprime backslashes et espaces
     - convertit http(s) -> ws(s)
     - si aucun scheme présent, ajoute wss:// par défaut
     - garantit exactement UN slash final
    """
    if not url:
        return url

    # clean stray backslashes/spaces
    url = url.replace("\\", "").strip()

    # convert common http schemes to ws schemes
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    # if it already starts with ws:// or wss://, keep as is

    # if no scheme (hostname-only or leading slash), add wss:// by default
    parts = urlsplit(url)
    if not parts.scheme:
        # remove leading slashes if present, then prefix
        url = "wss://" + url.lstrip("/")

    # ensure there is exactly one trailing slash
    url = url.rstrip("/") + "/"

    return url



def ws_handshake_test_sync(url: str, extra_headers=None, timeout=6):
    """
    Effectue un test de handshake websocket en créant une boucle dédiée.
    Retourne un dict contenant ok, exc (repr), status_code, response_headers.
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

    # Use a fresh event loop so we don't collide with Flask's runtime
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
# CloudLink client runner (used by routes)
# -------------------------
async def cloudlink_action_async(action_coro, ws_url):
    """
    Create a cloudlink client, connect to ws_url, run action_coro, disconnect.
    This function is intended to be executed inside asyncio.run(...) from a sync context.
    """
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
            # Monkeypatch websockets.connect used by the client to include headers
            try:
                client.ws.connect = functools.partial(websockets.connect, extra_headers=WS_EXTRA_HEADERS)
            except Exception as e:
                # warn but continue (sometimes client.ws may be different depending on version)
                print("Warning: failed to monkeypatch client.ws.connect ->", e)

            client.run(host=ws_url)
        except Exception as e:
            result["error"] = str(e)
            traceback.print_exc()
            # ensure finished gets set so the outer coroutine won't wait forever
            try:
                # if we're inside an asyncio context, schedule finished.set(); else set directly
                finished.set()
            except Exception:
                pass

    thread = threading.Thread(target=run_client, daemon=True)
    thread.start()

    # Wait until disconnect (or error sets finished)
    await finished.wait()
    if result["ok"]:
        return {"status": "ok", "username": result.get("username")}
    else:
        return {"status": "error", "username": result.get("username"), "detail": result.get("error")}


def cloudlink_action(action_coro):
    """
    Wrapper used by Flask routes to synchronously run an async cloudlink action.
    It discovers/sanitizes URL from environment and runs the async flow.
    """
    raw = os.getenv("CLOUDLINK_WS_URL", CLOUDLINK_WS_URL)
    ws_url = sanitize_ws_url(raw)
    try:
        return asyncio.run(cloudlink_action_async(action_coro, ws_url))
    except Exception as e:
        # If asyncio.run fails (rare), return the error
        return {"status": "error", "message": "internal_error", "detail": str(e)}


# -------------------------
# HTTP routes (existing)
# -------------------------
@app.route("/global-message", methods=["POST"])
def global_message():
    data = request.get_json(force=True, silent=True) or {}
    rooms = data.get("rooms")
    message = data.get("message")
    if not isinstance(rooms, list) or not message:
        return jsonify({"status": "error", "message": "rooms (list) and message required"}), 400

    async def action(client, username):
        await client.send_gmsg(message, rooms=rooms)

    return jsonify(cloudlink_action(action))


@app.route("/private-message", methods=["POST"])
def private_message():
    data = request.get_json(force=True, silent=True) or {}
    username_target = data.get("username")
    room = data.get("room")
    message = data.get("message")
    if not username_target or not room or not message:
        return jsonify({"status": "error", "message": "username, room and message required"}), 400

    async def action(client, username):
        await client.send_pmsg(username_target, room, message)

    return jsonify(cloudlink_action(action))


@app.route("/global-variable", methods=["POST"])
def global_variable():
    data = request.get_json(force=True, silent=True) or {}
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")
    if not room or name is None:
        return jsonify({"status": "error", "message": "room and name required"}), 400

    async def action(client, username):
        await client.send_gvar(room, name, val)

    return jsonify(cloudlink_action(action))


@app.route("/private-variable", methods=["POST"])
def private_variable():
    data = request.get_json(force=True, silent=True) or {}
    username_target = data.get("username")
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")
    if not username_target or not room or name is None:
        return jsonify({"status": "error", "message": "username, room and name required"}), 400

    async def action(client, username):
        await client.send_pvar(username_target, room, name, val)

    return jsonify(cloudlink_action(action))


@app.route("/_health")
def health():
    return jsonify({"status": "ok"})


@app.route("/")
def home():
    return "Serveur HTTP en ligne ✅"


# -------------------------
# Diagnostic endpoints (NEW)
# -------------------------
@app.route("/debug-handshake", methods=["GET"])
def debug_handshake():
    """
    Effectue trois tests de handshake depuis le même container que le proxy :
      - default (no extra headers)
      - origin only
      - origin + user-agent
    Retourne le résultat JSON complet.
    """
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
    Tente réellement de lancer un cl_client() vers CLOUDLINK_WS_URL en appliquant le monkeypatch
    pour inclure WS_EXTRA_HEADERS. Attend le résultat (timeout configurable via ?timeout=).
    Retourne le dict de résultat (ok/error/detail).
    Exemple: POST /debug-connect-client  (optionnel JSON body ignored)
    """
    raw = os.getenv("CLOUDLINK_WS_URL", CLOUDLINK_WS_URL)
    ws_url = sanitize_ws_url(raw)
    timeout = int(request.args.get("timeout", "8"))

    # shared result container
    result = {"ok": False, "error": None, "trace": None}

    def run_client_and_capture():
        client = cl_client()
        finished_flag = threading.Event()

        @client.on_connect
        async def _on_connect():
            try:
                # Set username then disconnect quickly; this tests the handshake & successful connect.
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
            # monkeypatch connect to include headers
            try:
                client.ws.connect = functools.partial(websockets.connect, extra_headers=WS_EXTRA_HEADERS)
            except Exception as e:
                # best-effort; continue even if monkeypatch not possible
                print("Warning: monkeypatch failed in debug_connect_client:", e)

            client.run(host=ws_url)  # blocking call inside this thread
        except Exception as e:
            result["error"] = str(e)
            result["trace"] = traceback.format_exc()
            finished_flag.set()

    thread = threading.Thread(target=run_client_and_capture, daemon=True)
    thread.start()

    # Wait for completion or timeout
    thread.join(timeout=timeout)
    if thread.is_alive():
        # still alive -> probably timeout connecting
        return jsonify({"status": "timeout", "detail": f"Client thread alive after {timeout}s; check logs for more details", "result": result})
    else:
        return jsonify({"status": "finished", "result": result})


# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
