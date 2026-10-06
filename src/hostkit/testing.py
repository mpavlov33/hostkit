"""Test doubles for code that drives hostkit.Connection: Sshd (paramiko), Clock (time) and Box (local sh).

Sshd and Clock take pytest-mock's ``mocker``; a Box skips the test where there is no POSIX sh.
"""
import os
import shutil
import socket
import subprocess
import threading

import paramiko

import hostkit
from hostkit.backends import CommandResult

VM_IP = "192.0.2.10"

# Taken before a test patches paramiko.SSHClient: each fake client is autospecced from the real class.
SSHClient = paramiko.SSHClient


class Clock:
    """time.time and time.sleep: sleeping moves the clock, nothing waits."""

    def __init__(self, mocker, start=1_790_000_000.0):
        self.now = start
        self.slept = []
        mocker.patch("time.sleep", side_effect=self.sleep)
        mocker.patch("time.time", side_effect=lambda: self.now)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class Reply:
    """What the remote side sends back for one command."""

    def __init__(self, rc=0, out=b"", err=b""):
        self.rc = rc
        self.out = out
        self.err = err


class Flood(Reply):
    """Output larger than the ssh channel window: its exit status arrives only once the output is read."""

    WINDOW = 2 * 1024 * 1024

    def __init__(self, rc=0):
        super().__init__(rc=rc, out=b"x" * (self.WINDOW + 1024) + b"\n")


# The remote never sends an exit status: a wedged VM, a dead network path.
HANG = object()


class _StatusEvent:
    """channel.status_event that records each wait's timeout in `waits` and never holds a test long."""

    def __init__(self, arrived, never=False):
        self._event = threading.Event()
        if arrived:
            self._event.set()
        self._never = never
        self.waits = []

    def set(self):
        self._event.set()

    def is_set(self):
        return self._event.is_set()

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self._event.is_set():
            return True
        if self._never:
            return False
        return self._event.wait(0.25 if timeout is None else min(timeout, 0.25))


class _Channel:
    """The paramiko.Channel of one command, hand-written since a spec'd mock per command is slow."""

    def __init__(self, reply):
        self.reply = reply
        self.closed = False
        self.pending = bytearray(reply.out if isinstance(reply, Flood) else b"")
        self.status_event = _StatusEvent(
            arrived=reply is not HANG and not isinstance(reply, Flood),
            never=reply is HANG)

    def recv_exit_status(self):
        if not self.status_event.is_set():
            raise AssertionError(
                "recv_exit_status() blocks forever here: the exit status never "
                "arrives (a wedged channel, or output nobody reads)")
        return self.reply.rc

    def exit_status_ready(self):
        return self.status_event.is_set()

    def recv_ready(self):
        return bool(self.pending)

    def recv(self, nbytes):
        data = bytes(self.pending[:nbytes])
        del self.pending[:nbytes]
        if not self.pending and self.reply is not HANG:
            self.status_event.set()
        return data

    def recv_stderr_ready(self):
        return False

    def recv_stderr(self, nbytes):
        return b""

    def shutdown_write(self):
        pass

    def close(self):
        self.closed = True


class _ChannelFile:
    """stdin, stdout or stderr of one exec_command (paramiko.ChannelFile)."""

    def __init__(self, channel, data=b""):
        self.channel = channel
        self._data = data
        self.written = []

    def read(self, size=None):
        channel = self.channel
        if channel.reply is HANG:
            raise AssertionError("read() on a channel that never closes "
                                 "blocks forever")
        if isinstance(channel.reply, Flood) and self._data is None:
            return channel.recv(len(channel.pending))
        return self._data

    def write(self, data):
        self.written.append(data)


