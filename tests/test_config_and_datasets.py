import pytest

from dicom_agent_mcp.config import load_settings
from dicom_agent_mcp.datasets import Level, build_identifier, parse_level, qido_params, to_tag
from dicom_agent_mcp.errors import DicomMcpError


def test_parse_level_accepts_instance_alias():
    assert parse_level("instance") is Level.IMAGE
    with pytest.raises(DicomMcpError):
        parse_level("frame")


def test_to_tag_accepts_keywords_and_hex():
    assert to_tag("PatientID") == 0x00100020
    assert to_tag("(0010,0020)") == 0x00100020
    assert to_tag("00100020") == 0x00100020
    with pytest.raises(DicomMcpError):
        to_tag("NotAKeyword")


def test_build_identifier_sets_filters_and_empty_return_keys():
    ds = build_identifier(Level.SERIES, {"Modality": "CT", "SeriesNumber": "3"}, ["SeriesDescription", "Modality"])
    assert ds.QueryRetrieveLevel == "SERIES"
    assert ds.Modality == "CT"
    assert ds.SeriesNumber == 3
    assert ds.SeriesDescription in (None, "")


def test_qido_params_use_keywords():
    params = qido_params({"00100020": "123"}, ["StudyDate"])
    assert ("PatientID", "123") in params
    assert ("includefield", "StudyDate") in params


def test_load_settings_resolves_relative_paths(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "storage_dir: cache\n"
        "nodes:\n"
        "  orthanc: {protocol: dimse, host: localhost, port: 4242, ae_title: ORTHANC}\n"
        "  web: {protocol: dicomweb, base_url: http://localhost:8042/dicom-web, auth: {type: bearer, token_env: TOKEN}}\n"
    )
    settings = load_settings(cfg)
    assert settings.storage_dir == (tmp_path / "cache").resolve()
    assert settings.nodes["orthanc"].protocol == "dimse"
    assert settings.nodes["web"].auth.token_env == "TOKEN"


def test_example_config_is_valid():
    from pathlib import Path

    load_settings(Path(__file__).parents[1] / "config.example.yaml")
