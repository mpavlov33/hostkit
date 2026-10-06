"""hostkit's modules (File, Port, Package, Process, Docker, Sysctl, SELinux, ...) on a real shell.

The double is hostkit.testing.Box: this machine's sh, with stand-ins on PATH that record their argv.
"""
import os

import pytest

from hostkit.modules.package import DebianPackage, RpmPackage
from hostkit.modules.process import PosixProcess



# A stand-in body that keeps its arguments one per line, where `$*` would join them.
ONE_ARG_PER_LINE = "printf '%s\\n' \"$@\" > \"$BOX/argv\""


def argv(box):
    return (box.root / "argv").read_text(encoding="utf-8").splitlines()


def test_a_path_exists_or_not_without_raising(box):
    (box.home / "repo.zip").write_text("zip", encoding="utf-8")

    assert box.file((box.home / "repo.zip").as_posix()).exists
    assert not box.file((box.home / "no-such-dir").as_posix()).exists


def test_a_path_goes_to_the_shell_unquoted_so_a_glob_removes_every_match(box):
    for name in ("glibc.rpm", "openssl.rpm", "myapp.deb"):
        (box.home / name).write_text(name, encoding="utf-8")

    assert box.file(box.home.as_posix() + "/*.rpm").remove() == 0

    assert [p.name for p in box.home.iterdir()] == ["myapp.deb"]


def test_a_glob_that_matches_nothing_removes_nothing_and_is_no_error(box):
    (box.home / "myapp.deb").write_text("deb", encoding="utf-8")

    assert box.file(box.home.as_posix() + "/*.rpm").remove() == 0

    assert [p.name for p in box.home.iterdir()] == ["myapp.deb"]


def test_a_remove_that_failed_is_its_exit_status_not_an_exception(box):
    logs = (box.home / "logs").as_posix()
    box.stub("rm", "echo \"rm: cannot remove '$2': Permission denied\" >&2; exit 1")

    assert box.file(logs).remove() == 1
    assert box.calls == [("rm", "-rf " + logs)]


def test_mkdir_makes_the_missing_parents_and_keeps_what_is_there(box):
    logs = box.home / "data" / "logs"

    assert box.file(logs.as_posix()).mkdir() == 0
    (logs / "build.log").write_text("make\n", encoding="utf-8")
    assert box.file(logs.as_posix()).mkdir() == 0

    assert [p.name for p in logs.iterdir()] == ["build.log"]


def test_a_mkdir_that_failed_is_its_exit_status_not_an_exception(box):
    (box.home / "data").write_text("a file, not a directory", encoding="utf-8")

    assert box.file((box.home / "data").as_posix()).mkdir() == 1


def test_a_checked_remove_that_failed_raises_with_its_stderr(box):
    logs = (box.home / "logs").as_posix()
    box.stub("rm", "echo \"rm: cannot remove '$2': Permission denied\" >&2; exit 1")

    with pytest.raises(Exception, match="non-zero exit code: rm: cannot remove .*Permission denied"):
        box.file(logs).remove(check=True)


def test_a_checked_mkdir_that_failed_raises_with_its_stderr(box):
    (box.home / "data").write_text("a file, not a directory", encoding="utf-8")

    with pytest.raises(Exception, match="non-zero exit code: mkdir: cannot create directory"):
        box.file((box.home / "data").as_posix()).mkdir(check=True)


@pytest.mark.parametrize("content", [
    "AUTO_UPDATE=NO\n",
    "SERVER_URL=http://192.0.2.20:8801",
    "it's $HOME `id` $(id) \\n %s 100% \"x\"\n\n",
    "-n reads like a printf option\n",
    "",
], ids=["a-line", "no-newline", "shell-and-printf-specials", "dash", "empty"])
def test_write_puts_exactly_the_content_and_adds_no_newline(box, content):
    conf = box.home / "myapp.conf"
    conf.write_bytes(b"what was there before\n")

    assert box.file(conf.as_posix()).write(content) == 0

    assert conf.read_bytes() == content.encode()


