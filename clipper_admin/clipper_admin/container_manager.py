import abc
from .exceptions import ClipperException
import random
import socket

# Constants
CLIPPER_INTERNAL_QUERY_PORT = 1337
CLIPPER_INTERNAL_MANAGEMENT_PORT = 1338
CLIPPER_INTERNAL_RPC_PORT = 7000
CLIPPER_INTERNAL_METRIC_PORT = 1390

CLIPPER_DOCKER_LABEL = "ai.clipper.container.label"
CLIPPER_MODEL_CONTAINER_LABEL = "ai.clipper.model_container.label"
CLIPPER_QUERY_FRONTEND_CONTAINER_LABEL = "ai.clipper.query_frontend.label"
CLIPPER_MGMT_FRONTEND_CONTAINER_LABEL = "ai.clipper.management_frontend.label"
CLIPPER_QUERY_FRONTEND_ID_LABEL = "ai.clipper.query_frontend.id"
CONTAINERLESS_MODEL_IMAGE = "NO_CONTAINER"

# NOTE: we use '_' as the delimiter because kubernetes allows the use
# '_' in labels but not in deployment names. We force model names and
# versions to be compliant with both limitations, so this gives us an extra
# character to use when creating labels.
_MODEL_CONTAINER_LABEL_DELIMITER = "_"


def create_model_container_label(name, version):
    return "{name}{delim}{version}".format(
        name=name, delim=_MODEL_CONTAINER_LABEL_DELIMITER, version=version)


def parse_model_container_label(label):
    splits = label.split(_MODEL_CONTAINER_LABEL_DELIMITER)
    if len(splits) != 2:
        raise ClipperException(
            "Unable to parse model container label {}".format(label))
    return splits


def find_unbound_port(start=None, increment=False, port_range=(34256, 50000), verbose=False, logger=None):
    """
    Fina a unbound port.

    Parameters
    ----------
    start : int
        The port number to start with. If this port is unbounded, return this port.
        If None, start will be a random port.
    increment : bool
        If True, find port by incrementing start port; else, random search.
    port_range : tuple
        The range of port for random number generation
    verbose : bool
        Verbose flag for logging
    logger: logging.Logger
    """
    while True:
        if not start:
            start = random.randint(*port_range)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", start))
            # Make sure we clean up after binding
            del sock
            return start
        except socket.error as e:
            if verbose and logger:
                logger.info("Socket error: {}".format(e))
                logger.info(
                    "randomly generated port %d is bound. Trying again." % start)

        if increment:
            start += 1
        else:
            start = random.randint(*port_range)


class ContainerManager(object):
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def start_clipper(self, query_frontend_image, mgmt_frontend_image,
                      cache_size, num_frontend_replicas):
        # NOTE: An implementation of this interface should be connected to a running
        # Clipper instance when this method returns. ClipperConnection will not
        # call ContainerManager.connect() separately after calling start_clipper(), so
        # if there are additional steps needed to connect a Clipper cluster after it
        # has been started, the implementor of this interface should manage that internally.
        # For example, KubernetesContainerManager calls self.connect() at the end of
        # start_clipper().
        return

    @abc.abstractmethod
    def connect(self):
        return

    @abc.abstractmethod
    def deploy_model(self, name, version, input_type, image):
        return

    @abc.abstractmethod
    def get_num_replicas(self, name, version):
        return

    @abc.abstractmethod
    def set_num_replicas(self, name, version, input_type, image, num_replicas):
        return

    @abc.abstractmethod
    def get_logs(self, logging_dir):
        """Get the container logs for all Docker containers launched by Clipper.

            This will get the logs for both Clipper core containers and
            any model containers deployed by Clipper admin.
            Any previous log files from existing containers will be overwritten.

        Parameters
        ----------
        logging_dir : str
            The directory to write the log files to. If the directory
            does not exist, it will be created.

        Returns
        -------
        list(str)
            The list of all log files created.
        """
        return

    @abc.abstractmethod
    def stop_models(self, models):
        """Stops all replicas of the specified models.

        Parameters
        ----------
        models : dict(str, list(str))
            For each entry in the dict, the key is a model name and the value is a list of model
            versions. All replicas for each version of each model will be stopped.
        """
        return

    @abc.abstractmethod
    def stop_all_model_containers(self):
        return

    @abc.abstractmethod
    def stop_all(self):
        """Stop all resources associated with Clipper."""
        pass

    @abc.abstractmethod
    def get_admin_addr(self):
        return

    @abc.abstractmethod
    def get_query_addr(self):
        return
