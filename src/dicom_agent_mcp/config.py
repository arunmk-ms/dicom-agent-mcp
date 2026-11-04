from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Union
from urllib.parse import unquote, urlsplit

import yaml
from pydantic import BaseModel, Field

from .errors import DicomMcpError

AeTitle = Annotated[str, Field(min_length=1, max_length=16, pattern=r"^[ -\[\]-~]+$")]


class BasicAuth(BaseModel):
    type: Literal["basic"]
    username: str
    password_env: str = Field(description="Environment variable that holds the password.")


class BearerAuth(BaseModel):
    type: Literal["bearer"]
    token_env: str = Field(description="Environment variable that holds the bearer token.")


Auth = Annotated[Union[BasicAuth, BearerAuth], Field(discriminator="type")]


class DimseNode(BaseModel):
    protocol: Literal["dimse"]
    description: str = ""
    read_only: bool = False
    host: str
    port: int = Field(ge=1, le=65535)
    ae_title: AeTitle


class DicomWebNode(BaseModel):
    protocol: Literal["dicomweb"]
    description: str = ""
    read_only: bool = False
    base_url: str
    auth: Auth | None = None
    verify_tls: bool = True
    # Value of the transfer-syntax parameter in WADO-RS Accept headers; "*" keeps the stored encoding.
    transfer_syntax: str | None = "*"


Node = Annotated[Union[DimseNode, DicomWebNode], Field(discriminator="protocol")]


class Timeouts(BaseModel):
    acse: float = 30
    dimse: float = 60
    network: float = 60
    http: float = 60


class Settings(BaseModel):
    calling_ae_title: AeTitle = "DICOM_MCP"
    storage_dir: Path = Path("dicom-cache")
    allowed_paths: list[Path] = Field(default_factory=list)
    max_results: int = Field(500, ge=1)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    max_volume_voxels: int = Field(100_000_000, ge=1, description="Upper bound for volumes loaded by render_mpr.")
    deid_key_env: str = Field(
        "DICOM_AGENT_MCP_DEID_KEY",
        description="Environment variable with the secret that makes pseudonyms and UIDs consistent across calls.",
    )
    nodes: dict[str, Node] = Field(default_factory=dict)
    verification: Literal["off", "advisory", "enforce"] = Field(
        "advisory",
        description="TEVL policy: off; advisory (record and report verdicts); enforce (block HARD_FAIL, and SOFT_FAIL on store/transfer).",
    )

    def readable_roots(self) -> list[Path]:
        return [self.storage_dir.resolve(), *(p.resolve() for p in self.allowed_paths)]

    @property
    def ledger_path(self) -> Path:
        return self.storage_dir / "verification" / "ledger.jsonl"


ENV_PREFIX = "DICOM_AGENT_MCP_"
DEFAULT_CONFIG_FILE = "dicom-agent-mcp.yaml"
# Used when no config file sets storage_dir: a harness may start the server in any working directory.
DEFAULT_STORAGE_DIR = Path("~/.dicom-agent-mcp/cache")
NODE_SPEC_HELP = (
    "name=dimse://AE_TITLE@host:port or name=https://server/dicom-web, optionally followed by "
    ";read-only ;token-env=VAR ;user=NAME;password-env=VAR ;no-verify-tls ;transfer-syntax=UID"
)


