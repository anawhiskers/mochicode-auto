"""Read-only capability probes for the installed Codex CLI.

The probes deliberately run with a temporary ``CODEX_HOME``.  This keeps
configuration acceptance checks away from the user's active profile and also
means that the only configuration written by this module contains public,
fixed probe values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any


DEFAULT_AGENT_MODEL = "gpt-5.6-luna"
DEFAULT_AGENT_REASONING_EFFORT = "medium"
DEFAULT_AGENT_MAX_THREADS = 8
CONTEXT_ASSUMPTION_TOKENS = 1_000_000
ASTRA_MODEL = "gpt-6-astra"
KNOWN_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)

AGENT_DEFAULTS: dict[str, object] = {
    "enabled": True,
    "max_concurrent_threads_per_session": DEFAULT_AGENT_MAX_THREADS,
    "default_subagent_model": DEFAULT_AGENT_MODEL,
    "default_subagent_reasoning_effort": DEFAULT_AGENT_REASONING_EFFORT,
    "interrupt_message": True,
}

SAFE_FEATURE_DEFAULTS = {
    "multi_agent": True,
    "fast_mode": True,
}

_PROBE_ENV_ALLOWLIST = {
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "SYSTEMROOT",
    "TERM",
    "WINDIR",
}


@dataclass(frozen=True)
class CommandResult:
    """The non-sensitive part of a subprocess observation."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    timed_out: bool = False


CommandRunner = Callable[
    [str | Path, Sequence[str], Mapping[str, str], Path, float],
    CommandResult,
]


