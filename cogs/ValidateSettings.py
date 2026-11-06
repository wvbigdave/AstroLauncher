"""Read/validate the dedicated-server and engine INI settings, force
stdout-standardized values, and expose helper network primitives."""

import json
import os
import secrets
import socket
import threading
import time
import uuid
from contextlib import contextmanager

from IPy import IP

from cogs.AstroLogging import AstroLogging
from cogs.MultiConfig import MultiConfig
from cogs.utils import AstroRequests


def get_public_ip():
    url = "https://api.ipify.org?format=json"
    x = json.load(AstroRequests.get(url))
    AstroLogging.logPrint(x, "debug")
    return x["ip"]


def valid_ip(address):
    if not isinstance(address, str) or not address:
        return False
    try:
        socket.inet_aton(address)
        return True
    except (OSError, ValueError):
        return False


def get_current_settings(launcher, ovrIP=False):
    curPath = launcher.astroPath
    curLog = "AstroServerSettings.ini"
    confPath = os.path.join(curPath, "Astro", "Saved", "Config", "WindowsServer", "AstroServerSettings.ini")

    AstroLogging.logPrint("Verifying PublicIP setting...", "debug")
    validIP = False
    try:
        tConfig = MultiConfig().baseline(confPath, {})
        bIP = tConfig.getdict()["/Script/Astro.AstroServerSettings"].get("PublicIP")
        validIP = valid_ip(bIP)
        if validIP and IP(bIP).iptype() != "PUBLIC":
            validIP = False
            AstroLogging.logPrint(
                "PublicIP field (AstroServerSettings.ini) contained a Private IP!", "warning"
            )
            AstroLogging.logPrint("This will be automatically fixed..", "warning")
    except Exception:  # noqa: BLE001
        validIP = False

    ovrConfig = {
        "/Script/Astro.AstroServerSettings": {
            "VerbosePlayerProperties": "True",
            "HeartbeatInterval": "0",
        }
    }

    try:
        if ovrIP:
            if launcher.launcherConfig.OverwritePublicIP or not validIP:
                ovrConfig["/Script/Astro.AstroServerSettings"]["PublicIP"] = get_public_ip()
    except Exception:  # noqa: BLE001
        t = "warning"
        if not validIP:
            t = "error"
        AstroLogging.logPrint("Could not update PublicIP!", t)

    try:
        curLog = "AstroServerSettings.ini"
        AstroLogging.logPrint(
            "Forcing standardized settings in AstroServerSettings.ini...", "debug"
        )
        MultiConfig().overwrite_with(confPath, ovrConfig)

        baseConfig = {
            "/Script/Astro.AstroServerSettings": {
                "bLoadAutoSave": "True",
                "MaxServerFramerate": "30.000000",
                "MaxServerIdleFramerate": "3.000000",
                "bWaitForPlayersBeforeShutdown": "False",
                "PublicIP": "",
                "ServerName": "Astroneer Dedicated Server",
                "MaximumPlayerCount": "8",
                "OwnerName": "",
                "OwnerGuid": "",
                "PlayerActivityTimeout": "0",
                "ServerPassword": "",
                "bDisableServerTravel": "False",
                "DenyUnlistedPlayers": "False",
                "VerbosePlayerProperties": "True",
                "AutoSaveGameInterval": "900",
                "BackupSaveGamesInterval": "7200",
                "ServerGuid": uuid.uuid4().hex,
                "ActiveSaveFileDescriptiveName": "SAVE_1",
                "ServerAdvertisedName": "",
                "ConsolePort": "1234",
                "ConsolePassword": uuid.uuid4().hex,
                "HeartbeatInterval": "0",
            }
        }
        AstroLogging.logPrint("Baselining AstroServerSettings.ini...", "debug")
        config = MultiConfig().baseline(confPath, baseConfig)

        settings = config.getdict()["/Script/Astro.AstroServerSettings"]

        baseConfig = {
            "URL": {"Port": "7777"},
            "/Script/OnlineSubsystemUtils.IpNetDriver": {
                "MaxClientRate": "1000000",
                "MaxInternetClientRate": "1000000",
            },
            "/Game/ChatMod/ChatManager.ChatManager_C": {
                "WebhookUrl": f'\\"{launcher.launcherConfig.RODataURL}\\"'
            },
        }
        curLog = "Engine.ini"
        AstroLogging.logPrint("Baselining Engine.ini...", "debug")
        config = MultiConfig().baseline(
            os.path.join(curPath, "Astro", "Saved", "Config", "WindowsServer", "Engine.ini"),
            baseConfig,
        )
        EngineINI = config.getdict()
        settings.update(EngineINI["URL"])
        if isinstance(EngineINI["URL"]["Port"], list):
            AstroLogging.logPrint("Duplicate Ports detected! Please only list one Port.", "critical")
            raise TypeError
        return settings
    except Exception as exc:  # noqa: BLE001
        AstroLogging.logPrint(f"Error parsing {curLog} file!", "critical")
        AstroLogging.logPrint("Could not retrieve INI settings!", "critical")
        AstroLogging.logPrint("Please ensure everything is correctly formatted...", "critical")
        AstroLogging.logPrint("or delete the INI and allow the launcher to recreate it!", "critical")
        AstroLogging.logPrint(exc, "critical", printTraceback=True)
        try:
            launcher.DedicatedServer.kill_server()
        except Exception:  # noqa: BLE001
            try:
                launcher.kill_launcher()
            except Exception:  # noqa: BLE001
                pass


