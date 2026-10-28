"""DICOMweb client: QIDO-RS (search), WADO-RS (retrieve and metadata) and STOW-RS (store)."""

from __future__ import annotations

import io
import os
import uuid
from email.message import Message
from pathlib import Path
from typing import Any

import httpx
from pydicom import dcmread
from pydicom.dataset import Dataset

from .config import BasicAuth, BearerAuth, DicomWebNode, Settings
from .datasets import Level, check_uid, instance_path
from .errors import DicomMcpError

DICOM_JSON = "application/dicom+json"
_RESOURCE = {Level.STUDY: "studies", Level.SERIES: "series", Level.IMAGE: "instances"}


def dataset_from_json(obj: dict[str, Any]) -> Dataset:
    # Bulk data is referenced by URI in DICOM JSON; the agent only needs to know it exists.
    return Dataset.from_json(obj, bulk_data_uri_handler=lambda tag, vr, uri: None)


def split_multipart(content_type: str, body: bytes) -> list[bytes]:
    """Split a ``multipart/related`` body into the raw bytes of each part."""
    msg = Message()
    msg["content-type"] = content_type
    boundary = msg.get_param("boundary")
    if not boundary:
        raise DicomMcpError(f"Multipart response has no boundary (Content-Type: {content_type}).")
    delimiter = b"--" + str(boundary).encode()
    parts = []
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break
        header_end = chunk.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        content = chunk[header_end + 4:]
        if content.endswith(b"\r\n"):
            content = content[:-2]
        parts.append(content)
    return parts


def build_multipart(payloads: list[bytes], part_type: str = "application/dicom") -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    body = bytearray()
    for payload in payloads:
        body += f"--{boundary}\r\nContent-Type: {part_type}\r\n\r\n".encode()
        body += payload + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return f'multipart/related; type="{part_type}"; boundary={boundary}', bytes(body)


class DicomWebClient:
    def __init__(self, name: str, node: DicomWebNode, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.name = name
        self.node = node
        self.settings = settings
        self.transport = transport

    def _client(self) -> httpx.Client:
        headers: dict[str, str] = {}
        auth: httpx.Auth | None = None
        if isinstance(self.node.auth, BearerAuth):
            headers["Authorization"] = f"Bearer {self._secret(self.node.auth.token_env)}"
        elif isinstance(self.node.auth, BasicAuth):
            auth = httpx.BasicAuth(self.node.auth.username, self._secret(self.node.auth.password_env))
        return httpx.Client(
            base_url=self.node.base_url.rstrip("/") + "/",
            headers=headers,
            auth=auth,
            timeout=self.settings.timeouts.http,
            verify=self.node.verify_tls,
            transport=self.transport,
        )

    def _secret(self, env: str) -> str:
        value = os.environ.get(env)
        if not value:
            raise DicomMcpError(f"Node '{self.name}' needs the environment variable {env}, which is not set.")
        return value

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            with self._client() as client:
                response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise DicomMcpError(f"DICOMweb request to '{self.name}' failed: {exc}") from exc
        if response.status_code >= 400 and response.status_code != 409:
            detail = response.text[:500]
            raise DicomMcpError(f"{method} {url} on '{self.name}' returned HTTP {response.status_code}: {detail}")
        return response

    def echo(self) -> dict[str, Any]:
        response = self._request("GET", "studies", params={"limit": 1}, headers={"Accept": DICOM_JSON})
        return {"ok": response.status_code in (200, 204), "status": f"HTTP {response.status_code}", "operation": "QIDO-RS"}

    def search(self, level: Level, params: list[tuple[str, str]], limit: int, offset: int) -> tuple[list[Dataset], bool]:
        if level is Level.PATIENT:
            raise DicomMcpError(
                "QIDO-RS has no patient level. Search at STUDY level with PatientID or PatientName filters."
            )
        query = [*params, ("limit", str(limit + 1)), ("offset", str(offset))]
        response = self._request("GET", _RESOURCE[level], params=query, headers={"Accept": DICOM_JSON})
        items = [] if response.status_code == 204 or not response.content else response.json()
        datasets = [dataset_from_json(item) for item in items]
        return datasets[:limit], len(datasets) > limit

    def metadata(self, study_uid: str, series_uid: str | None, sop_uid: str | None) -> list[Dataset]:
        response = self._request("GET", _path(study_uid, series_uid, sop_uid) + "/metadata", headers={"Accept": DICOM_JSON})
        if response.status_code == 204 or not response.content:
            return []
        return [dataset_from_json(item) for item in response.json()]

    def retrieve(self, study_uid: str, series_uid: str | None, sop_uid: str | None, dest: Path) -> dict[str, Any]:
        accept = 'multipart/related; type="application/dicom"'
        if self.node.transfer_syntax:
            accept += f"; transfer-syntax={self.node.transfer_syntax}"
        response = self._request("GET", _path(study_uid, series_uid, sop_uid), headers={"Accept": accept})
        files = []
        for part in split_multipart(response.headers.get("content-type", ""), response.content):
            ds = dcmread(io.BytesIO(part), stop_before_pixels=True)
            path = instance_path(dest, ds)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(part)
            files.append(str(path))
        return {"operation": "WADO-RS", "ok": True, "completed": len(files), "failed": 0, "files": files}

    def store(self, paths: list[Path]) -> dict[str, Any]:
        content_type, body = build_multipart([p.read_bytes() for p in paths])
        response = self._request(
            "POST", "studies", content=body, headers={"Content-Type": content_type, "Accept": DICOM_JSON}
        )
        result = dataset_from_json(response.json()) if response.content else Dataset()
        stored = [str(item.ReferencedSOPInstanceUID) for item in result.get("ReferencedSOPSequence", [])]
        failed = [
            {"sop_instance_uid": str(item.get("ReferencedSOPInstanceUID", "")), "reason": f"0x{int(item.get('FailureReason', 0)):04X}"}
            for item in result.get("FailedSOPSequence", [])
        ]
        return {
            "operation": "STOW-RS",
            "http_status": response.status_code,
            "stored": len(stored),
            "failed": len(failed),
            "stored_sop_instance_uids": stored,
            "failures": failed,
        }


def _path(study_uid: str, series_uid: str | None, sop_uid: str | None) -> str:
    path = f"studies/{check_uid(study_uid, 'StudyInstanceUID')}"
    if series_uid:
        path += f"/series/{check_uid(series_uid, 'SeriesInstanceUID')}"
        if sop_uid:
            path += f"/instances/{check_uid(sop_uid, 'SOPInstanceUID')}"
    elif sop_uid:
        raise DicomMcpError("sop_instance_uid requires series_uid for DICOMweb retrieval.")
    return path
