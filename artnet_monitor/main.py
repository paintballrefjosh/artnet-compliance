import argparse
import logging
import os

import uvicorn

from .config import load_config
from .config_bootstrap import ensure_config_exists
from .engine import ComplianceEngine
from .fixture_library.store import FixtureLibraryStore
from .manager import NodeManager
from .web.app import create_app


def main():
    parser = argparse.ArgumentParser(description="Art-Net compliance monitor")
    parser.add_argument("--config", default="config/patch.yaml",
                         help="Path to patch/compliance YAML config")
    parser.add_argument("--web-port", type=int, default=8000,
                         help="Port for the local web dashboard")
    parser.add_argument("--web-host", default="127.0.0.1",
                         help="Address to serve the dashboard on. Defaults to "
                              "127.0.0.1 (this machine only) because the dashboard "
                              "can rewrite the patch and restart the Art-Net node, "
                              "and has no authentication of its own. Use 0.0.0.0 to "
                              "reach it from other machines -- pair that with "
                              "--auth-token on any shared network.")
    parser.add_argument("--auth-token", default=os.environ.get("ARTNET_AUTH_TOKEN"),
                         help="Require this token on every dashboard request "
                              "(X-Auth-Token header, ?token= once, or its cookie). "
                              "Also settable via the ARTNET_AUTH_TOKEN env var.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("main")

    ensure_config_exists(args.config)

    config_dir = os.path.dirname(os.path.abspath(args.config)) or "."
    library = FixtureLibraryStore(config_dir)
    config = load_config(args.config, library=library)
    engine = ComplianceEngine(config)

    manager = NodeManager(config, args.config, engine)
    manager.start()

    app = create_app(engine, manager, library, args.config, auth_token=args.auth_token)

    reachable_host = "localhost" if args.web_host in ("127.0.0.1", "localhost") else config.listen_ip
    log.info("Dashboard: http://%s:%s/ (pick the network interface there if "
              "the advertised IP above isn't right)", reachable_host, args.web_port)
    if args.web_host not in ("127.0.0.1", "localhost") and not args.auth_token:
        log.warning("Dashboard is bound to %s with no --auth-token: anyone who can "
                    "reach this machine on port %s can rewrite the patch, change the "
                    "advertised Art-Net IP, or replace the whole configuration.",
                    args.web_host, args.web_port)
    uvicorn.run(app, host=args.web_host, port=args.web_port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()




