"""
AstroLauncher — an unofficial dedicated server launcher for Astroneer.

Modernized for Python 3.10+ and made cross-platform where the underlying
game server allows it (AstroServer.exe itself is Windows-only; on Linux it is
expected to be exposed via a Wine/container wrapper at the same relative path).
"""

import argparse
import asyncio
import atexit
import dataclasses
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import zipfile
from threading import Thread

import psutil
from packaging import version
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import cogs.AstroAPI as AstroAPI
import cogs.AstroWebServer as AstroWebServer
import cogs.ValidateSettings as ValidateSettings
from cogs.AstroDaemon import AstroDaemon
from cogs.AstroDedicatedServer import AstroDedicatedServer
from cogs.AstroLogging import AstroLogging
from cogs.MultiConfig import MultiConfig
from cogs.utils import ALVERSION, AstroRequests
from cogs.cross_platform import (
    configure_firewall,
    get_detached_flags,
    hide_console_window,
    is_admin,
    set_console_title,
)

"""
Build:
pyinstaller AstroLauncher.py -F --add-data "assets;./assets" --icon=assets/astrolauncherlogo.ico
or
python BuildEXE.py
"""


class AstroLauncher:
    """Starts a new instance of the Server Launcher."""

    @dataclasses.dataclass
    class LauncherConfig:
        AutoUpdateLauncherSoftware: bool = True
        AutoUpdateServerSoftware: bool = True
        UpdateOnServerRestart: bool = True
        HideServerConsoleWindow: bool = False
        HideLauncherConsoleWindow: bool = False
        ServerStatusFrequency: float = 2
        PlayfabAPIFrequency: float = 2
        HeartBeatFailRestartServer: int = 8
        DisableBackupRetention: bool = False
        BackupRetentionPeriodHours: float = 72
        BackupRetentionFolderLocation: str = r"Astro\Saved\Backup\LauncherBackups"
        EnableAutoRestart: bool = False
        AutoRestartEveryHours: float = 24
        AutoRestartSyncTimestamp: str = "00:00"
        DisableNetworkCheck: bool = False
        OverwritePublicIP: bool = True
        ShowServerFPSInConsole: bool = True
        AdminAutoConfigureFirewall: bool = True
        LogRetentionDays: int = 7
        DiscordWebHookURL: str = ""
        DiscordWebHookLevel: str = "cmd"
        RODataURL: str = secrets.token_hex(16)

        DisableWebServer: bool = False
        WebServerPort: int = 5000
        WebServerPasswordHash: str = ""
        WebServerBaseURL: str = "/"

        EnableWebServerSSL: bool = False
        SSLPort: int = 443
        SSLCertFile: str = ""
        SSLKeyFile: str = ""

        CPUAffinity: str = ""

        def __post_init__(self):
            hasError = False
            for field, data in self.__dataclass_fields__.items():
                try:
                    self.__dict__[field] = data.type(self.__dict__[field])
                except ValueError:
                    hasError = True
                    AstroLogging.logPrint(
                        f"INI error: {field} must be of type {data.type.__name__}", "critical"
                    )
            if hasError:
                AstroLogging.logPrint("Fix your launcher config file!", "critical")
                sys.exit()

    class SaveHandler(FileSystemEventHandler):
        def __init__(self, launcher):
            self.launcher = launcher
            self.astroPath = self.launcher.astroPath
            self.moveToPath = self.launcher.launcherConfig.BackupRetentionFolderLocation
            super().__init__()

        def on_created(self, event):
            try:
                fileName = os.path.basename(event.src_path)
                AstroLogging.logPrint(f"Server saved. {os.path.basename(fileName)}", dwet="s")
                AstroLogging.logPrint(f"{event.src_path}", msgType="debug")
            except Exception:  # noqa: BLE001
                pass

        def on_deleted(self, event):
            fileName = os.path.basename(event.src_path)
            AstroLogging.logPrint(f"Server deleted save. {os.path.basename(fileName)}", dwet="s")
            AstroLogging.logPrint(f"{event.src_path}", msgType="debug")

    class BackupHandler(FileSystemEventHandler):
        def __init__(self, launcher):
            self.launcher = launcher
            self.astroPath = self.launcher.astroPath
            self.moveToPath = self.launcher.launcherConfig.BackupRetentionFolderLocation
            self.retentionPeriodHours = self.launcher.launcherConfig.BackupRetentionPeriodHours
            self.pendingFiles = []
            super().__init__()

        def handle_files(self):
            time.sleep(2)

            path = os.path.join(self.astroPath, self.moveToPath)
            try:
                if not os.path.exists(path):
                    os.makedirs(path)
            except Exception as exc:  # noqa: BLE001
                AstroLogging.logPrint(exc, "error")
            now = time.time()
            try:
                for f in os.listdir(path):
                    fpath = os.path.join(path, f)
                    if os.stat(fpath).st_mtime < (now - (self.retentionPeriodHours * 60 * 60)):
                        os.remove(fpath)
            except Exception as exc:  # noqa: BLE001
                AstroLogging.logPrint(exc, "error")

            AstroLogging.logPrint("Copying backup(s) to retention folder.", dwet="b")
            try:
                dirName = os.path.dirname(self.pendingFiles[0])
                fileNames = [
                    os.path.join(dirName, f)
                    for f in os.listdir(dirName)
                    if os.path.isfile(os.path.join(dirName, f))
                ]
                for cFile in fileNames:
                    shutil.copy2(cFile, path)
            except Exception as exc:  # noqa: BLE001
                AstroLogging.logPrint(exc, "error")

            self.launcher.backupObserver.stop()
            self.launcher.backup_retention()

        def on_deleted(self, event):
            try:
                self.pendingFiles.append(event.src_path)
                if len(self.pendingFiles) == 1:
                    t = Thread(target=self.handle_files, args=())
                    t.daemon = True
                    t.start()
            except Exception:  # noqa: BLE001
                pass

    def __init__(self, astroPath, launcherINI="Launcher.ini", disable_auto_update=None):
        try:
            AstroLogging.setup_logging()
            self.launcherINI = launcherINI
            self.launcherConfig = self.LauncherConfig()
            self.launcherPath = os.getcwd()
            self.refresh_launcher_config()

            # check if path specified
            if astroPath is not None:
                if os.path.exists(os.path.join(astroPath, "AstroServer.exe")):
                    self.astroPath = astroPath
                else:
                    AstroLogging.logPrint(
                        "Specified path does not contain the server executable! (AstroServer.exe)",
                        "critical",
                    )
                    time.sleep(5)
                    return

            # check if executable in current directory
            elif os.path.exists(os.path.join(os.getcwd(), "AstroServer.exe")):
                self.astroPath = os.getcwd()

            else:
                AstroLogging.logPrint(
                    "Unable to find server executable anywhere! (AstroServer.exe)", "warning"
                )

                # finally, try to install the server
                try:
                    if astroPath is None:
                        self.astroPath = os.getcwd()
                        self.check_for_server_update()
                except Exception as exc:  # noqa: BLE001
                    AstroLogging.logPrint(exc, "critical")
                    return

            AstroLogging.discordWebhookURL = self.launcherConfig.DiscordWebHookURL
            dwhl = self.launcherConfig.DiscordWebHookLevel.lower()
            dwhl = dwhl if dwhl in ("all", "cmd", "chat") else "cmd"
            AstroLogging.discordWebhookLevel = dwhl
            self.start_WebHookLoop()
            AstroLogging.setup_loggingPath(
                astroPath=self.astroPath, logRetention=int(self.launcherConfig.LogRetentionDays)
            )
            if disable_auto_update is not None:
                self.launcherConfig.AutoUpdateLauncherSoftware = not disable_auto_update
            self.version = ALVERSION
            colsize = os.get_terminal_size().columns
            if colsize >= 77:
                vText = "Version " + self.version[1:]
                print(
                    " __________________________________________________________________________\n"
                    + "|     _        _               _                           _               |\n"
                    + "|    /_\\   ___| |_  _ _  ___  | |    __ _  _  _  _ _   __ | |_   ___  _ _  |\n"
                    + "|   / _ \\\\ (_-<|  _|| '_|/ _ \\\\ | |__ / _` || || || ' \\\\ / _|| ' \\\\ / -_)| '_| |\n"
                    + "|  /_/ \\\\_\\/__/ \\\\__||_|  \\\\___/ |____|\\\\__,_| \\\\_,_||_||_|\\\\__||_||_|\\\\___||_|   |\n"
                    + "|                                                                          |\n"
                    + "|" + vText.center(74) + "|\n"
                    + "|__________________________________________________________________________|"
                )

            AstroLogging.logPrint(
                f"AstroLauncher - Unofficial Dedicated Server Launcher {self.version}"
            )
            AstroLogging.logPrint("If you encounter any bugs please open a new issue at:")
            AstroLogging.logPrint("https://github.com/JoeJoeTV/AstroLauncher/issues")
            AstroLogging.logPrint("To safely stop the launcher and server press CTRL+C")

            self.latestURL = "https://github.com/JoeJoeTV/AstroLauncher/releases/latest"
            bName = os.path.basename(sys.executable)
            if sys.argv[0] == os.path.splitext(bName)[0]:
                self.isExecutable = True
            else:
                self.isExecutable = os.path.samefile(sys.executable, sys.argv[0])
            self.cur_server_version = "0.0"
            self.headers = AstroAPI.base_headers
            self.DaemonProcess = None
            self.saveObserver = None
            self.backupObserver = None
            self.hasUpdate = False
            self.is_admin = is_admin()
            self.affinity = self.launcherConfig.CPUAffinity
            try:
                if self.affinity != "":
                    affinityList = [int(x.strip()) for x in self.affinity.split(",")]
                    p = psutil.Process()
                    p.cpu_affinity(affinityList)
            except ValueError as exc:  # noqa: BLE001
                AstroLogging.logPrint(f"CPU Affinity Error: {exc}", "critical")
                AstroLogging.logPrint("Please correct this in your launcher config", "critical")
                return

            self.check_for_server_update()

            self.DedicatedServer = AstroDedicatedServer(self.astroPath, self)

            self.check_for_launcher_update()

            AstroLogging.logPrint("Starting a new session")

            self.validate_playfab_certs()
            self.check_ports_free()

            if self.launcherConfig.AdminAutoConfigureFirewall:
                self.configure_firewall()

            if not self.launcherConfig.DisableNetworkCheck:
                AstroLogging.logPrint("Checking the network configuration..")
                self.check_network_config()

            self.save_reporting()

            if not self.launcherConfig.DisableBackupRetention:
                self.backup_retention()
                AstroLogging.logPrint("Backup retention started")

            self.webServer = None
            if not self.launcherConfig.DisableWebServer:
                # start http server
                self.webServer = self.start_WebServer()
                self.start_InfoLoop()

            if self.launcherConfig.HideLauncherConsoleWindow:
                AstroLogging.logPrint(
                    "HideLauncherConsoleWindow enabled, Hiding window in 5 seconds..."
                )
                time.sleep(5)
                hide_console_window()

            self.start_server(firstLaunch=True)
        except Exception as err:
            ermsg2 = (
                "INIT Error on line {}".format(sys.exc_info()[-1].tb_lineno),
                type(err).__name__,
                err,
            )
            AstroLogging.logPrint(f"{ermsg2}", "critical", True)

    def save_reporting(self):
        if self.saveObserver:
            if not self.saveObserver.is_alive():
                self.saveObserver = None
                self.save_reporting()
        else:
            self.saveObserver = Observer()
            saveGamePath = r"Astro\Saved\SaveGames"
            watchPath = os.path.join(self.astroPath, saveGamePath)
            try:
                if not os.path.exists(watchPath):
                    os.makedirs(watchPath)
            except Exception as exc:  # noqa: BLE001
                AstroLogging.logPrint(exc)
            self.saveObserver.schedule(self.SaveHandler(self), watchPath)
            self.saveObserver.start()

    def backup_retention(self):
        if self.backupObserver:
            if not self.backupObserver.is_alive():
                self.backupObserver = None
                self.backup_retention()
        else:
            self.backupObserver = Observer()
            backupSaveGamePath = r"Astro\Saved\Backup\SaveGames"
            watchPath = os.path.join(self.astroPath, backupSaveGamePath)
            try:
                if not os.path.exists(watchPath):
                    os.makedirs(watchPath)
            except Exception as exc:  # noqa: BLE001
                AstroLogging.logPrint(exc)
            self.backupObserver.daemon = True

            self.backupObserver.schedule(self.BackupHandler(self), watchPath)
            self.backupObserver.start()

    def refresh_launcher_config(self, lcfg=None):
        field_names = set(f.name for f in dataclasses.fields(self.LauncherConfig))
        cleaned_config = {
            k: v for k, v in self.get_launcher_config(lcfg).items() if k in field_names
        }
        self.launcherConfig = dataclasses.replace(self.launcherConfig, **cleaned_config)

        config = MultiConfig()
        config.read_dict({"AstroLauncher": cleaned_config})
        with open(self.launcherINI, "w") as configfile:
            config.write(configfile)

    def overwrite_launcher_config(self, ovrDict):
        ovrConfig = {"AstroLauncher": ovrDict}
        MultiConfig().overwrite_with(self.launcherINI, ovrConfig)

    def get_launcher_config(self, lfcg=None):
        if not lfcg:
            lfcg = self.LauncherConfig()
        baseConfig = {"AstroLauncher": dataclasses.asdict(lfcg)}
        config = MultiConfig().baseline(self.launcherINI, baseConfig)
        settings = config.getdict()["AstroLauncher"]
        return settings

    def validate_playfab_certs(self):
        try:
            AstroLogging.logPrint("Attempting to validate Playfab Certs")
            playfabRequestCommand = [
                "powershell",
                "-executionpolicy",
                "bypass",
                "-command",
                "Invoke-WebRequest -uri https://5ea1.playfabapi.com/ -UseBasicParsing",
            ]
            with open(os.devnull, "w") as tempf:
                proc = subprocess.Popen(playfabRequestCommand, stdout=tempf, stderr=tempf)
                proc.communicate()
        except Exception as err:
            ermsg3 = (
                "VerifyPlayfabCert Error on line {}".format(sys.exc_info()[-1].tb_lineno),
                type(err).__name__,
                err,
            )
            AstroLogging.logPrint(f"{ermsg3}", "warning", True)

    def update_server(self, latest_version):
        updateLocation = os.path.join(
            self.astroPath, "steamcmd", "steamapps", "common", "ASTRONEER Dedicated Server"
        )
        steamcmdFolder = os.path.join(self.astroPath, "steamcmd")
        steamcmdExe = os.path.join(steamcmdFolder, "steamcmd.exe")
        steamcmdZip = os.path.join(self.astroPath, "steamcmd.zip")
        try:
            if not os.path.exists(steamcmdFolder):
                if not os.path.exists(steamcmdExe):
                    if not os.path.exists(steamcmdZip):
                        url = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
                        r = (AstroRequests.get(url)).read()
                        with open(steamcmdZip, "wb") as f:
                            f.write(r)
                    with zipfile.ZipFile(steamcmdZip, "r") as zip_ref:
                        zip_ref.extractall(steamcmdFolder)
            update_downloaded = False

            if os.path.exists(updateLocation):
                upd_version = "0.0"
                try:
                    with open(os.path.join(updateLocation, "build.version"), "r") as f:
                        upd_version = (f.readline())[:-10]
                    if upd_version == latest_version:
                        update_downloaded = True
                except Exception:  # noqa: BLE001
                    try:
                        shutil.rmtree(updateLocation)
                    except Exception:  # noqa: BLE001
                        pass

            if not update_downloaded:
                open("update.p", "wb").write(b"download")
                if os.path.exists(steamcmdExe):
                    try:
                        os.remove(steamcmdZip)
                    except Exception:  # noqa: BLE001
                        pass

                    AstroLogging.logPrint(f"AUTOMATICALLY UPDATING SERVER TO {latest_version}...")
                    try:
                        updateCMD = [
                            steamcmdExe,
                            "+login anonymous",
                            "+app_update 728470",
                            "validate",
                            "+quit",
                        ]
                        update = subprocess.Popen(updateCMD, creationflags=get_detached_flags())
                        while update.poll() is None:
                            time.sleep(0.1)
                    except Exception as exc:  # noqa: BLE001
                        for child in psutil.Process(update.pid).children():
                            try:
                                child.kill()
                            except Exception:  # noqa: BLE001
                                pass
                        try:
                            update.kill()
                        except Exception:  # noqa: BLE001
                            pass
                        raise Exception("") from exc

                upd_version = "0.0"
                try:
                    with open(os.path.join(updateLocation, "build.version"), "r") as f:
                        upd_version = (f.readline())[:-10]
                except Exception:  # noqa: BLE001
                    pass
                if upd_version == latest_version or (latest_version == "unknown"):
                    update_downloaded = True

            if update_downloaded:
                open("update.p", "wb").write(b"transfer")
                shutil.copytree(updateLocation, self.astroPath, dirs_exist_ok=True)
                open("update.p", "wb").write(b"complete")

            cur_version = "0.0"
            with open(os.path.join(self.astroPath, "build.version"), "r") as f:
                cur_version = (f.readline())[:-10]

            if cur_version == latest_version or (latest_version == "unknown"):
                AstroLogging.logPrint(f"UPDATE TO {latest_version} SUCCESSFUL.")
                steamcmdZip = os.path.join(self.astroPath, "steamcmd.zip")
                if os.path.exists(steamcmdZip):
                    os.remove(steamcmdZip)
            try:
                os.remove("update.p")
            except Exception:  # noqa: BLE001
                pass
            try:
                shutil.rmtree(updateLocation)
            except Exception:  # noqa: BLE001
                pass

        except Exception as exc:  # noqa: BLE001
            AstroLogging.logPrint(f"UPDATE TO {latest_version} FAILED.", "warning")
            AstroLogging.logPrint(
                f"{type(exc).__name__}: {str(exc)}", msgType="debug", printTraceback=True
            )

    def check_for_server_update(self, serverStart=False, check_only=False):
        try:
            if not self.launcherConfig.UpdateOnServerRestart and serverStart:
                return
            else:
                needs_update = False
                if os.path.exists("update.p"):
                    with open("update.p", "r") as f:
                        update_status = f.read()
                    if update_status != "completed":
                        needs_update = True

                cur_version = "0.0"
                try:
                    with open(os.path.join(self.astroPath, "build.version"), "r") as f:
                        cur_version = (f.readline())[:-10]
                except Exception:  # noqa: BLE001
                    pass
                if cur_version == "0.0":
                    needs_update = True
                url = "https://astroservercheck.joejoetv.de/api/stats"
                try:
                    data = json.load(AstroRequests.get(url))
                    latest_version = data["stats"]["latestVersion"]
                    if version.parse(latest_version) > version.parse(cur_version):
                        needs_update = True
                    if not os.path.exists(os.path.join(self.astroPath, "AstroServer.exe")):
                        needs_update = True
                    if needs_update:
                        AstroLogging.logPrint(
                            f"SERVER UPDATE AVAILABLE: {cur_version} -> {latest_version}", "warning"
                        )
                        if self.launcherConfig.AutoUpdateServerSoftware and not check_only:
                            self.update_server(latest_version)
                        return True, latest_version
                except Exception as exc:  # noqa: BLE001
                    print(exc)
                    AstroLogging.logPrint("Couldn't get latest version number!", "warning")

                    if self.launcherConfig.AutoUpdateServerSoftware and not check_only:
                        self.update_server("unknown")

                    with open(os.path.join(self.astroPath, "build.version"), "r") as f:
                        cur_version = (f.readline())[:-10]

                    return True, cur_version

            cur_version = "0.0"
            with open(os.path.join(self.astroPath, "build.version"), "r") as f:
                cur_version = (f.readline())[:-10]
            self.cur_server_version = cur_version
        except Exception as exc:  # noqa: BLE001
            print(exc)
            AstroLogging.logPrint("Failed to check if update is available", "warning")

        return False, "0.0"

    def check_for_launcher_update(self, serverStart=False):
        try:
            url = "https://api.github.com/repos/JoeJoeTV/AstroLauncher/releases/latest"
            data = json.load(AstroRequests.get(url))
            latestVersion = data["tag_name"]

            if version.parse(latestVersion) > version.parse(self.version):
                self.hasUpdate = latestVersion
                AstroLogging.logPrint(
                    f"UPDATE: There is a newer version of the launcher out! {latestVersion}"
                )
                AstroLogging.logPrint(f"Download it at {self.latestURL}")
                aupdate = self.launcherConfig.AutoUpdateLauncherSoftware
                if not self.launcherConfig.UpdateOnServerRestart and serverStart:
                    return

                if self.isExecutable and aupdate:
                    self.autoupdate_launcher(data)
        except Exception:  # noqa: BLE001
            AstroLogging.logPrint("Could not determine if new update exists.", msgType="debug")

    def autoupdate_launcher(self, data):
        x = data
        downloadFolder = os.path.dirname(sys.executable)
        for fileObj in x["assets"]:
            downloadURL = fileObj["browser_download_url"]
            fileName = os.path.splitext(fileObj["name"])[0]
            downloadPath = os.path.join(downloadFolder, fileName)

            downloadCMD = [
                "powershell",
                "-executionpolicy",
                "bypass",
                "-command",
                'Write-Host "Downloading latest AstroLauncher.exe..";',
                "wait-process",
                str(os.getpid()),
                ";",
                "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;",
                "$ProgressPreference = 'SilentlyContinue';",
                "Invoke-WebRequest",
                f"'{downloadURL}'",
                "-OutFile",
                f"'{downloadPath + '_new.exe'}'",
                ";",
                "Move-Item",
                "-path",
                f"'{downloadPath + '_new.exe'}'",
                "-destination",
                f"'{downloadPath + '.exe'}'",
                "-Force;",
                'Write-Host "Download complete!";',
                "Start-Process",
                f"'{downloadPath + '.exe'}'",
            ]
            subprocess.Popen(
                downloadCMD,
                shell=True,
                creationflags=get_detached_flags(),
                stdin=None,
                stdout=None,
                stderr=None,
                close_fds=True,
            )
        time.sleep(2)
        self.DedicatedServer.kill_server("Auto-Update")

    def signal_handler(self, sig, frame):
        self.DedicatedServer.kill_server(
            reason="Launcher shutting down via signal", save=True
        )

    def start_server(self, firstLaunch=False):
        """Starts the Dedicated Server process and waits for it to be registered."""
        if firstLaunch:
            atexit.register(
                self.DedicatedServer.kill_server,
                reason="Launcher shutting down via exit",
                save=True,
            )
            signal.signal(signal.SIGINT, self.signal_handler)
        else:
            self.check_for_server_update(serverStart=True)
            self.check_for_launcher_update(serverStart=True)
            self.DedicatedServer = AstroDedicatedServer(self.astroPath, self)

        self.DedicatedServer.status = "starting"
        self.DedicatedServer.busy = False

        gxAuth = None
        while gxAuth is None:
            try:
                gxAuth = AstroAPI.generate_XAUTH(self.DedicatedServer.settings.ServerGuid)
            except Exception:  # noqa: BLE001
                AstroLogging.logPrint(
                    "Unable to generate XAuth token... Are you connected to the internet?",
                    msgType="warning",
                )
                time.sleep(5)
        self.headers["X-Authorization"] = gxAuth
        oldLobbyIDs = self.DedicatedServer.deregister_all_server()
        AstroLogging.logPrint("Starting Server process...")
        if self.launcherConfig.EnableAutoRestart:
            AstroLogging.logPrint(f"Next restart is at {self.DedicatedServer.nextRestartTime}")
        startTime = time.time()
        try:
            self.DedicatedServer.start()
        except Exception:  # noqa: BLE001
            AstroLogging.logPrint("Unable to launch AstroServer.exe", "critical")
            return False

        reachableProcess = None
        pcounter = 40
        while not reachableProcess:
            try:
                reachableProcess = not bool(self.DedicatedServer.process.poll())
                pcounter -= 1
                time.sleep(0.25)
            except Exception:  # noqa: BLE001
                pcounter -= 2
                time.sleep(0.5)
            if pcounter <= 0:
                AstroLogging.logPrint(
                    "Unable to start Server Process after 10 seconds!", "critical"
                )
                return False

        AstroLogging.logPrint(
            f"Server started ( {self.cur_server_version} )! Getting ready....", ovrDWHL=True
        )

        try:
            self.DaemonProcess = AstroDaemon.launch(
                executable=self.isExecutable, consolePID=self.DedicatedServer.process.pid
            )
        except Exception:  # noqa: BLE001
            AstroLogging.logPrint("Unable to start watcher daemon", "warning")
            return False

        # Wait for server to finish registering...
        serverData = None
        oPFF = self.launcherConfig.PlayfabAPIFrequency
        while not self.DedicatedServer.registered:
            AstroLogging.logPrint("Waiting for server to register...", "debug")
            try:
                serverData = AstroAPI.get_server(self.DedicatedServer.ipPortCombo, self.headers)
                serverData = serverData["data"]["Games"]
                lobbyIDs = [x["LobbyID"] for x in serverData]
                if len(set(lobbyIDs) - set(oldLobbyIDs)) == 0:
                    time.sleep(self.launcherConfig.PlayfabAPIFrequency)
                else:
                    now = time.time()
                    if now - startTime > 15:
                        serverData = serverData[0]
                        self.DedicatedServer.registered = True
                        oldLobbyIDs = None
                        self.DedicatedServer.LobbyID = serverData["LobbyID"]

                if self.DedicatedServer.process.poll() is not None:
                    AstroLogging.logPrint(
                        "Server was forcefully closed before registration. Exiting...."
                    )
                    return False
            except KeyboardInterrupt:
                self.DedicatedServer.kill_server(
                    "Launcher shutting down via KeyboardInterrupt"
                )
            except Exception:  # noqa: BLE001
                AstroLogging.logPrint(
                    "Failed to check server. Probably hit rate limit. Backing off and trying again..."
                )
                if self.launcherConfig.PlayfabAPIFrequency < 30:
                    self.launcherConfig.PlayfabAPIFrequency += 1
                time.sleep(self.launcherConfig.PlayfabAPIFrequency)

        self.launcherConfig.PlayfabAPIFrequency = oPFF
        self.DedicatedServer.serverData = serverData
        doneTime = time.time()
        elapsed = doneTime - startTime
        AstroLogging.logPrint(
            f"Server ready! Took {round(elapsed,2)} seconds to register.", ovrDWHL=True
        )
        self.DedicatedServer.status = "ready"
        self.DedicatedServer.server_loop()

    def check_ports_free(self):
        serverPort = False
        sp = int(self.DedicatedServer.settings.Port)

        consolePort = False
        cp = int(self.DedicatedServer.settings.ConsolePort)

        webPort = False
        wp = int(self.launcherConfig.WebServerPort)

        def is_port_in_use(port, tcp=True):
            lc = psutil.net_connections("inet")
            lc = [
                x
                for x in lc
                if x.type == (socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM)
                and x.laddr[1] == port
            ]
            return len(lc) > 0

        serverPort = bool(is_port_in_use(sp, False))
        consolePort = bool(is_port_in_use(cp))

        if not self.launcherConfig.DisableWebServer:
            webPort = bool(is_port_in_use(wp))

        if serverPort:
            AstroLogging.logPrint(
                f"A process is already using your Server Port ( {sp} UDP )", "critical"
            )
        if consolePort:
            AstroLogging.logPrint(
                f"A process is already using your Console Port ( {cp} TCP )", "critical"
            )
        if webPort:
            AstroLogging.logPrint(
                f"A process is already using your Web Port ( {wp} TCP )", "critical"
            )
        if serverPort or consolePort or webPort:
            self.kill_launcher()

    def configure_firewall(self):
        configure_firewall(self)

    def check_network_config(self):
        localTest = ValidateSettings.test_network(
            self.DedicatedServer.settings.PublicIP, int(self.DedicatedServer.settings.Port), False
        )
        remoteTest = ValidateSettings.test_nonlocal(
            self.DedicatedServer.settings.PublicIP, int(self.DedicatedServer.settings.Port)
        )
        testMatrix = [localTest, remoteTest]

        if testMatrix == [True, True]:
            AstroLogging.logPrint("Server network configuration good!")
        elif testMatrix == [False, True]:
            AstroLogging.logPrint(
                "Your server is not accessible from your local network.", "warning"
            )
            AstroLogging.logPrint(
                "This usually indicates an issue with NAT Loopback", "warning"
            )
            AstroLogging.logPrint(
                "See if your router supports it, or setup your server with playit.gg", "warning"
            )
            AstroLogging.logPrint(
                "Guide to setting up playit.gg (11:28): https://youtu.be/SdLNFowq8WI?t=688",
                "warning",
            )
        elif testMatrix == [True, False]:
            AstroLogging.logPrint(
                "Your server can be seen locally, but not remotely.", "warning"
            )
            AstroLogging.logPrint(
                "This usually means you have a Loopback adapter that needs to be disabled",
                "warning",
            )
            AstroLogging.logPrint(
                "and that you may need to Port Forward/open your firewall.", "warning"
            )
        elif testMatrix == [False, False]:
            AstroLogging.logPrint("The server is completely unreachable!", "warning")
            AstroLogging.logPrint(
                f"Please port forward {self.DedicatedServer.settings.Port} UDP and ensure the firewall settings are correct.",
                "warning",
            )

        rconNetworkCorrect = not ValidateSettings.test_network(
            self.DedicatedServer.settings.PublicIP, int(self.DedicatedServer.settings.ConsolePort), True
        )
        if rconNetworkCorrect:
            AstroLogging.logPrint("Remote Console network configuration good!")
        else:
            AstroLogging.logPrint(
                f"SECURITY ALERT: Your console port ({self.DedicatedServer.settings.ConsolePort}) is Port Forwarded!",
                "warning",
                printToDiscord=False,
            )
            AstroLogging.logPrint(
                "SECURITY ALERT: This allows access to the server backend from outside of your network.",
                "warning",
                printToDiscord=False,
            )
            AstroLogging.logPrint(
                "SECURITY ALERT: Disable this ASAP to prevent issues.",
                "warning",
                printToDiscord=False,
            )
            time.sleep(5)

    def start_WebServer(self):
        ws = AstroWebServer.WebServer(self)

        def start_WebServerThread():
            if os.name == "nt":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.set_event_loop(asyncio.new_event_loop())
            ws.run()

        t = Thread(target=start_WebServerThread, args=())
        t.daemon = True
        t.start()
        return ws

    def autoUpdate_websockets_Loop(self):
        while True:
            time.sleep(1)
            self.webServer.iterWebSocketConnections()

    def start_InfoLoop(self):
        def start_InfoLoopThread(self):
            if os.name == "nt":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.set_event_loop(asyncio.new_event_loop())
            self.autoUpdate_websockets_Loop()

        t = Thread(target=start_InfoLoopThread, args=(self,))
        t.daemon = True
        t.start()

    def kill_launcher(self):
        time.sleep(5)
        try:
            for child in psutil.Process(os.getpid()).children():
                child.kill()
        except Exception:  # noqa: BLE001
            pass
        # Kill current process
        try:
            os.kill(os.getpid(), 9)
        except Exception:  # noqa: BLE001
            pass

    def start_WebHookLoop(self):
        t = Thread(target=AstroLogging.sendDiscordReqLoop, args=())
        t.daemon = True
        t.start()


