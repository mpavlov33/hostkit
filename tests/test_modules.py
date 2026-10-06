"""hostkit's modules, logging_wrap and CommandResult over a real ``hostkit.Connection``.

The double is ``hostkit.testing.Sshd``, answering with the output the real tools print."""
import io
import logging
import re
import subprocess
import sys

import pytest

import hostkit
from hostkit.backends import CommandResult
from hostkit.logging_wrap import CustomFormatter, log_color, set_colored_handler
from hostkit.modules.package import DebianPackage, RpmPackage
from hostkit.modules.process import BusyboxProcess, PosixProcess
from hostkit.testing import Reply


# ---- What the real tools print: parsing it is what the modules are for ----

ALMA_8 = b'''NAME="AlmaLinux"
VERSION="8.10 (Cerulean Leopard)"
ID="almalinux"
ID_LIKE="rhel centos fedora"
VERSION_ID="8.10"
PLATFORM_ID="platform:el8"
PRETTY_NAME="AlmaLinux 8.10 (Cerulean Leopard)"
ANSI_COLOR="0;34"
CPE_NAME="cpe:/o:almalinux:almalinux:8::baseos"
HOME_URL="https://almalinux.org/"
'''

DEBIAN_12 = b'''PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
VERSION_ID="12"
VERSION="12 (bookworm)"
VERSION_CODENAME=bookworm
ID=debian
HOME_URL="https://www.debian.org/"
'''

CENTOS_7 = b'''NAME="CentOS Linux"
VERSION="7 (Core)"
ID="centos"
ID_LIKE="rhel fedora"
VERSION_ID="7"
PRETTY_NAME="CentOS Linux 7 (Core)"
'''

OL_8 = b'''NAME="Oracle Linux Server"
VERSION="8.10"
ID="ol"
ID_LIKE="fedora"
VARIANT="Server"
VARIANT_ID="server"
VERSION_ID="8.10"
PLATFORM_ID="platform:el8"
'''

AMZN_2 = b'''NAME="Amazon Linux"
VERSION="2"
ID="amzn"
ID_LIKE="centos rhel fedora"
VERSION_ID="2"
PRETTY_NAME="Amazon Linux 2"
'''

RHEL_9 = b'''NAME="Red Hat Enterprise Linux"
VERSION="9.4 (Plow)"
ID="rhel"
ID_LIKE="fedora"
VERSION_ID="9.4"
'''

UBUNTU_LSB = b"Distributor ID:\tUbuntu\nDescription:\tUbuntu 22.04.4 LTS\nRelease:\t22.04\nCodename:\tjammy\n"

NO_LSB = Reply(127, b"", b"bash: lsb_release: command not found\n")
NO_FILE = Reply(1, b"", b"cat: /etc/os-release: No such file or directory\n")


def linux(os_release=ALMA_8, lsb=NO_LSB, redhat=NO_FILE, alpine=NO_FILE):
    """The answers SystemInfo gets from one Linux VM."""
    return [
        ("uname -s", Reply(0, b"Linux\n")),
        ("uname -m", Reply(0, b"x86_64\n")),
        ("lsb_release -a", lsb),
        ("cat /etc/os-release",
         os_release if isinstance(os_release, Reply) else Reply(0, os_release)),
        ("cat /etc/redhat-release", redhat),
        ("cat /etc/alpine-release", alpine),
    ]


# What host.process asks first, to pick the ps flavour; a test of its actions skips these.
PROBES = [command for command, _ in linux()]


class Ps:
    """procps ``ps``: prints exactly the ``-o`` columns asked for, of the ``-p`` pid or of all."""

    HEADERS = {"comm": "COMMAND", "pcpu": "%CPU", "pid": "PID",
               "pmem": "%MEM", "user": "USER", "lstart": "STARTED",
               "args": "COMMAND", "ppid": "PPID", "rss": "RSS"}
    NUMERIC = {"pcpu", "pid", "pmem", "ppid", "rss"}

    def __init__(self, procs):
        self.procs = procs

    def __call__(self, command):
        pid = re.search(r"-p (\d+)", command)
        procs = [p for p in self.procs
                 if pid is None or p["pid"] == int(pid.group(1))]
        spec = re.search(r"-o (\S+)", command)
        if spec is None:
            rows = ["    PID TTY          TIME CMD"] + [
                "{0:>7} ?        00:00:01 {1}".format(p["pid"], p["comm"])
                for p in procs]
            return Reply(0 if procs else 1, ("\n".join(rows) + "\n").encode())
        columns = []
        for item in spec.group(1).split(","):
            name, _, width = item.partition(":")
            columns.append((name, int(width) if width else 0))
        lines = [self._line([(n, w, self.HEADERS[n]) for n, w in columns])]
        for proc in procs:
            lines.append(self._line([(n, w, proc[n]) for n, w in columns]))
        return Reply(0 if procs else 1, ("\n".join(lines) + "\n").encode())

    def _line(self, cells):
        out = []
        for i, (name, width, value) in enumerate(cells):
            text = str(value)
            if i == len(cells) - 1:
                out.append(text)
            elif name in self.NUMERIC:
                out.append(text.rjust(max(width, 6)))
            else:
                out.append(text.ljust(max(width, 8)))
        return " ".join(out)


def proc(pid, comm, args, user="root", ppid=1, pcpu=0.0, pmem=0.1, rss=5120,
         lstart="Thu Oct  1 09:20:01 2026"):
    return dict(pid=pid, comm=comm, args=args, user=user, ppid=ppid,
                pcpu=pcpu, pmem=pmem, rss=rss, lstart=lstart)


