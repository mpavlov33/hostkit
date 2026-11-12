import contextlib
import copy
import logging
import os
import threading
import traceback
import re
import socket
import weakref
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


class _RefusedWhileBusy(paramiko.ChannelException):
    """The server refused one more session while other commands of the same backend ran on it."""


class _CutOff(paramiko.SSHException):
    """A reconnect closed the session while the command ran; `result` is what the command got, rc -1."""

    def __init__(self, message: str, result: "CommandResult"):
        super().__init__(message)
        self.result = result


_MAKING = threading.Lock()

# Live sessions a reconnect closed: a command they cut off has no answer, so it is resent.
_REPLACED = weakref.WeakSet()


def _shares_sessions(backend) -> bool:
    # A ParamikoBackend itself, not a Mock with its spec: its one session may serve many threads.
    return issubclass(type(backend), ParamikoBackend)


def _fresh(error: Exception) -> Exception:
    # A copy without traceback: one object raised in several threads would mix their tracebacks.
    try:
        return copy.copy(error)
    except Exception:
        return error


def _made_once(owner, name: str, make):
    # Made on first use and kept out of the pickled state, so the object copies and subclasses may skip __init__.
    value = owner.__dict__.get(name)
    if value is None:
        with _MAKING:
            value = owner.__dict__.setdefault(name, make())
    return value


def _without(state: dict, *names: str) -> dict:
    return {k: v for k, v in state.items() if k not in names}


@contextlib.contextmanager
def _unless_busy(lock):
    # Yields whether the lock was free: a thread does not wait for another's reconnect.
    free = lock.acquire(blocking=False)
    try:
        yield free
    finally:
        if free:
            lock.release()


