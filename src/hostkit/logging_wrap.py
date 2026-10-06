import logging
import sys
from contextlib import contextmanager


class CustomFormatter(logging.Formatter):
    """Logging formatter that colors each record by its level."""

    purple = "\x1b[35m"
    red = "\x1b[31;1m"
    black = "\x1b[30m"
    green = "\033[92m"
    blue = "\033[94m"
    orange = "\u001b[33m"
    reset = "\x1b[0m"
    frmt = "%(asctime)s - %(name)s - [%(threadName)s] - %(levelname)s - %(message)s (%(filename)s:%(lineno)d)"

    FORMATS = {
        logging.DEBUG: purple + frmt + reset,
        logging.INFO: black + frmt + reset,
        logging.WARNING: orange + frmt + reset,
        logging.ERROR: red + frmt + reset,
        logging.CRITICAL: red + frmt + reset,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


@contextmanager
def log_color(levelno: int, color: str):
    colors = {
        "green": CustomFormatter.green,
        "blue": CustomFormatter.blue,
        "orange": CustomFormatter.orange,
        "purple": CustomFormatter.purple,
        "black": CustomFormatter.black,
    }
    old_color = CustomFormatter.FORMATS[levelno]
    CustomFormatter.FORMATS[levelno] = (
        colors[color] + CustomFormatter.frmt + CustomFormatter.reset
    )
    yield
    CustomFormatter.FORMATS[levelno] = old_color


@contextmanager
def logging_in_stderr(logger_name: str):
    logger = logging.getLogger(logger_name)
    old_handlers = logger.handlers
    logger.handlers = []
    set_colored_handler(logger_name, stream="stderr")
    yield
    logger.handlers = old_handlers


def set_colored_handler(logger_name: str, stream: str = "stdout"):
    ch = logging.StreamHandler(
        sys.stdout if stream == "stdout" else sys.stderr
    )
    ch.setFormatter(CustomFormatter())
    logging.getLogger(logger_name).addHandler(ch)
