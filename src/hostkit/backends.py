import contextlib
import logging
import os
import traceback
import re
import socket
from datetime import datetime
from dataclasses import dataclass
from typing import Union

import docker
import docker.models.containers
import paramiko
import time
from scp import SCPClient

import hostkit
from hostkit.utils import cached_property, strip_exports
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_fixed,
)

logger = logging.getLogger(__name__)

# A hung channel would block a command forever, so it is aborted after this many seconds (0: never).
SSH_COMMAND_TIMEOUT = int(os.getenv("SSH_COMMAND_TIMEOUT", "3600"))


class CommandTimeout(Exception):
    """A remote command ran past its timeout and was aborted; it is never retried."""


@dataclass
class CommandResult:
    backend: str
    rc: int
    command: str
    stdout: str
    stderr: str
    container_id: str = ""
    exec_time: str = ""

    @property
    def output(self):
        return f"{self.stdout}\n{self.stderr}"

    @property
    def exit_status(self):
        return self.rc

    @property
    def succeeded(self):
        return self.rc == 0

    @property
    def failed(self):
        return self.rc != 0

    def __repr__(self):
        optional = (
            "container={!r}, ".format(self.container_id)
            if self.container_id
            else ""
        )
        return "{}command = {!r}\nrc = {}\n\nSTDOUT:\n{}\nSTDERR:\n{}\nTimestamp:\n{}".format(
            optional,
            self.command,
            self.rc,
            self.stdout,
            self.stderr,
            self.exec_time,
        )


