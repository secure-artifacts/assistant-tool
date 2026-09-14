import faulthandler
import logging
import os
import sys
import tempfile
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app_paths import APP_ROOT


LOGGER_NAME = "assistant_tool"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 4
FAULT_LOG_MAX_BYTES = 1024 * 1024

_log_file_path = None
_fault_stream = None
_hooks_installed = False


def _candidate_log_directories():
    yield Path(APP_ROOT) / "logs"
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        yield Path(local_app_data) / "AssistantTool" / "logs"
    yield Path(tempfile.gettempdir()) / "AssistantTool" / "logs"


def _create_file_handler(log_dir, max_bytes, backup_count):
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "assistant-tool.log"
    handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(name)s | %(message)s"
    ))
    return handler, log_path


def _rotate_fault_log(log_path):
    try:
        if not log_path.exists() or log_path.stat().st_size < FAULT_LOG_MAX_BYTES:
            return
        backup_path = log_path.with_name(log_path.name + ".1")
        try:
            backup_path.unlink()
        except FileNotFoundError:
            pass
        log_path.replace(backup_path)
    except OSError:
        pass


def _enable_fault_handler(log_dir, logger):
    global _fault_stream
    if _fault_stream is not None:
        return
    try:
        fault_path = log_dir / "python-fault.log"
        _rotate_fault_log(fault_path)
        _fault_stream = fault_path.open("a", encoding="utf-8", buffering=1)
        _fault_stream.write("\n=== Python fault logging enabled ===\n")
        faulthandler.enable(file=_fault_stream, all_threads=True)
    except (OSError, RuntimeError, ValueError) as error:
        _fault_stream = None
        logger.warning("无法启用 Python 致命错误日志：%s", error)


def configure_application_logging(max_bytes=DEFAULT_MAX_BYTES,
                                  backup_count=DEFAULT_BACKUP_COUNT):
    """Configure bounded local logs and return ``(logger, current_log_path)``."""
    global _log_file_path
    logger = logging.getLogger(LOGGER_NAME)
    if _log_file_path is not None:
        return logger, _log_file_path

    logger.setLevel(logging.INFO)
    logger.propagate = False

    last_error = None
    for log_dir in _candidate_log_directories():
        try:
            handler, log_path = _create_file_handler(
                log_dir,
                max(1024, int(max_bytes)),
                max(1, int(backup_count)),
            )
            logger.addHandler(handler)
            _log_file_path = log_path
            _enable_fault_handler(log_dir, logger)
            logger.info("程序日志已启动：%s", log_path)
            return logger, log_path
        except (OSError, ValueError) as error:
            last_error = error

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    logger.error("无法创建本地日志文件：%s", last_error)
    return logger, None


def get_application_logger():
    return configure_application_logging()[0]


def get_log_file_path():
    return configure_application_logging()[1]


def install_exception_hooks():
    """Persist uncaught main-thread, worker-thread and unraisable exceptions."""
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    logger = get_application_logger()

    original_sys_hook = sys.excepthook

    def system_exception_hook(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            original_sys_hook(exc_type, exc_value, exc_traceback)
            return
        logger.critical(
            "未捕获的 Python 异常",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        for handler in logger.handlers:
            handler.flush()
        original_sys_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = system_exception_hook

    if hasattr(threading, "excepthook"):
        original_thread_hook = threading.excepthook

        def thread_exception_hook(args):
            logger.critical(
                "后台线程发生未捕获异常：%s",
                getattr(args.thread, "name", "unknown"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            for handler in logger.handlers:
                handler.flush()
            original_thread_hook(args)

        threading.excepthook = thread_exception_hook

    if hasattr(sys, "unraisablehook"):
        original_unraisable_hook = sys.unraisablehook

        def unraisable_exception_hook(args):
            logger.error(
                "无法抛出的 Python 异常：%s",
                getattr(args, "err_msg", "") or repr(getattr(args, "object", None)),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            original_unraisable_hook(args)

        sys.unraisablehook = unraisable_exception_hook


def shutdown_application_logging():
    global _fault_stream
    logger = logging.getLogger(LOGGER_NAME)
    logger.info("程序正常退出")
    for handler in logger.handlers:
        handler.flush()
    if _fault_stream is not None:
        try:
            faulthandler.disable()
            _fault_stream.close()
        except (OSError, RuntimeError):
            pass
        _fault_stream = None
