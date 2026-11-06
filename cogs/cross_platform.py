"""
cross_platform.py — platform + path helpers.

Centralizes the platform-conditional code that was previously scattered
through every module (ctypes console-hiding, netsh firewall, PowerShell
invocations, DETACHED_PROCESS flags, CTRL_C_EVENT). Modules should use
``os.path`` everywhere instead of raw ``ntpath``; this module wraps the few
operating-system-specific behaviors so the rest of the codebase stays clean.
"""

import os
import signal
import subprocess
import sys

IS_WINDOWS = os.name == "nt"


def is_admin() -> bool:
    """Best-effort check for Administrator/root privileges."""
    if IS_WINDOWS:
        try:
            import ctypes  # noqa: PLC0415

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False
    return os.geteuid() == 0 if hasattr(os, "geteuid") else False


# ---------------------------------------------------------------------------
# Console window control (Windows only; no-op elsewhere)
# ---------------------------------------------------------------------------

def hide_console_window() -> None:
    """Hide the attached console window. No-op on non-Windows."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes  # noqa: PLC0415

        kernel32 = ctypes.WinDLL("kernel32")
        user32 = ctypes.WinDLL("user32")
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)  # 0 = SW_HIDE
    except Exception:  # noqa: BLE001
        pass


def set_console_title(title: str) -> None:
    """Set the console window title (Windows only; no-op elsewhere)."""
    if IS_WINDOWS:
        try:
            os.system(f"title {title}")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Process flags
# ---------------------------------------------------------------------------

def get_detached_flags() -> int:
    """Flags for subprocess.Popen to detach a process on Windows; 0 elsewhere."""
    if IS_WINDOWS:
        return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def ctrl_c_signal() -> signal.Signals:
    """Signal used to interrupt child processes (CTRL_C_EVENT on Windows)."""
    return getattr(signal, "CTRL_C_EVENT", signal.SIGTERM)


# ---------------------------------------------------------------------------
# Process termination helpers
# ---------------------------------------------------------------------------

def terminate_tree(process, kill: bool = True) -> None:
    """Best-effort termination of a Popen's children and then the process."""
    try:
        import psutil  # noqa: PLC0415

        for child in psutil.Process(process.pid).children():
            child.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        if kill:
            process.kill()
        else:
            process.terminate()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Firewall (Windows netsh only; no-op elsewhere)
# ---------------------------------------------------------------------------

def configure_firewall(launcher) -> None:
    """Add/refresh Windows firewall rules for the server, launcher and web UI.

    No-op on non-Windows, and when automatic firewall config is disabled.
    """
    if not IS_WINDOWS:
        return
    if not launcher.launcherConfig.AdminAutoConfigureFirewall:
        return

    ALRule = None
    ALWRule = None
    ASRule = None
    launcherEXEPath = None

    with os.popen(
        'netsh advfirewall show currentprofile | findstr /L "State" | findstr /L "ON"'
    ) as fwCheck:
        isFirewallEnabled = fwCheck.read()

    if not isFirewallEnabled:
        return

    serverExePath = os.path.join(
        launcher.astroPath, "astro", "binaries", "win64", "astroserver-win64-shipping.exe"
    )
    ASRule = os.popen(
        f'netsh advfirewall firewall show rule name=astroserver-win64-shipping.exe verbose | findstr /L "{serverExePath}"'
    ).read()

    if launcher.isExecutable:
        launcherEXEPath = os.path.join(os.getcwd(), sys.argv[0])
        ALRule = os.popen(
            f'netsh advfirewall firewall show rule name=astrolauncher.exe verbose | findstr /L "{launcherEXEPath}"'
        ).read()
        if not launcher.launcherConfig.DisableWebServer:
            ALWRule = os.popen(
                f'netsh advfirewall firewall show rule name=AstroLauncherWeb | findstr /L "{launcher.launcherConfig.WebServerPort}"'
            ).read()

    if not is_admin():
        missing = (not ASRule)
        if launcher.isExecutable and not ALRule:
            missing = True
        if not launcher.launcherConfig.DisableWebServer and launcher.isExecutable and not ALWRule:
            missing = True
        if missing:
            from cogs.AstroLogging import AstroLogging  # noqa: PLC0415

            AstroLogging.logPrint(
                "Could not find firewall settings! Please relaunch as Administrator.", "warning"
            )
        return

    from cogs.AstroLogging import AstroLogging  # noqa: PLC0415

    newRules = False
    if not ASRule:
        newRules = True
        subprocess.call(
            f'netsh advfirewall firewall delete rule name=astroserver-win64-shipping.exe dir=in program="{serverExePath}"'
            + f'& netsh advfirewall firewall add rule name=astroserver-win64-shipping.exe dir=in action=allow program="{serverExePath}"',
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if launcher.isExecutable and not ALRule:
        newRules = True
        subprocess.call(
            f'netsh advfirewall firewall delete rule name=astrolauncher.exe dir=in program="{launcherEXEPath}"'
            + f'& netsh advfirewall firewall add rule name=astrolauncher.exe dir=in action=allow program="{launcherEXEPath}"',
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if not launcher.launcherConfig.DisableWebServer and not ALWRule:
        newRules = True
        subprocess.call(
            f'netsh advfirewall firewall delete rule name=AstroLauncherWeb dir=in protocol=TCP localport={launcher.launcherConfig.WebServerPort}'
            + f'& netsh advfirewall firewall add rule name=AstroLauncherWeb dir=in action=allow protocol=TCP localport={launcher.launcherConfig.WebServerPort}',
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if newRules:
        AstroLogging.logPrint("Setting custom firewall rules...")
