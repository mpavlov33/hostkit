# Derived from pytest-testinfra (https://github.com/pytest-dev/pytest-testinfra), Apache-2.0,
# and modified. See LICENSE and NOTICE.
import hostkit
import shlex
from dataclasses import dataclass
from typing import Any

from hostkit.utils import act


def int_or_float(value):
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


class _Process(dict[str, Any]):
    def __getattr__(self, key):
        try:
            return self.__getitem__(key)
        except KeyError:
            attrs = self["_get_process_attribute_by_pid"](self["pid"], key)
            if attrs["lstart"] != self["lstart"]:
                raise RuntimeError(
                    (
                        "Process with pid {} start time changed from {} to {}."
                        " This mean the process you are working on does not not "
                        "exist anymore"
                    ).format(self["pid"], self["lstart"], attrs["lstart"])
                )
            return attrs[key]

    def __repr__(self):
        return "<process {} (pid={})>".format(self["comm"], self["pid"])


@dataclass
class Process:
    """Find, inspect and signal processes; attributes are named as ps(1) -o keywords

    >>> host.process.get(user="root", comm="nginx").args
    'nginx: master process /usr/sbin/nginx'
    """

    host: hostkit.backends.Connection

    def is_running(self, pid):
        return not self.host.exec_command(f"ps -p {pid}").rc

    def pgrep(self, pattern: str, full: bool = False) -> list[int]:
        """Return the pids of the processes whose name matches the regex

        full=True matches the whole command line; raises if pgrep fails (bad regex, no pgrep).

        >>> host.process.pgrep("myapp-server")
        [900]
        """
        # Sent alone so bash execs pgrep and -f does not match its own shell.
        res = self.host.exec_command(
            "pgrep %s%s" % ("-f " if full else "", shlex.quote(pattern)),
            ignore_errors=True,
        )
        # 1 is no match; a bad regex (2) or no pgrep (127) says nothing about processes.
        if res.rc not in (0, 1):
            raise Exception(f"pgrep {pattern!r} exited {res.rc}: {res.stderr}")
        return [int(pid) for pid in res.stdout.split()]

    def pkill(
        self, pattern: str, signal: str = "", full: bool = False, check: bool = False
    ) -> int:
        """Signal the processes whose name matches the regex; rc 1 is none matched

        full=True matches the whole command line.

        >>> host.process.pkill("myapp-server", signal="KILL", full=True)
        0
        """
        # Sent alone, no redirect: bash then execs pkill, so -f cannot kill its own shell.
        return act(
            self.host,
            "pkill %s%s%s"
            % (
                "-%s " % signal if signal else "",
                "-f " if full else "",
                shlex.quote(pattern),
            ),
            check,
        )

    def kill(self, pids, signal: str = "", check: bool = False) -> int:
        """Signal one pid or several, in one kill; the default signal is TERM

        >>> host.process.kill([2345, 2346], signal="KILL")
        0
        """
        if isinstance(pids, (int, str)):
            pids = [pids]
        # int(): a string holding two pids is refused, not run as a second command.
        return act(
            self.host,
            "kill %s%s"
            % (
                "-%s " % signal if signal else "",
                " ".join(str(int(pid)) for pid in pids),
            ),
            check,
        )

    def in_state(self, *states: str) -> dict[int, str]:
        """Return {pid: command line} of the processes in one of the states

        A state is one ps STAT letter, e.g. T stopped, Z zombie; raises ValueError otherwise.

        >>> host.process.in_state("T", "Z")
        {4242: 'myapp-server --port 8080'}
        """
        if not states or any(len(state) != 1 for state in states):
            raise ValueError(f"states are single ps STAT letters, not {states}")
        procs = {}
        # One -o per column is portable; args goes last so its spaces survive the split.
        for line in self.host.check_output("ps -A -o pid= -o stat= -o args=").splitlines():
            pid, stat, *args = line.split(None, 2)
            if stat[0] in states:
                procs[int(pid)] = args[0] if args else ""
        return procs

    def filter(self, **filters):
        """Return the processes whose ps attributes equal the filters

        >>> host.process.filter(user="root", comm="zsh")
        [<process zsh (pid=2715)>, <process zsh (pid=10502)>, ...]
        """
        match = []
        for attrs in self._get_processes(**filters):
            for key, value in filters.items():
                if str(attrs[key]) != str(value):
                    break
            else:
                attrs["_get_process_attribute_by_pid"] = (
                    self._get_process_attribute_by_pid
                )
                match.append(_Process(attrs))
        return match

    def get(self, **filters):
        """Return the one process matching the filters; raises RuntimeError if none or several"""
        matches = self.filter(**filters)
        if not matches:
            raise RuntimeError("No process found")
        if len(matches) > 1:
            raise RuntimeError("Multiple process found: {}".format(matches))
        return matches[0]

    def _get_processes(self, **filters):
        raise NotImplementedError

    def _get_process_attribute_by_pid(self, pid, name):
        raise NotImplementedError

    @classmethod
    def get_module_class(cls, host):
        if host.file("/bin/ps").linked_to == "/bin/busybox":
            return BusyboxProcess
        if host.file("/bin/busybox").exists:
            if host.file("/bin/ps").inode == host.file("/bin/busybox").inode:
                return BusyboxProcess
        if host.system_info.type == "linux" or host.system_info.type.endswith(
            "bsd"
        ):
            return PosixProcess
        raise NotImplementedError

    def __repr__(self):
        return "<process>"


