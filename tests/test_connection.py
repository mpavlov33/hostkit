"""hostkit.Connection over paramiko: command timeouts, scp, disconnect, one session shared by threads, and strip_exports.

The doubles are hostkit.testing.Sshd for the host's sshd and an autospecced scp.SCPClient.
"""
import contextlib
import copy
import pickle
import threading
import time

import paramiko
import pytest
import scp

import hostkit
from hostkit.backends import SSH_COMMAND_TIMEOUT, CommandResult, CommandTimeout, ParamikoBackend
from hostkit.testing import HANG, VM_IP, CutOff, Reply, Running, reply_once_then
from hostkit.utils import strip_exports

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
UPLOAD = "aws s3 cp /tmp/logs.tar.gz s3://example-artifacts/logs/"
WITH_CREDS = (f"export AWS_ACCESS_KEY_ID='AKIAIOSFODNN7EXAMPLE' "
              f"AWS_SECRET_ACCESS_KEY='{SECRET}'; {UPLOAD}")
WITH_SEMICOLON_CREDS = WITH_CREDS.replace(SECRET, "wJalrXUtnFEMI;K7MDENG/bPxRfiCY;EXAMPLEKEY")


@pytest.fixture
def scp_client(mocker):
    cls = mocker.patch("hostkit.backends.SCPClient", autospec=True)
    cls.return_value.__enter__.return_value = cls.return_value
    return cls


def test_a_command_that_never_exits_raises_command_timeout_and_is_not_resent(sshd, host):
    sshd.answers = [("dmesg", HANG)]

    with pytest.raises(CommandTimeout):
        host.exec_command("dmesg")

    assert sshd.ran == ["dmesg"], "a wedged command must not be re-run `retries` times"
    wedged = sshd.channels[-1]
    assert wedged.status_event.waits == [SSH_COMMAND_TIMEOUT]
    assert wedged.closed, "the wedged channel is closed"


@pytest.mark.parametrize("command", [WITH_CREDS, WITH_SEMICOLON_CREDS], ids=["plain", "semicolon"])
def test_a_timed_out_command_keeps_its_exported_credentials_out_of_the_error(
        sshd, host, command):
    sshd.answers = [("aws s3 cp", HANG)]

    with pytest.raises(CommandTimeout) as raised:
        host.exec_command(command)

    assert "EXAMPLEKEY" not in str(raised.value), (
        "the error would print AWS_SECRET_ACCESS_KEY into every log and report")
    assert str(raised.value).endswith(UPLOAD)


@pytest.mark.parametrize("command,stripped", [
    (WITH_CREDS, UPLOAD),
    (WITH_SEMICOLON_CREDS, UPLOAD),
    ("export A=1; export B='x y'; make", "make"),
    ("export A=it's; ls", "ls"),
    ("export A=1\necho keep; ls", "export A=1\necho keep; ls"),
    ("ls -la /root", "ls -la /root"),
], ids=["quoted", "semicolon-in-quotes", "two-exports", "unpaired-quote",
        "ends-at-its-line", "no-export"])
def test_strip_exports_cuts_every_export_prefix_and_nothing_else(command, stripped):
    assert strip_exports(command) == stripped


def test_stripping_exports_from_a_command_with_many_quoted_words_is_fast():
    """A failed cut must not backtrack through every split of the quoted words."""
    command = "export K='v' " + "'a' " * 18

    started = time.monotonic()
    assert strip_exports(command) == command
    assert time.monotonic() - started < 1


def test_put_copies_the_local_path_to_the_remote_path(sshd, host, scp_client):
    host.send_files("put", local_path="/opt/deploy/ansible", remote_path="/root",
                    recursive=True)

    scp_client.return_value.put.assert_called_once_with(
        "/opt/deploy/ansible", "/root", recursive=True)
    assert scp_client.call_args.args[0] is sshd.clients[-1].get_transport.return_value, (
        "the copy rides the session the connection already has")
    assert scp_client.call_args.kwargs == {"socket_timeout": 180}


def test_get_copies_the_remote_path_to_the_local_path(sshd, host, scp_client):
    host.send_files("get", local_path="/tmp/logs", remote_path="/opt/myapp/tests/out.log")

    scp_client.return_value.get.assert_called_once_with(
        "/opt/myapp/tests/out.log", "/tmp/logs")
    assert not scp_client.return_value.put.called


