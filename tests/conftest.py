"""Test doubles: an in-memory archive served over DIMSE (pynetdicom) and DICOMweb (httpx MockTransport)."""

from __future__ import annotations

import copy
import fnmatch
import io
import json
import re
import shutil
import socket
from pathlib import Path

import httpx
import pytest
from pydicom import dcmread
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid
from pynetdicom import AE, AllStoragePresentationContexts, StoragePresentationContexts, evt
from pynetdicom.sop_class import (
    PatientRootQueryRetrieveInformationModelFind,
    PatientRootQueryRetrieveInformationModelGet,
    PatientRootQueryRetrieveInformationModelMove,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelGet,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)

from dicom_agent_mcp.config import Settings
from dicom_agent_mcp.dicomweb import build_multipart, split_multipart

UNIQUE = {"PATIENT": "PatientID", "STUDY": "StudyInstanceUID", "SERIES": "SeriesInstanceUID", "IMAGE": "SOPInstanceUID"}
STUDY_UID = "1.2.826.0.1.3680043.8.498.1"
SERIES_UIDS = ["1.2.826.0.1.3680043.8.498.1.1", "1.2.826.0.1.3680043.8.498.1.2"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_instances() -> list[Dataset]:
    """Four CT instances in one study, two series; patient TEST^PATIENT / PID001."""
    template = dcmread(get_testdata_file("CT_small.dcm"))
    out = []
    for s_idx, series_uid in enumerate(SERIES_UIDS):
        for i in range(2):
            ds = copy.deepcopy(template)
            ds.PatientName = "TEST^PATIENT"
            ds.PatientID = "PID001"
            ds.StudyInstanceUID = STUDY_UID
            ds.SeriesInstanceUID = series_uid
            ds.SeriesNumber = s_idx + 1
            ds.Modality = "CT"
            ds.StudyDate = "20240115"
            ds.InstanceNumber = i + 1
            ds.SOPInstanceUID = generate_uid()
            ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
            out.append(ds)
    return out


class FakeArchive:
    def __init__(self, instances: list[Dataset] | None = None):
        self.instances: dict[str, Dataset] = {str(ds.SOPInstanceUID): ds for ds in instances or []}

    def add(self, ds: Dataset) -> None:
        self.instances[str(ds.SOPInstanceUID)] = ds

    def match(self, filters: dict[str, str]) -> list[Dataset]:
        def ok(ds: Dataset) -> bool:
            for key, pattern in filters.items():
                if pattern in (None, ""):
                    continue
                value = str(ds.get(key, ""))
                if "-" in pattern and key.endswith("Date"):
                    lo, hi = pattern.split("-")
                    if not ((not lo or value >= lo) and (not hi or value <= hi)):
                        return False
                elif not fnmatch.fnmatchcase(value, pattern):
                    return False
            return True
        return [ds for ds in self.instances.values() if ok(ds)]

    def query(self, level: str, filters: dict[str, str]) -> list[Dataset]:
        seen, out = set(), []
        for ds in self.match(filters):
            key = str(ds.get(UNIQUE[level], ""))
            if key not in seen:
                seen.add(key)
                out.append(ds)
        return out


# ---- DIMSE ---------------------------------------------------------------------------------------

class FakeDimsePacs:
    def __init__(self, archive: FakeArchive, ae_title: str = "FAKEPACS", peers: dict[str, tuple[str, int]] | None = None):
        self.archive = archive
        self.ae_title = ae_title
        self.port = free_port()
        self.peers = peers or {}
        ae = AE(ae_title=ae_title)
        ae.add_supported_context(Verification)
        for model in (
            PatientRootQueryRetrieveInformationModelFind, StudyRootQueryRetrieveInformationModelFind,
            PatientRootQueryRetrieveInformationModelGet, StudyRootQueryRetrieveInformationModelGet,
            PatientRootQueryRetrieveInformationModelMove, StudyRootQueryRetrieveInformationModelMove,
        ):
            ae.add_supported_context(model)
        for cx in AllStoragePresentationContexts:
            ae.add_supported_context(cx.abstract_syntax, scu_role=True, scp_role=True)
        self.ae = ae
        handlers = [
            (evt.EVT_C_ECHO, lambda e: 0x0000),
            (evt.EVT_C_STORE, self._on_store),
            (evt.EVT_C_FIND, self._on_find),
            (evt.EVT_C_GET, self._on_get),
            (evt.EVT_C_MOVE, self._on_move),
        ]
        self.server = ae.start_server(("127.0.0.1", self.port), block=False, evt_handlers=handlers)

    def stop(self) -> None:
        self.server.shutdown()

    def _filters(self, ident: Dataset) -> dict[str, str]:
        return {e.keyword: str(e.value) for e in ident if e.keyword != "QueryRetrieveLevel" and e.VR != "SQ" and e.value not in (None, "")}

    def _on_store(self, event):
        ds = event.dataset
        ds.file_meta = event.file_meta
        self.archive.add(ds)
        return 0x0000

    def _on_find(self, event):
        ident = event.identifier
        for ds in self.archive.query(ident.QueryRetrieveLevel, self._filters(ident)):
            rsp = Dataset()
            rsp.QueryRetrieveLevel = ident.QueryRetrieveLevel
            for elem in ident:
                if elem.keyword and elem.keyword != "QueryRetrieveLevel":
                    setattr(rsp, elem.keyword, ds.get(elem.keyword, ""))
            yield 0xFF00, rsp

    def _on_get(self, event):
        matches = self.archive.match(self._filters(event.identifier))
        yield len(matches)
        for ds in matches:
            yield 0xFF00, ds

    def _on_move(self, event):
        destination = event.move_destination
        if isinstance(destination, bytes):
            destination = destination.decode()
        peer = self.peers.get(destination.strip())
        if peer is None:
            yield None, None
            return
        yield peer[0], peer[1], {"contexts": StoragePresentationContexts}
        matches = self.archive.match(self._filters(event.identifier))
        yield len(matches)
        for ds in matches:
            yield 0xFF00, ds


# ---- DICOMweb ------------------------------------------------------------------------------------

class FakeDicomWeb:
    """Minimal QIDO/WADO/STOW server for httpx.MockTransport."""

    LEVEL = {"studies": "STUDY", "series": "SERIES", "instances": "IMAGE"}

    def __init__(self, archive: FakeArchive, prefix: str = "/dicom-web"):
        self.archive = archive
        self.prefix = prefix
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def _json(ds: Dataset) -> dict:
        clean = ds.copy()
        if "PixelData" in clean:
            del clean.PixelData
        return clean.to_json_dict()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix(self.prefix).strip("/")
        parts = path.split("/")
        if request.method == "POST" and path == "studies":
            return self._stow(request)
        if request.method != "GET":
            return httpx.Response(405)
        if len(parts) == 1 and parts[0] in self.LEVEL:
            params = request.url.params
            filters = {k: v for k, v in params.multi_items() if k not in ("includefield", "limit", "offset", "fuzzymatching")}
            found = self.archive.query(self.LEVEL[parts[0]], filters)
            offset, limit = int(params.get("offset", 0)), int(params.get("limit", 1000))
            found = found[offset: offset + limit]
            if not found:
                return httpx.Response(204)
            return httpx.Response(200, json=[self._json(ds) for ds in found], headers={"content-type": "application/dicom+json"})

        metadata = parts[-1] == "metadata"
        if metadata:
            parts = parts[:-1]
        filters = {}
        for key, attr in (("studies", "StudyInstanceUID"), ("series", "SeriesInstanceUID"), ("instances", "SOPInstanceUID")):
            if key in parts:
                filters[attr] = parts[parts.index(key) + 1]
        found = self.archive.match(filters)
        if not found:
            return httpx.Response(404, text="not found")
        if metadata:
            return httpx.Response(200, json=[self._json(ds) for ds in found], headers={"content-type": "application/dicom+json"})
        payloads = []
        for ds in found:
            buf = io.BytesIO()
            ds.save_as(buf, enforce_file_format=True)
            payloads.append(buf.getvalue())
        content_type, body = build_multipart(payloads)
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    def _stow(self, request: httpx.Request) -> httpx.Response:
        refs = []
        for part in split_multipart(request.headers["content-type"], request.content):
            ds = dcmread(io.BytesIO(part))
            self.archive.add(ds)
            item = Dataset()
            item.ReferencedSOPClassUID = ds.SOPClassUID
            item.ReferencedSOPInstanceUID = ds.SOPInstanceUID
            refs.append(item)
        rsp = Dataset()
        rsp.ReferencedSOPSequence = refs
        return httpx.Response(200, content=json.dumps(rsp.to_json_dict()), headers={"content-type": "application/dicom+json"})


# ---- fixtures ------------------------------------------------------------------------------------

@pytest.fixture
def source_archive() -> FakeArchive:
    return FakeArchive(make_instances())


@pytest.fixture
def dest_archive() -> FakeArchive:
    return FakeArchive()


@pytest.fixture
def pacs(source_archive, dest_archive):
    dest = FakeDimsePacs(dest_archive, ae_title="DESTPACS")
    src = FakeDimsePacs(source_archive, ae_title="FAKEPACS", peers={"DESTPACS": ("127.0.0.1", dest.port)})
    yield src, dest
    src.stop()
    dest.stop()


@pytest.fixture
def web(dest_archive) -> FakeDicomWeb:
    return FakeDicomWeb(dest_archive)


@pytest.fixture
def settings(tmp_path, pacs) -> Settings:
    src, dest = pacs
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), inbox / "ct.dcm")
    (inbox / "notes.txt").write_text("not dicom")
    return Settings.model_validate({
        "calling_ae_title": "DICOM_MCP",
        "storage_dir": str(tmp_path / "cache"),
        "allowed_paths": [str(inbox)],
        "timeouts": {"acse": 5, "dimse": 10, "network": 10, "http": 10},
        "nodes": {
            "pacs": {"protocol": "dimse", "host": "127.0.0.1", "port": src.port, "ae_title": src.ae_title},
            "dest": {"protocol": "dimse", "host": "127.0.0.1", "port": dest.port, "ae_title": dest.ae_title},
            "web": {"protocol": "dicomweb", "base_url": "http://dicomweb.test/dicom-web"},
            "web-ro": {"protocol": "dicomweb", "base_url": "http://dicomweb.test/dicom-web", "read_only": True},
        },
    })
