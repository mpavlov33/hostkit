import pytest

import hostkit
from hostkit.testing import VM_IP, Box, Clock, Sshd


@pytest.fixture
def clock(mocker):
    return Clock(mocker)


@pytest.fixture
def sshd(mocker, clock):
    return Sshd(mocker, clock)


@pytest.fixture
def host(sshd):
    """A hostkit.Connection to the VM behind sshd."""
    return hostkit.Connection(backend="paramiko", hostname=VM_IP,
                              username="root", password="secret", port=22,
                              key_filename="/opt/deploy/id_rsa")


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)
