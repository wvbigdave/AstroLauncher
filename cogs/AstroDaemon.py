"""Daemon process that watches the launcher + dedicated server and tears them
down together when either exits."""

import os
import subprocess
import sys
import time

import psutil

from cogs.cross_platform import ctrl_c_signal, get_detached_flags


class AstroDaemon:
    """Watches the launcher (and the console that spawned it)."""

    @staticmethod
    def launch(executable, consolePID):
        # Normalize the entry-point path so the argv entry is absolute.
        entry = os.path.realpath(sys.argv[0])
        if executable:
            daemonCMD = [
                sys.executable,
                "--daemon",
                "-l",
                str(os.getpid()),
                "-c",
                str(int(consolePID)),
            ]
        else:
            daemonCMD = [
                sys.executable,
                entry,
                "--daemon",
                "-l",
                str(os.getpid()),
                "-c",
                str(int(consolePID)),
            ]
        return subprocess.Popen(daemonCMD, shell=False, creationflags=get_detached_flags())

    @staticmethod
    def daemon(launcherPID, consolePID):
        while psutil.pid_exists(int(launcherPID)) and psutil.pid_exists(int(consolePID)):
            time.sleep(0.5)
        try:
            for child in psutil.Process(int(consolePID)).children():
                os.kill(child.pid, ctrl_c_signal())
        except KeyboardInterrupt as exc:
            print(exc)
