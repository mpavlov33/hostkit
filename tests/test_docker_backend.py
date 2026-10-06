"""hostkit's DockerBackend: commands in a container through the Docker API.

The double is the container: ShellContainer runs exec_run's argv on this machine's sh.
"""
import os
import subprocess

import docker
import pytest

import hostkit
from hostkit.backends import DockerBackend

from hostkit.testing import SH


class ShellContainer:
    """docker-py's Container.exec_run, run on this machine's sh."""

    def __init__(self, box):
        self.box = box
        self.calls = []

    def exec_run(self, cmd, stdout=True, stderr=True, stdin=False, tty=False,
                 privileged=False, user="", detach=False, stream=False,
                 socket=False, environment=None, workdir=None, demux=False):
        self.calls.append(dict(cmd=cmd, environment=environment, workdir=workdir,
                               user=user, privileged=privileged, demux=demux))
        if isinstance(cmd, str):
            # docker-py splits a string itself, with no shell.
            import shlex
            cmd = shlex.split(cmd)
        argv = [SH if part == "/bin/sh" else part for part in cmd]
        env = dict(os.environ, PATH=str(self.box.bin) + os.pathsep + os.environ.get("PATH", ""),
                   HOME=self.box.home.as_posix(), BOX=self.box.root.as_posix())
        env.update(environment or {})
        done = subprocess.run(argv, cwd=workdir or str(self.box.home), env=env,
                              capture_output=True, timeout=60)
        if demux:
            return docker.models.containers.ExecResult(
                done.returncode, (done.stdout or None, done.stderr or None))
        return docker.models.containers.ExecResult(done.returncode, done.stdout + done.stderr)


class Client:
    def __init__(self, container):
        self.containers = type("_Containers", (), {"get": staticmethod(lambda name: container)})()


@pytest.fixture(autouse=True)
def no_waiting(mocker):
    """A retry would sleep a minute: a backend error must fail the test at once."""
    mocker.patch("time.sleep", side_effect=AssertionError("hostkit retried a command the backend failed"))


@pytest.fixture
def container(box):
    return ShellContainer(box)


@pytest.fixture
def conn(container):
    return hostkit.Connection(backend="docker", hostname="192.0.2.1", port=2375,
                            container_id="app_0", _con=Client(container))


def home(box, name):
    return (box.home / name).as_posix()


def test_a_string_goes_to_a_shell_so_a_glob_removes_every_match(conn, box):
    for name in ("a.rpm", "b.rpm", "keep.txt"):
        (box.home / name).write_text("x", encoding="utf-8")

    assert conn.file(home(box, "*.rpm")).remove(check=True) == 0

    assert sorted(p.name for p in box.home.iterdir()) == ["keep.txt"]


def test_a_file_written_reads_back_byte_for_byte(conn, box):
    path = home(box, "properties.yml")

    conn.file(path).write("name: 'x' && y\n", check=True)

    assert conn.file(path).exists
    assert conn.file(path).content == "name: 'x' && y\n"


def test_stdout_and_stderr_come_apart(conn):
    res = conn.exec_command("echo out; echo err >&2")

    assert (res.rc, res.stdout, res.stderr) == (0, "out\n", "err\n")


def test_a_command_that_must_succeed_raises_with_its_stderr(conn):
    with pytest.raises(Exception, match="no such thing"):
        conn.check_output("echo no such thing >&2; exit 3")


def test_an_action_that_fails_returns_its_status(conn, box):
    """act() passes ignore_errors, a keyword exec_run does not take."""
    assert conn.file(home(box, "missing/x")).move(home(box, "y")) != 0


def test_only_what_exec_run_takes_reaches_it(conn, container):
    conn.exec_command("true", ignore_errors=True, environment={"K": "v"}, workdir="/")

    [call] = container.calls
    assert call["cmd"][:2] == ["/bin/sh", "-c"]
    assert call["environment"] == {"K": "v"} and call["workdir"] == "/"
    assert call["demux"] is True


def test_the_environment_reaches_the_command_without_being_in_it(conn, container):
    res = conn.exec_command('printf "%s" "$SECRET"', environment={"SECRET": "s3cr3t"})

    assert res.stdout == "s3cr3t"
    assert "s3cr3t" not in container.calls[0]["cmd"][-1]


def test_a_list_is_the_argv_as_given(conn, container):
    res = conn.exec_command(["printf", "%s|", "a b", "c"])

    assert res.stdout == "a b|c|"
    assert container.calls[0]["cmd"] == ["printf", "%s|", "a b", "c"]


def test_its_own_client_waits_for_long_commands(mocker):
    made = mocker.patch("docker.DockerClient")

    DockerBackend(hostname="192.0.2.1", container_id="app_0").connect()

    made.assert_called_once_with(base_url="http://192.0.2.1:2375", timeout=600)


def test_files_go_to_a_container_some_other_way(conn):
    with pytest.raises(NotImplementedError):
        conn.backend.send_files("put", "a", "b")
