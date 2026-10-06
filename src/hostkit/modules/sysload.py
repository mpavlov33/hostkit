import re
from dataclasses import dataclass

import hostkit.backends


@dataclass
class SystemLoad:
    """Read how loaded a host is: memory, disk, CPU and I/O"""

    host: hostkit.backends.Connection

    @property
    def memory(self) -> dict:
        """Return memory usage as free -h prints it

        >>> host.system_load.memory
        {'total': '31Gi', 'used': '12Gi', 'free': '2.1Gi', 'avail': '18Gi'}
        """
        res = [
            x
            for x in self.host.check_output("free -h | tail -n 2")
            .strip()
            .replace("\n", " ")
            .split(" ")
            if x
        ]
        return {
            "total": res[1],
            "used": res[2],
            "free": res[3],
            "avail": res[6],
        }

    @property
    def disk(self) -> dict:
        """Return usage of the root filesystem as df -h prints it

        >>> host.system_load.disk
        {'size': '99G', 'used': '61G', 'avail': '39G', 'use%': '62%'}
        """
        res = [
            x
            for x in self.host.check_output("df -h / | tail -n 1").split(" ")
            if x
        ]
        return {
            "size": res[1],
            "used": res[2],
            "avail": res[3],
            "use%": res[4],
        }

    @property
    def load_average(self) -> str:
        """Return /proc/loadavg as it reads"""
        res = self.host.exec_command("cat /proc/loadavg")
        if res.rc:
            raise Exception(
                f"command '{res.command}' returned non-zero exit code: {res.stderr}"
            )
        return res.output

    @property
    def load1(self) -> float:
        """Return the 1-minute load average uptime prints"""
        return float(
            self.host.check_output(
                "awk '{print $1}' <(uptime | sed 's/.*load average: //')"
            )[:-2]
        )

    @property
    def cpu_count(self) -> int:
        return int(self.host.check_output("nproc").strip())

    @property
    def cpu_idle(self) -> float:
        """Return the idle percentage of the CPU line top prints"""
        line = self.host.check_output('top -bn1 | grep "Cpu(s)"')
        return float(re.search(r"([\d.]+)\s*id", line).group(1))

    @property
    def memory_mb(self) -> tuple[float, float]:
        """Return total and available memory, in megabytes"""
        mem_info = self.host.check_output("free --mega | grep Mem").split()
        return float(mem_info[1]), float(mem_info[6])

    @property
    def memory_total_gb(self) -> float:
        return float(
            self.host.check_output("free --giga | awk '/Mem:/ {print $2}'").strip()
        )

    @property
    def io_util(self) -> float:
        """Return %util of the busiest device iostat reports; raises if it printed no report"""
        lines = self.host.check_output("iostat -xz 1 1").splitlines()
        header = next(
            (n for n, line in enumerate(lines) if line.startswith("Device")), None
        )
        if header is None:
            raise ValueError("iostat printed no device report")
        # Columns differ between sysstat versions, so %util is found by its header.
        util = lines[header].split().index("%util")
        utils = []
        for line in lines[header + 1 :]:
            if not line.strip():
                break
            utils.append(float(line.split()[util]))
        return max(utils, default=0.0)
