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

    app = create_app(engine, manager, library, args.config)
    log.info("Dashboard: http://%s:%s/ (pick the network interface there if "
              "the advertised IP above isn't right)", config.listen_ip, args.web_port)
    uvicorn.run(app, host="0.0.0.0", port=args.web_port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()