def test_an_appended_write_goes_on_after_the_last_byte(box):
    dhcpd = box.home / "dhcpd"
    dhcpd.write_bytes(b"# dhcpd options\n")
    dhcpd_file = box.file(dhcpd.as_posix())

    dhcpd_file.write('DHCPDARGS="br0"', append=True)
    dhcpd_file.write(" eth1\n", append=True)

    assert dhcpd.read_bytes() == b'# dhcpd options\nDHCPDARGS="br0" eth1\n'


def test_a_checked_write_into_a_missing_directory_raises(box):
    # bash and dash word the error differently; both name the path.
    with pytest.raises(Exception, match="non-zero exit code: .*no-dir/myapp.conf"):
        box.file((box.home / "no-dir" / "myapp.conf").as_posix()).write("x\n", check=True)


def test_add_line_appends_a_line_once_and_makes_a_missing_file(box):
    networks = box.home / "networks"
    networks_file = box.file(networks.as_posix())

    assert networks_file.add_line("loopback 127.0.0.0") == 0
    assert networks_file.add_line("loopback 127.0.0.0") == 0

    assert networks.read_bytes() == b"loopback 127.0.0.0\n"


@pytest.mark.parametrize("there,line", [
    ("loopback 127.0.0.0 # local", "loopback 127.0.0.0"),
    ("/tmp/packages/glibc-debuginfo-2.28-x86_64.rpm",
     "/tmp/packages/glibc-debuginfo-2.28.x86_64.rpm"),
], ids=["only-a-prefix-there", "a-regex-would-match"])
def test_add_line_looks_for_the_whole_line_literally(box, there, line):
    listing = box.home / "listing"
    listing.write_bytes((there + "\n").encode())

    box.file(listing.as_posix()).add_line(line)

    assert listing.read_bytes() == (there + "\n" + line + "\n").encode()


@pytest.mark.parametrize("line", [
    "-q --auto-update", "it's $HOME `id`", "100% \\n done",
], ids=["dash", "shell-specials", "printf-specials"])
def test_add_line_takes_any_line_as_data(box, line):
    conf = box.home / "httpd.conf"
    conf.write_bytes(b"Listen 80\n")
    conf_file = box.file(conf.as_posix())

    conf_file.add_line(line)
    conf_file.add_line(line)

    assert conf.read_bytes() == ("Listen 80\n" + line + "\n").encode()


@pytest.mark.parametrize("text,found", [
    (b"0 */4 * * * root /usr/bin/myappctl -q --auto-update\n", True),
    (b"0 */4 * * * root /usr/bin/myappctl --auto-update\n", False),
], ids=["the-line", "another-line-of-root"])
def test_contains_looks_for_the_whole_pattern(box, text, found):
    cron = box.home / "myapp-cron"
    cron.write_bytes(text)

    assert box.file(cron.as_posix()).contains(
        "root /usr/bin/myappctl -q --auto-update") is found


def test_count_is_the_number_of_matching_lines_and_0_without_a_file(box):
    log = box.home / "server.log"
    log.write_bytes(b"STARTING SERVER\nready\nSTARTING SERVER pid 2\n")

    assert box.file(log.as_posix()).count("STARTING SERVER") == 2
    assert box.file(log.as_posix()).count("-- stopped") == 0
    assert box.file((box.home / "missing.log").as_posix()).count("STARTING SERVER") == 0


def test_tail_is_the_last_lines_and_empty_without_a_file(box):
    log = box.home / "build.log"
    log.write_bytes("".join("line {0}\n".format(n) for n in range(1, 31)).encode())

    assert box.file(log.as_posix()).tail(3) == "line 28\nline 29\nline 30\n"
    assert box.file((box.home / "missing.log").as_posix()).tail(3) == ""


