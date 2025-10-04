# python/run_example_server.py
from cloudlink import server
from cloudlink.server.protocols import clpv4, scratch
import asyncio
import os


class example_callbacks:
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


class example_commands:
    def __init__(self, parent, protocol):
        
        # Creating custom commands - This example adds a custom command called "foobar".
        @server.on_command(cmd="foobar", schema=protocol.schema)
        async def foobar(client, message):
            print("Foobar!")

            # Reading the IP address of the client is as easy as calling get_client_ip from the clpv4 protocol object.
            print(protocol.get_client_ip(client))

            # In case you need to report a status code, use send_statuscode.
            protocol.send_statuscode(
                client=client,
                code=protocol.statuscodes.ok,
                message=message
            )


class example_events:
    def __init__(self):
        pass

    async def on_close(self, client):
        print("Client", client.id, "disconnected.")

    async def on_connect(self, client):
        print("Client", client.id, "connected.") 


if __name__ == "__main__":
    # Initialize the server object
    srv = server()
    
    # Configure logging settings
    srv.logging.basicConfig(
        level=srv.logging.DEBUG
    )

    # Load protocols
    clpv4 = clpv4(srv)
    scratch = scratch(srv)

    # Load examples
    callbacks = example_callbacks(srv)
    commands = example_commands(srv, clpv4)
    events = example_events()

    # Binding callbacks - This example binds the "handshake" command with example callbacks.
    # You can bind as many functions as you want to a callback, but they must use async.
    # To bind callbacks to built-in methods (example: gmsg), see cloudlink.cl_methods.
    srv.bind_callback(cmd="handshake", schema=clpv4.schema, method=callbacks.test1)
    srv.bind_callback(cmd="handshake", schema=clpv4.schema, method=callbacks.test2)

    # Binding events - This example will print a client connect/disconnect message.
    # You can bind as many functions as you want to an event, but they must use async.
    # To see all possible events for the server, see cloudlink.events.
    srv.bind_event(server.on_connect, events.on_connect)
    srv.bind_event(server.on_disconnect, events.on_close)

    # You can also bind an event to a custom command. We'll bind callbacks.test3 to our 
    # foobar command from earlier.
    srv.bind_callback(cmd="foobar", schema=clpv4.schema, method=callbacks.test3)

    # Initialize SSL support (optional)
    # srv.enable_ssl(certfile="cert.pem", keyfile="privkey.pem")
    
    # Determine host/port for Render compatibility
    host = "0.0.0.0"
    port_env = os.getenv("PORT")
    try:
        port = int(port_env) if port_env else 3000
    except Exception:
        port = 3000

    srv.logger.info(f"Starting CloudLink server — binding to {host}:{port}")
    srv.run(ip=host, port=port)
