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
import time

app = Flask(__name__)
app.logger.setLevel(logging.DEBUG)

# -------------------------
# Configuration
# -------------------------
CLOUDLINK_WS_URL = os.getenv("CLOUDLINK_WS_URL", "wss://cloudlink-server.onrender.com/")
WS_EXTRA_HEADERS = [
    ("Origin", "tw-editor://."),
    ("User-Agent", "turbowarp-desktop/1.14.4")
]

# Timeouts (en secondes)
USERNAME_TIMEOUT = int(os.getenv("USERNAME_TIMEOUT", "5"))
ACTION_TIMEOUT = int(os.getenv("ACTION_TIMEOUT", "6"))
TOTAL_ACTION_TIMEOUT = int(os.getenv("TOTAL_ACTION_TIMEOUT", "10"))

# -------------------------
# Helpers
# -------------------------
def sanitize_ws_url(url: str) -> str:
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
    url = url.rstrip("/") + "/"
    return url


def ws_handshake_test_sync(url: str, extra_headers=None, timeout=6):
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
# Core CloudLink runner (avec timeouts)
# -------------------------
async def cloudlink_action_async(action_coro, ws_url, total_timeout=TOTAL_ACTION_TIMEOUT):
    app.logger.debug("proxy: cloudlink_action_async start")
    client = cl_client()
    finished_thread = threading.Event()
    result = {"ok": False, "error": None, "username": None, "trace": None}

    @client.on_connect
    async def _on_connect():
        app.logger.debug("proxy: on_connect called inside client loop")
        try:
            username = str(random.randint(100_000_000, 999_999_999))
            result["username"] = username
            app.logger.debug(f"proxy: attempting set_username {username} with timeout {USERNAME_TIMEOUT}s")

            # Timeout for set_username
            try:
                await asyncio.wait_for(client.protocol.set_username(username), timeout=USERNAME_TIMEOUT)
            except asyncio.TimeoutError:
                raise TimeoutError(f"set_username timed out after {USERNAME_TIMEOUT}s")

            app.logger.debug("proxy: set_username OK, running action with timeout %ss" % ACTION_TIMEOUT)

            # Timeout for the provided action (send gmsg/pmsg/gvar/pvar)
            try:
                await asyncio.wait_for(action_coro(client, username), timeout=ACTION_TIMEOUT)
            except asyncio.TimeoutError:
                raise TimeoutError(f"action timed out after {ACTION_TIMEOUT}s")

            # give short time for the client internal tasks to flush the packet
            await asyncio.sleep(0.15)
            result["ok"] = True
            app.logger.debug("proxy: action completed successfully inside client loop")
        except Exception as e:
            result["error"] = str(e)
            result["trace"] = traceback.format_exc()
            app.logger.exception("proxy: exception in client on_connect")
        finally:
            # Always attempt a graceful disconnect from inside client's loop
            try:
                app.logger.debug("proxy: calling client.disconnect() from inside client loop")
                await client.disconnect()
            except Exception:
                app.logger.exception("proxy: exception while disconnecting (inside client loop)")

    @client.on_disconnect
    async def _on_disconnect():
        app.logger.debug("proxy: client.on_disconnect -> set finished_thread")
        finished_thread.set()

    def run_client():
        try:
            app.logger.debug("proxy: run_client thread starting; monkeypatching ws.connect")
            try:
                client.ws.connect = functools.partial(websockets.connect, extra_headers=WS_EXTRA_HEADERS)
            except Exception as e:
                app.logger.warning(f"proxy: monkeypatch client.ws.connect failed: {e}")

            app.logger.debug(f"proxy: client.run(host={ws_url})")
            client.run(host=ws_url)
            app.logger.debug("proxy: client.run returned (thread end)")
            finished_thread.set()
        except Exception as e:
            result["error"] = str(e)
            result["trace"] = traceback.format_exc()
            app.logger.exception("proxy: exception inside run_client")
            try:
                finished_thread.set()
            except Exception:
                pass

    thread = threading.Thread(target=run_client, daemon=True)
    thread.start()

    # Wait for finished_thread with an overall timeout to avoid blocking HTTP forever
    loop = asyncio.get_running_loop()
    try:
        app.logger.debug(f"proxy: waiting for finished_thread up to {total_timeout}s")
        await asyncio.wait_for(loop.run_in_executor(None, finished_thread.wait), timeout=total_timeout)
    except asyncio.TimeoutError:
        app.logger.warning("proxy: timeout waiting for client to finish")
        # Provide trace if exists
        out = {"status": "error", "username": result.get("username"), "detail": "timeout waiting for disconnect"}
        if result.get("trace"):
            out["trace"] = result.get("trace")
        return out
    except Exception as e:
        app.logger.exception("proxy: unexpected error waiting for finished_thread")
        return {"status": "error", "username": result.get("username"), "detail": str(e)}

    if result.get("ok"):
        return {"status": "ok", "username": result.get("username")}
    else:
        out = {"status": "error", "username": result.get("username"), "detail": result.get("error")}
        if result.get("trace"):
            out["trace"] = result.get("trace")
        return out


