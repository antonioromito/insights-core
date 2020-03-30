from __future__ import print_function
from __future__ import absolute_import
import sys
import logging
import logging.handlers
import os
import six

from .utilities import (generate_machine_id,
                        write_to_disk,
                        write_registered_file,
                        write_unregistered_file,
                        delete_registered_file,
                        delete_unregistered_file,
                        delete_cache_files,
                        determine_hostname)
from .support import registration_check
from .constants import InsightsConstants as constants
from .schedule import get_scheduler

NETWORK = constants.custom_network_log_level
LOG_FORMAT = ("%(asctime)s %(levelname)8s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def do_log_rotation():
    handler = get_file_handler()
    return handler.doRollover()


def get_file_handler(config):
    log_file = config.logging_file
    log_dir = os.path.dirname(log_file)
    if not log_dir:
        log_dir = os.getcwd()
    elif not os.path.exists(log_dir):
        os.makedirs(log_dir, 0o700)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, backupCount=3)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return file_handler


def get_console_handler(config):
    if config.silent:
        target_level = logging.FATAL
    elif config.verbose:
        target_level = logging.DEBUG
    elif config.net_debug:
        target_level = NETWORK
    elif config.quiet:
        target_level = logging.ERROR
    else:
        target_level = logging.INFO

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(target_level)

    log_format = LOG_FORMAT if config.verbose else "%(message)s"
    handler.setFormatter(logging.Formatter(log_format))

    return handler


def configure_level(config):
    config_level = 'NETWORK' if config.net_debug else config.loglevel
    config_level = 'DEBUG' if config.verbose else config.loglevel

    init_log_level = logging.getLevelName(config_level)
    if type(init_log_level) in six.string_types:
        print("Invalid log level %s, defaulting to DEBUG" % config_level)
        init_log_level = logging.DEBUG

    logger.setLevel(init_log_level)
    logging.root.setLevel(init_log_level)

    if not config.verbose:
        logging.getLogger('insights.core.dr').setLevel(logging.WARNING)


def set_up_logging(config):
    logging.addLevelName(NETWORK, "NETWORK")
    if len(logging.root.handlers) == 0:
        logging.root.addHandler(get_console_handler(config))
        logging.root.addHandler(get_file_handler(config))
        configure_level(config)
        logger.debug("Logging initialized")


# -LEGACY-
def register(config, pconn):
    """
    Do registration using basic auth
    """
    username = config.username
    password = config.password
    authmethod = config.authmethod
    auto_config = config.auto_config
    if not username and not password and not auto_config and authmethod == 'BASIC':
        logger.debug('Username and password must be defined in configuration file with BASIC authentication method.')
        return False
    return pconn.register()


# -LEGACY-
def _legacy_handle_registration(config, pconn):
    '''
    Handle the registration process
    Returns:
        True - machine is registered
        False - machine is unregistered
        None - could not reach the API
    '''
    logger.debug('Trying registration.')
    # force-reregister -- remove machine-id files and registration files
    # before trying to register again
    if config.reregister:
        delete_registered_file()
        delete_unregistered_file()
        write_to_disk(constants.machine_id_file, delete=True)
        logger.debug('Re-register set, forcing registration.')

    logger.debug('Machine-id: %s', generate_machine_id(new=config.reregister))

    # check registration with API
    check = get_registration_status(config, pconn)

    for m in check['messages']:
        logger.debug(m)

    if check['unreachable']:
        # Run connection test and exit
        return None

    if check['status']:
        # registered in API, resync files
        if config.register:
            logger.info('This host has already been registered.')
        write_registered_file()
        return True

    if config.register:
        # register if specified
        message, hostname, group, display_name = register(config, pconn)
        if not hostname:
            # API could not be reached, run connection test and exit
            logger.error(message)
            return None
        if config.display_name is None and config.group is None:
            logger.info('Successfully registered host %s', hostname)
        elif config.display_name is None:
            logger.info('Successfully registered host %s in group %s',
                        hostname, group)
        else:
            logger.info('Successfully registered host %s as %s in group %s',
                        hostname, display_name, group)
        if message:
            logger.info(message)
        write_registered_file()
        return True
    else:
        # unregistered in API, resync files
        write_unregistered_file(date=check['unreg_date'])
        # print messaging and exit
        if check['unreg_date']:
            # registered and then unregistered
            logger.info('This machine has been unregistered. '
                        'Use --register if you would like to '
                        're-register this machine.')
        else:
            # not yet registered
            logger.info('This machine has not yet been registered.'
                        'Use --register to register this machine.')
        return False


def handle_registration(config, pconn):
    '''
    Does nothing on the platform. Will be deleted eventually.
    '''
    if config.legacy_upload:
        return _legacy_handle_registration(config, pconn)


def get_registration_status(config, pconn):
    '''
        Handle the registration process
        Returns:
            True - machine is registered
            False - machine is unregistered
            None - could not reach the API
    '''
    return registration_check(pconn)


# -LEGACY-
def _legacy_handle_unregistration(config, pconn):
    """
        returns (bool): True success, False failure
    """
    check = get_registration_status(config, pconn)

    for m in check['messages']:
        logger.debug(m)

    if check['unreachable']:
        # Run connection test and exit
        return None

    if check['status']:
        unreg = pconn.unregister()
    else:
        unreg = True
        logger.info('This system is already unregistered.')
    if unreg:
        # only set if unreg was successful
        write_unregistered_file()
        get_scheduler(config).remove_scheduling()
        delete_cache_files()
    return unreg


def handle_unregistration(config, pconn):
    """
    Returns:
        True - machine was successfully unregistered
        False - machine could not be unregistered
        None - could not reach the API
    """
    if config.legacy_upload:
        return _legacy_handle_unregistration(config, pconn)

    unreg = pconn.unregister()
    if unreg:
        # only set if unreg was successful
        write_unregistered_file()
        delete_cache_files()
    return unreg


def get_machine_id():
    return generate_machine_id()


def update_rules(config, pconn):
    if not pconn:
        raise ValueError('ERROR: Cannot update rules in --offline mode. '
                         'Disable auto_update in config file.')

    pc = InsightsUploadConf(config, conn=pconn)
    return pc.get_conf_update()
