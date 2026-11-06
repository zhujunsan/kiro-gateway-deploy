"""Map a host CPU name to the architecture tokens used in release artifacts.

cloudflared release assets use ``amd64`` / ``arm64``. AppImage filenames and
``appimagetool`` use ``x86_64`` / ``aarch64``. Callers pass ``platform.machine()``
(or an equivalent string) and get the token for the artifact they are building.
"""
from __future__ import annotations


_CLOUDFLARED_ARCH = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}

_APPIMAGE_ARCH = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}


def _lookup(machine: str, table: dict[str, str]) -> str:
    """Return the architecture token for ``machine``.

    Args:
        machine: Value of ``platform.machine()``, any case.
        table: Mapping from normalized machine name to artifact token.

    Returns:
        The artifact architecture token.

    Raises:
        ValueError: ``machine`` is not a supported release architecture.
    """
    key = machine.lower()
    try:
        return table[key]
    except KeyError as exc:
        supported = ", ".join(sorted(table))
        raise ValueError(
            f"unsupported CPU architecture {machine!r}; expected one of: {supported}"
        ) from exc


def cloudflared_arch(machine: str) -> str:
    """Return the cloudflared asset architecture (``amd64`` or ``arm64``).

    Args:
        machine: Value of ``platform.machine()``.

    Returns:
        ``amd64`` or ``arm64``.
    """
    return _lookup(machine, _CLOUDFLARED_ARCH)


def appimage_arch(machine: str) -> str:
    """Return the AppImage filename architecture (``x86_64`` or ``aarch64``).

    Args:
        machine: Value of ``platform.machine()``.

    Returns:
        ``x86_64`` or ``aarch64``.
    """
    return _lookup(machine, _APPIMAGE_ARCH)


def linux_appimage_filename(version: str, machine: str) -> str:
    """Return the published Linux AppImage filename for this version and CPU.

    Args:
        version: Application version without a leading ``v``.
        machine: Value of ``platform.machine()``.

    Returns:
        Filename such as ``kiro-gateway-tray-1.2.3-linux-aarch64.AppImage``.
    """
    return f"kiro-gateway-tray-{version}-linux-{appimage_arch(machine)}.AppImage"


def appimagetool_filename(machine: str) -> str:
    """Return the ``appimagetool`` AppImage filename for this CPU.

    Args:
        machine: Value of ``platform.machine()``.

    Returns:
        Filename such as ``appimagetool-aarch64.AppImage``.
    """
    return f"appimagetool-{appimage_arch(machine)}.AppImage"
