"""Astroneer dedicated-server console (RCON) client.

The dedicated server exposes a plain-text console over TCP on the configured
ConsolePort; the first line sent is the console password, then commands are
newline-terminated JSON-returning requests.
"""

import json
import socket
import time
from contextlib import contextmanager

from cogs.AstroLogging import AstroLogging


class AstroRCON:
    def __init__(self, DedicatedServer):
        self.DS = DedicatedServer
        self.connected = False
        self.socket = None
        self.lock = False

    @contextmanager
    def lockRcon(self):  # noqa: N802
        # Simple spin-lock so only one RCON command is in flight at a time.
        while self.lock:
            time.sleep(0.01)
        self.lock = True
        try:
            yield self
        finally:
            self.lock = False

    def run(self):
        if self.socket is None or self.socket.fileno() == -1:
            self.socket = self.getSocket()
        if not self.connected:
            try:
                self.socket.send(b"u up?")
            except Exception:  # noqa: BLE001
                self.connected = False
                try:
                    self.connectSocket()
                except Exception:  # noqa: BLE001
                    pass

    def connectSocket(self):
        if not self.connected:
            self.socket.connect(("127.0.0.1", int(self.DS.settings.ConsolePort)))
            with self.lockRcon() as s:
                s.socket.sendall(f"{self.DS.settings.ConsolePassword}\n".encode())
            self.connected = True
            AstroLogging.logPrint("Connected to RCON Console!")

    @staticmethod
    def getSocket():
        return socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def _command(self, text):
        with self.lockRcon() as s:
            s.socket.sendall(text.encode() if isinstance(text, str) else text)
            rawdata = AstroRCON.recvall(s.socket)
        return AstroRCON.parseData(rawdata)

    def DSListPlayers(self):  # noqa: N802
        try:
            return self._command("DSListPlayers\n")
        except Exception as exc:  # noqa: BLE001
            print(f"Error retrieving player list: {exc}")
            return None

    def DSKickPlayerGuid(self, playerGuid):  # noqa: N802
        try:
            return self._command(f"DSKickPlayerGuid {playerGuid}\n")
        except Exception as exc:  # noqa: BLE001
            print(f"Error kicking player: {exc}")
            return None

    def DSSetPlayerCategoryForPlayerName(self, playerName, category):  # noqa: N802
        try:
            escapedName = playerName.replace('"', '\\"')
            return self._command(f'DSSetPlayerCategoryForPlayerName "{escapedName}" {category}\n')
        except Exception:  # noqa: BLE001
            return None

    def DSServerStatistics(self):  # noqa: N802
        try:
            return self._command("DSServerStatistics\n")
        except Exception as exc:  # noqa: BLE001
            print(f"Error retrieving server statistics: {exc}")
            return None

    def DSSaveGame(self, name=None):  # noqa: N802
        try:
            with self.lockRcon() as s:
                s.socket.sendall(f"DSSaveGame {name}\n".encode() if name is not None else b"DSSaveGame\n")
                # The dedicated server takes a moment to write the save; wait
                # before issuing further commands.
                time.sleep(1.1)
            return None
        except Exception:  # noqa: BLE001
            return None

    def DSSetDenyUnlisted(self, state):  # noqa: N802
        try:
            return self._command(f"DSSetDenyUnlisted {state}\n")
        except Exception:  # noqa: BLE001
            return None

    def DSServerShutdown(self):  # noqa: N802
        try:
            self.socket.sendall(b"DSServerShutdown\n")
            return None
        except Exception:  # noqa: BLE001
            return None

    def DSListGames(self):  # noqa: N802
        try:
            return self._command("DSListGames\n")
        except Exception as exc:  # noqa: BLE001
            print(f"Error retrieving savegame list: {exc}")
            return None

    def DSNewGame(self):  # noqa: N802
        try:
            with self.lockRcon() as s:
                s.socket.sendall(b"DSNewGame\n")
            return True
        except Exception:  # noqa: BLE001
            return None

    def DSLoadGame(self, name):  # noqa: N802
        try:
            return self._command(f"DSLoadGame {name}\n")
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def recvall(sock):
        try:
            BUFF_SIZE = 4096  # 4 KiB
            data = b""
            while True:
                part = sock.recv(BUFF_SIZE)
                data += part
                if len(part) < BUFF_SIZE:
                    break
            return data
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def parseData(rawdata):  # noqa: N802
        try:
            if rawdata != b"":
                rawdata = rawdata.rstrip()
                return json.loads(rawdata.decode())
            return None
        except Exception:  # noqa: BLE001
            return rawdata