class Connection:

    def __init__(
        self,
        backend: str = None,
        hostname: str = None,
        password: str = None,
        port: Union[int, str] = None,
        username: str = None,
        **kwargs,
    ):
        self.hostname: str = hostname
        self.port = int(port)
        self.username: str = username
        self.password: str = password
        self._stop_reconnect: bool = False
        if backend == "paramiko":
            self.backend: ParamikoBackend = ParamikoBackend(
                hostname=hostname,
                port=self.port,
                username=username,
                password=password,
                **kwargs,
            )
        if backend == "docker":
            self.backend: DockerBackend = DockerBackend(
                hostname=self.hostname, port=self.port, **kwargs
            )

    @staticmethod
    def check_host_is_reachable(address, port, timeout=1):
        try:
            s = socket.create_connection((address, int(port)), timeout=timeout)
            s.close()
        except Exception:
            raise ConnectionError(
                f"Could not establish connection to {address}:{port}, probably host is not up or network is not reachable"
            )

    def reconnect(self, reason: str = ""):
        logger.warning(f"Establish new SSH connection. Reason: {reason}")
        self.check_host_is_reachable(self.hostname, self.port)
        self.backend.disconnect()
        self.backend.connect()
        logger.warning("SSH session is successfuly established")

    def exec_command(
        self, cmd: str, retries: int = 10, wait: int = 60, **kwargs
    ) -> CommandResult:
        # The host may be back since the last failure; a dead one fails the 1 s reachability check fast.
        if self._stop_reconnect:
            with contextlib.suppress(Exception):
                self.reconnect("an earlier command could not reach the host")
                self._stop_reconnect = False

        @retry(
            wait=wait_fixed(wait),
            stop=stop_after_attempt(retries),
            retry=retry_if_not_exception_type(CommandTimeout),
            reraise=True,
        )
        def _run():
            try:
                return self.backend.exec_command(cmd, **kwargs)
            except CommandTimeout:
                # The channel is wedged: reconnecting and retrying would only multiply the wait.
                raise
            except Exception as e:
                if not self._stop_reconnect:
                    self.reconnect(str(e))
                raise

        try:
            return _run()
        except CommandTimeout:
            raise
        except Exception as e:
            tb = traceback.format_exc()
            self._stop_reconnect = True
            raise Exception(
                f"Could't execute cmd even with reconnect. {str(e)}:\n{tb}"
            )

    def run_expect(
        self,
        cmd,
        expect: str = None,
        timeout: int = 15,
        time_iter: int = 1,
        ignore_errors=True,
    ) -> tuple[bool, str]:
        """Rerun cmd until its output matches the regex `expect` or `timeout` passes; return (matched, output)."""
        msg = ""
        time_end = time.time() + timeout
        while not re.search(expect, msg) and time.time() < time_end:
            msg = (
                self.exec_command(cmd).stdout
                + "\n"
                + self.exec_command(cmd).stderr
            )
            time.sleep(time_iter)
        return bool(re.search(expect, msg)), msg

    def run_wait(
        self,
        cmd: str = "",
        timeout: int = 60,
        logdir: str = "/tmp",
        iter_time: int = 5,
        capture_stdout: bool = True,
        live_stdout: bool = True,
        mode: str = "tmux",
        nowait: bool = False,
        endless_cmd: bool = False,
        creds: dict = None,
    ) -> "hostkit.tmux.CommandRunnerNoSession":
        bg_cmd = hostkit.tmux.CommandRunnerNoSession(
            host=self,
            command=cmd,
            timeout=timeout,
            logdir=logdir,
            creds=creds,
            endless_cmd=endless_cmd,
            capture_stdout=capture_stdout,
        )
        bg_cmd.exec_cmd(mode)

        if nowait:
            return bg_cmd
        bg_cmd.wait_finish(
            iter_time=iter_time,
            max_wait=timeout + 10,
            mode=mode,
            live_stdout=live_stdout,
        )
        return bg_cmd

    def exists(self, command: str) -> bool:
        """Return True if `command` is found in $PATH."""
        return self.exec_command("which %s" % command).rc == 0

    def check_output(self, cmd: str, **kwargs) -> str:
        """Return stdout of a command that must succeed, raise if it did not"""
        res = self.exec_command(cmd, **kwargs)
        if res.rc:
            raise Exception(
                f"command '{cmd}' returned non-zero exit code: {res.stderr}"
            )
        return res.stdout

    @retry(wait=wait_fixed(30), stop=stop_after_attempt(3), reraise=True)
    def send_files(
        self,
        action: str = "put",
        local_path: str = "",
        remote_path: str = "",
        **kwargs,
    ):
        try:
            return self.backend.send_files(
                action=action,
                local_path=local_path,
                remote_path=remote_path,
                **kwargs,
            )
        except Exception as e:
            # A missing file is no lost connection, but no copy either: it raises too.
            if "No such file or directory" not in str(e):
                self.reconnect(str(e))
            raise

    @cached_property
    def system_info(self):
        return hostkit.modules.systeminfo.SystemInfo(self)

    def file(self, path: str):
        return hostkit.modules.file.File(path=path, host=self)

    @cached_property
    def process(self):
        if self.system_info.sysinfo["type"] == "linux":
            return hostkit.modules.process.PosixProcess(self)

    def package(self, name):
        return hostkit.modules.package.Package.get_module_class(self)(
            host=self, name=name
        )

    def service(self, name: str):
        return hostkit.modules.service.Service(name=name, host=self)

    @cached_property
    def systemd(self):
        return hostkit.modules.service.Systemd(self)

    @cached_property
    def system_load(self):
        return hostkit.modules.sysload.SystemLoad(self)

    @cached_property
    def docker(self):
        return hostkit.modules.docker.Docker(self)

    @cached_property
    def selinux(self):
        return hostkit.modules.selinux.SELinux(self)

    def sysctl(self, name: str):
        return hostkit.modules.sysctl.Sysctl(name=name, host=self)

    def tcp_port(self, number: Union[int, str]):
        return hostkit.modules.network.Port(number=number, host=self)