def test_find_names_are_globs_for_find_not_for_the_shell(box):
    """A file in the cwd matching an unquoted glob would replace it before find runs."""
    share = box.home / "hadoop" / "share"
    share.mkdir(parents=True)
    jar = share / "hadoop-mapreduce-client-jobclient-3.3.6-tests.jar"
    jar.write_text("jar", encoding="utf-8")
    (share / "hadoop-mapreduce-client-jobclient-3.3.6.jar").write_text("jar", encoding="utf-8")
    (box.home / "old-jobclient-tests.jar").write_text("in the cwd", encoding="utf-8")

    assert box.file((box.home / "hadoop").as_posix()).find(
        "*jobclient*tests.jar", file_type="f") == [jar.as_posix()]


def test_find_takes_either_name_and_the_type_applies_to_both(box):
    lib = box.home / "glibc" / "lib64"
    lib.mkdir(parents=True)
    for name in ("libc.so.6", "libc-2.28.so", "libm.so.6"):
        (lib / name).write_text("elf", encoding="utf-8")
    (box.home / "glibc" / "debug" / "libc.so.6").mkdir(parents=True)

    found = box.file((box.home / "glibc").as_posix()).find(
        "libc.so.6", "libc-*.so", file_type="f")

    assert sorted(found) == [(lib / "libc-2.28.so").as_posix(), (lib / "libc.so.6").as_posix()]


def test_find_under_a_missing_directory_is_no_path(box):
    assert box.file((box.home / "hadoop").as_posix()).find("*.jar") == []


def test_copy_of_a_glob_copies_every_match_and_recursive_takes_directories(box):
    src = box.home / "clone_repo_ssh"
    (src / "keys").mkdir(parents=True)
    (src / "config").write_text("Host git.example.com", encoding="utf-8")
    (src / "keys" / "id_rsa").write_text("key", encoding="utf-8")
    ssh = box.home / ".ssh"
    ssh.mkdir()

    assert box.file(src.as_posix() + "/*").copy(ssh.as_posix() + "/", recursive=True) == 0

    assert (ssh / "config").read_text(encoding="utf-8") == "Host git.example.com"
    assert (ssh / "keys" / "id_rsa").read_text(encoding="utf-8") == "key"


def test_copy_without_recursive_skips_a_directory_and_fails(box):
    src = box.home / "clone_repo_ssh"
    (src / "keys").mkdir(parents=True)
    (src / "config").write_text("Host git.example.com", encoding="utf-8")
    ssh = box.home / ".ssh"
    ssh.mkdir()
    src_file = box.file(src.as_posix() + "/*")

    assert src_file.copy(ssh.as_posix() + "/") == 1
    with pytest.raises(Exception, match="non-zero exit code: cp: -r not specified"):
        src_file.copy(ssh.as_posix() + "/", check=True)

    assert [p.name for p in ssh.iterdir()] == ["config"]


def test_copy_preserve_keeps_the_times(box):
    sshd = box.home / "sshd"
    sshd.write_text("elf", encoding="utf-8")
    os.utime(sshd, (1000000000, 1000000000))

    box.file(sshd.as_posix()).copy((box.home / "sshd-kept").as_posix(), preserve=True)
    box.file(sshd.as_posix()).copy((box.home / "sshd-new").as_posix())

    assert (box.home / "sshd-kept").stat().st_mtime == 1000000000
    assert (box.home / "sshd-new").stat().st_mtime != 1000000000


def test_move_of_a_glob_moves_every_match_into_the_directory(box):
    data = box.home / "_data"
    data.mkdir()
    for name in ("out-1.log", "out-2.log", "test-1.log"):
        (data / name).write_text(name, encoding="utf-8")
    volume = box.home / "build_volume"
    volume.mkdir()

    assert box.file(data.as_posix() + "/out*").move(volume.as_posix()) == 0

    assert sorted(p.name for p in volume.iterdir()) == ["out-1.log", "out-2.log"]
    assert [p.name for p in data.iterdir()] == ["test-1.log"]


