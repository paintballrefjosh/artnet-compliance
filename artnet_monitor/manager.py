"""
NodeManager: owns the running ArtNetNode instance and supports swapping its
advertised IP (e.g. from the dashboard's interface dropdown) without
restarting the whole process — stop the old node, update config, start a
new one, persist the choice back to the YAML file.
"""
import logging

from . import patch_writer
from .artnet.node import ArtNetNode

log = logging.getLogger("manager")


class NodeManager:
    def __init__(self, config, config_path: str, engine):
        self.config = config
        self.config_path = config_path
        self.engine = engine
        self.node = None

    def start(self):
        self.node = ArtNetNode(
            listen_ip=self.config.listen_ip,
            universes=self.config.universes,
            on_dmx_frame=self.engine.ingest_frame,
        )
        self.node.start()
        log.info("Art-Net node (re)started, advertising %s, universes: %s",
                  self.config.listen_ip, self.config.universes)

    def set_listen_ip(self, ip: str):
        log.info("Switching advertised IP from %s to %s", self.config.listen_ip, ip)
        if self.node:
            self.node.stop()
        self.config.listen_ip = ip
        self._persist_listen_ip()
        self.start()

    def _persist_listen_ip(self):
        """Rewrites just the `listen_ip:` line in patch.yaml in place, so the
        rest of the file (comments, fixtures, rules) survives untouched --
        a full YAML re-serialize would strip all the explanatory comments."""
        if not patch_writer.set_listen_ip(self.config_path, self.config.listen_ip):
            log.warning("Could not find listen_ip line in %s to update; "
                        "the new IP is active for this session but not saved.",
                        self.config_path)
