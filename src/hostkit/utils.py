import re

# An export ends at a ';' outside quotes on its line; a quote with no pair is a plain char.
EXPORT = re.compile(r"""export(?:'[^'\n]*'|"[^"\n]*"|'(?![^'\n]*')|"(?![^"\n]*")|[^;'"\n])*;""")


def strip_exports(cmd: str) -> str:
    """Return the command without its `export ...;` statements, where the credentials are"""
    return EXPORT.sub("", cmd).lstrip()


class cached_property(object):
    def __init__(self, func):
        self.func = func

    def __get__(self, instance, cls=None):
        result = instance.__dict__[self.func.__name__] = self.func(instance)
        return result


def act(host, cmd: str, check: bool = False) -> int:
    """Return an action's exit status; with check=True, 0 or an exception carrying its stderr"""
    if check:
        host.check_output(cmd)
        return 0
    return host.exec_command(cmd, ignore_errors=True).rc
