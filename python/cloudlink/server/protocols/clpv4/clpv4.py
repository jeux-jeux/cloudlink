# cloudlink/server/protocols/clpv4.py
from .schema import cl4_protocol
import os

class clpv4:
    def __init__(self, server):

        self.warn_if_multiple_username_matches = True
        self.enable_motd = False
        self.motd_message = str()
        self.real_ip_header = None
        self.schema = cl4_protocol
        self.__qualname__ = "clpv4"

        # Par défaut : origines autorisées (tu peux étendre via CLOUDLINK_ALLOWED_ORIGINS)
        default_allowed = [
            "tw-editor://.",
            "tw-editor://",
            "https://cloudlink-manager.onrender.com/",
            "https://cloudlink-manager.onrender.com",
            "https://jeux-jeux.github.io"
        ]

        # Charger origines autorisées additionnelles depuis l'env (séparées par des virgules)
        env_allowed = os.getenv("CLOUDLINK_ALLOWED_ORIGINS", "").strip()
        if env_allowed:
            env_list = [item.strip() for item in env_allowed.split(",") if item.strip()]
        else:
            env_list = []

        # Flag : accepter les connexions sans header Origin (utile pour clients non-navigateur)
        ALLOW_NO_ORIGIN = True

        # Construire liste finale d'origines autorisées
        self.allowed_origins = list(dict.fromkeys(default_allowed + env_list))

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
        # Validation de l'origine (robuste)
        # -------------------------
        def _normalize_origin(o):
            if o is None:
                return None
            o = o.strip().lower()
            # remove trailing slashes
            while o.endswith("/"):
                o = o[:-1]
            return o

        # Normalize allowed origins and prepare matchers (supports simple wildcard suffix '*')
        normalized_allowed = []
        for item in self.allowed_origins:
            it = _normalize_origin(item)
            if not it:
                continue
            if it.endswith("*"):
                normalized_allowed.append(("prefix", it[:-1]))
            else:
                normalized_allowed.append(("exact", it))

        @server.on_connect
        async def validate_origin(client):
            origin = client.request_headers.get("Origin")
            origin_norm = _normalize_origin(origin)

            # Allow when no origin header is present (unless you change ALLOW_NO_ORIGIN)
            if origin_norm is None:
                if ALLOW_NO_ORIGIN:
                    server.logger.debug(f"Client {getattr(client, 'snowflake', '?')} has no Origin header -> allowed (ALLOW_NO_ORIGIN=True).")
                    return
                else:
                    server.logger.warning(f"Client {getattr(client, 'snowflake', '?')} rejected: missing Origin header.")
                    await client.disconnect(code=4001, reason="Origin required")
                    return

            # Check against allowed list
            allowed = False
            for kind, pattern in normalized_allowed:
                if kind == "exact" and origin_norm == pattern:
                    allowed = True
                    break
                if kind == "prefix" and origin_norm.startswith(pattern):
                    allowed = True
                    break

            if not allowed:
                server.logger.warning(f"Client {getattr(client, 'snowflake', '?')} rejected: Origin '{origin}' not allowed. Allowed: {self.allowed_origins}")
                # déconnecter proprement le client en expliquant la raison
                try:
                    await client.disconnect(code=4001, reason="Origin not allowed")
                except Exception:
                    # si disconnect asynchrone échoue, forcer suppression (log)
                    server.logger.exception("Failed to disconnect client after origin rejection")
                return
            else:
                server.logger.debug(f"Client {getattr(client, 'snowflake', '?')} accepted origin '{origin}'.")

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
        # Protocol identified / disconnect events
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
        # Commandes principales
        # -------------------------
        @server.on_command(cmd="handshake", schema=cl4_protocol)
        async def on_handshake(client, message):
            await notify_handshake(client)
            send_statuscode(client, statuscodes.ok, message=message)

        @server.on_command(cmd="ping", schema=cl4_protocol)
        async def on_ping(client, message):
            send_statuscode(client, statuscodes.ok, message=message)

        @server.on_command(cmd="gmsg", schema=cl4_protocol)
        async def on_gmsg(client, message):
            if not valid(client, message, cl4_protocol.gmsg):
                return
            rooms = gather_rooms(client, message)
            async for room in server.async_iterable(rooms):
                if room not in client.rooms:
                    send_statuscode(
                        client,
                        statuscodes.room_not_joined,
                        details=f'Attempted to access room {room} while not joined.',
                        message=message
                    )
                    return
                clients = await server.rooms_manager.get_all_in_rooms(room, cl4_protocol)
                clients = server.copy(clients)
                if "listener" in message:
                    clients.remove(client)
                    tmp_message = {"cmd": "gmsg", "val": message["val"]}
                    server.send_packet(clients, tmp_message)
                    tmp_message = {
                        "cmd": "gmsg",
                        "val": message["val"],
                        "listener": message["listener"],
                        "rooms": room
                    }
                    server.send_packet(client, tmp_message)
                else:
                    server.send_packet(clients, {
                        "cmd": "gmsg",
                        "val": message["val"],
                        "rooms": room
                    })

        @server.on_command(cmd="pmsg", schema=cl4_protocol)
        async def on_pmsg(client, message):
            if not valid(client, message, cl4_protocol.pmsg):
                return
            if not require_username_set(client, message):
                return
            rooms = gather_rooms(client, message)
            any_results_found = False
            async for room in server.async_iterable(rooms):
                if room not in client.rooms:
                    send_statuscode(
                        client,
                        statuscodes.room_not_joined,
                        details=f'Attempted to access room {room} while not joined.',
                        message=message
                    )
                    return
                clients = await server.rooms_manager.get_specific_in_room(room, cl4_protocol, message['id'])
                if not len(clients):
                    continue
                if not any_results_found:
                    any_results_found = True
                if self.warn_if_multiple_username_matches and len(clients) >> 1:
                    send_statuscode(
                        client,
                        statuscodes.id_not_specific,
                        details=f'Multiple matches found for {message["id"]}, found {len(clients)} matches. Please use Snowflakes, UUIDs, or client objects instead.',
                        message=message
                    )
                    return
                tmp_message = {
                    "cmd": "pmsg",
                    "val": message["val"],
                    "origin": generate_user_object(client),
                    "rooms": room
                }
                server.send_packet(clients, tmp_message)
            if not any_results_found:
                send_statuscode(
                    client,
                    statuscodes.id_not_found,
                    details=f'No matches found: {message["id"]}',
                    message=message
                )
                return
            send_statuscode(client, statuscodes.ok, message=message)

        @server.on_command(cmd="gvar", schema=cl4_protocol)
        async def on_gvar(client, message):
            if not valid(client, message, cl4_protocol.gvar):
                return
            rooms = gather_rooms(client, message)
            async for room in server.async_iterable(rooms):
                if room not in client.rooms:
                    send_statuscode(
                        client,
                        statuscodes.room_not_joined,
                        details=f'Attempted to access room {room} while not joined.',
                        message=message
                    )
                    return
                clients = await server.rooms_manager.get_all_in_rooms(room, cl4_protocol)
                clients = server.copy(clients)
                tmp_message = {
                    "cmd": "gvar",
                    "name": message["name"],
                    "val": message["val"],
                    "rooms": room
                }
                if "listener" in message:
                    clients.remove(client)
                    server.send_packet(clients, tmp_message)
                    tmp_message["listener"] = message["listener"]
                    server.send_packet(client, tmp_message)
                else:
                    server.send_packet(clients, tmp_message)

        @server.on_command(cmd="pvar", schema=cl4_protocol)
        async def on_pvar(client, message):
            if not valid(client, message, cl4_protocol.pvar):
                return
            if not require_username_set(client, message):
                return
            rooms = gather_rooms(client, message)
            any_results_found = False
            async for room in server.async_iterable(rooms):
                if room not in client.rooms:
                    send_statuscode(
                        client,
                        statuscodes.room_not_joined,
                        details=f'Attempted to access room {room} while not joined.',
                        message=message
                    )
                    return
                clients = await server.rooms_manager.get_specific_in_room(room, cl4_protocol, message['id'])
                clients = server.copy(clients)
                if not len(clients):
                    continue
                if not any_results_found:
                    any_results_found = True
                if self.warn_if_multiple_username_matches and len(clients) >> 1:
                    send_statuscode(
                        client,
                        statuscodes.id_not_specific,
                        details=f'Multiple matches found for {message["id"]}, found {len(clients)} matches. Please use Snowflakes, UUIDs, or client objects instead.',
                        message=message
                    )
                    return
                tmp_message = {
                    "cmd": "pvar",
                    "name": message["name"],
                    "val": message["val"],
                    "origin": generate_user_object(client),
                    "rooms": room
                }
                server.send_packet(clients, tmp_message)
            if not any_results_found:
                send_statuscode(
                    client,
                    statuscodes.id_not_found,
                    details=f'No matches found: {message["id"]}',
                    message=message
                )
                return
            send_statuscode(client, statuscodes.ok, message=message)

        @server.on_command(cmd="setid", schema=cl4_protocol)
        async def on_setid(client, message):
            if not valid(client, message, cl4_protocol.setid):
                return
            if client.username_set:
                server.logger.error(f"Client {client.snowflake} attempted to set username again!")
                send_statuscode(
                    client,
                    statuscodes.id_already_set,
                    val=generate_user_object(client),
                    message=message
                )
                return
            server.rooms_manager.unsubscribe(client, "default")
            server.clients_manager.set_username(client, message['val'])
            server.rooms_manager.subscribe(client, "default")
            clients = await server.rooms_manager.get_all_in_rooms("default", cl4_protocol)
            clients = server.copy(clients)
            clients.remove(client)
            server.send_packet(clients, {
                "cmd": "ulist",
                "mode": "add",
                "val": generate_user_object(client),
                "rooms": "default"
            })
            server.send_packet(client, {
                "cmd": "ulist",
                "mode": "set",
                "val": server.rooms_manager.generate_userlist("default", cl4_protocol),
                "rooms": "default"
            })
            send_statuscode(
                client,
                statuscodes.ok,
                val=generate_user_object(client),
                message=message
            )

        @server.on_command(cmd="link", schema=cl4_protocol)
        async def on_link(client, message):
            if not valid(client, message, cl4_protocol.linking):
                return
            if not require_username_set(client, message):
                return
            if type(message["val"]) in [list, str]:
                if type(message["val"]) == list:
                    message["val"] = set(message["val"])
                if type(message["val"]) == str:
                    message["val"] = {message["val"]}
            if not "default" in message["val"]:
                server.rooms_manager.unsubscribe(client, "default")
                clients = await server.rooms_manager.get_all_in_rooms("default", cl4_protocol)
                clients = server.copy(clients)
                server.send_packet(clients, {
                    "cmd": "ulist",
                    "mode": "remove",
                    "val": generate_user_object(client),
                    "rooms": "default"
                })
            async for room in server.async_iterable(message["val"]):
                server.rooms_manager.subscribe(client, room)
                clients = await server.rooms_manager.get_all_in_rooms(room, cl4_protocol)
                clients = server.copy(clients)
                clients.remove(client)
                server.send_packet(clients, {
                    "cmd": "ulist",
                    "mode": "add",
                    "val": generate_user_object(client),
                    "rooms": room
                })
                server.send_packet(client, {
                    "cmd": "ulist",
                    "mode": "set",
                    "val": server.rooms_manager.generate_userlist(room, cl4_protocol),
                    "rooms": room
                })
            send_statuscode(client, statuscodes.ok, message=message)

        @server.on_command(cmd="unlink", schema=cl4_protocol)
        async def on_unlink(client, message):
            if not valid(client, message, cl4_protocol.linking):
                return
            if not require_username_set(client, message):
                return
            if type(message["val"]) == str and not len(message["val"]):
                message["val"] = client.rooms
            if type(message["val"]) in [list, str]:
                if type(message["val"]) == list:
                    message["val"] = set(message["val"])
                if type(message["val"]) == str:
                    message["val"] = {message["val"]}
            async for room in server.async_iterable(message["val"]):
                server.rooms_manager.unsubscribe(client, room)
                clients = await server.rooms_manager.get_all_in_rooms(room, cl4_protocol)
                clients = server.copy(clients)
                server.send_packet(clients, {
                    "cmd": "ulist",
                    "mode": "remove",
                    "val": generate_user_object(client),
                    "rooms": room
                })
            if not len(client.rooms):
                server.rooms_manager.subscribe(client, "default")
                clients = await server.rooms_manager.get_all_in_rooms("default", cl4_protocol)
                clients = server.copy(clients)
                clients.remove(client)
                server.send_packet(clients, {
                    "cmd": "ulist",
                    "mode": "add",
                    "val": generate_user_object(client),
                    "rooms": "default"
                })
                server.send_packet(client, {
                    "cmd": "ulist",
                    "mode": "set",
                    "val": server.rooms_manager.generate_userlist("default", cl4_protocol),
                    "rooms": "default"
                })
            send_statuscode(client, statuscodes.ok, message=message)

        @server.on_command(cmd="direct", schema=cl4_protocol)
        async def on_direct(client, message):
            if not valid(client, message, cl4_protocol.direct):
                return
            try:
                tmp_client = server.clients_manager.find_obj(message["id"])
                tmp_msg = {
                    "cmd": "direct",
                    "val": message["val"]
                }
                if client.username_set:
                    tmp_msg["origin"] = generate_user_object(client)
                else:
                    tmp_msg["origin"] = {
                        "id": client.snowflake,
                        "uuid": str(client.id)
                    }
                if "listener" in message:
                    tmp_msg["listener"] = message["listener"]
                server.send_packet_unicast(tmp_client, tmp_msg)
            except server.clients_manager.exceptions.NoResultsFound:
                send_statuscode(
                    client,
                    statuscodes.id_not_found,
                    message=message
                )
                return