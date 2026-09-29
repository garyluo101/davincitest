import datetime
import logging
import os
import shutil
import threading
from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler

log_name = "davincitest.log"
package_name = "davincitest"
_FORMAT = "%(asctime)s|%(levelname)8s|%(filename)s:%(lineno)s|Process:%(process)6d|Thread:%(thread)6d|%(message)s"


def _log_root():
    return Path(os.environ.get("DAVINCITEST_LOG_DIR", "logs")).expanduser().resolve()


def _configure(logger, handler):
    formatter = logging.Formatter(_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    handler.setFormatter(formatter)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(console)
    logger.addHandler(handler)


def _close(logger):
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)


class DailyLogHandler(ConcurrentRotatingFileHandler):
    def __init__(self, filename, maxBytes=5 * 1024 * 1024, backupCount=5, delay=True):
        self.filename = Path(filename).name
        self.root = Path(filename).resolve().parent
        self.folder_name = datetime.date.today().isoformat()
        directory = self.root / self.folder_name
        directory.mkdir(parents=True, exist_ok=True)
        super().__init__(str(directory / self.filename), mode="a", maxBytes=maxBytes,
                         backupCount=backupCount, delay=delay, encoding="utf-8")

    def emit(self, record):
        today = datetime.date.today().isoformat()
        if today != self.folder_name:
            self.close()
            directory = self.root / today
            directory.mkdir(parents=True, exist_ok=True)
            self.folder_name = today
            self.baseFilename = str(directory / self.filename)
            self.lockFilename = self.getLockFilename(None)
        super().emit(record)


class Logger:
    __instance = None
    __lock = threading.RLock()

    def __new__(cls):
        with cls.__lock:
            if cls.__instance is None:
                cls.__instance = super().__new__(cls)
                cls.__instance.logger_dic = {}
            return cls.__instance

    def __init__(self):
        self.file_path = str(_log_root() / datetime.date.today().isoformat())

    def logger(self):
        with self.__lock:
            if "logger" not in self.logger_dic:
                logger = logging.getLogger("davincitest.framework")
                _close(logger)
                _configure(logger, DailyLogHandler(_log_root() / log_name))
                self.logger_dic["logger"] = logger
            return self.logger_dic["logger"]

    def close_handlers(self):
        with self.__lock:
            logger = self.logger_dic.pop("logger", None)
            if logger is not None:
                _close(logger)


class DutLogger:
    def __init__(self, proj):
        self.logger_dict = {}
        self.proj = proj
        self.root = _log_root()
        self.file_path = str(self.root / datetime.date.today().isoformat())
        Path(self.file_path).mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def logger(self, logname: str):
        with self._lock:
            if logname in self.logger_dict:
                return self.logger_dict[logname]["handler"]
            logger = logging.Logger(f"davincitest.dut.{logname}")
            self.file_path = str(self.root / datetime.date.today().isoformat())
            Path(self.file_path).mkdir(parents=True, exist_ok=True)
            path = Path(self.file_path) / f"{logname}.log"
            _configure(logger, logging.FileHandler(path, encoding="utf-8"))
            self.logger_dict[logname] = {"handler": logger, "logname": path.name, "path": path}
            return logger

    def close_handlers(self, log_name: str):
        with self._lock:
            _close(self.logger_dict[log_name]["handler"])

    def redirect(self, logname, new_name, new_path) -> bool:
        with self._lock:
            entry = self.logger_dict[logname]
            for handler in entry["handler"].handlers:
                handler.flush()
            source = entry["path"]
            target = Path(new_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                shutil.copy2(source, target)
            return target.is_file()

    def del_logger_handler(self, logname):
        with self._lock:
            if logname in self.logger_dict:
                self.close_handlers(logname)
                del self.logger_dict[logname]
