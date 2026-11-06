# Windows build image — used only to cross-compile the AstroLauncher.exe
# self-updating binary via PyInstaller. The launcher itself runs on Linux.
FROM python:3.12-windowsservercore

WORKDIR C:/Build

COPY requirements.txt .

RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt pyinstaller

COPY . .

ENTRYPOINT ["python", "BuildEXE.py"]
