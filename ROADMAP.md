# dicom-agent-mcp roadmap

## Backlog

| Area | Ideas |
|---|---|
| Verification loop (TEVL) | Each tool gets a contract: preconditions on its inputs and postconditions on its outputs. Each tool result echoes `input_fingerprint`. Verifiers are built from the `check_geometry` issue codes. PASS / SOFT_FAIL / HARD_FAIL verdicts are recorded in the SR before `store` or `transfer` commits anything |
| Output objects | Write DICOM SEG (segmentations), SR TID 1500 (measurement reports, including verification verdicts) and Secondary Capture |
| Real-world testing | Run against a real Orthanc server (`docker-compose.yml`) and the Azure DICOM service |
| Protocol hardening | TLS for DIMSE, C-MOVE fallback in `retrieve` for archives without C-GET, streaming of large STOW-RS and WADO-RS transfers |
| Pixel and geometry | Isotropic resampling and oblique MPR. Test compressed transfer syntaxes (`[codecs]`), enhanced multi-frame objects and real-world data |
| Pixel analysis | PET standardised uptake values (SUV), comparison with the patient's earlier studies, HU checks against a calibration phantom |
| Adoption | Self-provisioning: expose tools based on the configured nodes and available data, and send `notifications/tools/list_changed`. A plugin SDK for third-party tools, demos in several harnesses, a benchmark |
| Workflow standards | Modality Worklist query, MPPS (procedure status), Storage Commitment, UPS-RS work items |
| DICOMweb extras | Server-side rendered images and single frames, thumbnails, Azure change feed and extended query tags |
| AI integrations | Segmentation with TotalSegmentator or MONAI, saved as SEG with the measurements in SR |
| Conversion | Export to NIfTI or NRRD, transcoding between transfer syntaxes, reading RTSTRUCT and SEG back in |
| Viewing context | Saved display settings (GSPS presentation states), key-image notes (KOS), overlays |
| Conformance checking | Check that objects have the attributes their type requires (like `dciodvfy`) and that character sets are correct |
| Privacy | Detect and black out burned-in text (OCR), full Basic Profile options, a de-identification audit trail |
| More MCP features | Studies as MCP resources, ready-made prompts such as "chest CT review", user confirmation before writes |
| Operations | Publish to PyPI and the MCP Registry (see [PUBLISHING.md](PUBLISHING.md)), HTTP transport with OAuth, audit logging (ATNA/syslog), rate limits, OpenTelemetry tracing |