def test_a_copy_that_breaks_is_retried_on_a_fresh_session(sshd, host, scp_client, clock):
    scp_client.return_value.put.side_effect = [
        scp.SCPException("Timeout waiting for scp response"), None]
    host.exec_command("true")
    first = sshd.clients[-1]

    host.send_files(local_path="/tmp/change_mtu.sh", remote_path="/tmp/")

    assert scp_client.return_value.put.call_count == 2
    assert first not in sshd.open_sessions, "the broken session is dropped"
    assert scp_client.call_args.args[0] is sshd.clients[-1].get_transport.return_value
    assert 30 in clock.slept


def test_a_copy_that_keeps_failing_raises_after_three_attempts(sshd, host, scp_client):
    scp_client.return_value.put.side_effect = scp.SCPException(
        "Timeout waiting for scp response")

    with pytest.raises(scp.SCPException):
        host.send_files(local_path="/tmp/a.sh", remote_path="/tmp/")

    assert scp_client.return_value.put.call_count == 3


class OnlyConnectAndDisconnect:
    """A backend of its own with only connect and disconnect."""

    def __init__(self):
        self.calls = []

    def connect(self):
        self.calls.append("connect")

    def disconnect(self):
        self.calls.append("disconnect")


def test_a_backend_with_only_connect_and_disconnect_is_still_reconnected(sshd, host):
    host.backend = OnlyConnectAndDisconnect()

    host.reconnect("lost")

    assert host.backend.calls == ["disconnect", "connect"]


class SkipsInit(hostkit.Connection):
    """A subclass that sets up only what it uses, as Connection subclasses may."""

    def __init__(self, backend):
        self.hostname, self.port, self.backend = VM_IP, 22, backend


def test_a_subclass_that_skips_init_still_reconnects(sshd):
    host = SkipsInit(OnlyConnectAndDisconnect())

    host.reconnect("lost")

    assert host.backend.calls == ["disconnect", "connect"]


@pytest.mark.parametrize("make", [
    lambda: hostkit.Connection(backend="paramiko", hostname=VM_IP, port=22, username="root", password="x"),
    lambda: hostkit.Connection(backend="docker", hostname=VM_IP, port=2375, container_id="app_0"),
], ids=["paramiko", "docker"])
def test_a_connection_not_yet_used_is_copied_and_pickled(make):
    host = make()

    for copied in (copy.deepcopy(host), pickle.loads(pickle.dumps(host))):
        assert copied.backend == host.backend


def test_a_connection_and_a_backend_that_were_used_are_still_copied_and_pickled(sshd, host):
    reconnected = SkipsInit(OnlyConnectAndDisconnect())
    reconnected.reconnect("lost")
    host.exec_command("true")
    host.backend.disconnect()

    for used in (reconnected, host.backend):
        assert type(pickle.loads(pickle.dumps(used))) is type(used)
        assert type(copy.deepcopy(used)) is type(used)


def test_a_mock_backend_is_reconnected_after_a_failure(sshd, host, mocker):
    host.backend = mocker.Mock()
    host.backend.exec_command.side_effect = [
        OSError("Socket is closed"), CommandResult(backend="mock", rc=0, command="uptime", stdout="", stderr="")]

    host.exec_command("uptime", wait=1)

    host.backend.disconnect.assert_called_once_with()
    host.backend.connect.assert_called_once_with()


class CountsConnects(ParamikoBackend):
    """A ParamikoBackend that adds its own step to connect()."""

    def connect(self):
        self.__dict__["connects"] = self.__dict__.get("connects", 0) + 1
        return super().connect()


def test_a_backend_s_own_connect_opens_its_first_session_and_every_new_one(sshd, host):
    host.backend = CountsConnects(hostname=VM_IP, port=22, username="root", password="secret")

    host.exec_command("true")
    host.reconnect("lost")

    assert host.backend.connects == 2
    assert len(sshd.connects) == 2


class OwnExecCommand(ParamikoBackend):
    """A ParamikoBackend that sends commands itself."""

    def exec_command(self, command, command_timeout=None, **kwargs):
        stdin, stdout, stderr = self.client.exec_command(command)
        return CommandResult(backend="own", rc=stdout.channel.recv_exit_status(), command=command,
                             stdout=stdout.read().decode(), stderr="")


def test_a_backend_with_its_own_exec_command_is_reconnected_when_its_session_dies(sshd, host):
    host.backend = OwnExecCommand(hostname=VM_IP, port=22, username="root", password="secret")
    host.exec_command("true")
    sshd.drop()

    res = host.exec_command("uptime", wait=1)

    assert res.rc == 0
    assert len(sshd.connects) == 2


