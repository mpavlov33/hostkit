import shlex
from dataclasses import dataclass

import hostkit.backends
from hostkit.utils import act


@dataclass
class Sysctl:
    """Read and set a kernel parameter of the running kernel"""

    name: str
    host: hostkit.backends.Connection

    @property
    def value(self) -> str:
        """Return the value sysctl -n prints; raises for a key the kernel lacks

        >>> host.sysctl("vm.swappiness").value
        '60'
        """
        return self.host.check_output("sysctl -n %s" % self.name).strip()

    def set(self, value, check: bool = False) -> int:
        """Set the parameter with sysctl -w; it lasts until the next boot

        >>> host.sysctl("fs.suid_dumpable").set(1)
        0
        """
        return act(
            self.host,
            "sysctl -w %s=%s" % (self.name, shlex.quote(str(value))),
            check,
        )

    def __repr__(self):
        return "<sysctl {}>".format(self.name)
