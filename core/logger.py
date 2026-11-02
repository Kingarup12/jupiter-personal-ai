from datetime import datetime

from core.security import redact

class Logger:

    @staticmethod
    def _time():
        return datetime.now().strftime("%H:%M:%S")

    @staticmethod
    def _safe_print(msg):
        msg = redact(msg)
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode('ascii', 'replace').decode('ascii'))

    @staticmethod
    def info(message):
        Logger._safe_print(f"[INFO {Logger._time()}] {message}")

    @staticmethod
    def warning(message):
        Logger._safe_print(f"[WARNING {Logger._time()}] {message}")

    @staticmethod
    def error(message):
        Logger._safe_print(f"[ERROR {Logger._time()}] {message}")

    @staticmethod
    def success(message):
        Logger._safe_print(f"[SUCCESS {Logger._time()}] {message}")

    @staticmethod
    def debug(message):
        Logger._safe_print(f"[DEBUG {Logger._time()}] {message}")