def test_a_command_cut_off_by_disconnect_returns_minus_one_and_is_not_resent(sshd, host):
    sent = threading.Event()

    def hold(command):
        sent.set()
        return CutOff()

    sshd.answers = [("sleep", hold)]
    host.exec_command("true")
    thread, outcome = in_thread(lambda: host.exec_command("sleep 600", wait=1))
    assert sent.wait(5)

    host.backend.disconnect()
    thread.join(10)

    assert outcome[0].rc == -1, "a caller may disconnect to stop a command; that is its answer"
    assert sshd.ran == ["true", "sleep 600"]


def test_refusals_that_persist_on_one_connection_are_cured_by_a_new_one(sshd, host):
    # A session slot stays taken while its command still runs remotely, after a timeout too.
    sshd.answers = [("uptime", lambda command: paramiko.ChannelException(2, "Connect failed")
                     if len(sshd.connects) == 1 else Reply())]
    host.exec_command("true")

    res = host.exec_command("uptime", wait=1)

    assert res.rc == 0
    assert len(sshd.connects) == 2


def test_a_refusal_that_used_up_its_retries_leaves_the_next_command_a_working_session(sshd, host):
    sshd.answers = [("uptime", lambda command: paramiko.ChannelException(2, "Connect failed")
                     if len(sshd.connects) == 1 else Reply())]
    host.exec_command("true")
    with pytest.raises(Exception, match="even with reconnect"):
        host.exec_command("uptime", retries=0)

    assert host.exec_command("uptime", retries=0).rc == 0
    assert len(sshd.connects) == 2


def test_a_test_may_mark_the_transport_dead_itself(sshd, host):
    host.exec_command("true")
    sshd.clients[0].get_transport.return_value.is_active.return_value = False
    sshd.answers = [("uptime", reply_once_then(paramiko.SSHException("Unable to open channel."), Reply()))]

    res = host.exec_command("uptime", wait=1)

    assert res.rc == 0
    assert len(sshd.connects) == 2, "an open that fails on a dead transport is no refusal"


def test_threads_that_waited_on_a_connect_that_failed_fail_with_it(sshd, host):
    connecting, go = threading.Event(), threading.Event()

    def slow_then_refused(kwargs):
        connecting.set()
        go.wait(5)
        return False

    sshd.accepts = slow_then_refused
    runs = [in_thread(lambda: host.backend.client)]
    assert connecting.wait(5)
    runs += [in_thread(lambda: host.backend.client) for _ in range(2)]
    # Real time: the two wait on the connect in progress; a loaded machine still starts two threads in a second.
    threading.Event().wait(1)
    go.set()
    for thread, _ in runs:
        thread.join(10)

    assert [type(outcome[0]).__name__ for _, outcome in runs] == ["AuthenticationException"] * 3
    assert len({id(outcome[0]) for _, outcome in runs}) == 3, "one error object raised in three threads mixes their tracebacks"
    assert len(sshd.connects) == 1, "a dead host would hold each waiting thread for its own handshake in turn"
    sshd.accepts = lambda kwargs: True
    assert host.backend.client is sshd.clients[-1], "a thread that comes later connects itself"


def test_the_unreachable_latch_leaves_a_session_opened_since_alone(sshd, host):
    host.exec_command("true")
    sshd.drop()
    sshd.reachable = False
    with pytest.raises(Exception, match="even with reconnect"):
        host.exec_command("uptime", retries=1)
    sshd.reachable = True
    reopened = in_thread(lambda: (host.backend.disconnect(), host.backend.client))[0]
    reopened.join(10)

    host.exec_command("uptime")

    assert len(sshd.connects) == 2, "closing the session another thread opened cuts off its commands"
    assert sshd.clients[-1] in sshd.open_sessions


def test_copies_sharing_a_backend_reconnect_it_once(sshd, host):
    together = threading.Barrier(2, timeout=5)
    lock, calls = threading.Lock(), []

    def lost_for_the_first_two(command):
        with lock:
            calls.append(command)
            lost = len(calls) <= 2
        if lost:
            together.wait()
            return paramiko.SSHException("Channel closed.")
        return Reply()

    def slow_reconnect(kwargs):
        # Real time: the other copy decides while the first reconnect is under way.
        if len(sshd.connects) == 2:
            threading.Event().wait(0.3)
        return True

    sshd.answers = [("uptime", lost_for_the_first_two)]
    host.exec_command("true")
    sshd.accepts = slow_reconnect
    twin = copy.copy(host)

    runs = [in_thread(lambda c=c: c.exec_command("uptime", wait=1)) for c in (host, twin)]
    for thread, _ in runs:
        thread.join(10)

    assert [outcome[0].rc for _, outcome in runs] == [0, 0]
    assert len(sshd.connects) == 2


