"""hostkit.Connection over paramiko: command timeouts, scp, disconnect and strip_exports.

The doubles are hostkit.testing.Sshd for the host's sshd and an autospecced scp.SCPClient.
"""
import time

import pytest
import scp

from hostkit.backends import SSH_COMMAND_TIMEOUT, CommandTimeout
from hostkit.testing import HANG
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
