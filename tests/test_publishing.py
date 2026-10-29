"""Publishing metadata must stay consistent, or PyPI / MCP Registry publishing fails."""

import json
import re
import tomllib
from pathlib import Path

from dicom_agent_mcp import __version__

ROOT = Path(__file__).parents[1]


def test_versions_and_registry_name_match():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    package = server["packages"][0]

    assert pyproject["version"] == __version__ == server["version"] == package["version"]
    assert package["identifier"] == pyproject["name"]
    assert len(server["description"]) <= 100

    marker = re.search(r"mcp-name:\s*(\S+)", (ROOT / "README.md").read_text(encoding="utf-8"))
    assert marker and marker.group(1) == server["name"]
