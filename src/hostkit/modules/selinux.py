import shlex
from dataclasses import dataclass

import hostkit.backends
from hostkit.utils import act


@dataclass
class SELinux:
    """Test and switch the SELinux mode of a host"""

    host: hostkit.backends.Connection

    @property
    def enabled(self) -> bool:
        """Return True if selinuxenabled exits 0; a host without it is not enabled

        >>> host.selinux.enabled
        True
        """
        return (
            self.host.exec_command("selinuxenabled", ignore_errors=True).rc
            == 0
        )

    @property
    def mode(self) -> str:
        """Return the mode getenforce prints, raise if it could not tell

        >>> host.selinux.mode
        'Enforcing'
        """
        return self.host.check_output("getenforce").strip()

    def enforce(self, mode, check: bool = False) -> int:
        """Switch the mode with setenforce: 1 or Enforcing, 0 or Permissive

        >>> host.selinux.enforce(1)
        0
        """
        return act(self.host, "setenforce %s" % shlex.quote(str(mode)), check)
