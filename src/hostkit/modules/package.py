# Derived from pytest-testinfra (https://github.com/pytest-dev/pytest-testinfra), Apache-2.0,
# and modified. See LICENSE and NOTICE.
from dataclasses import dataclass
import hostkit.backends
from hostkit.utils import act


@dataclass
class Package:
    """Query, install and remove a package with the host's package manager"""

    name: str
    host: hostkit.backends.Connection

    @property
    def is_installed(self):
        """Return True if the package is installed

        >>> host.package("nginx").is_installed
        True
        """
        raise NotImplementedError

    @property
    def release(self):
        """Return the release specific info from the package version

        >>> host.package("nginx").release
        '1.el6'
        """
        raise NotImplementedError

    @property
    def version(self):
        """Return package version as returned by the package system

        >>> host.package("nginx").version
        '1.2.1-2.2+wheezy3'
        """
        raise NotImplementedError

    def install(self, with_update: bool = True) -> None:
        """Install the package unless its program is on PATH already

        >>> host.package("tmux").install()
        """
        if self.host.exists(self.name):
            return
        for cmd in self._install_commands(with_update):
            self.host.check_output(cmd)

    def _install_commands(self, with_update: bool) -> list[str]:
        raise NotImplementedError

    def remove(self, check: bool = False) -> int:
        """Remove the package with the family's manager

        >>> host.package("nscd").remove()
        0
        """
        return act(self.host, self._remove_command(), check)

    def _remove_command(self) -> str:
        raise NotImplementedError

    def __repr__(self):
        return "<package {}>".format(self.name)

    @classmethod
    def get_module_class(cls, host):
        if host.system_info.type == "windows":
            return ChocolateyPackage
        if host.system_info.type == "freebsd":
            return FreeBSDPackage
        if host.system_info.type in ("openbsd", "netbsd"):
            return OpenBSDPackage
        if host.system_info.distribution in ("debian", "ubuntu"):
            return DebianPackage
        if host.system_info.distribution and (
            host.system_info.distribution.lower()
            in (
                "almalinux",
                "centos",
                "cloudlinux",
                "fedora",
                "opensuse-leap",
                "opensuse-tumbleweed",
                "rocky",
                "ol",
            )
        ):
            return RpmPackage
        if host.system_info.distribution in ("arch", "manjarolinux"):
            return ArchPackage
        if host.exists("apk"):
            return AlpinePackage
        # Unknown distribution: guess from the package tools present.
        if host.exists("dpkg-query"):
            return DebianPackage
        if host.exists("rpm"):
            return RpmPackage
        raise NotImplementedError


class DebianPackage(Package):
    @property
    def is_installed(self):
        result = self.host.exec_command(
            "dpkg-query -f '${Status}' -W %s" % self.name
        )
        if result.rc == 1:
            return False
        out = result.stdout.strip().split()
        installed_status = ["ok", "installed"]
        return out[0] in ["install", "hold"] and out[1:3] == installed_status

    @property
    def release(self):
        raise NotImplementedError

    @property
    def version(self):
        out = self.host.exec_command(
            "dpkg-query -f '${Status} ${Version}' -W %s" % self.name
        ).stdout
        splitted = out.split()
        assert splitted[0].lower() in (
            "install",
            "hold",
        ), "The package {} is not installed, dpkg-query output: {}".format(
            self.name, out
        )
        return splitted[3]

    def _install_commands(self, with_update: bool) -> list[str]:
        update = ["apt-get update"] if with_update else []
        return update + ["apt-get install -y %s" % self.name]

    def _remove_command(self) -> str:
        return "DEBIAN_FRONTEND=noninteractive apt-get remove -y %s" % self.name


class FreeBSDPackage(Package):
    @property
    def is_installed(self):
        res = self.host.exec_command("pkg query %%n %s" % self.name)
        EX_UNAVAILABLE = 69
        assert res.rc in [0, EX_UNAVAILABLE], "Return code is unexpected"
        return res.rc == 0

    @property
    def release(self):
        raise NotImplementedError

    @property
    def version(self):
        return self.host.exec_command("pkg query %%v %s" % self.name)


class OpenBSDPackage(Package):
    @property
    def is_installed(self):
        return (
            self.host.exec_command(
                "pkg_info -e %s" % "{}-*".format(self.name)
            ).rc
            == 0
        )

    @property
    def release(self):
        raise NotImplementedError

    @property
    def version(self):
        out = self.host.exec_command(
            "pkg_info -e %s" % "{}-*".format(self.name)
        ).stdout.rstrip("\r\n")
        # OpenBSD prints inst:zsh-5.0.5p0, NetBSD zsh-5.0.7nb1: split after the name.
        return out.split(self.name + "-", 1)[1]


class RpmPackage(Package):
    @property
    def is_installed(self):
        return self.host.exec_command("rpm -q %s" % self.name).rc == 0

    @property
    def version(self):
        return self.host.exec_command(
            'rpm -q --queryformat="%%{VERSION}" %s' % self.name
        ).stdout.rstrip("\r\n")

    @property
    def release(self):
        return self.host.exec_command(
            'rpm -q --queryformat="%%{RELEASE}" %s' % self.name
        ).stdout.rstrip("\r\n")

    def _install_commands(self, with_update: bool) -> list[str]:
        return ["yum install -y %s" % self.name]

    def _remove_command(self) -> str:
        return "yum remove -y %s" % self.name


class AlpinePackage(Package):
    @property
    def is_installed(self):
        return self.host.exec_command("apk -e info %s" % self.name).rc == 0

    @property
    def version(self):
        out = (
            self.host.exec_command("apk -e -v info %s" % self.name)
            .stdout.rstrip("\r\n")
            .split("-")
        )
        return out[-2]

    @property
    def release(self):
        out = (
            self.host.exec_command("apk -e -v info %s" % self.name)
            .stdout.rstrip("\r\n")
            .split("-")
        )
        return out[-1]


class ArchPackage(Package):
    @property
    def is_installed(self):
        return self.host.exec_command("pacman -Q %s" % self.name).rc == 0

    @property
    def version(self):
        out = (
            self.host.exec_command("pacman -Q %s" % self.name)
            .stdout.rstrip("\r\n")
            .split(" ")
        )
        return out[1]

    @property
    def release(self):
        raise NotImplementedError


class ChocolateyPackage(Package):
    @property
    def is_installed(self):
        return self.host.exec_command("choco info -lo %s" % self.name).rc == 0

    @property
    def version(self):
        _, version = (
            self.host.exec_command("choco info -lo %s -r" % self.name)
            .stdout.rstrip("\r\n")
            .split("|", 1)
        )
        return version

    @property
    def release(self):
        raise NotImplementedError