def parse_node(spec: str) -> tuple[str, dict[str, Any]]:
    """Parse a command-line node spec into (name, node config)."""
    name, sep, rest = spec.partition("=")
    name = name.strip()
    if not sep or not name or not rest.strip():
        raise DicomMcpError(f"Invalid node '{spec}'. Expected {NODE_SPEC_HELP}.")
    url, *raw_options = (part.strip() for part in rest.split(";"))
    options: dict[str, str] = {}
    for option in filter(None, raw_options):
        key, _, value = option.partition("=")
        options[key.strip().lower()] = value.strip()

    node: dict[str, Any] = {"read_only": options.pop("read-only", None) is not None}
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "dimse":
        try:
            port = parts.port
        except ValueError:
            port = None
        if not parts.username or not parts.hostname or not port:
            raise DicomMcpError(f"Invalid DIMSE node '{name}'. Expected dimse://AE_TITLE@host:port.")
        node.update(protocol="dimse", host=parts.hostname, port=port, ae_title=unquote(parts.username))
    elif scheme in ("http", "https"):
        node.update(protocol="dicomweb", base_url=url.rstrip("/"))
        if "token-env" in options:
            node["auth"] = {"type": "bearer", "token_env": options.pop("token-env")}
        elif "user" in options or "password-env" in options:
            if not options.get("user") or not options.get("password-env"):
                raise DicomMcpError(f"Node '{name}': basic auth needs both user=NAME and password-env=VAR.")
            node["auth"] = {"type": "basic", "username": options.pop("user"), "password_env": options.pop("password-env")}
        if options.pop("no-verify-tls", None) is not None:
            node["verify_tls"] = False
        if "transfer-syntax" in options:
            node["transfer_syntax"] = options.pop("transfer-syntax") or None
    else:
        raise DicomMcpError(f"Node '{name}' has unsupported scheme '{parts.scheme}'. Use dimse://, http:// or https://.")
    if options:
        raise DicomMcpError(f"Unknown option(s) for node '{name}': {', '.join(sorted(options))}.")
    return name, node


def build_settings(
    config: str | os.PathLike[str] | None = None,
    *,
    nodes: Iterable[str] = (),
    storage_dir: str | None = None,
    allowed_paths: Iterable[str] = (),
    calling_ae_title: str | None = None,
    verification: str | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Settings:
    """Merge settings from an optional YAML file, then DICOM_AGENT_MCP_* environment variables, then arguments.

    Scalars from later sources override earlier ones; nodes are merged by name; allowed paths are combined.
    """
    env = os.environ if environ is None else environ
    cwd = (cwd or Path.cwd()).resolve()

    config = config or env.get(f"{ENV_PREFIX}CONFIG")
    if config is None and (cwd / DEFAULT_CONFIG_FILE).is_file():
        config = cwd / DEFAULT_CONFIG_FILE

    raw: dict[str, Any] = {}
    if config is not None:
        config_path = _resolve(cwd, Path(config))
        if not config_path.is_file():
            raise DicomMcpError(f"Config file not found: {config_path}")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise DicomMcpError(f"Config file {config_path} must contain a mapping.")
        base = config_path.parent
        raw["storage_dir"] = str(_resolve(base, Path(raw.get("storage_dir") or "dicom-cache")))
        raw["allowed_paths"] = [str(_resolve(base, Path(p))) for p in raw.get("allowed_paths") or []]
    raw.setdefault("storage_dir", str(DEFAULT_STORAGE_DIR.expanduser()))
    raw["allowed_paths"] = list(raw.get("allowed_paths") or [])
    raw["nodes"] = dict(raw.get("nodes") or {})

    env_paths = [p for p in env.get(f"{ENV_PREFIX}ALLOWED_PATHS", "").split(os.pathsep) if p.strip()]
    for source_storage, source_paths, source_ae, source_nodes in (
        (env.get(f"{ENV_PREFIX}STORAGE_DIR"), env_paths, env.get(f"{ENV_PREFIX}CALLING_AE"), env.get(f"{ENV_PREFIX}NODES", "").split()),
        (storage_dir, list(allowed_paths), calling_ae_title, list(nodes)),
    ):
        if source_storage:
            raw["storage_dir"] = str(_resolve(cwd, Path(source_storage)))
        raw["allowed_paths"] += [str(_resolve(cwd, Path(p))) for p in source_paths]
        if source_ae:
            raw["calling_ae_title"] = source_ae
        for spec in source_nodes:
            name, node = parse_node(spec)
            raw["nodes"][name] = node
    for source_policy in (env.get(f"{ENV_PREFIX}VERIFICATION"), verification):
        if source_policy:
            raw["verification"] = source_policy.strip().lower()
    return Settings.model_validate(raw)


def load_settings(path: str | os.PathLike[str]) -> Settings:
    """Load settings from a YAML file only (relative paths resolve against the file's directory)."""
    return build_settings(path, environ={})


def _resolve(base: Path, path: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else (base / path).resolve()