@dataclass
class ParamikoBackend:
    hostname: str
    port: int
    username: str
    password: Union[None, str]
    key_filename: Union[None, str] = None
    disabled_algorithms: dict = None
    get_pty: bool = False
    _con = None
    _chan = None

    @property
    def client(self) -> paramiko.SSHClient:
        if self._con:
            return self._con
        return self.connect()

    def connect(self):
        try:
            self._con = paramiko.SSHClient()
            self._con.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._con.connect(
                self.hostname,
                username=self.username,
                password=self.password,
                key_filename=self.key_filename,
                port=self.port,
                disabled_algorithms=self.disabled_algorithms,
                timeout=180,
                banner_timeout=300,
            )
            self._chan = self._con.invoke_shell()
        except Exception as e:
            logger.error(f"Error connecting to {self.hostname}: {e}")
            self.disconnect()
            raise
        logger.debug(self.check_connection())
        return self._con

    def disconnect(self):
        try:
            if self._con:
                self._con.close()
        except Exception as e:
            logger.debug(f"Error closing connection: {e}")

        try:
            if self._chan:
                self._chan.close()
        except Exception as e:
            logger.debug(f"Error closing channel: {e}")
        self._con = None
        self._chan = None

    def check_connection(self) -> CommandResult:
        return self.exec_command("hostname")

    def exec_command(
        self, command: str, command_timeout: int = SSH_COMMAND_TIMEOUT, **kwargs
    ) -> CommandResult:
        exec_time = str(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        stdin, stdout, stderr = self.client.exec_command(
            command, timeout=command_timeout or None
        )
        channel = stdout.channel
        # recv_exit_status() blocks forever on a wedged channel, so wait for the status with a bound first.
        if command_timeout and not channel.status_event.wait(command_timeout):
            with contextlib.suppress(Exception):
                channel.close()
            # Without its `export ...;` prefix: that is where credentials are.
            raise CommandTimeout(
                f"Command exceeded {command_timeout}s and was aborted "
                f"(channel/VM likely wedged): {strip_exports(command)}"
            )
        return CommandResult(
            backend="paramiko",
            rc=int(channel.recv_exit_status()),
            # A stray non-UTF-8 byte is output, not a lost connection.
            stdout=stdout.read().decode(errors="replace"),
            stderr=stderr.read().decode(errors="replace"),
            command=command,
            exec_time=exec_time,
        )

    def send_files(
        self,
        action: str = "put",
        local_path: str = "",
        remote_path: str = "",
        **kwargs,
    ) -> None:
        """Copy a file over scp: `put` sends local_path to remote_path, `get` fetches it back."""
        with SCPClient(self.client.get_transport(), socket_timeout=180) as scp:
            if action == "put":
                scp.put(local_path, remote_path, **kwargs)
            elif action == "get":
                scp.get(remote_path, local_path, **kwargs)


@dataclass
class DockerBackend:
    hostname: str
    container_id: str
    port: int = 2375
    # exec_run holds its HTTP request until the command ends: docker-py's 60 s cuts long ones.
    timeout: int = 600
    _con: Union[docker.DockerClient, None] = None

    # What exec_run takes of the caller's arguments; the rest (ignore_errors, ...) is hostkit's own.
    EXEC_RUN_ARGS = ("user", "privileged", "environment", "workdir")

    @cached_property
    def container(self) -> docker.models.containers.Container:
        return self.client.containers.get(self.container_id)

    @property
    def client(self):
        if self._con:
            return self._con
        return self.connect()

    def connect(self) -> docker.DockerClient:
        self._con = docker.DockerClient(
            base_url=f"http://{self.hostname}:{self.port}", timeout=self.timeout
        )
        return self._con

    def disconnect(self):
        pass

    def check_connection(self):
        pass

    def exec_command(
        self, command, command_timeout: int = None, **kwargs
    ) -> CommandResult:
        """Run a command in the container like ``docker exec``; a string goes to /bin/sh, a list is argv.

        Only user, privileged, environment and workdir reach exec_run; command_timeout is ignored.
        """
        exec_time = str(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # docker-py splits a string into argv itself: no redirects, globs or || without a shell.
        argv = ["/bin/sh", "-c", command] if isinstance(command, str) else command
        rc, (stdout, stderr) = self.container.exec_run(
            argv,
            demux=True,
            **{k: v for k, v in kwargs.items() if k in self.EXEC_RUN_ARGS},
        )
        return CommandResult(
            command=command,
            rc=rc,
            stdout=(stdout or b"").decode("utf-8", "replace"),
            stderr=(stderr or b"").decode("utf-8", "replace"),
            backend="docker",
            container_id=self.container_id,
            exec_time=exec_time,
        )

    def send_files(
        self,
        action: str = "put",
        local_path: str = "",
        remote_path: str = "",
        **kwargs,
    ):
        raise NotImplementedError("files go to a container with docker cp on its host")

    @property
    def containers(self) -> list[docker.models.containers.Container]:
        return self.client.containers.list()
