import shlex
from dataclasses import dataclass
import hostkit.backends
from hostkit.utils import act


@dataclass
class Docker:
    """Test and drive the docker daemon of a host

    Actions return docker's exit status; check=True raises with its stderr.
    """

    host: hostkit.backends.Connection

    @property
    def running_count(self) -> int:
        """Return how many containers are running

        >>> host.docker.running_count
        16
        """
        return int(
            self.host.check_output("docker ps -q | wc -l").strip() or 0
        )

    def has_image(self, name: str) -> bool:
        """Return True if the host has the image, by its exact name

        >>> host.docker.has_image("centos:centos7")
        True
        """
        # Only the exit status matters, so print the id rather than the whole JSON.
        return (
            self.host.exec_command(
                "docker image inspect -f '{{.Id}}' %s" % name, ignore_errors=True
            ).rc
            == 0
        )

    def pull(self, image: str, check: bool = False) -> int:
        return act(self.host, "docker pull %s" % image, check)

    def tag(self, src: str, dst: str, check: bool = False) -> int:
        return act(self.host, "docker tag %s %s" % (src, dst), check)

    def remove_image(self, image: str, check: bool = False) -> int:
        return act(self.host, "docker rmi -f %s" % image, check)

    def start(self, name: str, check: bool = False) -> int:
        return act(self.host, "docker start %s" % name, check)

    def remove_containers(self, name_filter: str = "", check: bool = False) -> int:
        """Remove every container, running or stopped, or those whose name matches

        Returns xargs's status: 0 also when nothing matched or docker ps failed,
        123 when a docker rm failed.

        >>> host.docker.remove_containers("^myapp-")
        0
        """
        name = " --filter %s" % shlex.quote("name=" + name_filter) if name_filter else ""
        return act(self.host, "docker ps -aq%s | xargs -r docker rm -f" % name, check)
