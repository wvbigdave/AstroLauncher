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


def validate_server_path(raw_path):
    """Canonicalize and validate a user-supplied server directory.

    Resolves the path to an absolute real path, rejects anything that is not a
    directory, and returns the normalized form so downstream file operations
    never act on an ambiguous or relative path.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("server path must be a non-empty string")
    resolved = os.path.realpath(os.path.expanduser(raw_path))
    if not os.path.isdir(resolved):
        raise ValueError(f"server path is not a directory: {resolved}")
    return resolved


def validate_ini_path(raw_path):
    """Canonicalize a launcher INI path, requiring an absolute location."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("ini path must be a non-empty string")
    resolved = os.path.realpath(os.path.expanduser(raw_path))
    parent = os.path.dirname(resolved)
    if parent and not os.path.isdir(parent):
        raise ValueError(f"ini directory does not exist: {parent}")
    return resolved


# ---------------------------------------------------------------------------
# Firewall (Windows netsh only; no-op elsewhere)
# ---------------------------------------------------------------------------

def configure_firewall(launcher) -> None:
    """Add/refresh Windows firewall rules for the server, launcher and web UI.

    No-op on non-Windows, and when automatic firewall config is disabled.
    All netsh invocations use an argv list with ``shell=False`` so no
    interpreter ever parses a path or port as shell syntax.
    """
    if not IS_WINDOWS:
        return
    if not launcher.launcherConfig.AdminAutoConfigureFirewall:
        return

    def netsh(*args: str) -> str:
        result = subprocess.run(
            ["netsh", *args],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        return (result.stdout or "") + (result.stderr or "")

    if "ON" not in netsh("advfirewall", "show", "currentprofile").upper():
        return

    serverExePath = os.path.join(
        launcher.astroPath, "astro", "binaries", "win64", "astroserver-win64-shipping.exe"
    )
    ASRule = serverExePath.lower() in netsh(
        "advfirewall", "firewall", "show", "rule",
        "name=astroserver-win64-shipping.exe", "verbose",
    ).lower()

    ALRule = True
    launcherEXEPath = None
    if launcher.isExecutable:
        launcherEXEPath = os.path.normpath(os.path.join(os.getcwd(), sys.argv[0]))
        ALRule = launcherEXEPath.lower() in netsh(
            "advfirewall", "firewall", "show", "rule",
            "name=astrolauncher.exe", "verbose",
        ).lower()

    webPort = None
    ALWRule = True
    if launcher.isExecutable and not launcher.launcherConfig.DisableWebServer:
        webPort = int(launcher.launcherConfig.WebServerPort)
        ALWRule = str(webPort) in netsh(
            "advfirewall", "firewall", "show", "rule",
            "name=AstroLauncherWeb", "verbose",
        )

    if not is_admin():
        missing = not ASRule
        if launcher.isExecutable and not ALRule:
            missing = True
        if webPort is not None and not ALWRule:
            missing = True
        if missing:
            from cogs.AstroLogging import AstroLogging  # noqa: PLC0415

            AstroLogging.logPrint(
                "Could not find firewall settings! Please relaunch as Administrator.", "warning"
            )
        return

    from cogs.AstroLogging import AstroLogging  # noqa: PLC0415

    def ensure_rule(name: str, *rule_args: str) -> None:
        netsh("advfirewall", "firewall", "delete", "rule", f"name={name}")
        netsh("advfirewall", "firewall", "add", "rule", f"name={name}", *rule_args)

    newRules = False
    if not ASRule:
        newRules = True
        ensure_rule(
            "astroserver-win64-shipping.exe",
            "dir=in", "action=allow", f"program={serverExePath}",
        )
    if launcher.isExecutable and not ALRule:
        newRules = True
        ensure_rule(
            "astrolauncher.exe",
            "dir=in", "action=allow", f"program={launcherEXEPath}",
        )
    if webPort is not None and not ALWRule:
        newRules = True
        ensure_rule(
            "AstroLauncherWeb",
            "dir=in", "action=allow", "protocol=TCP", f"localport={webPort}",
        )
    if newRules:
        AstroLogging.logPrint("Setting custom firewall rules...")