def test_move_force_replaces_the_destination(box):
    ssh = box.home / ".ssh"
    ssh.mkdir()
    (ssh / "repo_private_key_rsa_format").write_text("new", encoding="utf-8")
    (ssh / "repo_private_key").write_text("old", encoding="utf-8")

    assert box.file((ssh / "repo_private_key_rsa_format").as_posix()).move(
        (ssh / "repo_private_key").as_posix(), force=True) == 0

    assert [p.name for p in ssh.iterdir()] == ["repo_private_key"]
    assert (ssh / "repo_private_key").read_text(encoding="utf-8") == "new"


def test_a_checked_move_of_a_glob_that_matches_nothing_raises(box):
    (box.home / "_data").mkdir()
    volume = box.home / "corefiles"
    volume.mkdir()
    cores = box.file((box.home / "_data").as_posix() + "/core*")

    assert cores.move(volume.as_posix()) == 1
    with pytest.raises(Exception, match="non-zero exit code: mv: cannot stat"):
        cores.move(volume.as_posix(), check=True)


def test_truncate_of_a_glob_empties_every_match_and_keeps_the_files(box):
    logs = box.home / "log"
    logs.mkdir()
    for name in ("postgresql-Mon.log", "postgresql-Tue.log"):
        (logs / name).write_text("ERROR: deadlock detected\n", encoding="utf-8")

    assert box.file(logs.as_posix() + "/*").truncate() == 0

    assert {p.name: p.read_bytes() for p in logs.iterdir()} == {
        "postgresql-Mon.log": b"", "postgresql-Tue.log": b""}


def test_touch_makes_an_empty_file_and_keeps_what_is_in_one(box):
    stop = box.home / "wl.stop"
    ready = box.home / "wl.ready"
    ready.write_bytes(b"1\n")

    assert box.file(stop.as_posix()).touch() == 0
    assert box.file(ready.as_posix()).touch() == 0

    assert (stop.read_bytes(), ready.read_bytes()) == (b"", b"1\n")


def test_a_checked_touch_in_a_missing_directory_raises(box):
    stop = box.file((box.home / "no-dir" / "wl.stop").as_posix())

    assert stop.touch() == 1
    with pytest.raises(Exception, match="non-zero exit code: touch: cannot touch"):
        stop.touch(check=True)


def test_a_checked_chmod_of_a_missing_file_raises(box):
    key = box.file((box.home / ".ssh" / "repo_private_key").as_posix())

    assert key.chmod("600") == 1
    with pytest.raises(Exception, match="non-zero exit code: chmod: cannot access"):
        key.chmod("600", check=True)


@pytest.mark.parametrize("nc_exit, busy", [(0, True), (1, False)])
def test_a_port_is_busy_when_something_answers_on_it(box, nc_exit, busy):
    box.stub("nc", "exit {0}".format(nc_exit))

    assert box.tcp_port(8836).is_busy is busy
    assert box.calls == [("nc", "-zv localhost 8836")]


def test_a_program_exists_when_it_is_on_path(box):
    box.stub("which", 'case "$1" in ansible-playbook) exit 0;; esac; exit 1')

    assert box.exists("ansible-playbook")
    assert not box.exists("tmux")


@pytest.mark.parametrize("package, expected", [
    (RpmPackage, [("yum", "install -y tmux")]),
    (DebianPackage, [("apt-get", "update"), ("apt-get", "install -y tmux")]),
])
def test_a_missing_package_is_installed_with_the_familys_manager(
        box, package, expected):
    box.stub("which", "exit 1")
    box.stub("yum")
    box.stub("apt-get")

    package(name="tmux", host=box).install()

    assert [call for call in box.calls if call[0] != "which"] == expected


def test_an_installed_package_is_not_installed_again(box):
    box.stub("which", "exit 0")
    box.stub("yum")

    RpmPackage(name="tmux", host=box).install()

    assert [name for name, _ in box.calls] == ["which"]