# Two root processes of one name and a non-root one: what filter and get must tell apart.
OPENSSL_VM = [
    proc(1, "systemd", "/usr/lib/systemd/systemd --switched-root --system "
                       "--deserialize 17", ppid=0, pcpu=0.1, pmem=0.6,
         lstart="Thu Oct  1 09:12:44 2026"),
    proc(33, "kworker/0:1-events", "[kworker/0:1-events]", ppid=2),
    proc(812, "sshd", "/usr/sbin/sshd -D -oCiphers=aes256-gcm@openssh.com"),
    proc(900, "myapp-server", "/usr/libexec/myapp/myapp-server -v"),
    proc(1500, "postgres", "postgres: checkpointer", user="postgres"),
    proc(2345, "openssl", "openssl s_server -accept 4433 -www -cert "
                          "/root/cert.pem", ppid=812, pcpu=12.5),
    proc(2346, "openssl", "openssl s_server -accept 4434 -www", ppid=812),
    proc(2400, "openssl", "openssl s_client -connect localhost:4433",
         user="myapp"),
]


# ---- File ----

@pytest.mark.parametrize("prop,flag", [
    ("exists", "-e"), ("is_file", "-f"), ("is_directory", "-d"),
    ("is_pipe", "-p"), ("is_socket", "-S"), ("is_symlink", "-L"),
    ("is_executable", "-x"),
])
@pytest.mark.parametrize("rc,expected", [(0, True), (1, False)])
def test_a_file_predicate_is_the_exit_status_of_test(
        sshd, host, prop, flag, rc, expected):
    sshd.answers = [("test {0} /run/myapp/myapp.sock".format(flag), Reply(rc))]

    assert getattr(host.file("/run/myapp/myapp.sock"), prop) is expected
    assert sshd.ran == ["test {0} /run/myapp/myapp.sock".format(flag)]


def test_file_content_is_what_cat_printed(sshd, host):
    sshd.answers = [("/etc/myapp/myapp.conf",
                     Reply(0, b"SERVER_URL=http://192.0.2.20:8801\n"))]

    assert host.file("/etc/myapp/myapp.conf").content == \
        "SERVER_URL=http://192.0.2.20:8801\n"


def test_file_size_is_stat_bytes_as_an_integer(sshd, host):
    sshd.answers = [("stat", Reply(0, b"104857600"))]

    assert host.file("/tmp/tmux1a2b3c/stdout").size == 104857600


@pytest.mark.parametrize("listing,expected", [
    (b"files\nroles\nsite.yml\n", ["files", "roles", "site.yml"]),
    (b"", []),
], ids=["three-entries", "empty-directory"])
def test_listdir_is_one_name_per_line(sshd, host, listing, expected):
    sshd.answers = [("ls -1", Reply(0, listing))]

    assert host.file("/root/ansible").listdir() == expected


