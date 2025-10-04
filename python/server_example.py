# python/run_example_server_render.py
from cloudlink import server
from cloudlink.server.protocols import clpv4, scratch
import os
import asyncio


# === EXAMPLES CALLBACKS, COMMANDS, EVENTS ===

class ExampleCallbacks:
    def __init__(self, parent):
        self.parent = parent

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
        # Exemple commande personnalisée "foobar"
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


# === MAIN SERVER ===

if __name__ == "__main__":
    srv = server()
    
    # Logging DEBUG
    srv.logging.basicConfig(level=srv.logging.DEBUG)

    # Protocoles
    clpv4_protocol = clpv4(srv)
    scratch_protocol = scratch(srv)

    # Exemples
    callbacks = ExampleCallbacks(srv)
    commands = ExampleCommands(srv, clpv4_protocol)
    events = ExampleEvents()

    # Bind callbacks handshake
    srv.bind_callback(cmd="handshake", schema=clpv4_protocol.schema, method=callbacks.test1)
    srv.bind_callback(cmd="handshake", schema=clpv4_protocol.schema, method=callbacks.test2)
    # Bind foobar callback
    srv.bind_callback(cmd="foobar", schema=clpv4_protocol.schema, method=callbacks.test3)

    # Bind events connect/disconnect
    srv.bind_event(server.on_connect, events.on_connect)
    srv.bind_event(server.on_disconnect, events.on_close)

    # Host et port pour Render
    host = "0.0.0.0"  # Très important : écoute toutes les interfaces
    port_env = os.getenv("PORT")
    port = int(port_env) if port_env else 3000

    srv.logger.info(f"Starting CloudLink server on {host}:{port}")
    srv.run(ip=host, port=port)