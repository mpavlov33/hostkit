"""Connection.run_wait: a long command run detached in tmux or nohup, polled for its rc file.

The double is hostkit.testing.Sshd answering with RemoteShell: the host's files and one job.
"""
import re

import pytest

from hostkit.testing import Reply

SECRET ="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class RemoteShell:
    """The host side of a detached command: the files the runner uses and one job.

    Once typed, the job has stream files; `runtime` seconds later (None: never), output and rc.
    """

    def __init__(self, clock, uploads, runtime=12, rc=0, out="", err="",
                 starts=True):
        self.clock = clock
        self.uploads = uploads
        self.runtime = runtime
        self.rc = rc
        self.out = out
        self.err = err
        self.starts = starts
        self.files = {}
        self.dirs = set()
        self.typed = []
        self.sourced = []
        self.removed = []
        self.job = None

    def __call__(self, command):
        self._advance()
        for pattern, handler in (
                (r"mkdir -p (\S+)$", self._mkdir),
                (r"test -e (\S+)$", self._test),
                (r"cat -- (\S+) \| tail -n 20000$", self._cat),
                (r'stat --printf="%s" (\S+)$', self._stat),
                (r"rm -rf (\S+)$", self._rm),
                (r'echo "(export .*)" > (\S+)$', self._write),
                (r"tmux send-keys 'source (\S+)' C-m$", self._source),
                (r"tmux send-keys '(.*)' C-m$", self._type),
                (r"nohup echo \$\$; .*bash (\S+) </dev/null", self._nohup)):
            match = re.match(pattern, command)
            if match:
                return handler(*match.groups())
        return Reply(0)

    def _start(self, text):
        found = lambda pattern: (re.search(pattern, text) or [None, None])[1]
        self.job = {
            "start": self.clock.now,
            "stdout": found(r">\s*(\S+/stdout)\b"),
            "stderr": found(r"2>\s*(\S+/stderr)\b"),
            "rc": found(r"echo \$\S+ > (\S+/rc)\b"),
        }
        self._advance()

    def _advance(self):
        job = self.job
        if not job or not self.starts:
            return
        self.files.setdefault(job["stdout"], "")
        self.files.setdefault(job["stderr"], "")
        done = (self.runtime is not None
                and self.clock.now >= job["start"] + self.runtime)
        if done and job["rc"] and job["rc"] not in self.files:
            self.files[job["stdout"]] = self.out
            self.files[job["stderr"]] = self.err
            self.files[job["rc"]] = "{0}\n".format(self.rc)

    def _mkdir(self, path):
        self.dirs.add(path)
        return Reply(0)

    def _test(self, path):
        return Reply(0 if path in self.files or path in self.dirs else 1)

    def _cat(self, path):
        if path not in self.files:
            return Reply(1, b"", "cat: {0}: No such file or directory\n"
                         .format(path).encode())
        return Reply(0, self.files[path].encode())

    def _stat(self, path):
        return Reply(0, str(len(self.files.get(path, ""))).encode())

    def _rm(self, path):
        self.removed.append(path)
        for name in [f for f in self.files if f.startswith(path)]:
            del self.files[name]
        return Reply(0)

    def _write(self, text, path):
        self.files[path] = text + "\n"
        return Reply(0)

    def _source(self, path):
        self.sourced.append(path)
        return Reply(0)

    def _type(self, line):
        self.typed.append(line)
        self._start(line)
        return Reply(0)

    def _nohup(self, wrapper):
        script = self.uploads[wrapper]
        self.typed.append(script)
        self._start(script)
        return Reply(0, b"4242\n")


def vm(sshd, clock, **job):
    """Answer every command with a RemoteShell running one job, and return that shell."""
    shell = RemoteShell(clock, sshd.uploads, **job)
    sshd.answers = [("", shell)]
    return shell


def test_the_line_typed_into_tmux_bounds_the_command_and_captures_its_streams(
        sshd, clock, host):
    shell = vm(sshd, clock)

    runner = host.run_wait("myappctl --update", timeout=600, nowait=True)

    line = shell.typed[-1]
    assert "timeout 600 myappctl --update > {0} 2> {1}".format(
        runner.stdout_file, runner.stderr_file) in line
    assert line.endswith("; echo $? > {0}".format(runner.rc_file)), (
        "the rc file is what the runner polls for")