def test_a_command_started_while_there_was_no_session_leaves_a_session_since_replaced_alone(sshd, host):
    def lost_while_another_thread_reconnects(command):
        if len(sshd.clients) == 2:
            other, _ = in_thread(lambda: host.reconnect("another thread lost the session"))
            other.join(10)
            return paramiko.SSHException("Channel closed.")
        return Reply()

    sshd.answers = [("uptime", lost_while_another_thread_reconnects)]
    host.exec_command("true")
    host.backend.disconnect()

    res = host.exec_command("uptime", wait=1)

    assert res.rc == 0
    assert len(sshd.connects) == 3, "the session this command ran on was replaced already: one more reconnect cuts the next"


def test_a_command_on_a_session_that_died_by_itself_returns_minus_one_though_another_thread_reconnects(sshd, host):
    sent = threading.Event()

    def hold(command):
        sent.set()
        return CutOff()

    sshd.answers = [("reboot", hold)]
    host.exec_command("true")
    thread, outcome = in_thread(lambda: host.exec_command("reboot", wait=1))
    assert sent.wait(5)

    sshd.drop()
    host.reconnect("another thread saw the session die")
    thread.join(10)

    assert outcome[0].rc == -1, "a reboot ended its own session: sending it again would reboot twice"
    assert sshd.ran.count("reboot") == 1


def test_a_mock_with_the_spec_of_paramiko_backend_is_reconnected_as_any_backend(sshd, host, mocker):
    host.backend = mocker.Mock(spec=ParamikoBackend)

    host.reconnect("lost")

    host.backend.disconnect.assert_called_once_with()
    host.backend.connect.assert_called_once_with()


def test_a_connect_called_on_a_session_in_use_that_fails_leaves_that_session_in_place(sshd, host):
    host.exec_command("true")
    in_use = sshd.clients[0]
    sshd.accepts = lambda kwargs: False

    with pytest.raises(paramiko.AuthenticationException):
        host.backend.connect()

    assert host.backend._con is in_use and in_use in sshd.open_sessions, "other threads still run commands on it"


def test_a_thread_whose_command_failed_leaves_alone_a_session_another_is_opening(sshd, host, clock):
    handshaking = threading.Event()

    def slow_until_the_failed_thread_decided(kwargs):
        if len(sshd.connects) == 2:
            handshaking.set()
            # The failed thread has decided once it waits for its retry (the test clock's sleep).
            for _ in range(200):
                if clock.slept:
                    break
                threading.Event().wait(0.01)
        return True

    def lost_while_another_thread_reopens(command):
        if len(sshd.connects) == 1:
            in_thread(lambda: (host.backend.disconnect(), host.backend.client))
            assert handshaking.wait(5)
            return paramiko.SSHException("Channel closed.")
        return Reply()

    sshd.answers = [("uptime", lost_while_another_thread_reopens)]
    host.exec_command("true")
    sshd.accepts = slow_until_the_failed_thread_decided

    res = host.exec_command("uptime", wait=1)

    assert res.rc == 0
    assert len(sshd.connects) == 2, "the session the other thread opened is closed right after"


def latch_with_no_session(sshd, host, clock):
    """Every attempt fails before a session opens, as on a host still booting: the latch has no session."""
    host.exec_command("true")
    sshd.boot_at = clock.now + 10_000
    sshd.drop()
    with pytest.raises(Exception, match="even with reconnect"):
        host.exec_command("uptime", retries=2, wait=1)
    sshd.boot_at = 0
    reopened, _ = in_thread(lambda: host.backend.client)
    reopened.join(10)
    return host.backend._con


def test_the_unreachable_latch_without_a_session_leaves_one_opened_since_alone(sshd, host, clock):
    opened_since = latch_with_no_session(sshd, host, clock)

    host.exec_command("uptime")

    assert host.backend._con is opened_since, "closing it cuts off the commands running on it"


def test_a_copy_of_a_latched_connection_leaves_a_session_opened_since_alone(sshd, host, clock):
    opened_since = latch_with_no_session(sshd, host, clock)

    copy.copy(host).exec_command("uptime")

    assert host.backend._con is opened_since


