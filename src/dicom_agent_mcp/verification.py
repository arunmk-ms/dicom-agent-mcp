"""Tool Execution Verification Loop (TEVL): verdicts, the geometry-code mapping and the verdict ledger.

Every tool call is checked against a contract: preconditions on its inputs, postconditions on its outputs.
Each check yields PASS, SOFT_FAIL or HARD_FAIL; the call's verdict is the worst of them. Verdicts are written to
an append-only, hash-chained JSON Lines ledger so they can be audited later (and, for ``store``/``transfer``,
are written before anything is committed to an archive).

Policies:
- ``off``: no checks, no ledger.
- ``advisory`` (default): checks run, verdicts are recorded and returned, nothing is blocked.
- ``enforce``: a HARD_FAIL precondition stops the tool; for commits (store/transfer) a SOFT_FAIL also stops it
  unless the caller passes ``accept_soft_fail``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import platform
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from .errors import DicomMcpError

PASS, SOFT_FAIL, HARD_FAIL = "PASS", "SOFT_FAIL", "HARD_FAIL"
_RANK = {PASS: 0, SOFT_FAIL: 1, HARD_FAIL: 2}
Policy = Literal["off", "advisory", "enforce"]
Phase = Literal["pre", "post", "gate"]

GENESIS = "sha256:" + "0" * 64


@dataclass
class Check:
    phase: Phase
    code: str
    verdict: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"phase": self.phase, "code": self.code, "verdict": self.verdict, "message": self.message}
        if self.evidence:
            out["evidence"] = self.evidence
        return out


def worst(verdicts: Iterable[str]) -> str:
    return max(verdicts, key=_RANK.__getitem__, default=PASS)


def overall(checks: Iterable[Check]) -> str:
    return worst(c.verdict for c in checks)


class VerificationError(DicomMcpError):
    """A tool call blocked by the ``enforce`` policy. The blocking verdict is already in the ledger."""

    def __init__(self, message: str, record: Mapping[str, Any]):
        super().__init__(message)
        self.record = dict(record)


# ---- check_geometry issue codes -> verdicts ------------------------------------------------------

SEVERITY_VERDICT = {"error": HARD_FAIL, "warning": SOFT_FAIL, "info": PASS}

# Per-tool overrides: the same geometry finding matters differently depending on what the tool does with it.
GEOMETRY_OVERRIDES: dict[str, dict[str, str]] = {
    # A reformat assumes axis-aligned slices; an oblique stack is drawn as if it were not.
    "render_mpr": {"OBLIQUE": SOFT_FAIL},
    # Archives accept incomplete or mixed series, so geometry problems only need acknowledging before a commit;
    # objects without plane geometry (SR, projection images) are normal.
    "store": {
        "MISSING_SLICES": SOFT_FAIL, "DUPLICATE_POSITIONS": SOFT_FAIL, "MIXED_ORIENTATION": SOFT_FAIL,
        "MULTIPLE_FRAMES_OF_REFERENCE": SOFT_FAIL, "INCONSISTENT_MATRIX": SOFT_FAIL,
        "INCONSISTENT_PIXEL_SPACING": SOFT_FAIL, "MISSING_PIXEL_SPACING": PASS, "NON_ORTHONORMAL_ORIENTATION": SOFT_FAIL,
        "GEOMETRY_MISSING": PASS, "NON_UNIFORM_SPACING": PASS, "SHEARED_STACK": PASS,
    },
}


def geometry_verdict(tool: str, severity: str, code: str) -> str:
    return GEOMETRY_OVERRIDES.get(tool, {}).get(code, SEVERITY_VERDICT.get(severity, SOFT_FAIL))


def geometry_checks(tool: str, issues: Iterable[Mapping[str, str]], phase: Phase = "pre", series_uid: str | None = None) -> list[Check]:
    checks = []
    for issue in issues:
        evidence = {"severity": issue["severity"]}
        if series_uid:
            evidence["series_uid"] = series_uid
        checks.append(Check(phase, issue["code"], geometry_verdict(tool, issue["severity"], issue["code"]), issue["message"], evidence))
    return checks


# ---- digests -------------------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any, extra: bytes | None = None) -> str:
    h = hashlib.sha256(canonical_json(value).encode())
    if extra:
        h.update(extra)
    return "sha256:" + h.hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


# ---- ledger --------------------------------------------------------------------------------------

_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path.resolve(), threading.Lock())


class Ledger:
    """Append-only JSON Lines file; each record carries the hash of the previous one, so edits are detectable."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = _lock_for(path)

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _tail(self) -> tuple[int, str]:
        last = None
        if self.path.exists():
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last = line
        if last is None:
            return 0, GENESIS
        record = json.loads(last)
        return int(record["seq"]), str(record["hash"])

    def append(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            seq, prev = self._tail()
            record = {
                "seq": seq + 1,
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                **entry,
                "prev_hash": prev,
            }
            record["hash"] = _record_hash(record)
            record["id"] = f"ver_{record['seq']:06d}_{record['hash'][7:15]}"
            with self.path.open("a", encoding="utf-8") as f:
                f.write(canonical_json(record) + "\n")
            return record

    def get(self, record_id: str) -> dict[str, Any] | None:
        return next((r for r in self.records() if r.get("id") == record_id), None)

    def find(self, *, fingerprint: str | None = None, file_digest: str | None = None, tool: str | None = None) -> list[dict[str, Any]]:
        out = []
        for r in self.records():
            if tool and r.get("tool") != tool:
                continue
            if fingerprint and fingerprint not in r.get("input_fingerprints", []) and fingerprint not in r.get("output_fingerprints", []):
                continue
            if file_digest and file_digest not in r.get("output_files", {}).values():
                continue
            out.append(r)
        return out

    def check_chain(self) -> dict[str, Any]:
        prev = GENESIS
        records = self.records()
        for i, r in enumerate(records):
            body = {k: v for k, v in r.items() if k not in ("hash", "id")}
            if r.get("prev_hash") != prev or _record_hash(body) != r.get("hash") or r.get("seq") != i + 1:
                return {"ok": False, "records": len(records), "first_bad_seq": i + 1}
            prev = r["hash"]
        return {"ok": True, "records": len(records)}


def _record_hash(record: Mapping[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k not in ("hash", "id")}
    return "sha256:" + hashlib.sha256(canonical_json(body).encode()).hexdigest()


_PACKAGES = ("dicom-agent-mcp", "pydicom", "pynetdicom", "numpy", "pillow", "httpx", "mcp")


@lru_cache(maxsize=1)
def environment() -> dict[str, str]:
    """Versions that determine a tool's output, recorded with every verdict so results can be reproduced."""
    versions = {"python": platform.python_version()}
    for name in _PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


# ---- running a contract --------------------------------------------------------------------------


class Verifier:
    def __init__(self, policy: Policy, ledger_path: Path):
        self.policy = policy
        self.ledger = Ledger(ledger_path)

    @property
    def enabled(self) -> bool:
        return self.policy != "off"

    def blocks(self, verdict: str, *, commit: bool, accept_soft_fail: bool) -> bool:
        if self.policy != "enforce":
            return False
        if verdict == HARD_FAIL:
            return True
        return commit and verdict == SOFT_FAIL and not accept_soft_fail

    def record(
        self,
        tool: str,
        kind: str,
        args: Mapping[str, Any],
        checks: list[Check],
        **extra: Any,
    ) -> dict[str, Any]:
        entry = {
            "tool": tool, "kind": kind, "policy": self.policy, "verdict": overall(checks),
            "args": dict(args), "checks": [c.to_dict() for c in checks], "environment": environment(), **extra,
        }
        return self.ledger.append(entry)

    def block(self, tool: str, args: Mapping[str, Any], checks: list[Check], *, commit: bool, **extra: Any) -> None:
        record = self.record(tool, "gate" if commit else "pre", args, checks, blocked=True, **extra)
        bad = [c.code for c in checks if c.verdict != PASS]
        hint = " Pass accept_soft_fail=true to proceed after review." if record["verdict"] == SOFT_FAIL else ""
        raise VerificationError(
            f"{tool} blocked by verification policy 'enforce': {record['verdict']} ({', '.join(bad)}). "
            f"Ledger record {record['id']}.{hint}",
            record,
        )


def summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """The verification block attached to a tool result."""
    out = {"id": record["id"], "verdict": record["verdict"], "policy": record["policy"], "checks": record["checks"]}
    for key in ("gate_id", "gate_verdict", "accepted_soft_fail", "input_fingerprints"):
        if key in record:
            out[key] = record[key]
    return out