if __name__ == "__main__":
    set_console_title("AstroLauncher - Unofficial Dedicated Server Launcher")
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "-d",
            "--daemon",
            dest="daemon",
            help="Set the launcher to run as a Daemon",
            action="store_true",
        )
        parser.add_argument(
            "-c", "--consolepid", help="Set the consolePID for the Daemon", type=str.lower
        )
        parser.add_argument(
            "-l", "--launcherpid", help="Set the launcherPID for the Daemon", type=str.lower
        )

        parser.add_argument("-p", "--path", help="Set the server folder path", type=str.lower)
        parser.add_argument(
            "-U",
            "--noupdate",
            dest="noautoupdate",
            default=None,
            help="Disable autoupdate if running as exe",
            action="store_true",
        )
        parser.add_argument(
            "-i",
            "--ini",
            dest="launcherINI",
            default="Launcher.ini",
            help="Set the location of the Launcher INI",
        )

        args = parser.parse_args()
        if args.daemon:
            if args.consolepid and args.launcherpid:
                hide_console_window()
                AstroDaemon().daemon(args.launcherpid, args.consolepid)
            else:
                print("Insufficient launch options!")
        else:
            AstroLauncher(
                args.path, disable_auto_update=args.noautoupdate, launcherINI=args.launcherINI
            )
    except KeyboardInterrupt:
        pass
    except Exception as err:
        ermsg1 = (
            "FINAL Error on line {}".format(sys.exc_info()[-1].tb_lineno),
            type(err).__name__,
            err,
        )
        AstroLogging.logPrint(f"{ermsg1}", "critical", True)