class PosixProcess(Process):
    # Should be portable on both Linux and BSD

    def _get_processes(self, **filters):
        cmd = "ps -Aww -o %s"
        # lstart and args contain spaces, so they go last.
        attributes = sorted(
            ({"pid", "comm", "pcpu", "pmem"} | set(filters))
            - {"lstart", "args"}
        ) + ["lstart", "args"]
        arg = ":50,".join(attributes)

        procs = []
        # skip first line (header)
        for line in self.host.exec_command(
            cmd % arg, ignore_errors=True
        ).stdout.splitlines()[1:]:
            splitted = line.split()
            attrs = {}
            i = 0
            for i, key in enumerate(attributes[:-2]):
                attrs[key] = int_or_float(splitted[i])
            attrs["lstart"] = " ".join(splitted[i + 1 : i + 6])
            attrs["args"] = " ".join(splitted[i + 6 :])
            procs.append(attrs)
        return procs

    def _get_process_attribute_by_pid(self, pid, name):
        out = self.host.exec_command(
            "ps -ww -p %s -o lstart,%s" % (str(pid), name), ignore_errors=True
        ).stdout
        splitted = out.splitlines()[1].split()
        return {
            "lstart": " ".join(splitted[:5]),
            name: int_or_float(splitted[5]),
        }


class BusyboxProcess(Process):
    def in_state(self, *states: str) -> dict[int, str]:
        # busybox ps has a stat column only when built with its additional columns.
        raise NotImplementedError

    def _get_processes(self, **filters):
        cmd = "ps -A -o %s"
        # args contains spaces, so it goes last.
        attributes = sorted(
            ({"pid", "comm", "time"} | set(filters)) - {"args"}
        ) + ["args"]
        arg = ",".join(attributes)

        procs = []
        # skip first line (header)
        for line in self.host.exec_command(
            cmd % arg, ignore_errors=True
        ).stdout.splitlines()[1:]:
            splitted = line.split()
            attrs = {}
            i = 0
            for i, key in enumerate(attributes[:-1]):
                attrs[key] = int_or_float(splitted[i])

            attrs["lstart"] = attrs["time"]
            attrs["args"] = " ".join(splitted[i + 1 :])
            procs.append(attrs)

        return procs

    def _get_process_attribute_by_pid(self, pid, name):
        out = self.host.exec_command(
            "ps -o pid,time,%s" % name, ignore_errors=True
        ).stdout

        # skip first line (header)
        for line in out.splitlines()[1:]:
            splitted = line.split()
            if int(splitted[0]) == pid:
                return {
                    "lstart": splitted[1],
                    name: int_or_float(splitted[2]),
                }
