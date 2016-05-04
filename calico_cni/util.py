# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import json
import sys
import logging
from cloghandler import ConcurrentRotatingFileHandler
from calico_cni.constants import *

from pycalico.datastore_errors import DataStoreError

_log = logging.getLogger("calico_cni")


def configure_logging(logger, log_level_file, log_level_stderr, filename):
    """Configures logging for given logger using the given filename.

    :return None.
    """
    # Create an IdentityFilter.
    identity = get_identifier()
    identity_filter = IdentityFilter(identity=identity)
    formatter = logging.Formatter(LOG_FORMAT)

    if log_level_file != "NONE":
        # If the logging directory doesn't exist, create it.
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR)

        # Determine path to log file.
        log_path = os.path.join(LOG_DIR, filename)

        # Create a log handler and formatter and apply to _log.
        handler = ConcurrentRotatingFileHandler(filename=log_path,
                                                maxBytes=1000000,
                                                backupCount=5)
        handler.addFilter(identity_filter)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(log_level_file)

    if log_level_stderr != "NONE":
        # Attach a stderr handler to the log.
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(log_level_stderr)
        stderr_handler.addFilter(identity_filter)
        stderr_handler.setFormatter(formatter)
        logger.addHandler(stderr_handler)


def parse_cni_args(cni_args):
    """Parses the given CNI_ARGS string into key value pairs
    and returns a dictionary containing the arguments.

    e.g "FOO=BAR;ABC=123" -> {"FOO": "BAR", "ABC": "123"}

    :param cni_args
    :return: args_to_return - dictionary of parsed cni args
    """
    # Dictionary to return.
    args_to_return = {}

    _log.debug("Parsing CNI_ARGS: %s", cni_args)
    for k, v in CNI_ARGS_RE.findall(cni_args):
        _log.debug("\tCNI_ARG: %s=%s", k, v)
        args_to_return[k.strip().upper()] = v.strip()
    _log.debug("Parsed CNI_ARGS: %s", args_to_return)
    return args_to_return


def print_cni_error(code, message, details=None):
    """Print an error response formatted according to the CNI spec.

    :param code: Error code to return (int)
    :param message: Short error message to return.
    :param details: Detailed error message to return.
    :return: None
    """
    error_response = {
        "cniVersion": "0.1.0",
        "code": code,
        "msg": message,
        "details": details
    }
    _log.exception("CNI Error:\n%s", json.dumps(error_response, indent=2))
    print(json.dumps(error_response))


def handle_datastore_error(func):
    """
    Decorator which handles errors connecting to etcd.
    """
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except DataStoreError, e:
            # Hit a datastore error - log and exit.
            print_cni_error(ERR_CODE_GENERIC, 
                            "Error accessing datastore", 
                            e.message)
            sys.exit(ERR_CODE_GENERIC)
    return wrapped


def get_identifier():
    """
    Returns an appropriate identifier for use in logging.

    For most orchestrators, this is the container ID.  For Kubernetes,
    this is the pod namespace/name.
    """
    cni_args = parse_cni_args(os.environ.get(CNI_ARGS_ENV, ""))
    if K8S_POD_NAME in cni_args:
        identifier = "%s/%s" % (cni_args.get(K8S_POD_NAMESPACE, "unknown"), 
                                cni_args.get(K8S_POD_NAME, "unknown"))
    else:
        identifier = os.environ.get(CNI_CONTAINERID_ENV, 
                                    "UnknownId")[:8]
    return identifier
    

class IdentityFilter(logging.Filter):
    """
    Filter class to impart contextual identity information onto loggers.
    """
    def __init__(self, identity):
        logging.Filter.__init__(self)
        self.identity = identity

    def filter(self, record):
        record.identity = self.identity
        return True


class CniError(Exception):
    def __init__(self, code, msg, details=""):
        self.code = code
        self.msg = msg
        self.details = details 

    def __str__(self):
        return "(%s) %s" % (self.code, self.msg)
