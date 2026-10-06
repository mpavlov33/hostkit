# hostkit

[![tests](https://github.com/mpavlov33/hostkit/actions/workflows/tests.yml/badge.svg)](https://github.com/mpavlov33/hostkit/actions/workflows/tests.yml)

A host over SSH (paramiko) or Docker, testinfra-style: query and change its files, processes, packages, services, docker, SELinux and sysctl, and run long commands detached in tmux or nohup.

Derived from [pytest-testinfra](https://github.com/pytest-dev/pytest-testinfra) (Apache-2.0); see `NOTICE`.

```python
import hostkit

host = hostkit.Connection(backend="paramiko", hostname="192.0.2.10", username="root",
                          key_filename="/root/.ssh/id_rsa", port=22)

host.file("/tmp/packages").exists                        # a query: a value, raises if it cannot answer
host.file("/root/*.rpm").remove(check=True)              # an action: exit status, check=True raises
host.process.pkill("openssl", signal="KILL", full=True)
host.package("openssl").is_installed
host.service("myapp").restart(check=True)
host.check_output("uname -r")
runner = host.run_wait("make src", timeout=600)          # tmux or nohup, polled until it ends
runner.result.rc, runner.result.stdout
```

- Paths go to the shell unquoted (globs work); data is shell-quoted.
- `hostkit.utils.strip_exports` cuts `export K='v';` prefixes, where credentials go, out of a command before it is logged.
- hostkit logs under the `hostkit` logger and prints nothing by itself; `hostkit.logging_wrap.set_colored_handler("hostkit")` turns colored output on.
- `hostkit.testing` has the doubles: `Sshd` (paramiko's side of a host), `Clock`, `Box` (a host on this machine's `sh`).

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e .[test]
.venv/bin/pytest
```
