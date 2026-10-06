import logging

from . import backends, utils, modules, tmux
from .backends import Connection
from .logging_wrap import set_colored_handler

logger_name = "hostkit"
logger = logging.getLogger(logger_name)
set_colored_handler(logger_name, stream="stdout")
