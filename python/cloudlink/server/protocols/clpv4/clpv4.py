from .schema import cl4_protocol

"""
This is the default protocol used for the CloudLink server.
The CloudLink 4.1 Protocol retains full support for CLPv4.

Each packet format is compliant with UPLv2 formatting rules.

Documentation for the CLPv4.1 protocol can be found here:
https://github.com/MikeDev101/cloudlink/wiki/The-CloudLink-Protocol
"""


class clpv4:
    def __init__(self, server):
        """
        Configuration settings
        """
        self.warn_if_multiple_username_matches = True
        self.enable_motd = False
        self.motd_message = str()
        self.real_ip_header = None

        # Allowed origins for connection (CORS-like)
        self.allowed_origins = set()  # Ajouter ici les URLs autorisées

        # Exposes the schema of the protocol
        self.schema = cl4_protocol
        self.__qualname__ = "clpv4"

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

        # Identification of a client's IP address
        def get_client_ip(client):
            if self.real_ip_header:
                if self.real_ip_header in client.request_headers:
                    return client.request_headers.get(self.real_ip_header)
            if type(client.remote_address) == tuple:
                return str(client.remote_address[0])

        self.get_client_ip = get_client_ip

        # Validate messages
        def valid(client, message, schema, allow_unknown=True):
            validator = server.validator(schema, allow_unknown=allow_unknown)
            if validator.validate(message):
                return True
            send_statuscode(client, statuscodes.syntax, details=dict(validator.errors))
            return False

        self.valid = valid

        # Send statuscode helper
        def send_statuscode(client, code, details=None, message=None, val=None):
            code_human, code_id = statuscodes.generate(code)
            tmp_message = {"cmd": "statuscode", "code": code_human, "code_id": code_id}
            if details:
                tmp_message["details"] = details
            if message and "listener" in message:
                tmp_message["listener"] = message["listener"]
            if val:
                tmp_message["val"] = val
            server.send_packet(client, tmp_message)

        self.send_statuscode = send_statuscode

        # Send message helper
        def send_message(client, payload, message=None):
            if message and "listener" in message:
                payload["listener"] = message["listener"]
            server.send_packet(client, payload)

        self.send_message = send_message

        # Require username
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

        # Gather rooms
        def gather_rooms(client, message):
                """
                Retourne un set de rooms à partir du message.
                Supporte :
                 - 'rooms' (str | list)
                 - 'room'  (str | int)
                 - si absent : client.rooms (déjà un set)
                Toutes les valeurs sont normalisées en str.
                """
                # priorité aux champs fournis
                if "rooms" in message:
                        rooms = message["rooms"]
                        # si simple string ou int
                        if isinstance(rooms, (str, int)):
                                return {str(rooms)}
                        # si list/tuple
                        if isinstance(rooms, (list, tuple, set)):
                                return set(str(r) for r in rooms)
                        # si autre -> tenter cast
                        try:
                                return {str(rooms)}
                        except Exception:
                                return client.rooms

                # support legacy 'room'
                if "room" in message:
                        room = message["room"]
                        if isinstance(room, (str, int)):
                                return {str(room)}
                        if isinstance(room, (list, tuple, set)):
                                return set(str(r) for r in room)
                        try:
                                return {str(room)}
                        except Exception:
                                return client.rooms

                # fallback : utiliser les rooms du client (déjà set de str probablement)
                return set(str(r) for r in client.rooms)
        self.gather_rooms = gather_rooms


        # Generate user object
        def generate_user_object(obj):
            if obj.username_set:
                return {"id": obj.snowflake, "username": obj.username, "uuid": str(obj.id)}
            return {"id": obj.snowflake, "uuid": str(obj.id)}

        self.generate_user_object = generate_user_object

        # Authorization check on connection
        @server.on_connect
        async def authorize_connection(client):
                # Récupérer l'Origin depuis les headers
                origin = client.request_headers.get("Origin", "")
                
                # Vérifier si les origines autorisées sont définies
                if hasattr(self, "allowed_origins") and self.allowed_origins:
                        if origin not in self.allowed_origins:
                        	# Refuser la connexion si l'Origin n'est pas dans la liste
                            self.send_statuscode(client, self.statuscodes.refused, details=f"Origin {origin} not allowed")
                            await client.disconnect()
                            return False
                
                # Sinon autoriser la connexion
                return True




        # Notify handshake
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

        # Exception handlers
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

        # Protocol identified
        @server.on_protocol_identified(schema=cl4_protocol)
        async def protocol_identified(client):
            server.logger.debug(f"Adding client {client.snowflake} to default room.")
            server.rooms_manager.subscribe(client, "default")


        # Protocol disconnect
        @server.on_protocol_disconnect(schema=cl4_protocol)
        async def protocol_disconnect(client):
            server.logger.debug(f"Removing client {client.snowflake} from rooms...")
            async for room_id in server.async_iterable(server.copy(client.rooms)):
                server.rooms_manager.unsubscribe(client, room_id)
                if client.username_set:
                    clients = await server.rooms_manager.get_all_in_rooms(room_id, cl4_protocol)
                    clients = server.copy(clients)
                    server.send_packet(clients, {"cmd": "ulist", "mode": "remove", "val": generate_user_object(client), "rooms": room_id})

        @server.on_command(cmd="delete_user", schema=cl4_protocol)
        async def delete_user(client, message):
            import os

            # Si ADMIN_SECRET est configuré côté serveur, l'exiger dans le message
            admin_secret_conf = os.getenv("ADMIN_SECRET", "").strip()
            if admin_secret_conf:
                if message.get("secret") != admin_secret_conf:
                    send_statuscode(
                        client,
                        statuscodes.refused,
                        details="Invalid admin secret"
                    )
                    return

            # Supporte "id" | "ids" | "target"
            queries = message.get("id") or message.get("ids") or message.get("target")
            if queries is None:
                send_statuscode(
                    client,
                    statuscodes.id_required,
                    details="No id/ids/target provided"
                )
                return

            # Normaliser en liste de strings
            if isinstance(queries, (str, int)):
                queries = [str(queries)]
            elif isinstance(queries, (set, tuple, list)):
                queries = [str(q) for q in queries]
            else:
                try:
                    queries = [str(queries)]
                except Exception:
                    send_statuscode(client, statuscodes.syntax, details="Invalid id format")
                    return

            disconnected = []
            not_found = []

            for q in queries:
                try:
                    target = server.clients_manager.find_obj(q)
                except server.clients_manager.exceptions.NoResultsFound:
                    not_found.append(q)
                    continue
                except Exception:
                    server.logger.exception("Unexpected error while finding object")
                    not_found.append(q)
                    continue

                # target peut être un set (plusieurs clients) ou un unique objet
                targets = target if isinstance(target, set) else {target}

                for obj in list(targets):
                    try:
                        # Priorité : obj.disconnect() (ancienne API), ensuite obj.close() (websockets),
                        # sinon demander au server de fermer la connexion (non await).
                        did_disconnect = False

                        if hasattr(obj, "disconnect") and callable(obj.disconnect):
                            try:
                                await obj.disconnect()
                                did_disconnect = True
                            except TypeError:
                                # disconnect() n'était pas awaitable, essayer sans await
                                try:
                                    obj.disconnect()
                                    did_disconnect = True
                                except Exception:
                                    pass

                        if not did_disconnect and hasattr(obj, "close") and callable(obj.close):
                            try:
                                await obj.close()
                                did_disconnect = True
                            except TypeError:
                                # close() n'est pas awaitable ? on ignore
                                pass

                        if not did_disconnect:
                            # fallback : demander au serveur de fermer (schedule)
                            try:
                                server.close_connection(obj, code=4003, reason="Deleted by admin")
                                did_disconnect = True
                            except Exception:
                                server.logger.exception("server.close_connection failed for object")

                        disconnected.append(getattr(obj, "snowflake", str(getattr(obj, "id", q))))
                    except Exception:
                        server.logger.exception(f"Failed to disconnect object {getattr(obj,'snowflake', getattr(obj,'id', q))}")
                        not_found.append(q)

            # Réponse au demandeur
            if disconnected:
                send_statuscode(
                    client,
                    statuscodes.ok,
                    details=f"Deleted users: {disconnected}. Not found: {not_found if not_found else 'none'}"
                )
            else:
                send_statuscode(
                    client,
                    statuscodes.id_not_found,
                    details=f"No matches found for: {queries}"
                )


        @server.on_command(cmd="handshake", schema=cl4_protocol)
        async def on_handshake(client, message):
            await notify_handshake(client)
            send_statuscode(client, statuscodes.ok, message=message)

        @server.on_command(cmd="ping", schema=cl4_protocol)
        async def on_ping(client, message):
            send_statuscode(client, statuscodes.ok, message=message)

        @server.on_command(cmd="gmsg", schema=cl4_protocol)
        async def on_gmsg(client, message):
                # Validate schema
                if not valid(client, message, cl4_protocol.gmsg):
                        return

                # Gather rooms to send to
                rooms = gather_rooms(client, message)

                # Si aucune room n’est donnée, utiliser celle(s) du client
                if not rooms:
                        rooms = client.rooms

                # Broadcast to all subscribed rooms
                async for room in server.async_iterable(rooms):

                        # ⚠️ Ignorer la room "default"
                        if room == "default":
                                server.logger.warning(f"[GMSG] Ignoré: message vers 'default' depuis {client.snowflake}")
                                continue

                        # Empêcher accès à une room non jointe
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
                                "cmd": "gmsg",
                                "val": message["val"],
                                "rooms": room
                        }

                        # Attach listener (if present)
                        if "listener" in message:
                                clients.remove(client)
                                server.send_packet(clients, tmp_message)
                                tmp_message["listener"] = message["listener"]
                                server.send_packet(client, tmp_message)
                        else:
                                server.send_packet(clients, tmp_message)


        @server.on_command(cmd="pmsg", schema=cl4_protocol)
        async def on_pmsg(client, message):
                if not valid(client, message, cl4_protocol.pmsg):
                        return
                if not require_username_set(client, message):
                        return

                rooms = gather_rooms(client, message)
                if not rooms:
                        rooms = client.rooms

                any_results_found = False
                async for room in server.async_iterable(rooms):

                        if room == "default":
                                server.logger.warning(f"[PMSG] Ignoré: message vers 'default' depuis {client.snowflake}")
                                continue

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

                        any_results_found = True
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
                # Validate schema
                if not valid(client, message, cl4_protocol.gvar):
                        return

                # Gather rooms to send to
                rooms = gather_rooms(client, message)
                if not rooms:
                        rooms = client.rooms

                # Broadcast to all subscribed rooms
                async for room in server.async_iterable(rooms):

                        # Optionally ignore default room (keep same behaviour as gmsg)
                        if room == "default":
                                server.logger.warning(f"[GVAR] Ignoré: variable vers 'default' depuis {client.snowflake}")
                                continue

                        # Prevent accessing rooms not joined
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

                        # Build correct gvar payload (name + val)
                        tmp_message = {
                                "cmd": "gvar",
                                "name": message.get("name"),
                                "val": message.get("val"),
                                "rooms": room
                        }

                        # Attach listener (if present) and broadcast
                        if "listener" in message:
                                try:
                                        clients.remove(client)
                                except KeyError:
                                        pass
                                server.send_packet(clients, tmp_message)
                                tmp_message["listener"] = message["listener"]
                                server.send_packet(client, tmp_message)
                        else:
                                server.send_packet(clients, tmp_message)

        @server.on_command(cmd="pvar", schema=cl4_protocol)
        async def on_pvar(client, message):
                # Validate schema
                if not valid(client, message, cl4_protocol.pvar):
                        return

                if not require_username_set(client, message):
                        return

                # Gather rooms to send to
                rooms = gather_rooms(client, message)
                if not rooms:
                        rooms = client.rooms

                any_results_found = False

                # Parcourir les rooms où envoyer la variable
                async for room in server.async_iterable(rooms):

                        # Ignorer la room "default"
                        if room == "default":
                                server.logger.warning(f"[PVAR] Ignoré: variable vers 'default' depuis {client.snowflake}")
                                continue

                        # Empêcher accès à une room non jointe
                        if room not in client.rooms:
                                send_statuscode(
                                        client,
                                        statuscodes.room_not_joined,
                                        details=f'Attempted to access room {room} while not joined.',
                                        message=message
                                )
                                return

                        # Trouver les clients cibles (par ID)
                        clients = await server.rooms_manager.get_specific_in_room(room, cl4_protocol, message["id"])
                        if not len(clients):
                                continue

                        any_results_found = True

                        # Construire la variable à envoyer
                        tmp_message = {
                                "cmd": "pvar",
                                "name": message.get("name"),
                                "val": message.get("val"),
                                "origin": generate_user_object(client),
                                "rooms": room
                        }

                        # Diffuser à la cible
                        server.send_packet(clients, tmp_message)

                # Si aucun client trouvé
                if not any_results_found:
                        send_statuscode(
                                client,
                                statuscodes.id_not_found,
                                details=f'No matches found: {message['id']}',
                                message=message
                        )
                        return

                # OK final
                send_statuscode(client, statuscodes.ok, message=message)

		
        @server.on_command(cmd="setid", schema=cl4_protocol)
        async def on_setid(client, message):
            # Validate schema
            if not valid(client, message, cl4_protocol.setid):
                return

            # Prevent setting the username more than once
            if client.username_set:
                server.logger.error(f"Client {client.snowflake} attempted to set username again!")
                send_statuscode(
                    client,
                    statuscodes.id_already_set,
                    val=generate_user_object(client),
                    message=message
                )

                # Exit setid command
                return

            # Leave default room
            server.rooms_manager.unsubscribe(client, "default")

            # Set the username
            server.clients_manager.set_username(client, message['val'])

            # Re-join default room
            server.rooms_manager.subscribe(client, "default")

            # Broadcast userlist state to existing members
            clients = await server.rooms_manager.get_all_in_rooms("default", cl4_protocol)
            clients = server.copy(clients)
            clients.remove(client)
            server.send_packet(clients, {
                "cmd": "ulist",
                "mode": "add",
                "val": generate_user_object(client),
                "rooms": "default"
            })

            # Notify client of current room state
            server.send_packet(client, {
                "cmd": "ulist",
                "mode": "set",
                "val": server.rooms_manager.generate_userlist("default", cl4_protocol),
                "rooms": "default"
            })

            # Attach listener (if present) and broadcast
            send_statuscode(
                client,
                statuscodes.ok,
                val=generate_user_object(client),
                message=message
            )

        @server.on_command(cmd="link", schema=cl4_protocol)
        async def on_link(client, message):
                # Validate schema
                if not valid(client, message, cl4_protocol.linking):
                        return

                # Require sending client to have set their username
                if not require_username_set(client, message):
                        return

                # Convert to set
                if isinstance(message["val"], list):
                        message["val"] = set(message["val"])
                elif isinstance(message["val"], str):
                        message["val"] = {message["val"]}

                # Unsubscribe from default room if not mentioned
                if "default" not in message["val"]:
                        server.rooms_manager.unsubscribe(client, "default")

                        # Broadcast userlist state to existing members of default room
                        clients = await server.rooms_manager.get_all_in_rooms("default", cl4_protocol)
                        clients = server.copy(clients)
                        server.send_packet(clients, {
                                "cmd": "ulist",
                                "mode": "remove",
                                "val": generate_user_object(client),
                                "rooms": "default"
                        })

                # Subscribe to each requested room
                async for room in server.async_iterable(message["val"]):
                        server.rooms_manager.subscribe(client, room)

                        # Broadcast to others that this user joined
                        clients = await server.rooms_manager.get_all_in_rooms(room, cl4_protocol)
                        clients = server.copy(clients)
                        clients.remove(client)
                        server.send_packet(clients, {
                                "cmd": "ulist",
                                "mode": "add",
                                "val": generate_user_object(client),
                                "rooms": room
                        })

                # Success response
                send_statuscode(
                        client,
                        statuscodes.ok,
                        message=message
                )
        @server.on_command(cmd="get_userlist", schema=cl4_protocol)
        async def on_get_userlist(client, message):
                # validation minimale
                room = message.get("room")
                if not room:
                        send_statuscode(client, statuscodes.id_required, details="Field 'room' missing", message=message)
                        return
                room = str(room)

                try:
                        objs = await server.rooms_manager.get_all_in_rooms(room, cl4_protocol)
                except Exception as e:
                        server.logger.exception(f"get_userlist: failed to get room {room}: {e}")
                        send_statuscode(client, statuscodes.internal_error, details=str(e), message=message)
                        return

                users = {}
                for c in objs:
                        # ne pas renvoyer l'appelant lui-même
                        if c is client:
                                continue

                        # collect info
                        snow = getattr(c, "snowflake", None)
                        uuid = str(getattr(c, "id", None))
                        ip = get_client_ip(c) or None
                        username = getattr(c, "username", None) if getattr(c, "username_set", False) else None

                        key = username if username else (snow if snow else uuid)
                        users[key] = {
                                "id": snow,
                                "uuid": uuid,
                                "ip": ip
                        }

                # envoi de la réponse 'ulist' (map username->info)
                server.send_packet(client, {
                        "cmd": "ulist",
                        "mode": "set",
                        "val": users,
                        "rooms": room
                })

                # confirmer exécution
                send_statuscode(client, statuscodes.ok, message=message)
        @server.on_command(cmd="get_rooms", schema=cl4_protocol)
        async def on_get_rooms(client, message):
                """
                Retourne la liste des rooms qui contiennent au moins un client.
                Réponse envoyée sous forme de paquet `rooms_list` avec "val": [room1, room2, ...]
                """
                # (pas besoin de validation poussée)
                try:
                        rooms = []
                        # server.rooms_manager.rooms est un dict room_id -> room_obj
                        for room_id, room_obj in server.rooms_manager.rooms.items():
                                # room_obj["clients"] contient protocol -> categories
                                # on considère la room "non vide" si au moins un protocole présente au moins un client
                                non_empty = False
                                for proto, proto_group in room_obj.get("clients", {}).items():
                                        # proto_group["all"] est un set d'objets clients
                                        if proto_group.get("all"):
                                                if len(proto_group["all"]) > 0:
                                                        non_empty = True
                                                        break
                                if non_empty:
                                        rooms.append(str(room_id))

                        # envoyer la réponse
                        server.send_packet(client, {
                                "cmd": "rooms_list",
                                "val": rooms
                        })

                        send_statuscode(client, statuscodes.ok, message=message)
                except Exception as e:
                        server.logger.exception(f"on_get_rooms: failed to enumerate rooms: {e}")
                        send_statuscode(client, statuscodes.internal_error, details=str(e), message=message)

        @server.on_command(cmd="unlink", schema=cl4_protocol)
        async def on_unlink(client, message):
            # Validate schema
            if not valid(client, message, cl4_protocol.linking):
                return

            # Require sending client to have set their username
            if not require_username_set(client, message):
                return

            # If blank, assume all rooms
            if type(message["val"]) == str and not len(message["val"]):
                message["val"] = client.rooms

            # Convert to set
            if type(message["val"]) in [list, str]:
                if type(message["val"]) == list:
                    message["val"] = set(message["val"])
                if type(message["val"]) == str:
                    message["val"] = {message["val"]}

            async for room in server.async_iterable(message["val"]):
                server.rooms_manager.unsubscribe(client, room)

                # Broadcast userlist state to existing members
                clients = await server.rooms_manager.get_all_in_rooms(room, cl4_protocol)
                clients = server.copy(clients)
                server.send_packet(clients, {
                    "cmd": "ulist",
                    "mode": "remove",
                    "val": generate_user_object(client),
                    "rooms": room
                })

            # Re-link to default room if no rooms are joined
            if not len(client.rooms):
                server.rooms_manager.subscribe(client, "default")

                # Broadcast userlist state to existing members
                clients = await server.rooms_manager.get_all_in_rooms("default", cl4_protocol)
                clients = server.copy(clients)
                clients.remove(client)
                server.send_packet(clients, {
                    "cmd": "ulist",
                    "mode": "add",
                    "val": generate_user_object(client),
                    "rooms": "default"
                })

                # Notify client of current room state
                server.send_packet(client, {
                    "cmd": "ulist",
                    "mode": "set",
                    "val": server.rooms_manager.generate_userlist("default", cl4_protocol),
                    "rooms": "default"
                })

            # Attach listener (if present) and broadcast
            send_statuscode(
                client,
                statuscodes.ok,
                message=message
            )

        @server.on_command(cmd="direct", schema=cl4_protocol)
        async def on_direct(client, message):
            # Validate schema
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

                # Stop direct command
                return
