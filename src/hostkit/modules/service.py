# Derived from pytest-testinfra (https://github.com/pytest-dev/pytest-testinfra), Apache-2.0,
# and modified. See LICENSE and NOTICE.
from dataclasses import dataclass
import hostkit.backends
from hostkit.utils import act


@dataclass
class Service:
    """Test and drive a systemd unit

    Actions return systemctl's exit status; check=True raises instead.
    """

    name: str
    host: hostkit.backends.Connection

    @property
    def state(self) -> str:
        """Return the state systemctl is-active prints

        >>> host.service("myapp.socket").state
        'active'
        """
        return self.host.exec_command(
            "systemctl is-active %s" % self.name, ignore_errors=True
        ).stdout.strip()

    @property
    def is_running(self) -> bool:
        return self.state == "active"

    def start(self, check: bool = False) -> int:
        return act(self.host, "systemctl start %s" % self.name, check)

    def stop(self, check: bool = False) -> int:
        return act(self.host, "systemctl stop %s" % self.name, check)

    def restart(self, check: bool = False) -> int:
        return act(self.host, "systemctl restart %s" % self.name, check)

    def reset_failed(self, check: bool = False) -> int:
        """Clear the failed state and the start rate limit of the unit"""
        return act(self.host, "systemctl reset-failed %s" % self.name, check)

    def __repr__(self):
        return "<service {}>".format(self.name)


@dataclass
class Systemd:
    """Drive the host's systemd manager"""

    host: hostkit.backends.Connection

    def daemon_reload(self, check: bool = False) -> int:
        """Reread the unit files, e.g. after one was written or changed

        >>> host.systemd.daemon_reload(check=True)
        0
        """
        return act(self.host, "systemctl daemon-reload", check)

    def __repr__(self):
        return "<systemd>"
