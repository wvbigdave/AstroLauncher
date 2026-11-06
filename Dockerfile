# Windows build image — used only to cross-compile the AstroLauncher.exe
# self-updating binary via PyInstaller. The launcher itself runs on Linux.
FROM python:3.12-windowsservercore

WORKDIR C:/Build

COPY requirements.txt ./

RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt pyinstaller

# Copy only the build inputs explicitly (no recursive wildcard), so stray
# local files — configs, keys, logs, virtualenvs — never enter the image.
COPY AstroLauncher.py BuildEXE.py ./
COPY cogs ./cogs
COPY assets ./assets

# Run the build as an unprivileged account rather than ContainerAdministrator.
RUN net user builder /add && net localgroup Users builder /add

USER builder

ENTRYPOINT ["python", "BuildEXE.py"]
