"""hostkit's Process.in_state: which processes on a host are in the given ps states.

The double is hostkit.testing.Box with a stand-in ``ps`` that prints a fixed procps table.
"""
import pytest

from hostkit.modules.process import BusyboxProcess, PosixProcess

from hostkit.testing import Box

# What procps prints for `ps -A -o pid= -o stat= -o args=`.
PS_TABLE = (
    "      1 Ss   /usr/lib/systemd/systemd --switched-root --system --deserialize 31\n"
    "      2 S    [kthreadd]\n"
    "    812 Ss   sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups\n"
    "   1203 Ssl  /usr/libexec/myapp/myapp-server\n"
    "   4242 T+   openssl s_server -accept 4433 -WWW\n"
    "   4243 Tl   /root/worker x 100\n"
    "   4244 Ts   /bin/bash /root/stress.sh /tmp/stress\n"
    "   4251 t    /root/worker y 100\n"
    "   4260 Z+   [worker] <defunct>\n"
    "   4261 Z    [myappctl] <defunct>\n"
    "   5150 S    /root/worker T Z t\n"
    "   5151 D    /usr/bin/rsync -a /var/log/myapp/ /tmp/logs\n"
    "   5152 R+   ps -A -o pid= -o stat= -o args=\n"
)


@pytest.fixture
def box(tmp_path):
    box = Box(tmp_path)
    (box.root / "ps-table").write_bytes(PS_TABLE.encode())
    box.stub("ps", 'cat "$BOX/ps-table"')
    return box


def test_the_stopped_and_the_dead_are_found_whatever_ps_appends_to_the_state(box):
    assert PosixProcess(box).in_state("T", "t", "Z") == {
        4242: "openssl s_server -accept 4433 -WWW",
        4243: "/root/worker x 100",
        4244: "/bin/bash /root/stress.sh /tmp/stress",
        4251: "/root/worker y 100",
        4260: "[worker] <defunct>",
        4261: "[myappctl] <defunct>",
    }
    assert box.calls == [("ps", "-A -o pid= -o stat= -o args=")]


def test_a_state_is_its_letter_alone_and_its_case_counts(box):
    process = PosixProcess(box)

    assert process.in_state("t") == {4251: "/root/worker y 100"}
    assert process.in_state("D") == {5151: "/usr/bin/rsync -a /var/log/myapp/ /tmp/logs"}


def test_no_process_in_the_states_is_an_empty_dict(box):
    assert PosixProcess(box).in_state("X", "W") == {}


def test_a_ps_that_failed_raises_with_its_stderr(box):
    """A failed ps is no answer, not an empty dict."""
    box.stub("ps", "echo 'error: unknown user-defined format specifier \"stat=\"' >&2; exit 1")

    with pytest.raises(Exception, match="non-zero exit code: error: unknown user-defined format"):
        PosixProcess(box).in_state("T", "t", "Z")


@pytest.mark.parametrize("states", [(), ("TtZ",)], ids=["none", "a-word"])
def test_states_that_are_not_single_letters_are_refused_not_matched(box, states):
    """A word would match no state and pass for an empty answer."""
    with pytest.raises(ValueError):
        PosixProcess(box).in_state(*states)

    assert box.calls == []


def test_busybox_ps_is_not_asked(box):
    with pytest.raises(NotImplementedError):
        BusyboxProcess(box).in_state("T")

    assert box.calls == []
