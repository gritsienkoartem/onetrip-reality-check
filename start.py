"""Запуск сайта OneTrip Reality Check из корня проекта.

Можно запускать напрямую: python start.py
Скрипт проверяет зависимости, при необходимости устанавливает их и
передаёт управление Streamlit.
"""
from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app.py"
REQUIREMENTS = ROOT / "requirements.txt"
RUNTIME_MODULES = ("streamlit", "pydantic", "requests", "dotenv")


def _missing_dependencies() -> list[str]:
    return [
        name for name in RUNTIME_MODULES if importlib.util.find_spec(name) is None
    ]


def _port_is_busy(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            continue
        for family, kind, proto, _, address in addresses:
            with socket.socket(family, kind, proto) as sock:
                sock.settimeout(0.3)
                if sock.connect_ex(address) == 0:
                    return True
    return False


def _free_port(preferred: int = 8501) -> int:
    for port in (preferred, preferred + 1, preferred + 2):
        if _port_is_busy(port):
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _open_browser_when_ready(port: int) -> None:
    url = f"http://localhost:{port}"
    for _ in range(120):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.5)


def main() -> int:
    if not APP.is_file():
        print("Не найден app.py рядом со start.py. Запустите файл из папки проекта.")
        return 1

    missing = _missing_dependencies()
    if missing:
        print(
            "Не хватает зависимостей: "
            + ", ".join(missing)
            + ". Устанавливаю из requirements.txt..."
        )
        code = subprocess.call(
            [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)]
        )
        if code != 0:
            print(
                "Не удалось установить зависимости. Проверьте интернет и версию Python "
                "(нужен Python 3.11 или новее)."
            )
            return code

    port = _free_port()
    print(f"Запускаю OneTrip Reality Check на http://localhost:{port} ...")
    print("Браузер откроется автоматически. Остановить сервер — Ctrl+C.")
    threading.Thread(
        target=_open_browser_when_ready, args=(port,), daemon=True
    ).start()
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP),
        "--server.headless=true",
        "--server.port",
        str(port),
        "--browser.gatherUsageStats=false",
    ]
    try:
        return subprocess.call(command, cwd=ROOT)
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
