from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True, slots=True)
class RoleConfig:
    name: str
    model: str
    reasoning_effort: str
    service_tier: str
    sandbox: str
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    max_model_calls: int
    max_rounds: int
    max_attempts_per_packet: int
    max_replans: int
    max_wall_seconds: int
    inherit_user_config: bool
    auto_merge_source_branch: bool
    windows_sandbox: str
    roles: dict[str, RoleConfig]


def load_config(path: Path) -> ControllerConfig:
    path = Path(path).resolve()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    raw_controller = data.get("controller")
    raw_platform = data.get("platform", {})
    raw_roles = data.get("roles")
    if not isinstance(raw_controller, dict):
        raise ValueError("config is missing [controller]")
    if not isinstance(raw_roles, dict):
        raise ValueError("config is missing [roles]")
    if not isinstance(raw_platform, dict):
        raise ValueError("config [platform] must be a table")

    required_roles = {
        "sol_plan",
        "terra_contract",
        "luna_execute",
        "terra_review",
        "sol_final",
    }
    if set(raw_roles) != required_roles:
        missing = sorted(required_roles - set(raw_roles))
        extra = sorted(set(raw_roles) - required_roles)
        raise ValueError(f"role configuration mismatch; missing={missing}, extra={extra}")

    allowed_efforts = {"low", "medium", "high", "xhigh", "max", "ultra"}
    allowed_sandboxes = {"read-only", "workspace-write"}
    roles: dict[str, RoleConfig] = {}
    for role_name, raw in raw_roles.items():
        if not isinstance(raw, dict):
            raise ValueError(f"role {role_name!r} must be a table")
        model = str(raw.get("model", "")).strip()
        effort = str(raw.get("reasoning_effort", "")).strip()
        service_tier = str(raw.get("service_tier", "")).strip()
        sandbox = str(raw.get("sandbox", "")).strip()
        timeout = int(raw.get("timeout_seconds", 0))
        if not model:
            raise ValueError(f"role {role_name!r} must select a model")
        if effort not in allowed_efforts:
            raise ValueError(f"role {role_name!r} has unsupported reasoning effort")
        if service_tier not in {"", "fast"}:
            raise ValueError(f"role {role_name!r} has unsupported service tier")
        if sandbox not in allowed_sandboxes:
            raise ValueError(f"role {role_name!r} has unsupported sandbox")
        if timeout <= 0:
            raise ValueError(f"role {role_name!r} must have a positive timeout")
        roles[role_name] = RoleConfig(
            name=role_name,
            model=model,
            reasoning_effort=effort,
            service_tier=service_tier,
            sandbox=sandbox,
            timeout_seconds=timeout,
        )

    if roles["sol_plan"].sandbox != "read-only" or roles["sol_final"].sandbox != "read-only":
        raise ValueError("Sol roles must remain read-only")
    if roles["terra_review"].sandbox != "read-only":
        raise ValueError("Terra review must remain read-only")
    if roles["luna_execute"].sandbox != "workspace-write":
        raise ValueError("Luna execution requires workspace-write isolation")
    if roles["luna_execute"].reasoning_effort != "max":
        raise ValueError("Luna execution must default to max reasoning")
    if roles["luna_execute"].service_tier:
        raise ValueError("Luna execution must use the standard service tier by default")

    max_attempts = int(raw_controller.get("max_attempts_per_packet", 2))
    max_replans = int(raw_controller.get("max_replans", 1))
    auto_merge = bool(raw_controller.get("auto_merge_source_branch", False))
    if "ignore_execpolicy_rules" in raw_controller:
        raise ValueError("controller no longer accepts ignore_execpolicy_rules")
    if not 1 <= max_attempts <= 2:
        raise ValueError("max_attempts_per_packet must stay between one and two")
    if not 0 <= max_replans <= 1:
        raise ValueError("max_replans must stay between zero and one")
    if auto_merge:
        raise ValueError("automatic merging into the source branch is forbidden")
    windows_sandbox = str(raw_platform.get("windows_sandbox", "elevated"))
    if windows_sandbox not in {"elevated", "unelevated"}:
        raise ValueError("windows_sandbox must be elevated or unelevated")

    config = ControllerConfig(
        max_model_calls=int(raw_controller.get("max_model_calls", 24)),
        max_rounds=int(raw_controller.get("max_rounds", 16)),
        max_attempts_per_packet=max_attempts,
        max_replans=max_replans,
        max_wall_seconds=int(raw_controller.get("max_wall_seconds", 7200)),
        inherit_user_config=bool(raw_controller.get("inherit_user_config", False)),
        auto_merge_source_branch=auto_merge,
        windows_sandbox=windows_sandbox,
        roles=roles,
    )
    if min(
        config.max_model_calls,
        config.max_rounds,
        config.max_wall_seconds,
    ) <= 0:
        raise ValueError("controller budgets must be positive")
    return config