def _refusal(error: Exception, client: paramiko.SSHClient, others_run: bool) -> Exception:
    """What a failed session open stands for: a refusal while other commands run, a refusal, or the error itself."""
    refused = isinstance(error, paramiko.ChannelException)
    if not refused:
        transport = client.get_transport()
        # paramiko keeps one open error per transport: of two refusals at once, one reads like this.
        refused = str(error) == "Unable to open channel." and transport is not None and transport.is_active()
    if not refused:
        return error
    code, text = getattr(error, "code", 2), getattr(error, "text", str(error))
    # Decided now: by the time the caller looks, the other commands may have ended.
    if others_run:
        return _RefusedWhileBusy(code, text)
    return error if isinstance(error, paramiko.ChannelException) else paramiko.ChannelException(code, text)


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

    @property
    def _reconnect_lock(self) -> threading.RLock:
        return _made_once(self, "_reconnect_lock_", threading.RLock)

    def __getstate__(self):
        return _without(self.__dict__, "_reconnect_lock_", "_latched_on_")

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
        with self._reconnect_lock:
            logger.warning(f"Establish new SSH connection. Reason: {reason}")
            self.check_host_is_reachable(self.hostname, self.port)
            backend = self.backend
            if _shares_sessions(backend):
                # The swap is one step for the threads sharing the session.
                with backend._connecting:
                    con = backend._con
                    # Only a live session is cut off here: a dead one ended its commands itself (a reboot).
                    with contextlib.suppress(Exception):
                        if con is not None and con.get_transport().is_active():
                            _REPLACED.add(con)
                    backend.disconnect()
                    backend.connect()
            else:
                backend.disconnect()
                backend.connect()
            logger.warning("SSH session is successfuly established")

    def _reconnect_unless_replaced(self, failed_on, reason: str):
        backend = self.backend
        with _unless_busy(self._reconnect_lock) as free:
            if not free:
                return
            # Nor while another thread opens a session: that one is the new session.
            idle = _unless_busy(backend._connecting) if _shares_sessions(backend) else contextlib.nullcontext(True)
            with idle as free:
                current = getattr(backend, "_con", None)
                # Not when another thread has already put a new session in place of the one this failed on.
                if free and (failed_on is None or current is None or current is failed_on):
                    self.reconnect(reason)

    def exec_command(
        self, cmd: str, retries: int = 10, wait: int = 60, **kwargs
    ) -> CommandResult:
        """Run cmd in up to `retries` attempts (one at least) `wait` seconds apart, reconnecting a failed session.

        Returns its CommandResult. A command cut off by another thread's reconnect is sent again: it runs at
        least once, maybe more.
        """
        # The host may be back since the last failure; a dead one fails the 1 s reachability check fast.
        if self._stop_reconnect:
            with _unless_busy(self._reconnect_lock) as free:
                # Another thread may have reconnected meanwhile.
                if free and self._stop_reconnect:
                    current = getattr(self.backend, "_con", None)
                    # A session opened since that failure is left as it is (none existed if none is kept).
                    if current is not None and current is not self.__dict__.get("_latched_on_"):
                        self._stop_reconnect = False
                    with contextlib.suppress(Exception):
                        if self._stop_reconnect:
                            self.reconnect("an earlier command could not reach the host")
                            self._stop_reconnect = False
        # The session the latest attempt ran on, and the attempts made.
        ran_on, attempts = [None], [0]

        @retry(
            wait=wait_fixed(wait),
            stop=stop_after_attempt(retries),
            retry=retry_if_not_exception_type(CommandTimeout),
            reraise=True,
        )
        def _run():
            ran_on[0] = None
            attempts[0] += 1
            try:
                if _shares_sessions(self.backend):
                    # Taken first, so the session read next is the one the command runs on.
                    self.backend.client
                ran_on[0] = getattr(self.backend, "_con", None)
                return self.backend.exec_command(cmd, **kwargs)
            except CommandTimeout:
                # The channel is wedged: reconnecting and retrying would only multiply the wait.
                raise
            except _CutOff as e:
                # No attempt left to send it again: -1 is its answer, as for a command that ends its session.
                if attempts[0] >= max(retries, 1):
                    return e.result
                raise
            except _RefusedWhileBusy:
                # The other commands hold the slots (MaxSessions): retried as is, since closing would cut them off.
                raise
            except Exception as e:
                if not self._stop_reconnect:
                    self._reconnect_unless_replaced(ran_on[0], str(e))
                raise

        try:
            return _run()
        except CommandTimeout:
            raise
        except Exception as e:
            tb = traceback.format_exc()
            # Attempts used up on a busy session say nothing of the host being down.
            if not isinstance(e, _RefusedWhileBusy):
                self._stop_reconnect = True
                self.__dict__["_latched_on_"] = ran_on[0]
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
        failed_on = None
        try:
            if _shares_sessions(self.backend):
                # Taken first, so the session read next is the one the copy runs on.
                self.backend.client
            failed_on = getattr(self.backend, "_con", None)
            return self.backend.send_files(
                action=action,
                local_path=local_path,
                remote_path=remote_path,
                **kwargs,
            )
        except _RefusedWhileBusy:
            # The other commands hold the slots: the retry reuses the connection they run on.
            raise
        except Exception as e:
            # A missing file is no lost connection, but no copy either: it raises too.
            if "No such file or directory" not in str(e):
                self._reconnect_unless_replaced(failed_on, str(e))
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
    def _connecting(self) -> threading.RLock:
        # One thread opens the session the others share.
        return _made_once(self, "_connecting_", threading.RLock)

    @property
    def _counting(self) -> threading.Lock:
        return _made_once(self, "_counting_", threading.Lock)

    def __getstate__(self):
        return _without(self.__dict__, "_connecting_", "_counting_", "_in_flight_", "_failed_connects_",
                        "_connect_error_")

    def _enter(self):
        with self._counting:
            self.__dict__["_in_flight_"] = self.__dict__.get("_in_flight_", 0) + 1

    def _leave(self) -> int:
        """Count this command out; return how many others still run on the backend."""
        with self._counting:
            self.__dict__["_in_flight_"] -= 1
            return self.__dict__["_in_flight_"]

    @property
    def client(self) -> paramiko.SSHClient:
        con = self._con
        if con:
            return con
        failed = self.__dict__.get("_failed_connects_", 0)
        with self._connecting:
            con = self._con
            if con:
                return con
            # A connect that failed while this thread waited: its error is this one's too, not one more handshake in turn.
            if self.__dict__.get("_failed_connects_", 0) != failed:
                raise _fresh(self.__dict__["_connect_error_"])
            return self.connect()

    def connect(self):
        con = paramiko.SSHClient()
        try:
            con.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            con.connect(
                self.hostname,
                username=self.username,
                password=self.password,
                key_filename=self.key_filename,
                port=self.port,
                disabled_algorithms=self.disabled_algorithms,
                timeout=180,
                banner_timeout=300,
            )
            chan = con.invoke_shell()
        except Exception as e:
            logger.error(f"Error connecting to {self.hostname}: {e}")
            with contextlib.suppress(Exception):
                con.close()
            self.__dict__["_connect_error_"] = _fresh(e)
            self.__dict__["_failed_connects_"] = self.__dict__.get("_failed_connects_", 0) + 1
            raise
        # Published once connected: no thread gets a client still in its handshake.
        self._con, self._chan = con, chan
        logger.debug(self.check_connection())
        return con

    def disconnect(self):
        con, chan = self._con, self._chan
        # Unpublished before the close: no thread takes a session that is closing.
        self._con = None
        self._chan = None
        try:
            if con:
                con.close()
        except Exception as e:
            logger.debug(f"Error closing connection: {e}")

        try:
            if chan:
                chan.close()
        except Exception as e:
            logger.debug(f"Error closing channel: {e}")

    def check_connection(self) -> CommandResult:
        return self.exec_command("hostname")

    def exec_command(
        self, command: str, command_timeout: int = SSH_COMMAND_TIMEOUT, **kwargs
    ) -> CommandResult:
        exec_time = str(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        client = self.client
        counted = True
        self._enter()
        try:
            try:
                stdin, stdout, stderr = client.exec_command(
                    command, timeout=command_timeout or None
                )
            except paramiko.SSHException as e:
                # Counted out before looking: of threads refused together, the last one sees no other left.
                counted = False
                raise _refusal(e, client, self._leave() > 0)
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
            result = CommandResult(
                backend="paramiko",
                rc=int(channel.recv_exit_status()),
                # A stray non-UTF-8 byte is output, not a lost connection.
                stdout=stdout.read().decode(errors="replace"),
                stderr=stderr.read().decode(errors="replace"),
                command=command,
                exec_time=exec_time,
            )
        finally:
            if counted:
                self._leave()
        # -1 is no exit status; with the session closed by a reconnect, the output is no answer either.
        if result.rc == -1 and client in _REPLACED:
            raise _CutOff(f"The session was closed while the command ran: {strip_exports(command)}", result)
        return result

    def send_files(
        self,
        action: str = "put",
        local_path: str = "",
        remote_path: str = "",
        **kwargs,
    ) -> None:
        """Copy a file over scp: `put` sends local_path to remote_path, `get` fetches it back."""
        client = self.client
        counted = True
        self._enter()
        try:
            with SCPClient(client.get_transport(), socket_timeout=180) as scp:
                if action == "put":
                    scp.put(local_path, remote_path, **kwargs)
                elif action == "get":
                    scp.get(remote_path, local_path, **kwargs)
        except paramiko.SSHException as e:
            counted = False
            raise _refusal(e, client, self._leave() > 0)
        finally:
            if counted:
                self._leave()


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
