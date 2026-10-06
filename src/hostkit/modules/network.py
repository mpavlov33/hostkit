from dataclasses import dataclass
from typing import Union

import hostkit.backends


@dataclass
class Port:
    """Test a TCP port on the host's localhost"""

    number: Union[int, str]
    host: hostkit.backends.Connection

    @property
    def is_busy(self) -> bool:
        """Return True if something answers on the port

        Raises when nc exits neither 0 nor 1, e.g. 127 for no nc.
        """
        rc = self.host.exec_command(
            "nc -zv localhost %s" % self.number, ignore_errors=True
        ).rc
        if rc not in (0, 1):
            raise Exception(
                f"cannot tell whether port {self.number} is busy: nc exited {rc}"
            )
        return rc == 0

    def __repr__(self):
        return "<port {}>".format(self.number)
