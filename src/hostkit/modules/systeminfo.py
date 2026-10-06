# Derived from pytest-testinfra (https://github.com/pytest-dev/pytest-testinfra), Apache-2.0,
# and modified. See LICENSE and NOTICE.
import re
from dataclasses import dataclass

import hostkit.backends
from hostkit.utils import cached_property


@dataclass
class SystemInfo:
    """Describe the host's operating system, kernel and hostname"""

    host: hostkit.backends.Connection

    @cached_property
    def sysinfo(self) -> dict:
        sysinfo = {
            "type": None,
            "distribution": None,
            "codename": None,
            "release": None,
            "arch": None,
        }
        uname: hostkit.backends.CommandResult = self.host.exec_command(
            "uname -s", ignore_errors=True
        )
        sysinfo["type"] = uname.stdout.rstrip("\r\n").lower()
        if sysinfo["type"] == "linux":
            sysinfo.update(**self._get_linux_sysinfo())
        elif sysinfo["type"] == "darwin":
            sysinfo.update(**self._get_darwin_sysinfo())
        else:
            # BSD
            sysinfo["release"] = self.host.exec_command(
                "uname -r", ignore_errors=True
            ).stdout
            sysinfo["distribution"] = sysinfo["type"]
            sysinfo["codename"] = None

        sysinfo["arch"] = self.host.exec_command(
            "uname -m", ignore_errors=True
        ).stdout.strip()
        return sysinfo

    @staticmethod
    def _get_sysinfo_from_lsb_release(lsb: str) -> dict:
        sysinfo = {}
        for line in lsb.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip().lower()
            if key == "distributor id":
                sysinfo["distribution"] = value
            elif key == "release":
                sysinfo["release"] = value
            elif key == "codename":
                sysinfo["codename"] = value
        return sysinfo

    @staticmethod
    def _get_sysinfo_from_os_release(os_release: list[str]) -> dict:
        sysinfo = {}
        for line in os_release:
            for key, attname in (
                ("ID=", "distribution"),
                ("VERSION_ID=", "release"),
                ("VERSION_CODENAME=", "codename"),
            ):
                if line.startswith(key):
                    sysinfo[attname] = (
                        line[len(key) :]
                        .replace('"', "")
                        .replace("'", "")
                        .strip()
                    )
        # Arch doesn't have releases
        if sysinfo["distribution"] == "arch":
            sysinfo["release"] = "rolling"
        return sysinfo

    @staticmethod
    def _get_sysinfo_from_redhat_release(redhat_release: str) -> dict:
        sysinfo = {}
        match = re.match(r"^(.+) release ([^ ]+) .*$", redhat_release)
        if match:
            sysinfo["distribution"], sysinfo["release"] = match.groups()
        return sysinfo

    def _get_linux_sysinfo(self) -> dict:
        sysinfo = {}

        lsb:hostkit.backends.CommandResult = self.host.exec_command(
            "lsb_release -a", ignore_errors=True
        )
        if lsb.rc == 0:
            return self._get_sysinfo_from_lsb_release(lsb.stdout)

        os_release = self.host.exec_command(
            "cat /etc/os-release", ignore_errors=True
        )
        if os_release.rc == 0:
            return self._get_sysinfo_from_os_release(
                os_release.stdout.splitlines()
            )

        # RedHat / CentOS 6 haven't /etc/os-release
        redhat_release = self.host.exec_command(
            "cat /etc/redhat-release", ignore_errors=True
        )
        if redhat_release.rc == 0:
            return self._get_sysinfo_from_redhat_release(
                redhat_release.stdout.strip()
            )

        # Alpine doesn't have /etc/os-release
        alpine_release = self.host.exec_command(
            "cat /etc/alpine-release", ignore_errors=True
        )
        if alpine_release.rc == 0:
            sysinfo["distribution"] = "alpine"
            sysinfo["release"] = alpine_release.stdout.strip()
            return sysinfo

        return sysinfo

    def _get_darwin_sysinfo(self) -> dict:
        sysinfo = {}

        sw_vers = self.host.exec_command("sw_vers", ignore_errors=True)
        if sw_vers.rc == 0:
            for line in sw_vers.stdout.splitlines():
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip()
                if key == "productname":
                    sysinfo["distribution"] = value
                elif key == "productversion":
                    sysinfo["release"] = value

        return sysinfo

    def _get_windows_sysinfo(self) -> dict:
        sysinfo = {}
        for line in self.host.exec_command(
            'systeminfo | findstr /B /C:"OS"', ignore_errors=True
        ).stdout.splitlines():
            key, value = line.split(":", 1)
            key = key.strip().replace(" ", "_").lower()
            value = value.strip()
            if key == "os_name":
                sysinfo["distribution"] = value
                sysinfo["type"] = value.split(" ")[1].lower()
            elif key == "os_version":
                sysinfo["release"] = value
        sysinfo["arch"] = self.host.exec_command(
            "echo %PROCESSOR_ARCHITECTURE%", ignore_errors=True
        )
        return sysinfo

    @cached_property
    def os_release(self) -> str:
        """Return the /etc/*release files as cat prints them"""
        return self.host.check_output("cat /etc/*release")

    @property
    def kernel_release(self) -> str:
        """Return the running kernel as uname -r prints it

        >>> host.system_info.kernel_release
        '5.14.0-503.14.1.el9_5.x86_64'
        """
        return self.host.exec_command(
            "uname -r", ignore_errors=True
        ).stdout.strip()

    @property
    def hostname(self) -> str:
        """Return the name the host gives itself, raise if hostname failed

        >>> host.system_info.hostname
        'web-1'
        """
        return self.host.check_output("hostname").strip()

    @property
    def type(self) -> str:
        return self.sysinfo["type"]

    @property
    def distribution(self) -> str:
        return self.sysinfo["distribution"]

    @property
    def release(self) -> str:
        return self.sysinfo["release"]

    @property
    def codename(self) -> str:
        return self.sysinfo["codename"]

    @property
    def arch(self) -> str:
        return self.sysinfo["arch"]