class Sshd:
    """A host's sshd behind paramiko.SSHClient: the first `answers` (fragment, reply) found in a command answers it."""

    # Connection checks, not the work under test: `ran` leaves them out.
    HOUSEKEEPING = ("hostname", "True")

    def __init__(self, mocker, clock, answers=()):
        self.mocker = mocker
        self.clock = clock
        self.answers = list(answers)
        self.boot_at = 0.0
        self.reachable = True
        self.accepts = lambda kwargs: True
        self.log = []           # (time, command) for every command that arrived
        self.connects = []      # (hostname, kwargs) of every SSHClient.connect
        self.clients = []
        self.channels = []
        self.uploads = {}       # path -> text written over sftp
        self._alive = {}
        mocker.patch("hostkit.backends.paramiko.SSHClient", autospec=True,
                     side_effect=self._new_client)
        mocker.patch("hostkit.backends.socket.create_connection",
                     side_effect=self._tcp)

    # -- what a test reads -------------------------------------------------

    @property
    def ran(self):
        return [c for _, c in self.log if c not in self.HOUSEKEEPING]

    @property
    def open_sessions(self):
        return [c for c in self.clients if self._alive[id(c)]]

    def drop(self):
        """Every session open now is gone for good, as after an outage."""
        for client in self.clients:
            self._alive[id(client)] = False

    # -- paramiko ------------------------------------------------------------

    def _tcp(self, address, timeout=None, *args, **kwargs):
        if not self.reachable:
            raise ConnectionRefusedError(111, "Connection refused")
        return self._like(socket.socket)

    def _new_client(self):
        client = self.mocker.create_autospec(SSHClient, instance=True)
        self._alive[id(client)] = False
        client.connect.side_effect = (
            lambda hostname, **kwargs: self._connect(client, hostname, kwargs))
        client.exec_command.side_effect = (
            lambda command, **kwargs: self._exec(client, command, kwargs))
        client.close.side_effect = lambda: self._close(client)
        client.invoke_shell.side_effect = (
            lambda *args, **kwargs: self._like(paramiko.Channel))
        client.get_transport.return_value = self._like(paramiko.Transport)
        client.open_sftp.side_effect = lambda: self._sftp()
        self.clients.append(client)
        return client

    def _connect(self, client, hostname, kwargs):
        self.connects.append((hostname, kwargs))
        if not self.reachable:
            raise paramiko.ssh_exception.NoValidConnectionsError(
                {(hostname, kwargs.get("port", 22)): OSError(113, "No route to host")})
        if self.clock.now < self.boot_at:
            raise paramiko.SSHException("Error reading SSH protocol banner")
        if not self.accepts(kwargs):
            raise paramiko.AuthenticationException("Authentication failed.")
        self._alive[id(client)] = True

    def _close(self, client):
        self._alive[id(client)] = False

    def _exec(self, client, command, kwargs):
        if not self._alive[id(client)]:
            raise paramiko.SSHException("SSH session not active")
        self.log.append((self.clock.now, command))
        answer = Reply()
        for fragment, candidate in self.answers:
            if fragment in command:
                answer = candidate
                break
        if callable(answer) and not isinstance(answer, (Reply, BaseException)):
            answer = answer(command)
        if isinstance(answer, BaseException):
            raise answer
        return self._streams(answer)

    def _like(self, cls):
        return self.mocker.NonCallableMagicMock(spec=cls)

    def _streams(self, reply):
        channel = _Channel(reply)
        self.channels.append(channel)
        if reply is HANG or isinstance(reply, Flood):
            stdout_data, stderr_data = None, b""
        else:
            stdout_data, stderr_data = reply.out, reply.err
        return (_ChannelFile(channel), _ChannelFile(channel, stdout_data),
                _ChannelFile(channel, stderr_data))

    def _sftp(self):
        sftp = self._like(paramiko.SFTPClient)
        sftp.__enter__.return_value = sftp

        def open_file(path, mode="r", *args, **kwargs):
            handle = self._like(paramiko.SFTPFile)
            handle.__enter__.return_value = handle
            handle.write.side_effect = (
                lambda data: self.uploads.__setitem__(
                    path, self.uploads.get(path, "") + data))
            return handle

        sftp.file.side_effect = open_file
        return sftp


def reply_once_then(first, then):
    """An answer that is `first` the first time and `then` ever after."""
    replies = [first]

    def answer(command):
        return replies.pop() if replies else then

    return answer


SH = shutil.which("sh")
_REAL_RUN = subprocess.run

STUB = """#!/bin/sh
printf '%s\\t%s\\n' "{name}" "$*" >> "{log}"
{body}
"""

# Stubbed to refuse in every Box, so an unstubbed command never reaches out with this machine's credentials.
REFUSED = ("aws", "git", "ssh", "scp", "rsync", "curl", "wget", "docker", "nc",
           "yum", "dnf", "apt-get", "apt", "rpm", "dpkg", "pip", "pip3",
           "ansible-playbook", "sudo", "systemctl")
REFUSE = ('echo "$0: not stubbed by this test; refusing to run the real one" >&2\n'
          "exit 127")


class Box(hostkit.Connection):
    """hostkit.Connection running commands in local sh; `stub(name, body)` stand-ins log to `calls`."""

    def __init__(self, root):
        if SH is None:
            import pytest
            pytest.skip("no POSIX sh on this machine; these commands mean what "
                        "the shell makes of them")
        self.root = root
        self.home = root / "home"
        self.bin = root / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.commands = []
        for name in REFUSED:
            self.stub(name, REFUSE)

    def stub(self, name, body="exit 0"):
        path = self.bin / name
        path.write_bytes(STUB.format(name=name, log=self.log.as_posix(),
                                     body=body).encode("utf-8"))
        path.chmod(0o755)

    @property
    def calls(self):
        if not self.log.exists():
            return []
        return [tuple(line.split("\t", 1))
                for line in self.log.read_text(encoding="utf-8").splitlines()]

    def exec_command(self, cmd, retries=10, wait=60, **kwargs):
        self.commands.append(cmd)
        env = dict(os.environ)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env["BOX"] = self.root.as_posix()
        # Not this machine's home: its ~/.aws, ~/.ssh and ~/.gitconfig stay out.
        env["HOME"] = self.home.as_posix()
        env["AWS_CONFIG_FILE"] = env["AWS_SHARED_CREDENTIALS_FILE"] = (
            self.root / "no-aws-config").as_posix()
        done = _REAL_RUN([SH, "-c", cmd], cwd=str(self.home), env=env,
                         capture_output=True, text=True, timeout=120)
        return CommandResult(backend="local", rc=done.returncode, command=cmd,
                             stdout=done.stdout, stderr=done.stderr)