def socket_server(port, secret, tcp):
    try:
        serversocket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM
        )
        serversocket.settimeout(10)
        serversocket.bind(("0.0.0.0", port))
        if tcp:
            serversocket.listen(1)
            connection, _client_address = serversocket.accept()
            data = connection.recv(32)
            matched = data == secret
            connection.close()
            return matched
        # UDP: wait for the probe packet until the socket timeout expires.
        data = serversocket.recv(32)
        matched = data == secret
        serversocket.close()
        return matched
    except (socket.timeout, OSError):
        return False
    except Exception:  # noqa: BLE001
        return False


def socket_server2(port):
    try:
        serversocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        serversocket.settimeout(10)
        serversocket.bind(("0.0.0.0", port))
        bytesData = bytes(
            [0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
             0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x08]
        )
        # Reply to the first matching query; give up on timeout.
        data, address = serversocket.recvfrom(32)
        if data == bytesData:
            serversocket.sendto(b"Hello from AstroLauncher", address)
            serversocket.close()
            return True
        return False
    except (socket.timeout, OSError):
        return False
    except Exception:  # noqa: BLE001
        return False


def socket_client(ip, port, secret, tcp):
    try:
        if tcp:
            with session_scope(ip, port) as s:
                s.sendall(secret)
        else:
            time.sleep(2)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(secret, (ip, port))
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def session_scope(ip, consolePort: int):
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((ip, int(consolePort)))
        yield s
    except Exception:  # noqa: BLE001
        pass
    finally:
        if s is not None:
            s.close()


def test_network(ip, port, tcp):
    secretPhrase = secrets.token_hex(16).encode()
    x = threading.Thread(target=socket_client, args=(ip, port, secretPhrase, tcp))
    x.start()
    return socket_server(port, secretPhrase, tcp)


def test_nonlocal(ip, port):
    x = threading.Thread(target=socket_server2, args=(port,))
    x.start()
    try:
        r = json.load(
            AstroRequests.get(
                f"https://astroservercheck.joejoetv.de/api/check?url={ip}:{port}", timeout=10
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(exc)
        AstroLogging.logPrint("Unable to verify outside connectivity.", "warning")
        AstroLogging.logPrint("Could not contact server checker API!", "warning")
        AstroLogging.logPrint("+------------------------------------------------+", "warning")
        AstroLogging.logPrint("|THIS DOES NOT MEAN YOUR SERVER IS NOT REACHABLE,|", "warning")
        AstroLogging.logPrint("|JUST THAT IT COULDN'T BE VERIFIED!              |", "warning")
        AstroLogging.logPrint("+------------------------------------------------+", "warning")
        return False

    AstroLogging.logPrint(r, "debug")
    return r["server"]["network"]