@pytest.mark.parametrize("prop,printed,expected", [
    ("md5sum", b"d41d8cd98f00b204e9800998ecf8427e\n",
     "d41d8cd98f00b204e9800998ecf8427e"),
    ("sha256sum", b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n",
     "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("linked_to", b"/bin/busybox\n", "/bin/busybox"),
    ("user", b"root\n", "root"),
    ("build_id", b"1b0bb1b0a4d4ee8b1c0e3f2d6a1f8e0c5d3b9a71\n",
     "1b0bb1b0a4d4ee8b1c0e3f2d6a1f8e0c5d3b9a71"),
])
def test_one_line_file_facts_come_back_without_the_newline(
        sshd, host, prop, printed, expected):
    sshd.answers = [("/bin/ps", Reply(0, printed))]

    assert getattr(host.file("/bin/ps"), prop) == expected


# (path, method, args, the one command it sends)
FILE_ACTIONS = [
    ("/var/log/myapp/*", "remove", (), "rm -rf /var/log/myapp/*"),
    ("/root/myapp", "mkdir", (), "mkdir -p /root/myapp"),
    ("/root/worker", "copy", ("/root/worker_copy",),
     "cp /root/worker /root/worker_copy"),
    ("/root/ansible/files/clone_repo_ssh/*", "copy", ("/root/.ssh/", True),
     "cp -r /root/ansible/files/clone_repo_ssh/* /root/.ssh/"),
    ("/usr/sbin/sshd", "copy", ("/usr/sbin/sshd-seccomp", False, True),
     "cp -p /usr/sbin/sshd /usr/sbin/sshd-seccomp"),
    ("/tmp/packages", "copy", ("/root", True, True), "cp -r -p /tmp/packages /root"),
    ("/var/lib/docker/volumes/build/_data/out*", "move",
     ("/data/logs/build_volume",),
     "mv /var/lib/docker/volumes/build/_data/out* /data/logs/build_volume"),
    ("/root/.ssh/repo_private_key_rsa_format", "move",
     ("/root/.ssh/repo_private_key", True),
     "mv -f /root/.ssh/repo_private_key_rsa_format /root/.ssh/repo_private_key"),
    ("/root/.ssh/repo_private_key", "chmod", ("600",),
     "chmod 600 /root/.ssh/repo_private_key"),
    ("/tmp/netgroup_ok", "touch", (), "touch /tmp/netgroup_ok"),
    ("/var/lib/pgsql/data/log/*", "truncate", (),
     "truncate -s 0 /var/lib/pgsql/data/log/*"),
    ("/etc/myapp/myapp.conf", "write", ("AUTO_UPDATE=NO\n",),
     "printf '%s' 'AUTO_UPDATE=NO\n' > /etc/myapp/myapp.conf"),
    ("/etc/sysconfig/dhcpd", "write", ('DHCPDARGS="br0"\n', True),
     "printf '%s' 'DHCPDARGS=\"br0\"\n' >> /etc/sysconfig/dhcpd"),
    ("/etc/networks", "add_line", ("loopback 127.0.0.0",),
     "grep -qxF -- 'loopback 127.0.0.0' /etc/networks 2>/dev/null"
     " || printf '%s\\n' 'loopback 127.0.0.0' >> /etc/networks"),
]
ACTION_IDS = ["remove", "mkdir", "copy", "copy-r", "copy-p", "copy-r-p", "move",
              "move-f", "chmod", "touch", "truncate", "write", "write-append",
              "add-line"]


@pytest.mark.parametrize("path,method,args,command", FILE_ACTIONS, ids=ACTION_IDS)
def test_a_file_action_sends_its_one_command_and_returns_0(
        sshd, host, path, method, args, command):
    assert getattr(host.file(path), method)(*args) == 0
    assert sshd.ran == [command]


@pytest.mark.parametrize("path,method,args,command", FILE_ACTIONS, ids=ACTION_IDS)
def test_a_file_action_that_failed_returns_its_exit_status(
        sshd, host, path, method, args, command):
    sshd.answers = [(command, Reply(1, err=b"Permission denied\n"))]

    assert getattr(host.file(path), method)(*args) == 1


@pytest.mark.parametrize("path,method,args,command", FILE_ACTIONS, ids=ACTION_IDS)
def test_a_checked_file_action_that_failed_raises_with_its_stderr(
        sshd, host, path, method, args, command):
    sshd.answers = [(command, Reply(1, err=b"Permission denied\n"))]

    with pytest.raises(Exception, match="non-zero exit code: Permission denied"):
        getattr(host.file(path), method)(*args, check=True)
    assert sshd.ran == [command]


@pytest.mark.parametrize("path,method,args,command", FILE_ACTIONS, ids=ACTION_IDS)
def test_a_checked_file_action_that_succeeded_returns_0(
        sshd, host, path, method, args, command):
    sshd.answers = [(command, Reply(0, b"anything printed is not the result\n"))]

    assert getattr(host.file(path), method)(*args, check=True) == 0
    assert sshd.ran == [command]


@pytest.mark.parametrize("printed,rc,expected", [
    (b"make: *** [all] Error 2\n", 0, "make: *** [all] Error 2\n"),
    (b"", 1, ""),
], ids=["a-log", "no-log"])
def test_tail_is_what_tail_printed(sshd, host, printed, rc, expected):
    sshd.answers = [("tail -n 50 /data/logs/build.log", Reply(
        rc, printed, b"" if rc == 0 else
        b"tail: cannot open '/data/logs/build.log' for reading: No such file\n"))]

    assert host.file("/data/logs/build.log").tail(50) == expected
    assert sshd.ran == ["tail -n 50 /data/logs/build.log"]


@pytest.mark.parametrize("path,names,file_type,command", [
    ("/tmp/packages/glibc-2.28/", ("ld-linux-x86-64.so.2",), "",
     "find /tmp/packages/glibc-2.28/ -name ld-linux-x86-64.so.2"),
    ("/root/hadoop/", ("*jobclient*tests.jar",), "f",
     "find /root/hadoop/ -name '*jobclient*tests.jar' -type f"),
    ("/tmp/packages/glibc-2.28/", ("libc.so.6", "libc-*.so"), "f",
     "find /tmp/packages/glibc-2.28/ \\( -name libc.so.6 -o -name 'libc-*.so' \\) -type f"),
    ("/var/cache/myapp/data", (), "d", "find /var/cache/myapp/data -type d"),
], ids=["a-name", "a-glob-of-a-type", "either-name", "only-a-type"])
def test_find_quotes_each_name_and_ors_several(
        sshd, host, path, names, file_type, command):
    sshd.answers = [("find", Reply(0, b"/tmp/packages/glibc-2.28/lib64/libc.so.6\n"
                                      b"/tmp/packages/glibc-2.28/lib/libc.so.6\n"))]

    assert host.file(path).find(*names, file_type=file_type) == [
        "/tmp/packages/glibc-2.28/lib64/libc.so.6",
        "/tmp/packages/glibc-2.28/lib/libc.so.6"], "one path per line"
    assert sshd.ran == [command]


def test_find_under_a_missing_directory_is_no_path(sshd, host):
    sshd.answers = [("find", Reply(1, err=b"find: '/root/hadoop/': No such file or directory\n"))]

    assert host.file("/root/hadoop/").find("*jobclient*tests.jar", file_type="f") == []


@pytest.mark.parametrize("answer,expected", [
    (Reply(0, b"2\n"), 2),
    (Reply(1, b"0\n"), 0),
    (Reply(2, err=b"grep: /var/log/myapp/server.log: No such file or directory\n"), 0),
], ids=["two", "none", "no-file"])
def test_count_is_the_number_grep_c_printed(sshd, host, answer, expected):
    sshd.answers = [("grep -c", answer)]

    assert host.file("/var/log/myapp/server.log").count("STARTING SERVER") == expected
    assert sshd.ran == ["grep -c -- 'STARTING SERVER' /var/log/myapp/server.log"]


@pytest.mark.parametrize("rc,found", [(0, True), (1, False)])
def test_contains_sends_the_whole_pattern_as_one_argument(sshd, host, rc, found):
    sshd.answers = [("grep -qs", Reply(rc))]

    assert host.file("/etc/cron.d/myapp-cron").contains(
        "root /usr/bin/myappctl -q --auto-update") is found
    assert sshd.ran == ["grep -qs -- 'root /usr/bin/myappctl -q --auto-update' "
                        "/etc/cron.d/myapp-cron"]


@pytest.mark.parametrize("accessor", ["file", "package", "service", "tcp_port",
                                      "sysctl"])
def test_no_module_accessor_is_hidden_by_what_init_sets(host, accessor):
    """An attribute set in Connection.__init__ would shadow an accessor of the same name."""
    assert callable(getattr(host, accessor))


@pytest.mark.parametrize("rc,busy", [(0, True), (1, False)])
def test_a_tcp_port_of_a_real_connection_is_busy_when_nc_reaches_it(
        sshd, host, rc, busy):
    sshd.answers = [("nc -zv localhost 8836", Reply(rc))]

    assert host.tcp_port(8836).is_busy is busy


def test_a_host_without_nc_cannot_tell_whether_a_port_is_busy(sshd, host):
    """Without nc, "not busy" would be a guess that the port is free."""
    sshd.answers = [("nc -zv localhost 8836", Reply(127, b"", b"bash: nc: command not found\n"))]

    with pytest.raises(Exception, match="cannot tell whether port 8836 is busy: nc exited 127"):
        host.tcp_port(8836).is_busy


def test_check_output_is_the_stdout_of_a_command_that_succeeded(sshd, host):
    sshd.answers = [("nproc", Reply(0, b"16\n"))]

    assert host.check_output("nproc") == "16\n"


def test_check_output_of_a_command_that_failed_raises_with_its_stderr(sshd, host):
    sshd.answers = [("nproc", Reply(127, err=b"bash: nproc: command not found\n"))]

    with pytest.raises(Exception, match="'nproc' returned non-zero exit code: "
                                        "bash: nproc: command not found"):
        host.check_output("nproc")


def test_a_file_equals_its_path():
    host = object()
    assert hostkit.modules.file.File("/etc/hosts", host) == "/etc/hosts"
    assert hostkit.modules.file.File("/etc/hosts", host) == \
        hostkit.modules.file.File("/etc/hosts", None)
    assert hostkit.modules.file.File("/etc/hosts", host) != "/etc/hostname"


# ---- SystemInfo ----

@pytest.mark.parametrize("answers,distribution,release,codename", [
    (linux(lsb=Reply(0, UBUNTU_LSB, b"No LSB modules are available.\n")),
     "ubuntu", "22.04", "jammy"),
    (linux(ALMA_8), "almalinux", "8.10", None),
    (linux(DEBIAN_12), "debian", "12", "bookworm"),
    (linux(CENTOS_7), "centos", "7", None),
    (linux(OL_8), "ol", "8.10", None),
    (linux(AMZN_2), "amzn", "2", None),
    (linux(NO_FILE, redhat=Reply(0, b"CentOS release 6.10 (Final)\n")),
     "CentOS", "6.10", None),
    (linux(NO_FILE, alpine=Reply(0, b"3.19.1\n")), "alpine", "3.19.1", None),
], ids=["ubuntu-22.04-lsb", "alma-8.10", "debian-12", "centos-7", "ol-8",
        "amzn-2", "centos-6-redhat-release", "alpine"])
def test_system_info_reads_the_distribution_the_way_the_vm_reports_it(
        sshd, host, answers, distribution, release, codename):
    sshd.answers = answers

    info = host.system_info

    assert info.type == "linux"
    assert info.distribution == distribution
    assert info.release == release
    assert info.codename == codename
    assert info.arch == "x86_64"


def test_system_info_asks_the_vm_once_per_connection(sshd, host):
    sshd.answers = linux(ALMA_8)

    host.system_info.distribution
    host.system_info.release
    host.package("tmux")
    host.package("glibc")

    assert sshd.ran.count("uname -s") == 1, (
        "system_info is a cached property: one probe per Connection")


def test_os_release_is_every_release_file_as_cat_prints_them_read_once(sshd, host):
    sshd.answers = [("cat /etc/*release", Reply(0, DEBIAN_12))]

    assert host.system_info.os_release == DEBIAN_12.decode()
    host.system_info.os_release

    assert sshd.ran.count("cat /etc/*release") == 1


def test_os_release_that_cannot_be_read_raises(sshd, host):
    sshd.answers = [("cat /etc/*release", Reply(1, err=b"cat: '/etc/*release': No such file\n"))]

    with pytest.raises(Exception, match="non-zero exit code"):
        host.system_info.os_release


def test_the_kernel_release_is_what_uname_r_printed(sshd, host):
    sshd.answers = [("uname -r", Reply(0, b"5.14.0-503.14.1.el9_5.x86_64\n"))]

    assert host.system_info.kernel_release == "5.14.0-503.14.1.el9_5.x86_64"


def test_the_hostname_is_what_hostname_printed(sshd, host):
    sshd.answers = [("hostname", Reply(0, b"web-1\n"))]

    assert host.system_info.hostname == "web-1"


def test_a_hostname_that_cannot_be_read_raises(sshd, host):
    sshd.answers = [("hostname", Reply(127, err=b"bash: hostname: command not found\n"))]

    with pytest.raises(Exception, match="'hostname' returned non-zero exit code: "
                                        "bash: hostname: command not found"):
        host.system_info.hostname


# ---- SELinux ----

@pytest.mark.parametrize("rc,err,enabled", [
    (0, b"", True),
    (1, b"", False),
    (127, b"bash: selinuxenabled: command not found\n", False),
], ids=["enabled", "disabled", "no-selinux-utils"])
def test_selinux_is_enabled_only_when_selinuxenabled_exits_0(
        sshd, host, rc, err, enabled):
    sshd.answers = [("selinuxenabled", Reply(rc, err=err))]

    assert host.selinux.enabled is enabled
    assert sshd.ran == ["selinuxenabled"]


def test_the_selinux_mode_is_what_getenforce_printed(sshd, host):
    sshd.answers = [("getenforce", Reply(0, b"Permissive\n"))]

    assert host.selinux.mode == "Permissive"
    assert sshd.ran == ["getenforce"]


def test_a_selinux_mode_that_cannot_be_read_raises(sshd, host):
    sshd.answers = [("getenforce", Reply(127, err=b"bash: getenforce: command not found\n"))]

    with pytest.raises(Exception, match="'getenforce' returned non-zero exit code"):
        host.selinux.mode


@pytest.mark.parametrize("mode,sent", [
    (1, "setenforce 1"), (0, "setenforce 0"), ("Permissive", "setenforce Permissive"),
    ("1; reboot", "setenforce '1; reboot'"),
], ids=["1", "0", "Permissive", "shell-syntax-stays-data"])
@pytest.mark.parametrize("rc", [0, 1])
def test_enforce_sends_setenforce_and_returns_its_exit_status(
        sshd, host, mode, sent, rc):
    sshd.answers = [("setenforce", Reply(rc, err=b"setenforce: SELinux is disabled\n"
                                         if rc else b""))]

    assert host.selinux.enforce(mode) == rc
    assert sshd.ran == [sent]


def test_a_checked_enforce_that_failed_raises_with_its_stderr(sshd, host):
    sshd.answers = [("setenforce", Reply(1, err=b"setenforce: SELinux is disabled\n"))]

    with pytest.raises(Exception, match="'setenforce 1' returned non-zero exit code: "
                                        "setenforce: SELinux is disabled"):
        host.selinux.enforce(1, check=True)


def test_a_checked_enforce_that_succeeded_returns_0(sshd, host):
    sshd.answers = [("setenforce", Reply(0))]

    assert host.selinux.enforce(1, check=True) == 0
    assert sshd.ran == ["setenforce 1"]


# ---- Sysctl ----

def test_a_sysctl_value_is_what_sysctl_n_printed(sshd, host):
    sshd.answers = [("sysctl -n", Reply(0, b"262144\n"))]

    assert host.sysctl("net.netfilter.nf_conntrack_max").value == "262144"
    assert sshd.ran == ["sysctl -n net.netfilter.nf_conntrack_max"]


def test_a_sysctl_the_kernel_has_not_raises_when_read(sshd, host):
    sshd.answers = [("sysctl -n", Reply(
        255, err=b"sysctl: cannot stat /proc/sys/net/nf_conntrack_max: "
                 b"No such file or directory\n"))]

    with pytest.raises(Exception, match="cannot stat /proc/sys/net/nf_conntrack_max"):
        host.sysctl("net.nf_conntrack_max").value


@pytest.mark.parametrize("name,value,sent", [
    ("net.netfilter.nf_conntrack_max", 1548576,
     "sysctl -w net.netfilter.nf_conntrack_max=1548576"),
    ("kernel.core_pattern", "/opt/corefiles/core.%e.%t.%p",
     "sysctl -w kernel.core_pattern=/opt/corefiles/core.%e.%t.%p"),
    ("net.ipv4.ip_local_port_range", "1024 65535",
     "sysctl -w net.ipv4.ip_local_port_range='1024 65535'"),
], ids=["number", "core-pattern", "two-words"])
@pytest.mark.parametrize("rc", [0, 255])
def test_set_sends_sysctl_w_and_returns_its_exit_status(
        sshd, host, name, value, sent, rc):
    """A value is sent as written; only one with blanks is quoted."""
    sshd.answers = [("sysctl -w", Reply(rc))]

    assert host.sysctl(name).set(value) == rc
    assert sshd.ran == [sent]


def test_a_checked_set_that_failed_raises_with_its_stderr(sshd, host):
    sshd.answers = [("sysctl -w", Reply(
        255, err=b'sysctl: permission denied on key "kernel.io_uring_disabled"\n'))]

    with pytest.raises(Exception, match="permission denied on key"):
        host.sysctl("kernel.io_uring_disabled").set(1, check=True)


def test_a_checked_set_that_succeeded_returns_0(sshd, host):
    sshd.answers = [("sysctl -w", Reply(0, b"kernel.io_uring_disabled = 1\n"))]

    assert host.sysctl("kernel.io_uring_disabled").set(1, check=True) == 0
    assert sshd.ran == ["sysctl -w kernel.io_uring_disabled=1"]


# ---- Service ----

@pytest.mark.parametrize("printed,rc,running", [
    (b"active\n", 0, True),
    (b"activating\n", 3, False),
    (b"inactive\n", 3, False),
    (b"failed\n", 3, False),
])
def test_a_unit_runs_only_when_systemctl_says_active(sshd, host, printed, rc, running):
    sshd.answers = [("systemctl is-active myapp.socket", Reply(rc, printed))]

    service = host.service("myapp.socket")

    assert service.state == printed.decode().strip()
    assert service.is_running is running


# ---- Package ----

@pytest.mark.parametrize("os_release,expected", [
    (DEBIAN_12, DebianPackage),
    (ALMA_8, RpmPackage),
    (CENTOS_7, RpmPackage),
    (OL_8, RpmPackage),
    (AMZN_2, RpmPackage),
    (RHEL_9, RpmPackage),
], ids=["debian", "almalinux", "centos", "oracle", "amazon", "rhel"])
def test_the_package_backend_follows_the_distribution(
        sshd, host, os_release, expected):
    """rhel and amzn are not in the distro list: they reach rpm through `which`."""
    sshd.answers = linux(os_release) + [
        ("which apk", Reply(1)), ("which dpkg-query", Reply(1)),
        ("which rpm", Reply(0, b"/usr/bin/rpm\n"))]

    assert type(host.package("tmux")) is expected


def test_ubuntu_through_lsb_release_uses_dpkg(sshd, host):
    sshd.answers = linux(lsb=Reply(0, UBUNTU_LSB))

    assert type(host.package("tmux")) is DebianPackage


def test_a_vm_with_no_known_package_tool_is_refused(sshd, host):
    sshd.answers = linux(b'ID="gentoo"\nVERSION_ID="2.15"\n') + [
        ("which", Reply(1))]

    with pytest.raises(NotImplementedError):
        host.package("tmux")


@pytest.mark.parametrize("printed,rc,installed", [
    (b"install ok installed", 0, True),
    (b"hold ok installed", 0, True),
    (b"deinstall ok config-files", 0, False),
    (b"unknown ok not-installed", 0, False),
    (b"", 1, False),
], ids=["installed", "held", "removed-config-left", "unknown", "never-heard-of"])
def test_dpkg_query_status_decides_whether_a_deb_is_installed(
        sshd, host, printed, rc, installed):
    sshd.answers = linux(DEBIAN_12) + [(
        "dpkg-query", Reply(rc, printed,
                            b"" if rc == 0 else
                            b"dpkg-query: no packages found matching tmux\n"))]

    assert host.package("tmux").is_installed is installed


def test_a_deb_version_is_the_version_field_of_dpkg_query(sshd, host):
    sshd.answers = linux(DEBIAN_12) + [
        ("dpkg-query", Reply(0, b"install ok installed 3.3a-3"))]

    assert host.package("tmux").version == "3.3a-3"


def test_asking_the_version_of_a_removed_deb_fails_loudly(sshd, host):
    sshd.answers = linux(DEBIAN_12) + [
        ("dpkg-query", Reply(0, b"deinstall ok config-files 3.3a-3"))]

    with pytest.raises(AssertionError, match="not installed"):
        host.package("tmux").version


@pytest.mark.parametrize("rc,printed,installed", [
    (0, b"tmux-2.7-3.el8.x86_64\n", True),
    (1, b"package tmux is not installed\n", False),
])
def test_rpm_q_exit_status_decides_whether_an_rpm_is_installed(
        sshd, host, rc, printed, installed):
    sshd.answers = linux(ALMA_8) + [("rpm -q", Reply(rc, printed))]

    assert host.package("tmux").is_installed is installed


def test_an_rpm_version_and_release_are_the_queryformat_fields(sshd, host):
    sshd.answers = linux(ALMA_8) + [
        ("%{VERSION}", Reply(0, b"2.28")),
        ("%{RELEASE}", Reply(0, b"251.el8_10.27"))]

    glibc = host.package("glibc")

    assert (glibc.version, glibc.release) == ("2.28", "251.el8_10.27")


@pytest.mark.parametrize("os_release,sent", [
    (ALMA_8, "yum remove -y nscd"),
    (DEBIAN_12, "DEBIAN_FRONTEND=noninteractive apt-get remove -y nscd"),
], ids=["rpm", "deb"])
@pytest.mark.parametrize("rc", [0, 1])
def test_remove_sends_the_familys_manager_and_returns_its_exit_status(
        sshd, host, os_release, sent, rc):
    sshd.answers = linux(os_release) + [("remove", Reply(rc))]

    assert host.package("nscd").remove() == rc
    assert [c for c in sshd.ran if "remove" in c] == [sent]


def test_a_checked_remove_that_failed_raises_with_its_stderr(sshd, host):
    sshd.answers = linux(ALMA_8) + [("yum remove", Reply(
        1, err=b"Error: No packages marked for removal.\n"))]

    with pytest.raises(Exception, match="'yum remove -y nscd' returned non-zero "
                                        "exit code: Error: No packages marked"):
        host.package("nscd").remove(check=True)


def test_a_checked_remove_that_succeeded_returns_0(sshd, host):
    sshd.answers = linux(ALMA_8) + [("yum remove", Reply(0))]

    assert host.package("nscd").remove(check=True) == 0


# ---- Process ----

@pytest.fixture
def ps(sshd):
    table = Ps(list(OPENSSL_VM))
    sshd.answers = linux(ALMA_8) + [("ps ", table)]
    return table


def test_the_root_processes_of_a_name_are_every_one_of_them(ps, host):
    processes = host.process.filter(user="root", comm="openssl")
    assert [int(p.pid) for p in processes] == [2345, 2346], (
        "the non-root s_client must not be counted among the servers")


def test_a_single_daemon_is_got_by_its_name(ps, host):
    assert host.process.get(user="root", comm="myapp-server").pid == 900


def test_get_refuses_an_ambiguous_name(ps, host):
    with pytest.raises(RuntimeError, match="Multiple process found"):
        host.process.get(user="root", comm="openssl")


def test_get_of_a_process_that_is_not_running_raises(ps, host):
    with pytest.raises(RuntimeError, match="No process found"):
        host.process.get(user="root", comm="mysqld")


def test_a_process_row_is_parsed_into_typed_fields(ps, host):
    server = host.process.get(pid=2345)

    assert server.pid == 2345 and isinstance(server.pid, int)
    assert server.pcpu == 12.5
    assert server.user == "root"
    assert server.comm == "openssl"
    assert server.lstart == "Thu Oct 1 09:20:01 2026", (
        "the start time, whitespace-normalised: it is compared, not printed")
    assert server.args == ("openssl s_server -accept 4433 -www -cert "
                           "/root/cert.pem"), "args keeps its spaces"


def test_a_filter_on_a_column_not_listed_by_default_is_asked_for(ps, host):
    children = host.process.filter(ppid=812)

    assert sorted(p.pid for p in children) == [2345, 2346]


def test_an_attribute_not_fetched_up_front_is_read_for_that_pid(ps, host):
    server = host.process.get(pid=2345)
    ps.procs = [dict(p, rss=7340) if p["pid"] == 2345 else p for p in ps.procs]

    assert server.rss == 7340


def test_a_pid_reused_by_another_process_is_detected(ps, host):
    server = host.process.get(pid=2345)
    ps.procs = [proc(2345, "bash", "bash -l", lstart="Thu Oct  1 11:02:13 2026")]

    with pytest.raises(RuntimeError, match="start time changed"):
        server.rss


@pytest.mark.parametrize("pid,running", [(900, True), (4242, False)])
def test_is_running_is_the_exit_status_of_ps_p(ps, host, pid, running):
    assert host.process.is_running(pid) is running


@pytest.mark.parametrize("pattern,full,command,reply,pids", [
    ("myapp-server", False, "pgrep myapp-server", Reply(0, b"900\n"), [900]),
    ("openssl s_server", True, "pgrep -f 'openssl s_server'",
     Reply(0, b"2345\n2346\n"), [2345, 2346]),
    ("mysqld", False, "pgrep mysqld", Reply(1), []),
], ids=["one", "whole-command-line", "none"])
def test_pgrep_is_the_pids_it_printed(sshd, host, pattern, full, command, reply, pids):
    sshd.answers = [(command, reply)] + linux(ALMA_8)

    assert host.process.pgrep(pattern, full=full) == pids
    assert [c for c in sshd.ran if c not in PROBES] == [command]


@pytest.mark.parametrize("reply", [
    Reply(2, err=b"pgrep: cannot compile regular expression '[': "
                 b"Unmatched [, [^, [:, [., or [=\n"),
    Reply(127, err=b"bash: line 1: pgrep: command not found\n"),
], ids=["bad-regex", "no-pgrep"])
def test_a_pgrep_that_could_not_look_raises_rather_than_find_nothing(
        sshd, host, reply):
    """An empty list is "nothing runs": a check that something is gone would pass."""
    sshd.answers = [("pgrep", reply)] + linux(ALMA_8)

    with pytest.raises(Exception, match=re.escape(reply.err.decode().strip())):
        host.process.pgrep("[")


def test_a_string_holding_two_pids_is_refused_before_anything_is_sent(sshd, host):
    """Sent as is, the newline would run the second pid as a command."""
    sshd.answers = linux(ALMA_8)

    with pytest.raises(ValueError):
        host.process.kill("2345\n2346")
    assert [c for c in sshd.ran if c not in PROBES] == []


@pytest.mark.parametrize("flavour", [PosixProcess, BusyboxProcess])
def test_busybox_and_procps_hosts_find_and_signal_alike(sshd, host, flavour):
    sshd.answers = [("pgrep", Reply(0, b"812\n"))]
    process = flavour(host)

    assert process.pgrep("sshd -D", full=True) == [812]
    assert process.pkill("sshd -D", signal="HUP", full=True) == 0
    assert process.kill(812, signal="HUP") == 0
    assert sshd.ran == ["pgrep -f 'sshd -D'", "pkill -HUP -f 'sshd -D'",
                        "kill -HUP 812"]


# ---- Docker ----

CENTOS7_IMAGE = "registry.example.com:5000/qa/centos7:fixed-repos"


@pytest.mark.parametrize("reply,present", [
    (Reply(0, b"sha256:5d0da3dc976460b72c77d94c8a1ad043720b0416bfc16c52c45d4847e53fadb6\n"),
     True),
    (Reply(1, err=b"Error: No such image: centos:centos7\n"), False),
], ids=["there", "missing"])
def test_an_image_is_there_when_docker_inspects_it_by_its_exact_name(
        sshd, host, reply, present):
    """Only the id is printed, not the whole inspect JSON."""
    sshd.answers = [("docker image inspect", reply)]

    assert host.docker.has_image("centos:centos7") is present
    assert sshd.ran == ["docker image inspect -f '{{.Id}}' centos:centos7"]


def test_a_pull_the_vm_refused_raises_with_dockers_stderr(sshd, host):
    sshd.answers = [("docker pull", Reply(1, err=b"Error response from daemon: "
                                                 b"manifest unknown\n"))]
    docker = host.docker

    with pytest.raises(Exception, match="non-zero exit code: Error response "
                                        "from daemon: manifest unknown"):
        docker.pull(CENTOS7_IMAGE, check=True)
    assert docker.pull(CENTOS7_IMAGE) == 1


# ---- Actions: one command each; its exit status, or with check=True an exception ----

# (action, the command it sends, a failure as the real tool reports it)
ACTIONS = [
    pytest.param(
        lambda h, **kw: h.service("myapp.service").start(**kw),
        "systemctl start myapp.service",
        Reply(5, err=b"Failed to start myapp.service: Unit myapp.service "
                     b"not found.\n"),
        id="service-start"),
    pytest.param(
        lambda h, **kw: h.service("myapp").stop(**kw),
        "systemctl stop myapp",
        Reply(5, err=b"Failed to stop myapp.service: Unit myapp.service "
                     b"not loaded.\n"),
        id="service-stop"),
    pytest.param(
        lambda h, **kw: h.service("myapp").restart(**kw),
        "systemctl restart myapp",
        Reply(1, err=b"Job for myapp.service failed because the control "
                     b"process exited with error code.\n"),
        id="service-restart"),
    pytest.param(
        lambda h, **kw: h.service("myapp-worker.service").reset_failed(**kw),
        "systemctl reset-failed myapp-worker.service",
        Reply(1, err=b"Failed to reset failed state of unit "
                     b"myapp-worker.service: Unit myapp-worker.service not "
                     b"loaded.\n"),
        id="service-reset-failed"),
    pytest.param(
        lambda h, **kw: h.systemd.daemon_reload(**kw),
        "systemctl daemon-reload",
        Reply(1, err=b"Failed to reload daemon: Access denied\n"),
        id="systemd-daemon-reload"),
    pytest.param(
        lambda h, **kw: h.process.pkill("qemu", **kw),
        "pkill qemu", Reply(1), id="pkill"),
    pytest.param(
        lambda h, **kw: h.process.pkill("avocado", signal="9", **kw),
        "pkill -9 avocado", Reply(1), id="pkill-signal"),
    pytest.param(
        lambda h, **kw: h.process.pkill("openssl s_server", full=True, **kw),
        "pkill -f 'openssl s_server'", Reply(1), id="pkill-full"),
    pytest.param(
        lambda h, **kw: h.process.pkill("myapp-server", signal="KILL",
                                        full=True, **kw),
        "pkill -KILL -f myapp-server", Reply(1), id="pkill-signal-full"),
    pytest.param(
        lambda h, **kw: h.process.kill(1053, **kw),
        "kill 1053",
        Reply(1, err=b"bash: line 1: kill: (1053) - No such process\n"),
        id="kill"),
    pytest.param(
        lambda h, **kw: h.process.kill("1053", signal="QUIT", **kw),
        "kill -QUIT 1053",
        Reply(1, err=b"bash: line 1: kill: (1053) - No such process\n"),
        id="kill-signal"),
    pytest.param(
        lambda h, **kw: h.process.kill([2345, "2346"], signal="KILL", **kw),
        "kill -KILL 2345 2346",
        Reply(1, err=b"bash: line 1: kill: (2346) - No such process\n"),
        id="kill-several"),
    pytest.param(
        lambda h, **kw: h.docker.pull(CENTOS7_IMAGE, **kw),
        "docker pull " + CENTOS7_IMAGE,
        Reply(1, err=b"Error response from daemon: manifest for "
                     + CENTOS7_IMAGE.encode() + b" not found: manifest "
                     b"unknown: manifest unknown\n"),
        id="docker-pull"),
    pytest.param(
        lambda h, **kw: h.docker.tag(CENTOS7_IMAGE, "centos:centos7", **kw),
        "docker tag " + CENTOS7_IMAGE + " centos:centos7",
        Reply(1, err=b"Error response from daemon: No such image: "
                     + CENTOS7_IMAGE.encode() + b"\n"),
        id="docker-tag"),
    pytest.param(
        lambda h, **kw: h.docker.remove_image("centos:centos7", **kw),
        "docker rmi -f centos:centos7",
        Reply(1, err=b"Error response from daemon: conflict: unable to delete "
                     b"5d0da3dc9764 (cannot be forced) - image is being used "
                     b"by running container 3f2a9c1b7e04\n"),
        id="docker-rmi"),
    pytest.param(
        lambda h, **kw: h.docker.start("centos7-coverage", **kw),
        "docker start centos7-coverage",
        Reply(1, err=b"Error response from daemon: No such container: "
                     b"centos7-coverage\n"),
        id="docker-start"),
    pytest.param(
        lambda h, **kw: h.docker.remove_containers(**kw),
        "docker ps -aq | xargs -r docker rm -f",
        Reply(123, err=b"Error response from daemon: removal of container "
                       b"3f2a9c1b7e04 is already in progress\n"),
        id="docker-remove-containers"),
    pytest.param(
        lambda h, **kw: h.docker.remove_containers("^test_", **kw),
        "docker ps -aq --filter 'name=^test_' | xargs -r docker rm -f",
        Reply(123, err=b"Error response from daemon: removal of container "
                       b"3f2a9c1b7e04 is already in progress\n"),
        id="docker-remove-containers-filter"),
]


@pytest.mark.parametrize("action,command,failure", ACTIONS)
def test_an_action_sends_exactly_its_command_and_is_0_when_it_worked(
        sshd, host, action, command, failure):
    sshd.answers = [(command, Reply(0))] + linux(ALMA_8)

    assert action(host) == 0
    assert action(host, check=True) == 0
    assert [c for c in sshd.ran if c not in PROBES] == [command, command]


@pytest.mark.parametrize("action,command,failure", ACTIONS)
def test_an_action_that_failed_is_its_exit_status_not_an_exception(
        sshd, host, action, command, failure):
    sshd.answers = [(command, failure)] + linux(ALMA_8)

    assert action(host) == failure.rc


@pytest.mark.parametrize("action,command,failure", ACTIONS)
def test_a_checked_action_that_failed_raises_with_the_tools_stderr(
        sshd, host, action, command, failure):
    sshd.answers = [(command, failure)] + linux(ALMA_8)

    with pytest.raises(Exception, match="non-zero exit code: " + re.escape(
            failure.err.decode())):
        action(host, check=True)


# ---- CommandResult ----

def test_a_command_result_joins_its_streams_and_knows_whether_it_failed():
    passed = CommandResult("paramiko", 0, "rpm -q tmux", "tmux-2.7\n", "")
    failed = CommandResult("paramiko", 1, "rpm -q vim", "", "not installed\n")

    assert passed.output == "tmux-2.7\n\n"
    assert (passed.succeeded, passed.failed, passed.exit_status) == (True, False, 0)
    assert (failed.succeeded, failed.failed, failed.exit_status) == (False, True, 1)
    assert "not installed" in failed.output


def test_the_printed_result_shows_the_command_rc_and_both_streams():
    text = repr(CommandResult("paramiko", 2, "make src", "cc ...\n", "Error 1\n",
                              exec_time="2026-10-01 09:20:01"))

    assert "command = 'make src'" in text and "rc = 2" in text
    assert "STDOUT:\ncc ..." in text and "STDERR:\nError 1" in text
    assert "2026-10-01 09:20:01" in text
    assert "container=" not in text
    assert "container='3f2a'" in repr(
        CommandResult("docker", 0, "ls", "", "", container_id="3f2a"))


# ---- logging_wrap ----

def _record(level, msg="myappctl --update finished"):
    return logging.LogRecord("hostkit", level, "backends.py", 42, msg, None, None)


@pytest.mark.parametrize("level,color", [
    (logging.ERROR, CustomFormatter.red),
    (logging.WARNING, CustomFormatter.orange),
    (logging.DEBUG, CustomFormatter.purple),
])
def test_each_level_is_printed_in_its_own_color(level, color):
    line = CustomFormatter().format(_record(level))

    assert line.startswith(color) and line.endswith(CustomFormatter.reset)
    assert "myappctl --update finished" in line
    assert "(backends.py:42)" in line


def test_log_color_recolors_a_level_only_inside_the_block():
    record = _record(logging.INFO)
    before = CustomFormatter().format(record)

    with log_color(logging.INFO, "green"):
        inside = CustomFormatter().format(record)

    assert inside.startswith(CustomFormatter.green)
    assert CustomFormatter().format(record) == before


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_the_colored_handler_writes_formatted_lines_to_the_chosen_stream(
        monkeypatch, stream):
    sink = io.StringIO()
    monkeypatch.setattr("sys." + stream, sink)
    name = "hostkit-test-" + stream
    set_colored_handler(name, stream=stream)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    try:
        logger.error("SSH session is lost")
    finally:
        logger.handlers.clear()

    assert CustomFormatter.red in sink.getvalue()
    assert "SSH session is lost" in sink.getvalue()


def test_importing_hostkit_writes_nothing_until_the_application_asks():
    probe = ("import logging, hostkit; "
             "logging.getLogger('hostkit.backends').warning('should stay silent'); "
             "print([type(h).__name__ for h in logging.getLogger('hostkit').handlers])")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "['NullHandler']"
    assert "should stay silent" not in done.stderr