def test_threads_refused_together_by_slots_nothing_of_theirs_holds_reconnect(sshd, host, clock):
    together = threading.Barrier(2, timeout=5)

    def refused_on_the_first_session(command):
        if len(sshd.connects) == 1:
            with contextlib.suppress(threading.BrokenBarrierError):
                together.wait()
            return paramiko.ChannelException(1, "open failed")
        return Reply()

    sshd.answers = [("uptime", refused_on_the_first_session)]
    host.exec_command("true")
    # Real time between attempts too, as `wait` gives: a retry does not start while the other is still refused.
    time.sleep.side_effect = lambda seconds: (clock.sleep(seconds), threading.Event().wait(0.05))

    runs = [in_thread(lambda: host.exec_command("uptime", retries=3, wait=1)) for _ in range(2)]
    for thread, _ in runs:
        thread.join(20)

    assert [getattr(outcome[0], "rc", outcome[0]) for _, outcome in runs] == [0, 0], (
        "a thread that is only being refused holds no slot: counting it as load keeps the session refused")
    assert len(sshd.connects) == 2


def test_a_copy_started_while_there_was_no_session_leaves_a_session_since_replaced_alone(sshd, host, scp_client):
    def broken_while_another_thread_reconnects(*args, **kwargs):
        if scp_client.return_value.put.call_count == 1:
            other, _ = in_thread(lambda: host.reconnect("another thread lost the session"))
            other.join(10)
            raise scp.SCPException("Timeout waiting for scp response")

    scp_client.return_value.put.side_effect = broken_while_another_thread_reconnects
    host.exec_command("true")
    host.backend.disconnect()

    host.send_files(local_path="/tmp/a.sh", remote_path="/tmp/")

    assert len(sshd.connects) == 3, "the session this copy ran on was replaced already: one more reconnect cuts the next"


def test_a_command_cut_off_on_its_last_attempt_returns_minus_one(sshd, host):
    sent = threading.Event()

    def hold(command):
        sent.set()
        return CutOff()

    sshd.answers = [("sleep", hold)]
    host.exec_command("true")
    thread, outcome = in_thread(lambda: host.exec_command("sleep 600", retries=1))
    assert sent.wait(5)

    host.reconnect("another thread lost the session")
    thread.join(10)

    assert getattr(outcome[0], "rc", outcome[0]) == -1, "with no attempt left, -1 is the answer, as before"


def test_a_cut_off_command_without_a_timeout_is_resent_too(sshd, host):
    sent = threading.Event()

    def cut_off_first(command):
        sent.set()
        return CutOff() if len(sshd.clients) == 1 else Reply(out=MD5)

    sshd.answers = [("md5sum", cut_off_first)]
    host.exec_command("true")
    thread, outcome = in_thread(lambda: host.exec_command("md5sum /srv/app.zip", wait=1, command_timeout=0))
    assert sent.wait(5)

    host.reconnect("another thread lost the session")
    thread.join(10)

    assert getattr(outcome[0], "stdout", outcome[0]) == MD5.decode()
    assert len(sshd.connects) == 2, "the command waited for its session to close, as a real channel does"


def test_a_failed_handshake_closes_its_client(sshd, host):
    sshd.accepts = lambda kwargs: False

    with pytest.raises(Exception):
        host.exec_command("true", retries=1)

    sshd.clients[0].close.assert_called_with()
    assert host.backend._con is None


def test_a_missing_local_file_fails_the_copy_without_a_new_session(sshd, host, scp_client):
    scp_client.return_value.put.side_effect = FileNotFoundError(
        2, "No such file or directory", "/tmp/nope.sh")
    host.exec_command("true")
    first = sshd.clients[-1]

    with pytest.raises(FileNotFoundError):
        host.send_files(local_path="/tmp/nope.sh", remote_path="/tmp/")

    assert sshd.clients == [first] and first in sshd.open_sessions


def test_disconnect_closes_the_session_and_the_next_command_opens_a_new_one(sshd, host):
    host.exec_command("uptime")
    first = sshd.clients[-1]

    host.backend.disconnect()

    assert first not in sshd.open_sessions
    host.exec_command("uptime")
    assert sshd.clients[-1] is not first and sshd.clients[-1] in sshd.open_sessions


def test_disconnecting_a_connection_that_never_connected_opens_none(sshd, host):
    host.backend.disconnect()

    assert sshd.connects == []


MD5 = b"d41d8cd98f00b204e9800998ecf8427e  /srv/app.zip\n"


