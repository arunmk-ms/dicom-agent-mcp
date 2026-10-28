"""Configuration without a YAML file: node specs, environment variables and precedence."""

import json
import os
from pathlib import Path

import pytest

from dicom_agent_mcp.config import build_settings, parse_node
from dicom_agent_mcp.errors import DicomMcpError
from dicom_agent_mcp.server import main


def test_parse_dimse_node():
    name, node = parse_node("pacs=dimse://ORTHANC@localhost:4242;read-only")
    assert name == "pacs"
    assert node == {"protocol": "dimse", "host": "localhost", "port": 4242, "ae_title": "ORTHANC", "read_only": True}


def test_parse_dicomweb_node_with_options():
    _, node = parse_node("azure=https://x.dicom.azurehealthcareapis.com/v2/;token-env=AZ_TOKEN;no-verify-tls;transfer-syntax=")
    assert node["base_url"] == "https://x.dicom.azurehealthcareapis.com/v2"
    assert node["auth"] == {"type": "bearer", "token_env": "AZ_TOKEN"}
    assert node["verify_tls"] is False and node["transfer_syntax"] is None and node["read_only"] is False

    _, node = parse_node("web=http://localhost:8042/dicom-web;user=orthanc;password-env=ORTHANC_PW")
    assert node["auth"] == {"type": "basic", "username": "orthanc", "password_env": "ORTHANC_PW"}


@pytest.mark.parametrize("spec", [
    "no-equals",
    "pacs=dimse://localhost:4242",          # missing AE title
    "pacs=dimse://AE@localhost",             # missing port
    "pacs=ftp://host/x",                     # unsupported scheme
    "pacs=dimse://AE@host:104;token-env=X",  # auth on a DIMSE node
    "web=http://h/dicom-web;user=bob",       # basic auth without password-env
    "web=http://h/dicom-web;readonly",       # typo is rejected rather than ignored
])
def test_parse_node_rejects_bad_specs(spec):
    with pytest.raises(DicomMcpError):
        parse_node(spec)


def test_build_settings_without_config_file(tmp_path):
    settings = build_settings(
        nodes=["pacs=dimse://ORTHANC@localhost:4242"], allowed_paths=["inbox"], calling_ae_title="AGENT",
        environ={}, cwd=tmp_path,
    )
    assert settings.nodes["pacs"].ae_title == "ORTHANC"
    assert settings.allowed_paths == [tmp_path.resolve() / "inbox"]
    assert settings.calling_ae_title == "AGENT"
    assert settings.storage_dir == Path("~/.dicom-agent-mcp/cache").expanduser()


def test_build_settings_with_no_nodes_is_valid(tmp_path):
    assert build_settings(environ={}, cwd=tmp_path).nodes == {}


def test_precedence_file_then_env_then_args(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "storage_dir: from-file\n"
        "allowed_paths: [file-inbox]\n"
        "timeouts: {dimse: 5}\n"
        "nodes:\n"
        "  pacs: {protocol: dimse, host: filehost, port: 104, ae_title: FILE}\n"
        "  keep: {protocol: dimse, host: keephost, port: 104, ae_title: KEEP}\n"
    )
    env = {
        "DICOM_AGENT_MCP_CONFIG": str(cfg),
        "DICOM_AGENT_MCP_STORAGE_DIR": str(tmp_path / "from-env"),
        "DICOM_AGENT_MCP_ALLOWED_PATHS": os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]),
        "DICOM_AGENT_MCP_NODES": "pacs=dimse://ENV@envhost:11112 web=http://envweb/dicom-web",
        "DICOM_AGENT_MCP_CALLING_AE": "ENV_AE",
    }
    settings = build_settings(nodes=["pacs=dimse://ARG@arghost:4242"], storage_dir=str(tmp_path / "from-arg"), environ=env, cwd=tmp_path)

    assert settings.storage_dir == tmp_path / "from-arg"
    assert settings.allowed_paths == [tmp_path / "file-inbox", tmp_path / "a", tmp_path / "b"]
    assert settings.calling_ae_title == "ENV_AE"
    assert settings.nodes["pacs"].ae_title == "ARG"          # argument overrides env and file
    assert settings.nodes["keep"].ae_title == "KEEP"         # file-only node survives
    assert settings.nodes["web"].base_url == "http://envweb/dicom-web"
    assert settings.timeouts.dimse == 5


def test_default_config_file_in_cwd_is_picked_up(tmp_path):
    (tmp_path / "dicom-agent-mcp.yaml").write_text("nodes:\n  pacs: {protocol: dimse, host: h, port: 104, ae_title: A}\n")
    settings = build_settings(environ={}, cwd=tmp_path)
    assert "pacs" in settings.nodes and settings.storage_dir == tmp_path / "dicom-cache"


def test_missing_explicit_config_is_an_error(tmp_path):
    with pytest.raises(DicomMcpError, match="not found"):
        build_settings("nope.yaml", environ={}, cwd=tmp_path)


def test_cli_print_config(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in [k for k in os.environ if k.startswith("DICOM_AGENT_MCP_")]:
        monkeypatch.delenv(key)
    main(["--node", "pacs=dimse://ORTHANC@localhost:4242", "--node", "web=https://h/dicom-web;token-env=T",
          "--storage-dir", "cache", "--print-config"])
    printed = json.loads(capsys.readouterr().out)
    assert printed["nodes"]["pacs"]["port"] == 4242
    assert printed["nodes"]["web"]["auth"] == {"type": "bearer", "token_env": "T"}
    assert Path(printed["storage_dir"]) == tmp_path.resolve() / "cache"


def test_cli_reports_bad_node(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["--node", "pacs=dimse://localhost:4242", "--print-config"])
    assert "Invalid DIMSE node" in capsys.readouterr().err
