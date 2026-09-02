from __future__ import annotations

from collections.abc import Mapping
import os
import re
import threading


DEFAULT_CAPTURE_LIMIT_BYTES = 4 * 1024 * 1024
RESOURCE_LIMIT_RETURN_CODE = 125
TRUNCATION_MARKER = "\n[mochicode output limit exceeded]\n"

_SAFE_ENVIRONMENT_NAMES = {
    "ALLUSERSPROFILE",
    "APPDATA",
    "CODEX_HOME",
    "CODEX_CI",
    "COLORTERM",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432",
    "COMSPEC",
    "COMPUTERNAME",
    "GIT_CONFIG_NOSYSTEM",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "LOGONSERVER",
    "NO_COLOR",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PSMODULEPATH",
    "PUBLIC",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "USERDOMAIN",
    "USERDOMAIN_ROAMINGPROFILE",
    "USERNAME",
    "WINDIR",
}
_EXPLICIT_ALLOWLIST_VARIABLE = "MOCHICODE_CHILD_ENV_ALLOWLIST"
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def build_child_environment(
    source: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a small child environment with optional operator-declared passthroughs."""

    values = os.environ if source is None else source
    allowed = set(_SAFE_ENVIRONMENT_NAMES)
    requested = values.get(_EXPLICIT_ALLOWLIST_VARIABLE, "")
    for raw_name in requested.split(","):
        name = raw_name.strip()
        if name and _ENVIRONMENT_NAME.fullmatch(name):
            allowed.add(name.upper())

    environment = {
        key: value
        for key, value in values.items()
        if key.upper() in allowed or key.upper().startswith("LC_")
    }
    environment.pop(_EXPLICIT_ALLOWLIST_VARIABLE, None)
    if overrides:
        environment.update({str(key): str(value) for key, value in overrides.items()})
    return environment


class BoundedTextCapture:
    """Thread-safe UTF-8 capture that signals immediately after a byte limit."""

    def __init__(
        self,
        max_bytes: int = DEFAULT_CAPTURE_LIMIT_BYTES,
        *,
        limit_event: threading.Event | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("capture limit must be positive")
        self.max_bytes = int(max_bytes)
        self.limit_event = limit_event or threading.Event()
        self._parts: list[str] = []
        self._stored_bytes = 0
        self._truncated = False
        self._lock = threading.Lock()

    @property
    def truncated(self) -> bool:
        with self._lock:
            return self._truncated

    @property
    def stored_bytes(self) -> int:
        with self._lock:
            return self._stored_bytes

    def append(self, value: str) -> str:
        encoded = value.encode("utf-8")
        with self._lock:
            if self._truncated:
                return ""
            remaining = self.max_bytes - self._stored_bytes
            if len(encoded) <= remaining:
                self._parts.append(value)
                self._stored_bytes += len(encoded)
                return value
            prefix = encoded[:remaining].decode("utf-8", errors="ignore")
            if prefix:
                self._parts.append(prefix)
                self._stored_bytes += len(prefix.encode("utf-8"))
            self._truncated = True
            self.limit_event.set()
            return prefix

    def text(self) -> str:
        with self._lock:
            value = "".join(self._parts)
            return value + (TRUNCATION_MARKER if self._truncated else "")