def in_thread(call):
    """Start `call` in a thread; the returned list gets its result or exception."""
    outcome = []

    def run():
        try:
            outcome.append(call())
        except Exception as e:
            outcome.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_a_command_cut_off_by_another_threads_reconnect_is_resent_on_the_new_session(sshd, host):
    sent = threading.Event()

    def cut_off_first(command):
        sent.set()
        return CutOff() if len(sshd.clients) == 1 else Reply(out=MD5)

    sshd.answers = [("md5sum", cut_off_first)]
    host.exec_command("true")
    thread, outcome = in_thread(lambda: host.exec_command("md5sum /srv/app.zip", wait=1))
    assert sent.wait(5)

    host.reconnect("another thread lost the session")
    thread.join(10)

    assert len(outcome) == 1 and not isinstance(outcome[0], Exception), outcome
    assert (outcome[0].rc, outcome[0].stdout) == (0, MD5.decode()), (
        "a cut-off command has no exit status and no output: -1 and '' are no answer")
    assert sshd.ran == ["true", "md5sum /srv/app.zip", "md5sum /srv/app.zip"]
    assert len(sshd.connects) == 2, "the other thread's new session is used, not replaced again"


def test_a_command_on_a_dead_session_reconnects_and_is_sent_on_the_new_one(sshd, host, clock):
    host.exec_command("true")
    sshd.drop()

    res = host.exec_command("uptime", wait=7)

    assert res.rc == 0
    assert sshd.ran == ["true", "uptime"]
    assert len(sshd.connects) == 2
    assert 7 in clock.slept


def test_a_thread_still_reconnects_when_the_session_another_thread_opened_is_dead_too(sshd, host, clock):
    def lost_then_replaced_by_a_dead_one(command):
        if len(sshd.connects) == 1:
            other, _ = in_thread(lambda: (host.reconnect("another thread lost the session"), sshd.drop()))
            other.join(10)
            return paramiko.SSHException("Channel closed.")
        return Reply()

    sshd.answers = [("uptime", lost_then_replaced_by_a_dead_one)]
    host.exec_command("true")

    res = host.exec_command("uptime", wait=1)

    assert res.rc == 0
    assert len(sshd.connects) == 3, "a reconnect by another thread does not exempt this one from the next"
    assert clock.slept == [1, 1], "the replaced session is left alone, the dead new one is reconnected"


def test_a_session_another_thread_reopened_after_a_failed_reconnect_is_left_alone(sshd, host):
    def lost_while_another_thread_reopened(command):
        if len(sshd.connects) == 1:
            # One thread's reconnect failed and left no session; a third opened one on its next command.
            other, _ = in_thread(lambda: (host.backend.disconnect(), host.backend.client))
            other.join(10)
            return paramiko.SSHException("Channel closed.")
        return Reply()

    sshd.answers = [("uptime", lost_while_another_thread_reopened)]
    host.exec_command("true")

    res = host.exec_command("uptime", wait=1)

    assert res.rc == 0
    assert len(sshd.connects) == 2, "the session the third thread opened is in use: closing it cuts off its commands"


def test_a_thread_whose_command_failed_does_not_wait_for_another_threads_reconnect(sshd, host):
    checking, done = threading.Event(), threading.Event()

    def slow_check(command):
        if len(sshd.connects) == 2:
            checking.set()
            done.wait(5)
        return Reply(out=b"web-1\n")

    sshd.answers = [("hostname", slow_check),
                    ("uptime", reply_once_then(paramiko.SSHException("Channel closed."), Reply()))]
    host.exec_command("true")
    reconnecting, _ = in_thread(lambda: host.reconnect("lost"))
    assert checking.wait(5)

    started = time.monotonic()
    res = host.exec_command("uptime", wait=1)
    waited = time.monotonic() - started
    done.set()
    reconnecting.join(10)

    assert res.rc == 0
    assert waited < 2, "waiting on a reconnect that may hang for an hour serializes every thread behind it"


def test_threads_that_find_the_unreachable_latch_set_reconnect_once(sshd, host):
    host.exec_command("true")
    sshd.drop()
    sshd.reachable = False
    with pytest.raises(Exception, match="even with reconnect"):
        host.exec_command("uptime", retries=1)
    sshd.reachable = True
    handshaking, asked = threading.Event(), threading.Event()

    def slow_handshake(kwargs):
        if len(sshd.connects) == 2:
            handshaking.set()
            asked.wait(5)
            threading.Event().wait(0.2)
        return True

    sshd.accepts = slow_handshake
    first, outcome = in_thread(lambda: host.exec_command("uptime", wait=1))
    assert handshaking.wait(5)

    asked.set()
    res = host.exec_command("uptime", wait=1)
    first.join(10)

    assert res.rc == 0 and outcome[0].rc == 0
    assert len(sshd.connects) == 2, "the second reconnect closes the session the first just opened"