def test_an_endless_command_writes_no_rc_file(sshd, clock, host):
    shell = vm(sshd, clock)

    host.run_wait("qemu-system-x86_64 -m 512 -nographic", timeout=86400,
                  endless_cmd=True, nowait=True)

    assert "echo $?" not in shell.typed[-1]


def test_credentials_reach_the_job_through_a_file_that_is_removed_afterwards(
        sshd, clock, host):
    shell = vm(sshd, clock, out="uploaded\n")

    runner = host.run_wait(
        "cd /root/packer; python3 start.py -i myapp-alma8", timeout=600,
        creds={"AWS_SECRET_ACCESS_KEY": SECRET})
    assert runner.result.rc == 0

    creds_writes = [c for c in sshd.ran if c.startswith('echo "export')]
    assert len(creds_writes) == 1 and SECRET in creds_writes[0]
    creds_file = creds_writes[0].rsplit("> ", 1)[1]
    assert shell.sourced == [creds_file], "the tmux shell sources the creds file"
    assert all(SECRET not in line for line in shell.typed), (
        "the secret must not be on the typed command line")
    assert creds_file not in shell.files, "the creds file is removed afterwards"


def test_a_long_command_ends_with_its_exit_status_and_output(sshd, clock, host):
    vm(sshd, clock, runtime=95, out="Updates applied\n")

    runner = host.run_wait("myappctl --update", timeout=600, iter_time=5)

    assert runner.err is None
    assert runner.result.rc == 0
    assert runner.result.stdout == "Updates applied\n"


def test_the_nohup_wrapper_runs_the_command_and_records_its_exit_status(
        sshd, clock, host):
    shell = vm(sshd, clock, runtime=30, out="Complete!\n")

    runner = host.run_wait("yum update -y", timeout=600, mode="nohup")

    assert runner.result.rc == 0 and runner.result.stdout == "Complete!\n"
    wrapper = shell.typed[-1]
    assert "yum update -y </dev/null" in wrapper
    assert "echo $exit_code >" in wrapper


@pytest.mark.parametrize("mode", ["tmux", "nohup"])
def test_a_failed_long_command_is_its_exit_status_and_streams_not_an_error(
        sshd, clock, host, mode):
    vm(sshd, clock, rc=2, out="cc -c src/elf.c\n", err="make: *** [src] Error 1\n")

    runner = host.run_wait("cd /root/myapp; make src", timeout=600, mode=mode)

    assert runner.err is None
    assert runner.result.rc == 2
    assert runner.result.stdout == "cc -c src/elf.c\n"
    assert "Error 1" in runner.result.stderr


def test_a_command_killed_by_its_remote_timeout_exits_124(sshd, clock, host):
    vm(sshd, clock, rc=124)

    runner = host.run_wait("pgbench -T 900 bench", timeout=600)

    assert runner.err is None and runner.result.rc == 124


def test_a_command_that_outlives_the_wait_is_a_timeout_and_reads_as_124(
        sshd, clock, host):
    vm(sshd, clock, runtime=None, out="partial log\n")
    started = clock.now

    runner = host.run_wait("./build.sh --package glibc", timeout=60, iter_time=5)

    assert isinstance(runner.err, TimeoutError)
    assert 60 <= clock.now - started <= 60 + 10 + 5, (
        "the runner gives up at timeout + 10 s, it does not wait forever")
    assert runner.result.rc == 124


def test_a_command_that_never_started_is_reported_after_two_minutes(
        sshd, clock, host):
    vm(sshd, clock, starts=False)
    started = clock.now

    runner = host.run_wait("myappctl --update", timeout=3600, iter_time=5)

    assert "probably not started" in str(runner.err)
    assert clock.now - started < 130


def test_nowait_returns_at_once_and_the_result_arrives_later(sshd, clock, host):
    vm(sshd, clock, runtime=300, out="done\n")
    started = clock.now

    runner = host.run_wait("pgbench -T 300 bench", timeout=600, nowait=True)

    assert clock.now == started, "nowait does not wait"
    assert runner.result is None, "still running"
    clock.now += 301
    assert runner.result.rc == 0 and runner.result.stdout == "done\n"


def test_the_runner_logs_under_the_hostkit_logger(sshd, clock, host, caplog):
    vm(sshd, clock)

    with caplog.at_level("INFO", logger="hostkit"):
        runner = host.run_wait("make src", timeout=600, nowait=True)

    assert any(r.name.startswith("hostkit.") and runner.unique_name in r.getMessage()
               for r in caplog.records)
