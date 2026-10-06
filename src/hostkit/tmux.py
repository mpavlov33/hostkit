import contextlib
import pathlib
from dataclasses import dataclass

import time
import os
import uuid
from hostkit.backends import CommandResult, Connection
from hostkit.utils import cached_property
from datetime import datetime
import logging
from typing import Union

logger = logging.getLogger(__name__)


@dataclass
class CommandRunnerNoSession:
    host: Connection
    command: str
    logdir: str
    timeout: int
    creds: dict = None
    capture_stdout: bool = True
    endless_cmd: bool = False
    err: Union[Exception, None] = None
    exec_time: str = ""
    _result: Union[None, CommandResult] = None
    _start_time: float = None

    def __post_init__(self):
        logger.info(f"start cmd-session: {self.unique_name}")
        self.host.file(
            str(pathlib.PurePosixPath(self.logdir) / f"{self.unique_name}")
        ).mkdir()

    @cached_property
    def unique_name(self) -> str:
        return f"tmux{uuid.uuid4().hex[:6]}"

    @cached_property
    def creds_str(self) -> str:
        c = f"tmux_session={self.unique_name} "
        if self.creds:
            for k, v in self.creds.items():
                c += f"{k}='{v}' "
        self.host.exec_command(f"""echo "export {c}" > {self.creds_file}""")
        return c

    @cached_property
    def creds_file(self) -> str:
        return str(
            pathlib.PurePosixPath(self.logdir) / f"{self.unique_name}/creds"
        )

    @property
    def output_now(self) -> str:
        output = ""
        output += self.host.file(self.stdout_file).content[-1000:]
        output += self.host.file(self.stderr_file).content[-1000:]
        return output

    @cached_property
    def stdout_file(self) -> str:
        return str(
            pathlib.PurePosixPath(self.logdir) / f"{self.unique_name}/stdout"
        )

    @cached_property
    def stderr_file(self) -> str:
        return str(
            pathlib.PurePosixPath(self.logdir) / f"{self.unique_name}/stderr"
        )

    @cached_property
    def rc_file(self) -> str:
        return str(
            pathlib.PurePosixPath(self.logdir) / f"{self.unique_name}/rc"
        )

    @cached_property
    def bytes_file(self) -> str:
        return str(
            pathlib.PurePosixPath(self.logdir)
            / f"{self.unique_name}/bytes_read"
        )

    @cached_property
    def pid_file(self) -> str:
        return str(
            pathlib.PurePosixPath(self.logdir) / f"{self.unique_name}/pid"
        )

    @cached_property
    def wrapper_file(self) -> str:
        return str(
            pathlib.PurePosixPath(self.logdir)
            / f"{self.unique_name}/wrapper.sh"
        )

    @property
    def pid_nohup(self) -> int:
        n = 0
        while not self.host.file(self.pid_file).exists and n < 10:
            time.sleep(1)
            n += 1
        if n == 10:
            raise ValueError(f"{self.command}: pid not found")
        return int(self.host.file(self.pid_file).content)

    @property
    def bytes_shown(self) -> int:
        if not self.host.file(self.bytes_file).exists:
            return 0
        return int(self.host.file(self.bytes_file).content)

    @property
    def result(self) -> Union[CommandResult, None]:
        if self._result:
            return self._result

        if (
            time.time() >= self._start_time + self.timeout
            or self.host.file(self.rc_file).exists
        ):
            self.make_result()
        elif time.time() < self._start_time + self.timeout:
            logger.info(
                f"cmd '{self.command}' is probably running (timeout is not expired yet)"
            )
        else:
            logger.error(
                f"cmd '{self.command}': something went wrong (timeout expired but rc file not found)"
            )
        return self._result

    def exec_cmd_tmux_session(self):
        self._start_time = time.time()
        self.host.exec_command(f"tmux new-session -d -s {self.unique_name}")
        if self.creds_str:
            self.host.exec_command(
                "tmux send-keys 'source {}' C-m".format(self.creds_file)
            )
        timeout_prefix = (
            ""
            if self.command.startswith("cd ")
            else f"timeout {self.timeout} "
        )
        cmd_end = "" if self.endless_cmd else f"; echo $? > {self.rc_file}"
        self.host.exec_command(
            "tmux send-keys 'stdbuf -oL {}{} > {} 2> {}{}' C-m".format(
                timeout_prefix,
                self.command,
                self.stdout_file,
                self.stderr_file,
                cmd_end,
            )
        )

    def exec_cmd_nohup(self):
        self.create_bash_wrapper_nohup()
        self._start_time = time.time()
        self.host.exec_command(
            f"nohup echo $$; set -o pipefail; export {self.creds_str}; bash {self.wrapper_file} </dev/null > {self.wrapper_file}.stdout 2>&1&"
        )

    def exec_cmd(self, mode):
        self.exec_time = str(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        try:
            if mode == "tmux":
                self.exec_cmd_tmux_session()
            elif mode == "nohup":
                self.exec_cmd_nohup()
            else:
                raise ValueError("Unknown mode")
        except Exception as e:
            self.err = e

    def is_finish(self):
        return self.host.file(self.rc_file).exists

    def wait_finish(
        self,
        iter_time: int = 5,
        max_wait: int = 60,
        mode: str = "tmux",
        live_stdout: bool = True,
    ):
        def _iter_actions():
            if time.time() >= time_end:
                self.err = TimeoutError(f"'{self.command}': timeout")
                return
            time.sleep(iter_time)
            if time.time() - self._start_time >= 120 and (
                not self.host.file(self.stdout_file).exists
                or not self.host.file(self.stderr_file).exists
            ):
                logger.error(
                    f"cmd '{self.command}' probably not started for some reasons"
                )
                tmux_dir = self.host.exec_command(
                    f"ls -la {os.path.dirname(self.stdout_file)}"
                )
                self.err = Exception(
                    f"cmd '{self.command}' probably not started for some reasons, debug info:\n\n{str(tmux_dir)}"
                    f"\nStart time: {int(self._start_time)}\n Current time: {int(time.time())}\n"
                    f"Stdout exists: {str(self.host.file(self.stdout_file).exists)}\n"
                    f"Stderr exist: {str(self.host.file(self.stderr_file).exists)}"
                )
                return
            if live_stdout:
                with contextlib.suppress(Exception):
                    self.read_new_stdout()
            else:
                logger.info(
                    f"cmd '{self.command}' is running, live stdout is turned off, please wait..."
                )

        time_end = time.time() + max_wait
        while not self.host.file(self.rc_file).exists and not self.err:
            _iter_actions()
        if not self.host.file(self.rc_file).content and not self.err:
            raise FileNotFoundError("rc not found")

    def make_result(self) -> CommandResult:
        rc = -1
        if self.err and type(self.err) == TimeoutError:
            rc = 124
        elif self.host.file(self.rc_file).content:
            rc = int(self.host.file(self.rc_file).content)

        self._result = CommandResult(
            "paramiko",
            rc,
            self.command,
            stdout=(
                self.host.file(self.stdout_file).content
                if self.capture_stdout
                else ""
            ),
            stderr=(
                self.host.file(self.stderr_file).content
                if self.capture_stdout
                else str(self.err)
            ),
            exec_time=self.exec_time,
        )
        self.host.file(self.creds_file).remove()
        return self._result

    def read_new_stdout(self):
        if self.host.file(self.stdout_file).exists:
            c = f"""b=$(cat {self.stdout_file} | wc -c) && echo "$b" > {self.bytes_file} && tail -c $((b - {self.bytes_shown})) {self.stdout_file}"""
        elif self.host.file(self.stderr_file).exists:
            c = f"tail -n 50 {self.stderr_file}"
        else:
            return
        data = self.host.exec_command(c)
        if data.stdout and not data.rc:
            logger.info(data.output)

    def create_bash_wrapper_nohup(self):
        timeout_prefix = (
            ""
            if self.command.startswith("cd ")
            else f"timeout {self.timeout} "
        )
        wrapper = f"""
#!/bin/bash

set -o pipefail

{timeout_prefix} {self.command} </dev/null > {self.stdout_file} 2>{self.stderr_file} &
pid=$!
echo $pid > {self.pid_file}

wait $pid
exit_code=$?

echo $exit_code > {self.rc_file}
"""
        with self.host.backend.client.open_sftp() as sftp:
            with sftp.file(self.wrapper_file, "w") as remote_script:
                remote_script.write(wrapper)

    def move_stdout(self, new_path: str) -> CommandResult:
        if self.host.file(self.stderr_file).exists:
            return self.host.exec_command(
                f"cat {self.stdout_file} {self.stderr_file} >> {new_path}"
            )
        return self.host.exec_command(f"cat {self.stdout_file} >> {new_path}")

    def clean(self):
        self.host.file(
            str(pathlib.PurePosixPath(self.logdir) / f"{self.unique_name}")
        ).remove()
        self.host.exec_command(f"tmux kill-session -t {self.unique_name}")
