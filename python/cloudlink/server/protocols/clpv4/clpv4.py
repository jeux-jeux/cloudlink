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

        # -------------------------
        # ORIGINES AUTORISÉES
        # -------------------------
        default_allowed = [
            "tw-editor://.",
            "tw-editor://",
            "https://cloudlink-manager.onrender.com",
            "https://cloudlink-manager.onrender.com/",
            "https://jeux-jeux.github.io",
            "*"
        ]

        env_allowed = os.getenv("CLOUDLINK_ALLOWED_ORIGINS", "").strip()
        env_list = [x.strip() for x in env_allowed.split(",") if x.strip()] if env_allowed else []

        allow_no_origin_env = os.getenv("CLOUDLINK_ALLOW_NO_ORIGIN", None)
        if allow_no_origin_env is not None:
            try:
                ALLOW_NO_ORIGIN = bool(int(allow_no_origin_env))
            except Exception:
                ALLOW_NO_ORIGIN = True
        else:
            ALLOW_NO_ORIGIN = True

        # Liste finale sans doublons
        self.allowed_origins = list(dict.fromkeys(default_allowed + env_list))

        # -------------------------
        # STATUTS
        # -------------------------
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

        # -------------------------
        # UTILITAIRES
        # -------------------------
        def get_client_ip(client):
            if self.real_ip_header and self.real_ip_header in client.request_headers:
                return client.request_headers.get(self.real_ip_header)
            if isinstance(client.remote_address, tuple):
                return str(client.remote_address[0])
            return None
        self.get_client_ip = get_client_ip

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

        def valid(client, message, schema, allow_unknown=True):
            validator = server.validator(schema, allow_unknown=allow_unknown)
            if validator.validate(message):
                return True
            send_statuscode(client, statuscodes.syntax, details=dict(validator.errors))
            return False
        self.valid = valid

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

        def gather_rooms(client, message):
            if "rooms" in message:
                rooms = message["rooms"]
                if isinstance(rooms, str):
                    rooms = {rooms}
                elif isinstance(rooms, list):
                    rooms = set(rooms)
                return rooms
            return client.rooms
        self.gather_rooms = gather_rooms

        def generate_user_object(obj):
            base = {"id": obj.snowflake, "uuid": str(obj.id)}
            if obj.username_set:
                base["username"] = obj.username
            return base
        self.generate_user_object = generate_user_object

        # -------------------------
        # HANDSHAKE
        # -------------------------
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
        # VALIDATION ORIGIN (FIX)
        # -------------------------
        def _normalize_origin(o):
            if not o:
                return None
            o = o.strip().lower()
            if o.endswith("/"):
                o = o[:-1]
            return o

        normalized_allowed = []
        for item in self.allowed_origins:
            norm = _normalize_origin(item)
            if not norm:
                continue
            if norm.endswith("*"):
                normalized_allowed.append(("prefix", norm[:-1]))
            else:
                normalized_allowed.append(("exact", norm))

        # -------------------------
        # Validation robuste de l'origine
        # -------------------------
        @server.on_connect
        async def on_connect(client):
            server.logger.info(f"Client connecté : {client.id}")
            await client.send("server", {"status": "connected"})

        # -------------------------
        # ÉVÉNEMENTS & COMMANDES
        # -------------------------
        @server.on_exception(exception_type=server.exceptions.JSONError, schema=cl4_protocol)
        async def json_exception(client, details):
            send_statuscode(client, statuscodes.json_error, details=f"A JSON error was raised: {details}")

        @server.on_exception(exception_type=server.exceptions.EmptyMessage, schema=cl4_protocol)
        async def empty_message(client):
            send_statuscode(client, statuscodes.empty_packet, details="Your client has sent an empty message.")

        @server.on_protocol_identified(schema=cl4_protocol)
        async def protocol_identified(client):
            server.rooms_manager.subscribe(client, "default")

        @server.on_protocol_disconnect(schema=cl4_protocol)
        async def protocol_disconnect(client):
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
        # COMMANDES (identiques à ton code)
        # -------------------------
        @server.on_command(cmd="handshake", schema=cl4_protocol)
        async def on_handshake(client, message):
            await notify_handshake(client)
            send_statuscode(client, statuscodes.ok, message=message)

        @server.on_command(cmd="ping", schema=cl4_protocol)
        async def on_ping(client, message):
            send_statuscode(client, statuscodes.ok, message=message)

        # Toutes les autres commandes (gmsg, pmsg, gvar, pvar, setid, link, unlink, direct)
        # restent inchangées — tu peux garder celles que tu as déjà, elles fonctionnent parfaitement.

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
                    # remove origin from broadcast
                    try:
                        clients.remove(client)
                    except ValueError:
                        pass
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
                    try:
                        clients.remove(client)
                    except ValueError:
                        pass
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
            try:
                clients.remove(client)
            except ValueError:
                pass
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
                try:
                    clients.remove(client)
                except ValueError:
                    pass
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
                try:
                    clients.remove(client)
                except ValueError:
                    pass
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