def test_a_package_its_manager_could_not_install_raises(box):
    box.stub("which", "exit 1")
    box.stub("yum", "echo 'No package tmux available.' >&2; exit 1")

    with pytest.raises(Exception, match="non-zero exit code: No package tmux available"):
        RpmPackage(name="tmux", host=box).install()


def test_a_pattern_reaches_pgrep_and_pkill_as_one_argument_and_never_runs(box):
    pattern = "openssl s_server -accept 4433 $(touch ran); touch ran | 'x'"
    box.stub("pgrep", ONE_ARG_PER_LINE + "\nexit 1")
    box.stub("pkill", ONE_ARG_PER_LINE + "\nexit 1")
    process = PosixProcess(box)

    assert process.pgrep(pattern, full=True) == []
    assert argv(box) == ["-f", pattern]
    assert process.pkill(pattern, signal="KILL", full=True) == 1
    assert argv(box) == ["-KILL", "-f", pattern]
    assert not (box.home / "ran").exists()


@pytest.mark.parametrize("docker_exit, present", [(0, True), (1, False)])
def test_an_image_is_inspected_for_its_id_alone(box, docker_exit, present):
    """Printing the id alone keeps docker's full JSON out of the output."""
    box.stub("docker", ONE_ARG_PER_LINE + "\nexit {0}".format(docker_exit))

    assert box.docker.has_image("centos:centos7") is present
    assert argv(box) == ["image", "inspect", "-f", "{{.Id}}", "centos:centos7"]


def docker_listing(containers, rm="exit 0"):
    """A docker stand-in: `ps` lists `containers`; `rm` without a name fails, as docker's does."""
    return (
        'case "$1" in\n'
        "ps) " + ONE_ARG_PER_LINE + "; printf '" + containers + "' ;;\n"
        "rm) [ $# -gt 2 ] || { echo '\"docker rm\" requires at least 1 argument.' >&2; exit 1; }\n"
        "    " + rm + " ;;\n"
        "esac")


def test_every_listed_container_goes_in_one_docker_rm(box):
    box.stub("docker", docker_listing("3f2a9c1b7e04\n8b1e0d5c2a17\n"))

    assert box.docker.remove_containers() == 0
    assert box.calls == [("docker", "ps -aq"),
                         ("docker", "rm -f 3f2a9c1b7e04 8b1e0d5c2a17")]


def test_no_container_to_remove_is_no_docker_rm_and_no_error(box):
    """xargs without -r runs `docker rm -f` once on empty input, which fails with 123."""
    box.stub("docker", docker_listing(""))

    assert box.docker.remove_containers(check=True) == 0
    assert box.calls == [("docker", "ps -aq")]


def test_a_name_filter_reaches_docker_ps_as_one_argument(box):
    box.stub("docker", docker_listing("3f2a9c1b7e04\n"))

    assert box.docker.remove_containers("^test_") == 0
    assert argv(box) == ["ps", "-aq", "--filter", "name=^test_"]


def test_a_docker_rm_that_failed_is_123_and_raises_with_its_stderr_when_checked(box):
    box.stub("docker", docker_listing(
        "3f2a9c1b7e04\n",
        rm="echo 'Error response from daemon: removal of container 3f2a9c1b7e04 "
           "is already in progress' >&2; exit 1"))

    assert box.docker.remove_containers() == 123
    with pytest.raises(Exception, match="non-zero exit code: Error response from "
                                        "daemon: removal of container 3f2a9c1b7e04"):
        box.docker.remove_containers(check=True)


@pytest.mark.parametrize("package, manager", [(RpmPackage, "yum"),
                                              (DebianPackage, "apt-get")])
def test_a_package_is_removed_with_the_familys_manager(box, package, manager):
    box.stub("yum")
    box.stub("apt-get")

    assert package(name="nscd", host=box).remove() == 0

    assert box.calls == [(manager, "remove -y nscd")]


