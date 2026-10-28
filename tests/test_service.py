"""End-to-end tests of every operation against the fake DIMSE archive and fake DICOMweb server."""

from pathlib import Path

import pytest

from conftest import SERIES_UIDS, STUDY_UID
from dicom_agent_mcp.errors import DicomMcpError
from dicom_agent_mcp.service import DicomService


@pytest.fixture
def service(settings, web):
    return DicomService(settings, http_transport=web.transport())


# ---- DIMSE ---------------------------------------------------------------------------------------

def test_c_echo(service):
    assert service.echo("pacs")["ok"] is True


def test_c_find_study_and_series(service):
    studies = service.search("pacs", "STUDY", {"PatientID": "PID001"})
    assert studies["operation"] == "C-FIND"
    assert [r["StudyInstanceUID"] for r in studies["results"]] == [STUDY_UID]
    assert studies["results"][0]["PatientName"] == "TEST^PATIENT"

    series = service.search("pacs", "SERIES", {"StudyInstanceUID": STUDY_UID})
    assert sorted(r["SeriesInstanceUID"] for r in series["results"]) == SERIES_UIDS


def test_c_find_wildcard_and_paging(service):
    page = service.search("pacs", "IMAGE", {"PatientName": "TEST*"}, limit=3)
    assert page["count"] == 3 and page["truncated"] is True
    rest = service.search("pacs", "IMAGE", {"PatientName": "TEST*"}, limit=3, offset=3)
    assert rest["count"] == 1 and rest["truncated"] is False


def test_c_find_metadata(service):
    meta = service.get_metadata("pacs", STUDY_UID, SERIES_UIDS[0])
    assert meta["count"] == 2
    assert meta["instances"][0]["Modality"] == "CT"
    assert "note" in meta


def test_c_get_retrieve_series(service, settings):
    result = service.retrieve("pacs", STUDY_UID, SERIES_UIDS[0])
    assert result["operation"] == "C-GET" and result["ok"]
    assert len(result["files"]) == 2
    for f in result["files"]:
        assert Path(f).is_relative_to(settings.storage_dir)
    header = service.read_local_metadata(result["files"][0])
    assert header["attributes"]["SeriesInstanceUID"] == SERIES_UIDS[0]
    assert "PixelData" not in header["attributes"]


def test_c_move_to_named_node(service, dest_archive):
    result = service.move("pacs", "dest", STUDY_UID, SERIES_UIDS[1])
    assert result["ok"] and result["completed"] == 2
    assert len(dest_archive.instances) == 2


def test_c_store_skips_non_dicom(service, settings, dest_archive):
    inbox = settings.allowed_paths[0]
    result = service.store("dest", [str(inbox)])
    assert result["operation"] == "C-STORE"
    assert result["stored"] == 1 and result["failed"] == 0
    assert any(p.endswith("notes.txt") for p in result["skipped"])
    assert len(dest_archive.instances) == 1


# ---- DICOMweb ------------------------------------------------------------------------------------

def test_stow_then_qido_wado(service, settings, dest_archive):
    stored = service.store("web", [str(settings.allowed_paths[0] / "ct.dcm")])
    assert stored["operation"] == "STOW-RS" and stored["stored"] == 1

    studies = service.search("web", "STUDY", {"Modality": "CT"})
    assert studies["operation"] == "QIDO-RS" and studies["count"] == 1
    study_uid = studies["results"][0]["StudyInstanceUID"]

    meta = service.get_metadata("web", study_uid, fields=["Rows"])
    assert meta["operation"] == "WADO-RS" and meta["instances"][0]["Rows"] == 128
    assert "PatientName" not in meta["instances"][0]

    retrieved = service.retrieve("web", study_uid)
    assert retrieved["operation"] == "WADO-RS" and len(retrieved["files"]) == 1


def test_qido_sends_includefield_and_paging(service, web):
    service.search("web", "SERIES", {"Modality": "CT"}, return_fields=["BodyPartExamined"], limit=10, offset=5)
    params = web.requests[-1].url.params
    assert "BodyPartExamined" in params.get_list("includefield")
    assert params["limit"] == "11" and params["offset"] == "5"


def test_qido_patient_level_is_rejected(service):
    with pytest.raises(DicomMcpError, match="no patient level"):
        service.search("web", "PATIENT")


def test_transfer_dimse_to_dicomweb(service, dest_archive):
    result = service.transfer("pacs", "web", STUDY_UID)
    assert result["retrieve"]["operation"] == "C-GET"
    assert result["store"]["operation"] == "STOW-RS" and result["store"]["stored"] == 4
    assert len(dest_archive.instances) == 4


def test_bearer_auth_header(settings, web, monkeypatch):
    raw = settings.model_dump(mode="json")
    raw["nodes"]["web"]["auth"] = {"type": "bearer", "token_env": "TEST_DICOM_TOKEN"}
    settings = type(settings).model_validate(raw)
    monkeypatch.setenv("TEST_DICOM_TOKEN", "secret-token")
    DicomService(settings, http_transport=web.transport()).echo("web")
    assert web.requests[-1].headers["authorization"] == "Bearer secret-token"


# ---- guard rails ---------------------------------------------------------------------------------

def test_read_only_node_blocks_store(service, settings):
    with pytest.raises(DicomMcpError, match="read-only"):
        service.store("web-ro", [str(settings.allowed_paths[0] / "ct.dcm")])


def test_paths_outside_allowed_roots_are_rejected(service, tmp_path):
    outside = tmp_path / "elsewhere.dcm"
    outside.write_bytes(b"x")
    with pytest.raises(DicomMcpError, match="outside the allowed"):
        service.read_local_metadata(str(outside))


def test_invalid_uid_is_rejected(service):
    with pytest.raises(DicomMcpError, match="Invalid"):
        service.retrieve("web", "../../etc")


def test_unknown_node(service):
    with pytest.raises(DicomMcpError, match="Unknown node"):
        service.echo("nope")


def test_move_rejects_dicomweb_source(service):
    with pytest.raises(DicomMcpError, match="transfer"):
        service.move("web", "dest", STUDY_UID)