def test_a_refusal_on_a_session_that_then_dies_still_ends_in_a_reconnect(sshd, host):
    # paramiko keeps one pending open error per transport: a dying one may surface as a refusal.
    calls = []

    def refused_as_the_session_dies(command):
        calls.append(command)
        if len(calls) == 1:
            sshd.drop()
            return paramiko.ChannelException(2, "Connect failed")
        return Reply()

    sshd.answers = [("uptime", refused_as_the_session_dies)]
    host.exec_command("true")

    res = host.exec_command("uptime", wait=1)

    assert res.rc == 0
    assert len(sshd.connects) == 2


def busy(sshd, host, refusal, refusals=2, threads=2):
    """`threads` long commands hold the session; "uptime" is refused `refusals` times, then ends them."""
    running, calls = Running(), []

    def refuse_then_end_them(command):
        calls.append(command)
        if len(calls) <= refusals:
            return refusal
        running.finish()
        return Reply()

    sshd.answers = [("uptime", refuse_then_end_them), ("sleep", running)]
    host.exec_command("true")
    holders = [in_thread(lambda: host.exec_command("sleep 30", wait=1)) for _ in range(threads)]
    for _ in range(500):
        if len(running.channels) >= threads:
            break
        threading.Event().wait(0.01)
    return holders


@pytest.mark.parametrize("refusal", [
    paramiko.ChannelException(2, "Connect failed"),
    # paramiko keeps one open error per transport: of two refusals at once, one reads like this.
    paramiko.SSHException("Unable to open channel."),
], ids=["refused", "error-taken-by-another-refusal"])
def test_a_session_refused_while_other_commands_hold_its_slots_is_not_closed(sshd, host, refusal):
    holders = busy(sshd, host, refusal)

    res = host.exec_command("uptime", wait=1)
    for thread, _ in holders:
        thread.join(10)

    assert res.rc == 0
    assert [outcome[0].rc for _, outcome in holders] == [0, 0]
    assert len(sshd.connects) == 1, "the other commands hold the slots: closing the session would cut them off"


def test_an_open_that_fails_on_a_dead_transport_while_others_run_reconnects_at_once(sshd, host, clock):
    running, calls = Running(), []

    def dies_while_opening(command):
        calls.append(command)
        if len(calls) == 1:
            sshd.drop()
            return paramiko.SSHException("Unable to open channel.")
        running.finish()
        return Reply()

    sshd.answers = [("uptime", dies_while_opening), ("sleep", running)]
    host.exec_command("true")
    holder, _ = in_thread(lambda: host.exec_command("sleep 30", wait=1))
    for _ in range(500):
        if running.channels:
            break
        threading.Event().wait(0.01)

    res = host.exec_command("uptime", wait=1)
    holder.join(10)

    assert res.rc == 0
    assert clock.slept.count(1) == 1, "a dead transport is no busy one: reconnected on the first failure"


def test_a_copy_refused_while_other_commands_hold_the_slots_keeps_the_session(sshd, host, scp_client):
    holders = busy(sshd, host, Reply(), refusals=0, threads=1)
    put = scp_client.return_value.put
    put.side_effect = [paramiko.ChannelException(2, "Connect failed"), None]

    host.send_files(local_path="/tmp/a.sh", remote_path="/tmp/")
    host.exec_command("uptime")
    for thread, _ in holders:
        thread.join(10)

    assert put.call_count == 2
    assert len(sshd.connects) == 1


def test_commands_refused_to_the_end_while_others_ran_leave_no_unreachable_latch(sshd, host):
    holders = busy(sshd, host, paramiko.ChannelException(2, "Connect failed"), refusals=1)
    with pytest.raises(Exception, match="even with reconnect"):
        host.exec_command("uptime", retries=1)

    host.exec_command("uptime", wait=1)
    for thread, _ in holders:
        thread.join(10)

    assert len(sshd.connects) == 1, "a busy host is no unreachable one: the next command reconnects for nothing"