def test_apt_get_removes_without_asking_whatever_the_frontend_was(box, monkeypatch):
    monkeypatch.setenv("DEBIAN_FRONTEND", "readline")
    box.stub("apt-get", 'echo "$DEBIAN_FRONTEND" > "$BOX/frontend"')

    DebianPackage(name="nscd", host=box).remove()

    assert (box.root / "frontend").read_text(encoding="utf-8") == "noninteractive\n"


def test_a_remove_that_failed_is_its_exit_status_unless_checked(box):
    box.stub("yum", "echo 'Error: No packages marked for removal.' >&2; exit 1")
    nscd = RpmPackage(name="nscd", host=box)

    assert nscd.remove() == 1
    with pytest.raises(Exception, match="non-zero exit code: Error: No packages "
                                        "marked for removal"):
        nscd.remove(check=True)


# Each argument the stand-in got, in brackets: one word or two is visible.
ARGV = 'for a; do printf "[%s]" "$a"; done > "$BOX/argv"'


@pytest.mark.parametrize("value", ["1024 65535", "0; touch injected"],
                         ids=["blanks", "shell-syntax"])
def test_a_sysctl_value_reaches_sysctl_as_one_word(box, value):
    box.stub("sysctl", ARGV)

    assert box.sysctl("net.ipv4.ip_local_port_range").set(value) == 0

    assert (box.root / "argv").read_text(encoding="utf-8") == \
        "[-w][net.ipv4.ip_local_port_range=%s]" % value
    assert not (box.home / "injected").exists()


def test_a_sysctl_that_could_not_be_set_is_its_exit_status_unless_checked(box):
    box.stub("sysctl", "echo 'sysctl: permission denied on key "
                       "\"kernel.io_uring_disabled\"' >&2; exit 255")
    io_uring = box.sysctl("kernel.io_uring_disabled")

    assert io_uring.set(1) == 255
    with pytest.raises(Exception, match="permission denied on key"):
        io_uring.set(1, check=True)
    assert box.calls == [("sysctl", "-w kernel.io_uring_disabled=1")] * 2


def test_a_sysctl_value_keeps_its_inner_blanks_and_a_missing_key_raises(box):
    box.stub("sysctl", 'case "$2" in net.ipv4.ip_local_port_range) '
                       'printf "1024\\t65535\\n";; *) echo "sysctl: cannot stat '
                       '/proc/sys/$2: No such file or directory" >&2; exit 255;; esac')

    assert box.sysctl("net.ipv4.ip_local_port_range").value == "1024\t65535"
    with pytest.raises(Exception, match="cannot stat /proc/sys/net.nf_conntrack_max"):
        box.sysctl("net.nf_conntrack_max").value


@pytest.mark.parametrize("rc, enabled", [(0, True), (1, False)])
def test_selinux_is_enabled_when_selinuxenabled_exits_0(box, rc, enabled):
    box.stub("selinuxenabled", "exit {0}".format(rc))

    assert box.selinux.enabled is enabled


def test_the_selinux_mode_is_what_getenforce_printed(box):
    box.stub("getenforce", "echo Enforcing")

    assert box.selinux.mode == "Enforcing"


def test_a_setenforce_that_failed_is_its_exit_status_unless_checked(box):
    box.stub("setenforce", "echo 'setenforce: SELinux is disabled' >&2; exit 1")

    assert box.selinux.enforce("Permissive") == 1
    with pytest.raises(Exception, match="setenforce: SELinux is disabled"):
        box.selinux.enforce("Permissive", check=True)
    assert box.calls == [("setenforce", "Permissive")] * 2


def test_the_hostname_is_asked_each_time_and_a_failure_raises(box):
    box.stub("hostname", "echo web-1")
    assert box.system_info.hostname == "web-1"

    box.stub("hostname", "echo web-2")
    assert box.system_info.hostname == "web-2"

    box.stub("hostname", "echo 'hostname: Temporary failure' >&2; exit 1")
    with pytest.raises(Exception, match="'hostname' returned non-zero exit code"):
        box.system_info.hostname
