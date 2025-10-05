from .schema import cl4_protocol

class clpv4:
    def __init__(self, server):

        self.warn_if_multiple_username_matches = True
        self.enable_motd = False
        self.motd_message = str()
        self.real_ip_header = None
        self.schema = cl4_protocol
        self.__qualname__ = "clpv4"

        # Origines autorisées pour la connexion
        self.allowed_origins = [
            "tw-editor://.",
            "tw-editor://",
            "https://cloudlink-manager.onrender.com",
            "https://cloudlink-manager.onrender.com/",
            "https://jeux-jeux.github.io"
        ]

        # Status codes
        class statuscodes:
            info = "I"
            error = "E"
            test = (info, 0, "Test")
            echo = (info, 1, "Echo")
            ok = (info, 100, "OK")
            syntax = (error, 101, "Syntax")
            datatype = (error, 102, "Datatype")
            id_not_found = (error, 103, "ID not found")
            id_not_specific = (error, 104, "ID not specific enough")
            internal_error = (error, 105, "Internal server error")
            empty_packet = (error, 106, "Empty packet")
            id_already_set = (error, 107, "ID already set")
            refused = (error, 108, "Refused")
            invalid_command = (error, 109, "Invalid command")
            disabled_command = (error, 110, "Command disabled")
            id_required = (error, 111, "ID required")
            id_conflict = (error, 112, "ID conflict")
            too_large = (error, 113, "Too large")
            json_error = (error, 114, "JSON error")
            room_not_joined = (error, 115, "Room not joined")

            @staticmethod
            def generate(code: tuple):
                return f"{code[0]}:{code[1]} | {code[2]}", code[1]

        self.statuscodes = statuscodes

        # Récupération IP client
        def get_client_ip(client):
            if self.real_ip_header and self.real_ip_header in client.request_headers:
                return client.request_headers.get(self.real_ip_header)
            if type(client.remote_address) == tuple:
                return str(client.remote_address[0])
        self.get_client_ip = get_client_ip

        # Validation des messages
        def valid(client, message, schema, allow_unknown=True):
            validator = server.validator(schema, allow_unknown=allow_unknown)
            if validator.validate(message):
                return True
            send_statuscode(client, statuscodes.syntax, details=dict(validator.errors))
            return False
        self.valid = valid

        # Envoi des statuscodes
        def send_statuscode(client, code, details=None, message=None, val=None):
            code_human, code_id = statuscodes.generate(code)
            tmp_message = {
                "cmd": "statuscode",
                "code": code_human,
                "code_id": code_id
            }
            if details:
                tmp_message["details"] = details
            if message and "listener" in message:
                tmp_message["listener"] = message["listener"]
            if val:
                tmp_message["val"] = val
            server.send_packet(client, tmp_message)
        self.send_statuscode = send_statuscode

        # Envoi de messages simples
        def send_message(client, payload, message=None):
            if message and "listener" in message:
                payload["listener"] = message["listener"]
            server.send_packet(client, payload)
        self.send_message = send_message

        # Vérifie si username défini
        def require_username_set(client, message):
            if not client.username_set:
                send_statuscode(
                    client,
                    statuscodes.id_required,
                    details="This command requires setting a username.",
                    message=message
                )
            return client.username_set
        self.require_username_set = require_username_set

        # Récupération rooms
        def gather_rooms(client, message):
            if "rooms" in message:
                rooms = message["rooms"]
                if type(rooms) == str:
                    rooms = {rooms}
                if type(rooms) == list:
                    rooms = set(rooms)
                return rooms
            return client.rooms
        self.gather_rooms = gather_rooms

        # Génération user object
        def generate_user_object(obj):
            if obj.username_set:
                return {
                    "id": obj.snowflake,
                    "username": obj.username,
                    "uuid": str(obj.id)
                }
            return {
                "id": obj.snowflake,
                "uuid": str(obj.id)
            }
        self.generate_user_object = generate_user_object

        # Handshake automatique
        async def notify_handshake(client):
            if client.handshake:
                return
            client.handshake = True
            server.send_packet(client, {"cmd": "client_ip", "val": get_client_ip(client)})
            server.send_packet(client, {"cmd": "server_version", "val": server.version})
            if self.enable_motd:
                server.send_packet(client, {"cmd": "motd", "val": self.motd_message})
            server.send_packet(client, {"cmd": "client_obj", "val": generate_user_object(client)})
            async for room in server.async_iterable(client.rooms):
                server.send_packet(client, {
                    "cmd": "ulist",
                    "mode": "set",
                    "val": server.rooms_manager.generate_userlist(room, cl4_protocol),
                    "rooms": room
                })

        # -------------------------
        # Validation de l'origine
        # -------------------------
        @server.on_connect
        async def validate_origin(client):
            origin = client.request_headers.get("Origin")
            if origin not in self.allowed_origins:
                server.logger.warning(f"Client {client.snowflake} rejected: Origin {origin} not allowed")
                await client.disconnect(code=4001, reason="Origin not allowed")

        # -------------------------
        # Exceptions & événements
        # -------------------------
        @server.on_exception(exception_type=server.exceptions.ValidationError, schema=cl4_protocol)
        async def validation_failure(client, details):
            send_statuscode(client, statuscodes.syntax, details=dict(details))

        @server.on_exception(exception_type=server.exceptions.InvalidCommand, schema=cl4_protocol)
        async def invalid_command(client, details):
            send_statuscode(client, statuscodes.invalid_command, details=f"{details} is an invalid command.")

        @server.on_disabled_command(schema=cl4_protocol)
        async def disabled_command(client, details):
            send_statuscode(client, statuscodes.disabled_command, details=f"{details} is a disabled command.")

        @server.on_exception(exception_type=server.exceptions.JSONError, schema=cl4_protocol)
        async def json_exception(client, details):
            send_statuscode(client, statuscodes.json_error, details=f"A JSON error was raised: {details}")

        @server.on_exception(exception_type=server.exceptions.EmptyMessage, schema=cl4_protocol)
        async def empty_message(client):
            send_statuscode(client, statuscodes.empty_packet, details="Your client has sent an empty message.")

        # -------------------------
        # Commandes principales
        # -------------------------
        @server.on_protocol_identified(schema=cl4_protocol)
        async def protocol_identified(client):
            server.logger.debug(f"Adding client {client.snowflake} to default room.")
            server.rooms_manager.subscribe(client, "default")

        @server.on_protocol_disconnect(schema=cl4_protocol)
        async def protocol_disconnect(client):
            server.logger.debug(f"Removing client {client.snowflake} from rooms...")
            async for room_id in server.async_iterable(server.copy(client.rooms)):
                server.rooms_manager.unsubscribe(client, room_id)
                if client.username_set:
                    clients = await server.rooms_manager.get_all_in_rooms(room_id, cl4_protocol)
                    clients = server.copy(clients)
                    server.send_packet(clients, {
                        "cmd": "ulist",
                        "mode": "remove",
                        "val": generate_user_object(client),
                        "rooms": room_id
                    })

        # -------------------------
        # Les commandes (ping, handshake, gmsg, pmsg, gvar, pvar, setid, link, unlink, direct)
        # -------------------------
        # (ici tu colles toutes les commandes existantes du fichier original)
        # Exemple minimal : ping
        @server.on_command(cmd="ping", schema=cl4_protocol)
        async def on_ping(client, message):
            send_statuscode(client, statuscodes.ok, message=message)
