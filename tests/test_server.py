"""Exercise the MCP layer through FastMCP's in-process API."""

import asyncio
import base64
import json

from conftest import STUDY_UID
from dicom_agent_mcp.server import create_server

EXPECTED_TOOLS = {
    "list_nodes", "echo", "search", "get_metadata", "retrieve", "move",
    "store", "transfer", "list_local_files", "read_local_metadata",
    "render_image", "pixel_statistics", "measure_distance", "check_geometry", "render_mpr", "deidentify",
}


def _payload(result):
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def test_tools_are_registered_with_annotations(settings, web):
    server = create_server(settings, http_transport=web.transport())
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == EXPECTED_TOOLS
    assert tools["search"].annotations.readOnlyHint is True
    assert tools["store"].annotations.readOnlyHint is False
    assert "filters" in tools["search"].inputSchema["properties"]
    assert tools["pixel_statistics"].annotations.readOnlyHint is True
    assert tools["deidentify"].annotations.readOnlyHint is False


def test_render_image_returns_png_and_info(settings, web):
    server = create_server(settings, http_transport=web.transport())
    inbox = settings.allowed_paths[0]
    result = asyncio.run(server.call_tool("render_image", {"path": str(inbox / "ct.dcm"), "max_size": 64}))
    content = result[0] if isinstance(result, tuple) else result
    image = next(c for c in content if c.type == "image")
    text = next(c for c in content if c.type == "text")
    assert image.mimeType == "image/png" and base64.b64decode(image.data).startswith(b"\x89PNG")
    info = json.loads(text.text)
    assert info["input_fingerprint"].startswith("sha256:") and info["png_size"] == [64, 64]


def test_call_search_tool(settings, web):
    server = create_server(settings, http_transport=web.transport())
    result = asyncio.run(server.call_tool("search", {"node": "pacs", "level": "SERIES", "filters": {"StudyInstanceUID": STUDY_UID}}))
    assert _payload(result)["count"] == 2


def test_tool_errors_are_reported(settings, web):
    server = create_server(settings, http_transport=web.transport())
    try:
        asyncio.run(server.call_tool("echo", {"node": "missing"}))
    except Exception as exc:
        assert "Unknown node" in str(exc)
    else:
        raise AssertionError("expected an error")