def audit_capabilities(
    codex_exe: str | Path,
    *,
    selected_model: str | None = None,
    probe_agent_defaults: bool = True,
    probe_features: bool = True,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Return a redacted capability report for ``codex_exe``.

    Only model slugs, supported effort names, feature states, version
    numbers, and numeric context bounds are retained from command output.
    Command diagnostics are represented by exit status and fixed messages,
    never by raw stderr or stdout.
    """

    executable = str(codex_exe)
    probe_executable = _resolve_executable(codex_exe)
    safe_selected_model = (
        selected_model if _safe_model_slug(selected_model) is not None else None
    )
    runner = command_runner or _run_command
    report: dict[str, Any] = {
        "executable": executable,
        "version": None,
        "available": False,
        "catalog_available": False,
        "catalog_source": "disposable_home",
        "account_access_verified": False,
        "model_catalog": [],
        "astra": {
            "model": ASTRA_MODEL,
            "required_effort": "high",
            "status": "catalog_unavailable",
            "available": False,
            "activation_ready": False,
            "reasoning_efforts": [],
            "fast": False,
        },
        "selected_model": safe_selected_model,
        "selected_model_bounds": None,
        "agent_defaults_probe": {
            "attempted": False,
            "supported": None,
            "detail": "not requested",
        },
        "feature_probe": {
            "attempted": False,
            "features": {},
            "detail": "not requested",
        },
        "unsupported_assumptions": [],
        "warnings": [],
    }

    with _temporary_home() as home:
        version_result = runner(
            probe_executable,
            ("--version",),
            _probe_environment(home),
            home,
            15.0,
        )
        report["version"] = _version_from_output(
            version_result.stdout + "\n" + version_result.stderr
        )
        report["available"] = version_result.returncode == 0

    with _temporary_home() as home:
        catalog_result = runner(
            probe_executable,
            ("debug", "models"),
            _probe_environment(home),
            home,
            20.0,
        )
    catalog = (
        parse_model_catalog(catalog_result.stdout)
        if catalog_result.returncode == 0
        else []
    )
    report["model_catalog"] = catalog
    report["catalog_available"] = bool(catalog) and catalog_result.returncode == 0
    report["astra"] = model_readiness(report, ASTRA_MODEL, "high")
    if report["catalog_available"]:
        report["warnings"].append(
            "The disposable-home catalog describes CLI capabilities; account model access is unverified."
        )
    if catalog_result.returncode == 0:
        report["available"] = True
    elif not report["available"]:
        report["warnings"].append("Codex capability commands were unavailable.")
    if not report["catalog_available"]:
        report["warnings"].append(
            "The installed Codex did not provide a readable model catalog."
        )

    selected = _select_model(catalog, safe_selected_model)
    report["selected_model_bounds"] = selected
    report["unsupported_assumptions"] = _unsupported_context_assumptions(
        catalog, selected, safe_selected_model
    )
    if report["unsupported_assumptions"]:
        report["warnings"].extend(
            item["detail"] for item in report["unsupported_assumptions"]
        )

    if probe_agent_defaults:
        report["agent_defaults_probe"] = _probe_agent_defaults(
            probe_executable, runner
        )
    if probe_features:
        report["feature_probe"] = _probe_features(probe_executable, runner)
    return report


def selected_model_context_bounds(
    capabilities: Mapping[str, Any],
) -> dict[str, int | str | None] | None:
    """Return validated limits used by the opt-in stale-context removal.

    A missing or malformed limit returns ``None`` rather than turning an
    unverified assumption into permission to remove user configuration.
    """

    raw = capabilities.get("selected_model_bounds")
    if not isinstance(raw, Mapping):
        return None
    model = raw.get("slug")
    maximum = _positive_int(raw.get("max_context_window"))
    effective = _positive_int(raw.get("effective_context_window"))
    if (
        _safe_model_slug(model) is None
        or (capabilities.get("selected_model") is not None and model != capabilities["selected_model"])
        or (maximum is None and effective is None)
    ):
        return None
    return {
        "slug": model,
        "max_context_window": maximum,
        "effective_context_window": effective,
    }


def model_readiness(
    capabilities: Mapping[str, Any], model_slug: str, required_effort: str
) -> dict[str, Any]:
    """Check catalog compatibility for an opt-in candidate, not authenticated API access."""

    if (
        _safe_model_slug(model_slug) is None
        or not isinstance(required_effort, str)
        or required_effort not in KNOWN_REASONING_EFFORTS
    ):
        raise ValueError("model readiness request is invalid")
    catalog = capabilities.get("model_catalog")
    if capabilities.get("catalog_available") is not True or not isinstance(catalog, list):
        return {
            "model": model_slug,
            "required_effort": required_effort,
            "status": "catalog_unavailable",
            "available": False,
            "activation_ready": False,
            "reasoning_efforts": [],
            "fast": False,
        }
    matches = [
        item for item in catalog
        if isinstance(item, Mapping) and item.get("slug") == model_slug
    ]
    model = matches[0] if len(matches) == 1 else None
    if model is None:
        return {
            "model": model_slug,
            "required_effort": required_effort,
            "status": "catalog_ambiguous" if matches else "slug_absent",
            "available": False,
            "activation_ready": False,
            "reasoning_efforts": [],
            "fast": False,
        }
    efforts = model.get("reasoning_efforts")
    safe_efforts = [
        value
        for value in efforts
        if isinstance(value, str) and value in KNOWN_REASONING_EFFORTS
    ] if isinstance(efforts, list) else []
    supported = required_effort in safe_efforts
    return {
        "model": model_slug,
        "required_effort": required_effort,
        "status": "ready" if supported else "effort_unsupported",
        "available": True,
        "activation_ready": supported,
        "reasoning_efforts": safe_efforts,
        "fast": model.get("fast") is True,
    }


def _temporary_home():
    return tempfile.TemporaryDirectory(prefix="mochicode-adaptive-")


def _resolve_executable(executable: str | Path) -> str:
    value = str(executable)
    candidate = Path(value)
    if candidate.is_absolute() or candidate.parent != Path("."):
        return str(candidate.resolve(strict=False))
    return shutil.which(value) or value


def _probe_environment(home: str | Path) -> dict[str, str]:
    home_path = str(home)
    probe_root = Path(home_path)
    for relative in ("appdata", "localappdata", "temp"):
        (probe_root / relative).mkdir(parents=True, exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _PROBE_ENV_ALLOWLIST
    }
    environment["CODEX_HOME"] = home_path
    environment["HOME"] = home_path
    environment["USERPROFILE"] = home_path
    environment["APPDATA"] = str(probe_root / "appdata")
    environment["LOCALAPPDATA"] = str(probe_root / "localappdata")
    environment["TEMP"] = str(probe_root / "temp")
    environment["TMP"] = str(probe_root / "temp")
    environment["TMPDIR"] = str(probe_root / "temp")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _run_command(
    executable: str | Path,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    cwd: Path,
    timeout: float,
) -> CommandResult:
    try:
        result = subprocess.run(
            [str(executable), *arguments],
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(None, error="command timed out", timed_out=True)
    except OSError:
        return CommandResult(None, error="command could not be started")
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _version_from_output(output: str) -> str | None:
    match = re.search(r"\b(?:codex-cli\s+)?(\d+\.\d+(?:\.\d+){0,2})\b", output)
    return match.group(1) if match else None


def parse_model_catalog(output: str) -> list[dict[str, Any]]:
    value = _json_value(output)
    if not isinstance(value, Mapping):
        return []
    raw_models = value.get("models")
    if not isinstance(raw_models, list):
        raw_catalog = value.get("model_catalog")
        raw_models = raw_catalog if isinstance(raw_catalog, list) else []

    catalog: list[dict[str, Any]] = []
    for raw in raw_models:
        if not isinstance(raw, Mapping):
            continue
        slug = raw.get("slug") or raw.get("id") or raw.get("name")
        if _safe_model_slug(slug) is None:
            continue
        item: dict[str, Any] = {"slug": slug}
        display_name = raw.get("display_name")
        if _safe_display_name(display_name):
            item["display_name"] = display_name
        for output_name, input_names in (
            ("context_window", ("context_window", "context_length")),
            ("max_context_window", ("max_context_window", "max_context_length")),
            (
                "effective_context_window",
                (
                    "effective_context_window",
                    "effective_context_window_tokens",
                    "effective_window",
                ),
            ),
            (
                "effective_context_window_percent",
                ("effective_context_window_percent",),
            ),
        ):
            numeric = _first_positive_number(raw, input_names)
            if output_name == "effective_context_window_percent":
                if numeric is not None and numeric > 100:
                    numeric = None
            else:
                numeric = _positive_int(numeric)
            if numeric is not None:
                item[output_name] = numeric
        if "effective_context_window" not in item:
            context_window = item.get("context_window")
            if isinstance(context_window, int):
                item["effective_context_window"] = context_window
            elif (
                isinstance(item.get("max_context_window"), int)
                and isinstance(item.get("effective_context_window_percent"), (int, float))
            ):
                item["effective_context_window"] = int(
                    item["max_context_window"]
                    * item["effective_context_window_percent"]
                    / 100
                )

        effort_values = raw.get("supported_reasoning_levels")
        if isinstance(effort_values, list):
            efforts = []
            for effort in effort_values:
                if isinstance(effort, Mapping) and isinstance(effort.get("effort"), str):
                    efforts.append(effort["effort"])
                elif isinstance(effort, str):
                    efforts.append(effort)
            safe_efforts = [
                effort
                for effort in efforts
                if effort in KNOWN_REASONING_EFFORTS
            ]
            if safe_efforts:
                item["reasoning_efforts"] = list(dict.fromkeys(safe_efforts))

        item["fast"] = _has_fast_support(raw)
        catalog.append(item)
    return catalog


def _json_value(output: str) -> Any:
    if not isinstance(output, str):
        return None
    try:
        return json.loads(output)
    except (json.JSONDecodeError, ValueError, RecursionError):
        pass
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(output[start : end + 1])
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None


def _first_positive_number(
    value: Mapping[str, Any], names: Sequence[str]
) -> int | float | None:
    for name in names:
        candidate = value.get(name)
        if isinstance(candidate, bool):
            continue
        if (
            isinstance(candidate, (int, float))
            and 0 < candidate <= 2**63 - 1
            and (isinstance(candidate, int) or math.isfinite(candidate))
        ):
            return candidate
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _has_fast_support(value: Mapping[str, Any]) -> bool:
    speed_values = value.get("additional_speed_tiers")
    if isinstance(speed_values, list) and any(
        isinstance(item, str) and item.lower() == "fast" for item in speed_values
    ):
        return True
    services = value.get("service_tiers")
    if isinstance(services, list):
        for service in services:
            if isinstance(service, Mapping):
                values = (service.get("id"), service.get("name"))
                if any(isinstance(item, str) and item.lower() == "fast" for item in values):
                    return True
    return False


def _select_model(
    catalog: Sequence[Mapping[str, Any]], selected_model: str | None
) -> dict[str, Any] | None:
    if not isinstance(selected_model, str):
        return None
    matches = [model for model in catalog if model.get("slug") == selected_model]
    return dict(matches[0]) if len(matches) == 1 else None


def _safe_model_slug(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        return None
    return value


def _safe_display_name(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}", value)
    )


def _unsupported_context_assumptions(
    catalog: Sequence[Mapping[str, Any]],
    selected: Mapping[str, Any] | None,
    selected_model: str | None,
) -> list[dict[str, Any]]:
    if selected is not None:
        maximum = _positive_int(selected.get("max_context_window"))
        if maximum is not None and CONTEXT_ASSUMPTION_TOKENS > maximum:
            return [
                {
                    "assumption": CONTEXT_ASSUMPTION_TOKENS,
                    "supported": False,
                    "model": selected.get("slug"),
                    "detail": (
                        f"The catalog advertises a lower maximum for {selected.get('slug')} "
                        f"than the {CONTEXT_ASSUMPTION_TOKENS:,}-token context setting; "
                        "effective account limits are unverified."
                    ),
                }
            ]
        return []

    bounded_models = [
        model
        for model in catalog
        if isinstance(model.get("max_context_window"), int)
        or isinstance(model.get("effective_context_window"), int)
    ]
    if bounded_models and not any(
        max(
            int(model.get("max_context_window") or 0),
            int(model.get("effective_context_window") or 0),
        )
        >= CONTEXT_ASSUMPTION_TOKENS
        for model in bounded_models
    ):
        detail = (
            "The current model catalog does not support the "
            f"{CONTEXT_ASSUMPTION_TOKENS:,}-token context assumption."
        )
        if selected_model:
            detail = (
                f"Model {selected_model} was not found, and the current catalog does not "
                f"support the {CONTEXT_ASSUMPTION_TOKENS:,}-token context assumption."
            )
        return [
            {
                "assumption": CONTEXT_ASSUMPTION_TOKENS,
                "supported": False,
                "model": selected_model,
                "detail": detail,
            }
        ]
    return []


def _probe_agent_defaults(
    executable: str | Path, runner: CommandRunner
) -> dict[str, Any]:
    with _temporary_home() as home:
        config_lines = ["[agents]"]
        for key, value in AGENT_DEFAULTS.items():
            config_lines.append(f"{key} = {_toml_literal(value)}")
        (Path(home) / "config.toml").write_text(
            "\n".join(config_lines) + "\n", encoding="utf-8"
        )
        result = runner(
            executable,
            ("features", "list"),
            _probe_environment(home),
            Path(home),
            15.0,
        )
    if result.returncode == 0:
        return {
            "attempted": True,
            "supported": True,
            "detail": "Codex accepted the scalar [agents] probe in a disposable home.",
        }
    return {
        "attempted": True,
        "supported": False,
        "detail": "Codex did not accept the scalar [agents] probe; agent defaults were not proven safe.",
    }


def _probe_features(
    executable: str | Path, runner: CommandRunner
) -> dict[str, Any]:
    with _temporary_home() as home:
        config = "[features]\n" + "".join(
            f"{name} = true\n" for name in SAFE_FEATURE_DEFAULTS
        )
        (Path(home) / "config.toml").write_text(config, encoding="utf-8")
        result = runner(
            executable,
            ("features", "list"),
            _probe_environment(home),
            Path(home),
            15.0,
        )
    parsed = _feature_rows(result.stdout) if result.returncode == 0 else {}
    features: dict[str, Any] = {}
    for name in SAFE_FEATURE_DEFAULTS:
        row = parsed.get(name)
        if row is None:
            features[name] = {
                "supported": False,
                "stage": None,
                "enabled": None,
            }
        else:
            features[name] = {
                "supported": row["stage"] != "removed" and row["enabled"] is True,
                "stage": row["stage"],
                "enabled": row["enabled"],
            }
    detail = (
        "Codex accepted the feature probe in a disposable home."
        if result.returncode == 0
        else "Codex did not accept the feature probe."
    )
    return {
        "attempted": True,
        "features": features,
        "detail": detail,
    }


def _feature_rows(output: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[-1].lower() not in {"true", "false"}:
            continue
        name = parts[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            continue
        rows[name] = {
            "stage": " ".join(parts[1:-1]).lower(),
            "enabled": parts[-1].lower() == "true",
        }
    return rows


def _toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise TypeError(f"unsupported probe value type: {type(value).__name__}")
