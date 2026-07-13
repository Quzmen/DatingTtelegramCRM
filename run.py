"""
Единая точка запуска.

    python run.py

Поднимает локальный сервер на http://127.0.0.1:8730 и открывает
браузер автоматически. Никаких внешних сервисов, всё работает
только на этом компьютере.
"""
import threading
import time
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8730


def _open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()
    print(f"\nTelegram Contacts CRM запущен: http://{HOST}:{PORT}")
    print("Остановить: Ctrl+C\n")
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=False)
