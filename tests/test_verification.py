"""Tool Execution Verification Loop: verdicts, ledger, contracts, the store gate, verify_claim and result states."""

import json
import shutil

import pytest
from pydicom import dcmread
from pydicom.uid import generate_uid

from dicom_agent_mcp import contracts
from dicom_agent_mcp.config import Settings, build_settings
from dicom_agent_mcp.errors import DicomMcpError
from dicom_agent_mcp.service import DicomService
from dicom_agent_mcp.verification import (
    HARD_FAIL,
    PASS,
    SOFT_FAIL,
    Ledger,
    VerificationError,
    geometry_verdict,
    worst,
)
from test_imaging import SERIES, SLICES, make_slice, write_series


def with_policy(settings: Settings, policy: str) -> DicomService:
    return DicomService(settings.model_copy(update={"verification": policy}))


@pytest.fixture
def local(tmp_path):
    inbox = tmp_path / "inbox"
    write_series(inbox / "axial", [make_slice(k) for k in range(SLICES)])
    settings = Settings.model_validate({"storage_dir": str(tmp_path / "cache"), "allowed_paths": [str(inbox)], "nodes": {}})
    return settings, inbox


def ledger_of(service: DicomService) -> list[dict]:
    return service.verifier.ledger.records()


# ---- core ------------------------------------------------------------------------------------------


def test_worst_and_geometry_mapping():
    assert worst([]) == PASS
    assert worst([PASS, SOFT_FAIL, PASS]) == SOFT_FAIL
    assert worst([SOFT_FAIL, HARD_FAIL]) == HARD_FAIL
    assert geometry_verdict("check_geometry", "error", "MISSING_SLICES") == HARD_FAIL
    assert geometry_verdict("check_geometry", "warning", "NON_UNIFORM_SPACING") == SOFT_FAIL
    assert geometry_verdict("check_geometry", "info", "OBLIQUE") == PASS
    assert geometry_verdict("render_mpr", "info", "OBLIQUE") == SOFT_FAIL
    assert geometry_verdict("store", "error", "MISSING_SLICES") == SOFT_FAIL
    assert geometry_verdict("store", "error", "GEOMETRY_MISSING") == PASS