def test_an_open_that_fails_on_a_dead_transport_reconnects_at_once(sshd, host, clock):
    calls = []

    def dies_while_opening(command):
        calls.append(command)
        if len(calls) == 1:
            sshd.drop()
            return paramiko.SSHException("Unable to open channel.")
        return Reply()

    sshd.answers = [("uptime", dies_while_opening)]
    host.exec_command("true")

    res = host.exec_command("uptime", wait=1)

    assert res.rc == 0
    assert clock.slept == [1], "reconnected on the first failure, not after a retry on the dead session"


def test_a_reconnect_lets_no_other_thread_open_a_session_of_its_own_in_between(sshd, host):
    host.exec_command("true")
    first = sshd.clients[0]
    closing, asked = threading.Event(), threading.Event()
    close = first.close.side_effect

    def slow_close():
        closing.set()
        asked.wait(5)
        # Real time: paramiko's close() waits for its transport thread.
        threading.Event().wait(0.2)
        close()

    first.close.side_effect = slow_close
    reconnecting, _ = in_thread(lambda: host.reconnect("lost"))
    assert closing.wait(5)

    asked.set()
    res = host.exec_command("uptime", wait=1)
    reconnecting.join(10)

    assert res.rc == 0
    assert len(sshd.connects) == 2
    assert len(sshd.open_sessions) == 1, "a session opened in between is left open, never closed"


def test_other_threads_use_a_new_session_while_its_first_check_still_runs(sshd, host):
    checking, done = threading.Event(), threading.Event()

    def slow_check(command):
        checking.set()
        done.wait(5)
        return Reply(out=b"web-1\n")

    sshd.answers = [("hostname", slow_check)]
    connecting, _ = in_thread(lambda: host.exec_command("true"))
    assert checking.wait(5)

    started = time.monotonic()
    res = host.exec_command("uptime", wait=1)
    waited = time.monotonic() - started
    done.set()
    connecting.join(10)

    assert res.rc == 0
    assert waited < 2, "the check of a connected session must not hold every other thread"


def test_a_command_that_ends_its_own_session_returns_minus_one_and_is_not_resent(sshd, host):
    sshd.answers = [("reboot", Reply(rc=-1))]

    res = host.exec_command("reboot")

    assert res.rc == -1
    assert sshd.ran == ["reboot"], "a reboot must not be sent again"
    assert len(sshd.connects) == 1


def test_a_session_refused_with_no_other_command_running_is_reconnected(sshd, host, clock):
    sshd.answers = [("uptime", reply_once_then(
        paramiko.ChannelException(2, "Connect failed"), Reply(out=b" 10:00:00 up 1 day\n")))]
    host.exec_command("true")

    res = host.exec_command("uptime", wait=7)

    assert res.rc == 0
    assert len(sshd.connects) == 2, "nothing of this connection holds the slots: a new one frees them"
    assert 7 in clock.slept


def test_threads_whose_commands_fail_on_one_lost_session_reconnect_it_once(sshd, host):
    lock, calls = threading.Lock(), []
    together = threading.Barrier(2, timeout=5)

    def lost_for_the_first_two(command):
        with lock:
            calls.append(command)
            lost = len(calls) <= 2
        if lost:
            together.wait()
            return paramiko.SSHException("Channel closed.")
        return Reply()

    sshd.answers = [("uptime", lost_for_the_first_two)]
    host.exec_command("true")

    runs = [in_thread(lambda: host.exec_command("uptime", wait=1)) for _ in range(2)]
    for thread, _ in runs:
        thread.join(10)

    assert [outcome[0].rc for _, outcome in runs] == [0, 0], [o for _, o in runs]
    assert len(sshd.connects) == 2, (
        "one lost session, one reconnect: a second one would close the session the first just opened")


def test_a_command_sent_while_another_thread_reconnects_waits_for_the_new_session(sshd, host, clock):
    handshaking, asked = threading.Event(), threading.Event()

    def slow_handshake(kwargs):
        if len(sshd.connects) == 2:
            handshaking.set()
            asked.wait(5)
            # Real time: time.sleep is the test clock's.
            threading.Event().wait(0.2)
        return True

    host.exec_command("true")
    sshd.accepts = slow_handshake
    reconnecting, _ = in_thread(lambda: host.reconnect("lost"))
    assert handshaking.wait(5)

    asked.set()
    res = host.exec_command("uptime", wait=1)
    reconnecting.join(10)

    assert res.rc == 0
    assert sshd.ran == ["true", "uptime"]
    assert clock.slept == [], "a session still in its handshake is no session to send on: the first try failed"
    assert len(sshd.connects) == 2
