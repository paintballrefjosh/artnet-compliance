"""
First-run config bootstrapping: creates config/patch.yaml from a bundled
`.default` template if (and only if) no config file exists yet.

This is what makes app updates safe: releases only ever ship
`patch.yaml.default`, never `patch.yaml` itself, so re-extracting a new
version into the same folder can freely overwrite the default template
without ever touching a live, already-populated config file.
"""
import logging
import os
import shutil

log = logging.getLogger("config_bootstrap")


def ensure_config_exists(config_path: str):
    if os.path.exists(config_path):
        return

    default_path = config_path + ".default"
    if not os.path.exists(default_path):
        raise FileNotFoundError(
            f"Neither {config_path} nor {default_path} exists. Expected a "
            f"bundled default template to be shipped alongside the app."
        )

    os.makedirs(os.path.dirname(os.path.abspath(config_path)) or ".", exist_ok=True)
    shutil.copyfile(default_path, config_path)
    log.info("No existing config found -- created %s from the bundled default "
              "template. This file is now yours: future app updates will "
              "never overwrite it.", config_path)