def test_ledger_is_hash_chained_and_detects_tampering(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    first = ledger.append({"tool": "a", "verdict": PASS})
    second = ledger.append({"tool": "b", "verdict": SOFT_FAIL})
    assert second["prev_hash"] == first["hash"]
    assert ledger.get(second["id"])["tool"] == "b"
    assert ledger.check_chain() == {"ok": True, "records": 2}

    lines = ledger.path.read_text().splitlines()
    edited = json.loads(lines[0])
    edited["verdict"] = HARD_FAIL
    lines[0] = json.dumps(edited)
    ledger.path.write_text("\n".join(lines) + "\n")
    assert ledger.check_chain() == {"ok": False, "records": 2, "first_bad_seq": 1}


def test_policy_from_env_and_argument(tmp_path):
    env = {"DICOM_AGENT_MCP_VERIFICATION": "Enforce"}
    assert build_settings(environ=env, cwd=tmp_path).verification == "enforce"
    assert build_settings(environ=env, cwd=tmp_path, verification="off").verification == "off"
    assert build_settings(environ={}, cwd=tmp_path).verification == "advisory"


def test_policy_off_records_nothing(local):
    settings, inbox = local
    service = with_policy(settings, "off")
    result = service.pixel_statistics(str(inbox / "axial" / "slice000.dcm"))
    assert "verification" not in result
    assert not settings.ledger_path.exists()


# ---- pixel tools -----------------------------------------------------------------------------------


def test_pixel_statistics_pass_is_recorded_with_fingerprint(local):
    settings, inbox = local
    service = DicomService(settings)
    result = service.pixel_statistics(str(inbox / "axial" / "slice000.dcm"), roi={"shape": "rectangle", "row": 10, "column": 20, "height": 5, "width": 10})
    v = result["verification"]
    assert v["verdict"] == PASS and v["policy"] == "advisory"
    assert {"STATS_CONSISTENT", "AREA_CONSISTENT", "ROI_COVERAGE"} <= {c["code"] for c in v["checks"]}
    record = service.verifier.ledger.get(v["id"])
    assert record["input_fingerprints"] == [result["input_fingerprint"]]
    assert record["output_digest"].startswith("sha256:")


def test_clipped_roi_is_soft_fail(local):
    settings, inbox = local
    result = DicomService(settings).pixel_statistics(
        str(inbox / "axial" / "slice000.dcm"), roi={"shape": "rectangle", "row": 28, "column": 0, "height": 10, "width": 5})
    assert result["verification"]["verdict"] == SOFT_FAIL
    assert "ROI_CLIPPED" in {c["code"] for c in result["verification"]["checks"]}


def test_inconsistent_statistics_are_hard_fail():
    ds = make_slice(0)
    bad = {"pixel_count": 10, "min": 0.0, "p05": 5.0, "median": 3.0, "p95": 9.0, "max": 10.0, "mean": 4.0, "std": 1.0}
    checks = contracts.statistics_post(bad, ds, 0, None)
    assert worst(c.verdict for c in checks) == HARD_FAIL


def test_measure_distance_is_cross_checked_in_patient_space(local):
    settings, inbox = local
    result = DicomService(settings).measure_distance(str(inbox / "axial" / "slice000.dcm"), [0, 0], [10, 0])
    assert result["distance_mm"] == 8.0
    assert result["input_fingerprint"].startswith("sha256:")
    assert result["verification"]["verdict"] == PASS
    wrong = {**result, "distance_mm": 9.0}
    assert contracts.distance_post(wrong)[0].verdict == HARD_FAIL


def test_render_image_preset_on_non_hu_is_soft_fail(tmp_path):
    inbox = tmp_path / "inbox"
    ds = make_slice(0)
    ds.RescaleType = "US"
    write_series(inbox, [ds])
    settings = Settings.model_validate({"storage_dir": str(tmp_path / "cache"), "allowed_paths": [str(inbox)], "nodes": {}})
    _, info = DicomService(settings).render_image(str(inbox / "slice000.dcm"), window="lung")
    codes = {c["code"]: c["verdict"] for c in info["verification"]["checks"]}
    assert codes["PRESET_UNITS_MISMATCH"] == SOFT_FAIL and codes["ASPECT_MATCHES_MM"] == PASS


def test_aspect_mismatch_is_hard_fail():
    checks = contracts.aspect_checks((32, 40), (0.8, 0.6), (40, 32), 512)
    assert checks[0].code == "ASPECT_MISMATCH" and checks[0].verdict == HARD_FAIL


def test_render_mpr_on_gapped_series(tmp_path):
    inbox = tmp_path / "inbox"
    write_series(inbox / "gap", [make_slice(k) for k in range(SLICES) if k != 7])
    settings = Settings.model_validate({"storage_dir": str(tmp_path / "cache"), "allowed_paths": [str(inbox)], "nodes": {}})

    advisory = DicomService(settings)
    with pytest.raises(DicomMcpError, match="MISSING_SLICES"):
        advisory.render_mpr([str(inbox / "gap")])
    record = ledger_of(advisory)[-1]
    assert record["verdict"] == HARD_FAIL and {"MISSING_SLICES", "TOOL_ERROR"} <= {c["code"] for c in record["checks"]}

    with pytest.raises(VerificationError, match="MISSING_SLICES") as exc:
        with_policy(settings, "enforce").render_mpr([str(inbox / "gap")])
    assert exc.value.record["blocked"] is True


def test_check_geometry_clean_series_passes(local):
    settings, inbox = local
    result = DicomService(settings).check_geometry([str(inbox / "axial")])
    assert result["verification"]["verdict"] == PASS


# ---- deidentify ----------------------------------------------------------------------------------


def test_deidentify_postconditions_pass_and_record_outputs(local):
    settings, inbox = local
    service = DicomService(settings)
    result = service.deidentify([str(inbox / "axial")])
    v = result["verification"]
    assert v["verdict"] == PASS, v["checks"]
    assert {"IDENTIFIERS_REMOVED", "UIDS_REMAPPED", "PIXELS_UNCHANGED", "PRIVATE_TAGS_REMOVED"} <= {c["code"] for c in v["checks"]}
    record = service.verifier.ledger.get(v["id"])
    assert len(record["output_files"]) == SLICES


def test_deidentify_keep_private_is_soft_fail(local):
    settings, inbox = local
    result = DicomService(settings).deidentify([str(inbox / "axial" / "slice000.dcm")], keep_private=True)
    assert result["verification"]["verdict"] == SOFT_FAIL


def test_deidentify_postconditions_catch_a_copy(local, tmp_path):
    _, inbox = local
    source = inbox / "axial" / "slice000.dcm"
    copy = tmp_path / "copy.dcm"
    shutil.copy(source, copy)
    checks, _ = contracts.deidentify_post({"files": [{"source": str(source), "output": str(copy)}]}, False, False)
    verdicts = {c.code: c.verdict for c in checks}
    assert verdicts["IDENTIFIER_PRESENT"] == verdicts["UID_NOT_REMAPPED"] == verdicts["PRIVATE_TAGS_PRESENT"] == HARD_FAIL
    assert verdicts["PIXELS_UNCHANGED"] == PASS


# ---- store gate ------------------------------------------------------------------------------------


def series_in_inbox(settings: Settings, name: str, slices) -> str:
    folder = settings.allowed_paths[0] / name
    write_series(folder, slices)
    return str(folder)


def test_gate_blocks_soft_fail_until_accepted(settings, dest_archive):
    gapped = series_in_inbox(settings, "gap", [make_slice(k) for k in range(6) if k != 3])
    service = with_policy(settings, "enforce")
    with pytest.raises(VerificationError, match="accept_soft_fail"):
        service.store("dest", [gapped])
    assert not dest_archive.instances
    blocked = ledger_of(service)[-1]
    assert blocked["kind"] == "gate" and blocked["blocked"] and blocked["verdict"] == SOFT_FAIL

    result = service.store("dest", [gapped], accept_soft_fail=True)
    assert result["status"] == "ok" and len(dest_archive.instances) == 5
    v = result["verification"]
    assert v["verdict"] == PASS and v["gate_verdict"] == SOFT_FAIL and v["accepted_soft_fail"] is True
    gate = service.verifier.ledger.get(v["gate_id"])
    commit = service.verifier.ledger.get(v["id"])
    assert gate["seq"] < commit["seq"] and gate["kind"] == "gate" and commit["kind"] == "commit"


def test_advisory_gate_records_but_does_not_block(settings, dest_archive):
    gapped = series_in_inbox(settings, "gap", [make_slice(k) for k in range(6) if k != 3])
    result = DicomService(settings).store("dest", [gapped])
    assert result["status"] == "ok" and len(dest_archive.instances) == 5
    gate = DicomService(settings).verifier.ledger.get(result["verification"]["gate_id"])
    assert gate["verdict"] == SOFT_FAIL and "MISSING_SLICES" in {c["code"] for c in gate["checks"]}


def test_gate_blocks_duplicate_instances_and_mixed_patients(settings, dest_archive):
    sop = generate_uid()
    a, b = make_slice(0, sop=sop), make_slice(1, sop=sop)
    b.PatientID = "OTHER"
    folder = series_in_inbox(settings, "dup", [a, b])
    with pytest.raises(VerificationError) as exc:
        with_policy(settings, "enforce").store("dest", [folder], accept_soft_fail=True)
    codes = {c["code"] for c in exc.value.record["checks"] if c["verdict"] == HARD_FAIL}
    assert codes == {"DUPLICATE_SOP_INSTANCE", "PATIENT_MISMATCH"}
    assert not dest_archive.instances


def test_gate_inherits_failed_verdict_of_the_tool_that_made_the_files(settings, dest_archive):
    folder = series_in_inbox(settings, "axial", [make_slice(k) for k in range(4)])
    service = with_policy(settings, "enforce")
    deid = service.deidentify([folder], keep_descriptions=True)  # SeriesDescription "Jane Doe chest" leaks the name
    assert deid["verification"]["verdict"] == HARD_FAIL
    outputs = [f["output"] for f in deid["files"]]
    with pytest.raises(VerificationError, match="PRIOR_HARD_FAIL"):
        service.store("dest", outputs, accept_soft_fail=True)
    assert not dest_archive.instances

    clean = service.deidentify([folder])
    assert clean["verification"]["verdict"] == PASS
    result = service.store("dest", [f["output"] for f in clean["files"]])
    assert result["status"] == "ok" and len(dest_archive.instances) == 4
    gate = service.verifier.ledger.get(result["verification"]["gate_id"])
    assert "PRIOR_VERIFIED" in {c["code"] for c in gate["checks"]}


# ---- verify_claim ----------------------------------------------------------------------------------


def test_verify_claim_matches_with_rounding_aware_tolerance(local):
    settings, inbox = local
    service = DicomService(settings)
    path = str(inbox / "axial" / "slice003.dcm")
    stats = service.pixel_statistics(path)
    claim = service.verify_claim(
        "pixel_statistics", {"path": path}, {"mean": round(stats["mean"], 1), "max": 500, "units": "HU"},
        input_fingerprint=stats["input_fingerprint"],
    )
    assert claim["verdict"] == PASS, claim["fields"]
    assert claim["verification"]["id"].startswith("ver_")
    assert service.verifier.ledger.get(claim["verification"]["id"])["kind"] == "claim"


def test_verify_claim_catches_wrong_values_unknown_fields_and_other_pixels(local):
    settings, inbox = local
    service = DicomService(settings)
    path = str(inbox / "axial" / "slice000.dcm")
    distance = {"path": path, "start": [0, 0], "end": [10, 0]}
    assert service.verify_claim("measure_distance", distance, {"distance_mm": 8})["verdict"] == PASS
    assert service.verify_claim("measure_distance", distance, {"distance_mm": 8.04})["verdict"] == HARD_FAIL
    assert service.verify_claim("measure_distance", distance, {"distance_mm": 8.1})["verdict"] == HARD_FAIL
    assert service.verify_claim("measure_distance", distance, {"distance_mm": 8.1}, tolerance=0.2)["verdict"] == PASS
    unknown = service.verify_claim("measure_distance", distance, {"length_mm": 8})
    assert unknown["fields"][0]["verdict"] == HARD_FAIL
    other = service.pixel_statistics(str(inbox / "axial" / "slice001.dcm"))["input_fingerprint"]
    mismatch = service.verify_claim("measure_distance", distance, {"distance_mm": 8}, input_fingerprint=other)
    assert mismatch["verdict"] == HARD_FAIL
    assert "FINGERPRINT_MISMATCH" in {c["code"] for c in mismatch["verification"]["checks"]}


def test_verify_claim_on_geometry(local):
    settings, inbox = local
    claim = DicomService(settings).verify_claim("check_geometry", {"paths": [str(inbox / "axial")]},
                                                 {"ok": True, "series.0.series_uid": SERIES, "series.0.volume_ready": True})
    assert claim["verdict"] == PASS


def test_compare_claim_tolerance_follows_claimed_precision():
    assert contracts.compare_claim(47, 47.4, None) == (True, 0.5)
    assert contracts.compare_claim(47, 47.6, None)[0] is False
    assert contracts.compare_claim(8.0, 8.04, None)[0] is True
    assert contracts.compare_claim(True, 1, None)[0] is False
    assert contracts.compare_claim([1.0, 2.0], [1.02, 2.0], None)[0] is True


def test_get_verification(local):
    settings, inbox = local
    service = DicomService(settings)
    stats = service.pixel_statistics(str(inbox / "axial" / "slice000.dcm"))
    by_id = service.get_verification(record_id=stats["verification"]["id"])
    assert by_id["records"][0]["tool"] == "pixel_statistics" and by_id["integrity"]["ok"]
    by_fp = service.get_verification(fingerprint=stats["input_fingerprint"])
    assert by_fp["count"] == 1
    with pytest.raises(DicomMcpError):
        service.get_verification(record_id="ver_missing")


# ---- explicit result states ------------------------------------------------------------------------


def test_empty_search_explains_itself(settings):
    result = DicomService(settings).search("pacs", "STUDY", {"PatientID": "NOPE", "InstitutionName": "X*"})
    assert result["status"] == "empty"
    assert result["query"]["filters"] == {"PatientID": "NOPE", "InstitutionName": "X*"}
    assert result["possibly_unsupported_keys"] == ["InstitutionName"]
    assert "not proof" in result["hint"]


def test_search_ok_and_truncated(settings):
    service = DicomService(settings)
    assert service.search("pacs", "IMAGE")["status"] == "ok"
    assert service.search("pacs", "IMAGE", limit=1)["status"] == "truncated"


def test_retrieve_reports_status_and_checks_uids(settings):
    from conftest import STUDY_UID

    result = DicomService(settings).retrieve("pacs", STUDY_UID)
    assert result["status"] == "ok" and len(result["files"]) == 4
    assert {c["code"] for c in result["verification"]["checks"]} >= {"RESULT_OK", "UIDS_MATCH_REQUEST"}


def test_transfer_of_nothing_is_empty_not_an_error(settings, dest_archive):
    result = DicomService(settings).transfer("pacs", "dest", "1.2.3.999")
    assert result["status"] == "empty" and result["store"] is None
    assert result["verification"]["verdict"] == SOFT_FAIL
    assert not dest_archive.instances


def test_transfer_status_and_verification(settings, dest_archive):
    from conftest import STUDY_UID

    result = DicomService(settings).transfer("pacs", "dest", STUDY_UID)
    assert result["status"] == "ok" and len(dest_archive.instances) == 4
    assert len(result["verification"]["records"]) == 2


def test_stored_files_are_unchanged_by_verification(settings):
    from conftest import STUDY_UID

    service = DicomService(settings)
    files = service.retrieve("pacs", STUDY_UID)["files"]
    before = [dcmread(f).SOPInstanceUID for f in files]
    service.store("dest", files)
    assert [dcmread(f).SOPInstanceUID for f in files] == before

# ---- reproducibility: environment, effective parameters, protocol path and provenance -------------


def test_records_carry_tool_and_library_versions(local):
    from dicom_agent_mcp import __version__

    settings, inbox = local
    service = DicomService(settings)
    stats = service.pixel_statistics(str(inbox / "axial" / "slice000.dcm"))
    env = service.verifier.ledger.get(stats["verification"]["id"])["environment"]
    assert env["dicom-agent-mcp"] == __version__
    assert {"python", "pydicom", "pynetdicom", "numpy", "pillow", "httpx"} <= set(env)


def test_records_carry_effective_parameters(local):
    settings, inbox = local
    service = DicomService(settings)
    _, info = service.render_image(str(inbox / "axial" / "slice000.dcm"))  # window="auto" is resolved by the tool
    params = service.verifier.ledger.get(info["verification"]["id"])["effective_params"]
    assert params["window"] == info["window"] and params["window"]["source"] != "auto"
    assert params["png_size"] == info["png_size"] and "png_resampling" in params

    _, mpr = service.render_mpr([str(inbox / "axial")], plane="sagittal")
    params = service.verifier.ledger.get(mpr["verification"]["id"])["effective_params"]
    assert params["plane"] == "sagittal" and params["index"] == mpr["index"] and "nearest slice" in params["sampling"]


def test_deidentify_record_holds_profile_key_id_validation_and_provenance(local, monkeypatch):
    settings, inbox = local
    service = DicomService(settings)
    random_key = service.deidentify([str(inbox / "axial" / "slice000.dcm")])
    record = service.verifier.ledger.get(random_key["verification"]["id"])
    params = record["effective_params"]
    assert params["key_id"] is None and params["linkable"] is False and params["date_mode"] == "shift"
    assert any(p.startswith("113100 ") for p in params["profile"]) and any(p.startswith("113107 ") for p in params["profile"])
    assert {"IDENTIFIERS_REMOVED", "UIDS_REMAPPED"} <= {c["code"] for c in record["checks"]}  # validation in the same record
    source = str(inbox / "axial" / "slice000.dcm")
    assert record["input_files"][source]["produced_by"] is None
    assert record["input_files"][source]["transfer_syntax"]

    monkeypatch.setenv(settings.deid_key_env, "test-secret")
    first = service.deidentify([str(inbox / "axial" / "slice000.dcm")], date_mode="keep")
    second = service.deidentify([str(inbox / "axial" / "slice000.dcm")], date_mode="keep")
    assert first["key_id"] and first["key_id"] == second["key_id"] and "test-secret" not in first["key_id"]
    assert any(p.startswith("113106 ") for p in first["profile"])

    output = second["files"][0]["output"]
    stats = service.pixel_statistics(output)
    link = service.verifier.ledger.get(stats["verification"]["id"])["input_files"][output]
    assert link["produced_by"] == second["verification"]["id"]


def test_dimse_and_dicomweb_paths_are_distinguishable_in_the_ledger(settings, web):
    from conftest import STUDY_UID

    service = DicomService(settings, http_transport=web.transport())
    dimse = service.retrieve("pacs", STUDY_UID)
    service.store("web", dimse["files"])
    dicomweb = service.retrieve("web", STUDY_UID)
    ledger = service.verifier.ledger

    a = ledger.get(dimse["verification"]["id"])["source"]
    b = ledger.get(dicomweb["verification"]["id"])["source"]
    assert (a["protocol"], a["operation"], a["calling_ae_title"]) == ("dimse", "C-GET", "DICOM_MCP")
    assert a["endpoint"].startswith("FAKEPACS@127.0.0.1:")
    assert (b["protocol"], b["operation"], b["endpoint"]) == ("dicomweb", "WADO-RS", "http://dicomweb.test/dicom-web")
    assert b["requested_transfer_syntax"] == "*"
    assert a["received_transfer_syntaxes"] and b["received_transfer_syntaxes"]

    commit = ledger.get(ledger.records()[-2]["id"])
    assert commit["kind"] == "commit" and commit["source"]["operation"] == "STOW-RS" and commit["source"]["sent_transfer_syntaxes"]

    geometry = service.check_geometry(dicomweb["files"])
    links = ledger.get(geometry["verification"]["id"])["input_files"]
    assert {v["produced_by"] for v in links.values()} == {dicomweb["verification"]["id"]}


def test_endpoint_never_records_credentials(tmp_path):
    settings = Settings.model_validate({"storage_dir": str(tmp_path), "nodes": {
        "w": {"protocol": "dicomweb", "base_url": "https://user:pw@dicom.example.org:8443/v2?code=secret"}}})
    assert DicomService(settings)._source("w", "retrieve")["endpoint"] == "https://dicom.example.org:8443/v2"
