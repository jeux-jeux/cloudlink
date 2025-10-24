# server_cloudlink.py
from cloudlink import server
from cloudlink.server.protocols import clpv4, scratch
import os
import asyncio
from flask import Flask, request, jsonify

CLE = os.environ.get('CLE')
PROXY_AUTH_URL = os.environ.get('URL')

resp = request.post(PROXY_AUTH_URL, json={"cle": CLE}, timeout=5 )
resp.raise_for_status()
j = resp.json()
port_env = j.get("port")

# --- Callbacks & Events ---
class Callbacks:
    async def handshake_cb(self, client, message):
        print(f"Handshake reçu pour client {client.id}")

    async def foobar_cb(self, client, message):
        print(f"Commande foobar reçue pour client {client.id}")

class Events:
    async def on_connect(self, client):
        print(f"Client {client.id} connecté")
    async def on_disconnect(self, client):
        print(f"Client {client.id} déconnecté")

# --- Serveur principal ---
if __name__ == "__main__":
    srv = server()
    srv.logging.basicConfig(level=srv.logging.DEBUG)

    # Protocoles
    clpv4_protocol = clpv4(srv)
    scratch_protocol = scratch(srv)
    # --- Handler serveur pour la commande "kick" ---

    ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()  # optionnel, protège l'API kick

    @srv.on_command(cmd="kick", schema=clpv4_protocol.schema)
    async def on_kick(client, message):
        """
        message attendu:
        {
          "room": "default",
          "targets": ["snowflake1","snowflake2"]   # liste d'IDs (snowflakes/uuids) ou d'objets utilisables par find_obj
          "secret": "xxx"  # facultatif si ADMIN_SECRET configuré
        }
        """
        # Autorisation : si ADMIN_SECRET configuré, exiger qu'il soit fourni ou que le client soit admin
        if ADMIN_SECRET:
            ok_auth = False
            # si client a défini un username "admin" on l'accepte aussi
            try:
                if client.username_set and getattr(client, "username", "") == "admin":
                    ok_auth = True
            except Exception:
                pass
            if not ok_auth:
                if message.get("secret") != ADMIN_SECRET:
                    # Refuser
                    srv.send_packet(client, {
                        "cmd": "statuscode",
                        "code": "E:108 | Refused",
                        "details": "Not authorized to perform kick"
                    })
                    return

        room = message.get("room")
        targets = message.get("targets", [])
        if not room or not isinstance(targets, list) or not targets:
            srv.send_packet(client, {
                "cmd": "statuscode",
                "code": "E:101 | Syntax",
                "details": "room and targets (list) required"
            })
            return
    
        kicked = []
        not_found = []

        for t in targets:
            try:
                # find_obj tente de résoudre snowflake/uuid/username/client obj
                tmp_client = srv.clients_manager.find_obj(t)
                # unsubscribe from room (server.rooms_manager.unsubscribe is sync)
                try:
                    srv.rooms_manager.unsubscribe(tmp_client, room)
                except Exception:
                    # ignore if not subscribed
                    pass

                # request close (this schedules the close coroutine)
                srv.close_connection(tmp_client, code=4000, reason="Kicked by admin")
                kicked.append(t)
            except Exception as e:
                # find_obj lance une exception NoResultsFound — on capture tout et marque not_found
                not_found.append({"id": t, "error": str(e)})

        # Répondre à l'initiateur (client admin)
        srv.send_packet(client, {
            "cmd": "statuscode",
            "code": "I:100 | OK",
            "details": {"kicked": kicked, "not_found": not_found}
         })


    callbacks = Callbacks()
    events = Events()

    # Bind callbacks handshake et foobar
    srv.bind_callback(cmd="handshake", schema=clpv4_protocol.schema, method=callbacks.handshake_cb)
    srv.bind_callback(cmd="foobar", schema=clpv4_protocol.schema, method=callbacks.foobar_cb)

    # Bind events connect/disconnect
    srv.bind_event(server.on_connect, events.on_connect)
    srv.bind_event(server.on_disconnect, events.on_disconnect)

    # Host et port Render
    host = "0.0.0.0"  # Écoute toutes les interfaces
    port_env = os.getenv("CLOUDLINK_PORT")
    port = int(port_env)
    server.allowed_origins = [
		"tw-editor://.",
		"tw-editor://",
		"https://cloudlink-manager.onrender.com",
		"https://cloudlink-manager.onrender.com/",
		"https://jeux-jeux.github.io",
		"*"
	]
    srv.logger.info(f"Démarrage CloudLink WS sur {host}:{port}")
    srv.run(ip=host, port=port)
