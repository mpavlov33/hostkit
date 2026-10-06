"""hostkit's SystemLoad: a host's memory, disk, load and cpu readings.

The double is hostkit.testing.Sshd, answering each command with what the tool prints.
"""
import pytest

from hostkit.testing import Reply


def load_of(sshd, host, answers):
    sshd.answers = [(fragment, Reply(0, out.encode()) if isinstance(out, str)
                     else Reply(out[0], out[1].encode(), out[2].encode()))
                    for fragment, out in answers]
    return host.system_load


def test_memory_usage_is_read_from_free(sshd, host):
    load = load_of(sshd, host, [("free -h", (
        "Mem:            31Gi       8.7Gi       1.2Gi        41Mi        21Gi        22Gi\n"
        "Swap:          8.0Gi       1.0Mi       8.0Gi\n"))])

    assert load.memory == {
        "total": "31Gi", "used": "8.7Gi", "free": "1.2Gi", "avail": "22Gi"}


def test_disk_usage_is_read_from_df(sshd, host):
    load = load_of(sshd, host, [("df -h /", "/dev/mapper/rl-root   70G   23G   48G  33% /\n")])

    assert load.disk == {"size": "70G", "used": "23G", "avail": "48G", "use%": "33%"}


def test_load_average_is_proc_loadavg(sshd, host):
    load = load_of(sshd, host, [("/proc/loadavg", "0.52 0.58 0.59 2/312 12345\n")])

    assert load.load_average.split()[:3] == ["0.52", "0.58", "0.59"]


def test_a_reading_the_host_could_not_take_raises(sshd, host):
    """A reading that failed must not pass for an idle host."""
    load = load_of(sshd, host, [("nproc", (127, "", "bash: nproc: command not found\n"))])

    with pytest.raises(Exception, match="'nproc' returned non-zero exit code"):
        load.cpu_count
