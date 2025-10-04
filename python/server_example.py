# server_cloudlink.py
from cloudlink import server
from cloudlink.server.protocols import clpv4, scratch
import os
import asyncio

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
    port = int(port_env) if port_env else 3000

    srv.logger.info(f"Démarrage CloudLink WS sur {host}:{port}")
    srv.run(ip=host, port=port)