def cloudlink_action(action_coro):
    raw = os.getenv("CLOUDLINK_WS_URL", CLOUDLINK_WS_URL)
    ws_url = sanitize_ws_url(raw)
    app.logger.info(f"proxy: starting cloudlink_action -> {ws_url}")
    try:
        return asyncio.run(cloudlink_action_async(action_coro, ws_url))
    except Exception as e:
        app.logger.exception("proxy: exception in cloudlink_action asyncio.run")
        return {"status": "error", "message": "internal_error", "detail": str(e)}


# -------------------------
# Routes (4 principales)
# -------------------------
@app.route("/global-message", methods=["POST"])
def route_global_message():
    app.logger.info("proxy: /global-message called")
    data = request.get_json(force=True, silent=True) or {}
    rooms = data.get("rooms")
    message = data.get("message")
    if not isinstance(rooms, list) or not message:
        return jsonify({"status": "error", "message": "rooms (list) and message required"}), 400

    async def action(client, username):
        app.logger.debug(f"proxy: action send gmsg username={username} rooms={rooms} message={message!r}")
        # send_packet is synchronous; don't await it
        client.send_packet({"cmd": "gmsg", "val": message, "rooms": rooms})

    return jsonify(cloudlink_action(action))


@app.route("/private-message", methods=["POST"])
def route_private_message():
    app.logger.info("proxy: /private-message called")
    data = request.get_json(force=True, silent=True) or {}
    username_target = data.get("username")
    room = data.get("room")
    message = data.get("message")
    if not username_target or not room or not message:
        return jsonify({"status": "error", "message": "username, room and message required"}), 400

    async def action(client, username):
        app.logger.debug(f"proxy: action send pmsg username={username} -> target={username_target} room={room}")
        client.send_packet({"cmd": "pmsg", "val": message, "id": username_target, "room": room})

    return jsonify(cloudlink_action(action))


@app.route("/global-variable", methods=["POST"])
def route_global_variable():
    app.logger.info("proxy: /global-variable called")
    data = request.get_json(force=True, silent=True) or {}
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")
    if not room or name is None:
        return jsonify({"status": "error", "message": "room and name required"}), 400

    async def action(client, username):
        app.logger.debug(f"proxy: action send gvar name={name} room={room}")
        client.send_packet({"cmd": "gvar", "name": name, "val": val, "room": room})

    return jsonify(cloudlink_action(action))


@app.route("/private-variable", methods=["POST"])
def route_private_variable():
    app.logger.info("proxy: /private-variable called")
    data = request.get_json(force=True, silent=True) or {}
    username_target = data.get("username")
    room = data.get("room")
    name = data.get("name")
    val = data.get("val")
    if not username_target or not room or name is None:
        return jsonify({"status": "error", "message": "username, room and name required"}), 400

    async def action(client, username):
        app.logger.debug(f"proxy: action send pvar name={name} room={room} -> target={username_target}")
        client.send_packet({"cmd": "pvar", "name": name, "val": val, "room": room, "id": username_target})

    return jsonify(cloudlink_action(action))


# -------------------------
# Health & Debug
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
    raw = os.getenv("CLOUDLINK_WS_URL", CLOUDLINK_WS_URL)
    ws_url = sanitize_ws_url(raw)
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
        app.logger.warning("proxy: debug_connect_client timed out")
        return jsonify({"status": "timeout", "detail": f"Client still alive after {timeout}s", "result": result})
    else:
        return jsonify({"status": "finished", "result": result})


# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.logger.info(f"proxy: starting app on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
