# hostkit — project rules

## HARD RULES

1. **A docstring is short and universal.** One line says what the code does and what the caller gets; a public method may add a line or two for its contract (what it raises, what a flag changes) and one `>>>` example. It never names who calls it, which project, product, ticket, build, incident or test uses it, and never tells the history of a bug.
2. **A comment is one line and says why the code is as it is.** No history, no incident or build numbers, no caller names; what the code does is read from the code.
3. **Nothing project-specific anywhere** — code, docstrings, tests, README: no product, company, host, registry or bucket names, no real IP addresses. Examples use `myapp`, `example.com`, `web-1` and `192.0.2.x` (RFC 5737).
4. **An action takes `check=False` and returns the exit status; `check=True` raises with the tool's stderr.** A query returns a value and raises when it cannot answer.
5. **Paths go to the shell unquoted (globs rely on it); data is `shlex.quote`d.** Credentials only in an `export K='v';` prefix, which `utils.strip_exports` cuts from every error and log.
6. **A test is written only once you broke the code it covers and saw it red.** Doubles stand only where the process ends: paramiko (`testing.Sshd`), docker-py, the clock, the shell (`testing.Box`).
7. **The suite is green on the lowest and the latest dependency versions `pyproject.toml` allows** before a release.
8. **Files derived from pytest-testinfra keep their Apache-2.0 notice;** a new derived file gets one and is listed in `NOTICE`.
9. **A commit message is the gist, short:** what changed and why. No line numbers, no links.
