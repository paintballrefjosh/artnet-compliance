"""Shared fixtures. Every test works on a throwaway copy of a config in a
tmp_path -- nothing here ever touches the real config/patch.yaml."""
import textwrap

import pytest

from artnet_monitor.config import load_config
from artnet_monitor.engine import ComplianceEngine
from artnet_monitor.fixture_library.store import FixtureLibraryStore
from artnet_monitor.web.app import create_app

MINIMAL_CONFIG = textwrap.dedent("""\
    # header comment that must survive every write
    listen_ip: "10.0.0.5"

    universes: [1, 2]

    fixtures:
    - name: Par 1
      universe: 1
      start_channel: 1
      channels: {intensity: 1, red: 2, green: 3}
    - name: Par 2
      universe: 1
      start_channel: 4
      channels: {intensity: 4, red: 5, green: 6}

    groups:
    - name: Pars
      fixtures: [Par 1, Par 2]

    rules:
    - id: rule-cap
      name: Intensity cap
      applies_to: '*'
      always_active: true
      entries:
      - channel: intensity
        max: 200
    - id: rule-look
      name: House Blue 65%
      applies_to:
      - group:Pars
      always_active: false
      entries:
      - channel: intensity
        min: 100
        max: 100
      - channel: red
        min: 0
        max: 0

    cues:
    - name: Blue Look
      description: ''
      rule_assignments:
      - rule_id: rule-look
        applies_to:
        - group:Pars
    - name: Empty Cue
      description: ''
      rule_assignments: []
    """)


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "patch.yaml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    return str(path)


@pytest.fixture
def library(tmp_path):
    return FixtureLibraryStore(str(tmp_path))


@pytest.fixture
def engine(config_path, library):
    return ComplianceEngine(load_config(config_path, library=library))


@pytest.fixture
def routes(engine, library, config_path):
    """{(path, method): endpoint} for calling endpoints directly -- httpx
    isn't a dependency, and these are plain functions."""
    class _Manager:
        config = engine.config

    app = create_app(engine, _Manager(), library, config_path)
    table = {}
    for route in app.routes:
        for method in getattr(route, "methods", ()) or ():
            table[(route.path, method)] = route.endpoint
    return table


def frame(channel_values):
    """A 512-byte DMX frame from a {1-based channel: value} mapping."""
    data = bytearray(512)
    for channel, value in channel_values.items():
        data[int(channel) - 1] = value
    return bytes(data)
