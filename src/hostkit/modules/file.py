# Derived from pytest-testinfra (https://github.com/pytest-dev/pytest-testinfra), Apache-2.0,
# and modified. See LICENSE and NOTICE.
import shlex
from dataclasses import dataclass
import hostkit.backends
from hostkit.utils import act


@dataclass
class File:
    path: str
    host: hostkit.backends.Connection

    @property
    def exists(self) -> bool:
        """Return True if the path exists

        >>> host.file("/etc/passwd").exists
        True
        """
        return (
            self.host.exec_command(
                "test -e %s" % self.path, ignore_errors=True
            ).rc
            == 0
        )

    @property
    def is_file(self) -> bool:
        return (
            self.host.exec_command(
                "test -f %s" % self.path, ignore_errors=True
            ).rc
            == 0
        )

    @property
    def is_directory(self) -> bool:
        return (
            self.host.exec_command(
                "test -d %s" % self.path, ignore_errors=True
            ).rc
            == 0
        )

    @property
    def is_pipe(self) -> bool:
        return (
            self.host.exec_command(
                "test -p %s" % self.path, ignore_errors=True
            ).rc
            == 0
        )

    @property
    def is_socket(self) -> bool:
        return (
            self.host.exec_command(
                "test -S %s" % self.path, ignore_errors=True
            ).rc
            == 0
        )

    @property
    def is_symlink(self) -> bool:
        return (
            self.host.exec_command(
                "test -L %s" % self.path, ignore_errors=True
            ).rc
            == 0
        )

    @property
    def is_executable(self) -> bool:
        return (
            self.host.exec_command(
                "test -x %s" % self.path, ignore_errors=True
            ).rc
            == 0
        )

    @property
    def linked_to(self) -> str:
        return self.host.exec_command(
            "readlink -f %s" % self.path
        ).stdout.strip()

    @property
    def user(self) -> str:
        """Return file owner"""
        return self.host.exec_command(
            "ls -l %s | awk '{print $3}'" % self.path
        ).stdout.strip()

    @property
    def group(self) -> str:
        """Return file group owner"""
        return self.host.exec_command(
            "ls -l %s | awk '{print $4}'" % self.path
        ).stdout.strip()

    @property
    def uid(self) -> int:
        """Return file user id as integer"""
        return int(
            self.host.exec_command("id -u %s" % self.user).stdout.strip()
        )

    @property
    def gid(self) -> int:
        """Return file group id as integer"""
        return int(
            self.host.exec_command("id -g %s" % self.group).stdout.strip()
        )

    def contains(self, pattern) -> bool:
        """Return True if a line matches the grep pattern, False also for no file"""
        return (
            self.host.exec_command(
                f"grep -qs -- {shlex.quote(pattern)} {self.path}",
                ignore_errors=True,
            ).rc
            == 0
        )

    def count(self, pattern: str) -> int:
        """Return the number of lines matching pattern, 0 if none or no file

        >>> host.file("/var/log/myapp/server.log").count("STARTING SERVER")
        2
        """
        return int(
            self.host.exec_command(
                f"grep -c -- {shlex.quote(pattern)} {self.path}",
                ignore_errors=True,
            ).stdout.strip()
            or 0
        )

    @property
    def md5sum(self) -> str:
        return self.host.exec_command(
            "md5sum %s | awk '{print $1}'" % self.path
        ).stdout.strip()

    @property
    def sha256sum(self) -> str:
        return self.host.exec_command(
            "sha256sum %s | awk '{print $1}'" % self.path
        ).stdout.strip()

    @property
    def build_id(self) -> str:
        """Return the GNU Build ID of an ELF file, "" if it has none

        >>> host.file("/lib64/libc.so.6").build_id
        '1b0bb1b0a4d4ee8b1c0e3f2d6a1f8e0c5d3b9a71'
        """
        return self.host.check_output(
            "readelf -n %s | grep 'Build ID' | awk '{print $3}'" % self.path
        ).strip()

    @property
    def content(self) -> str:
        # Capped at the last 20000 lines so a huge file does not flood the output.
        return self.host.exec_command(
            "cat -- %s | tail -n 20000" % self.path
        ).stdout

    def tail(self, lines: int) -> str:
        """Return the last lines of the file, "" if there is no file"""
        return self.host.exec_command(
            "tail -n %s %s" % (lines, self.path)
        ).stdout

    @property
    def mtime(self):
        """Return time of last modification as a datetime; not implemented"""
        raise NotImplementedError

    @property
    def size(self) -> int:
        """Return size of file in bytes"""
        return int(
            self.host.exec_command(
                'stat --printf="%s" {}'.format(self.path)
            ).stdout.strip()
        )

    def listdir(self) -> list[str]:
        """Return the names of the entries in the directory

        >>> host.file("/opt/myapp").listdir()
        ['bin', 'myapp.conf']
        """
        return (
            self.host.exec_command("ls -1 -q -- %s" % self.path)
            .stdout.strip()
            .splitlines()
        )

    def find(self, *names: str, file_type: str = "") -> list[str]:
        """Return the paths under the directory whose name matches any of the globs

        file_type is find's -type letter, e.g. "f" or "d".

        >>> host.file("/opt/myapp").find("*.log", file_type="f")
        ['/opt/myapp/logs/server.log']
        """
        cmd = "find %s" % self.path
        match = " -o ".join("-name %s" % shlex.quote(n) for n in names)
        if len(names) > 1:
            match = r"\( %s \)" % match
        if match:
            cmd += " %s" % match
        if file_type:
            cmd += " -type %s" % file_type
        return self.host.exec_command(cmd).stdout.strip().splitlines()

    def remove(self, check: bool = False) -> int:
        return act(self.host, "rm -rf %s" % self.path, check)

    def mkdir(self, check: bool = False) -> int:
        return act(self.host, "mkdir -p %s" % self.path, check)

    def copy(
        self,
        dst: str,
        recursive: bool = False,
        preserve: bool = False,
        check: bool = False,
    ) -> int:
        flags = ("-r " if recursive else "") + ("-p " if preserve else "")
        return act(self.host, "cp %s%s %s" % (flags, self.path, dst), check)

    def move(self, dst: str, force: bool = False, check: bool = False) -> int:
        flags = "-f " if force else ""
        return act(self.host, "mv %s%s %s" % (flags, self.path, dst), check)

    def chmod(self, mode: str, check: bool = False) -> int:
        return act(self.host, "chmod %s %s" % (mode, self.path), check)

    def touch(self, check: bool = False) -> int:
        return act(self.host, "touch %s" % self.path, check)

    def truncate(self, check: bool = False) -> int:
        return act(self.host, "truncate -s 0 %s" % self.path, check)

    def write(
        self, content: str, append: bool = False, check: bool = False
    ) -> int:
        """Write content byte for byte: no newline is added"""
        redirect = ">>" if append else ">"
        return act(
            self.host,
            "printf '%%s' %s %s %s"
            % (shlex.quote(content), redirect, self.path),
            check,
        )

    def add_line(self, line: str, check: bool = False) -> int:
        """Append line unless the file has it already; makes a missing file"""
        quoted = shlex.quote(line)
        return act(
            self.host,
            f"grep -qxF -- {quoted} {self.path} 2>/dev/null"
            f" || printf '%s\\n' {quoted} >> {self.path}",
            check,
        )

    def __repr__(self):
        return "<file {}>".format(self.path)

    def __eq__(self, other):
        if isinstance(other, File):
            return self.path == other.path
        if isinstance(other, str):
            return self.path == other
        return False
