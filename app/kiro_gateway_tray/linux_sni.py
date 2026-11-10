# app/kiro_gateway_tray/linux_sni.py
"""GNOME Wayland tray via StatusNotifierItem + DBusMenu.

pystray's Linux backends do not receive clicks on GNOME 50 under Wayland.
Gtk.StatusIcon / XEmbed icons are drawn by the shell, but GNOME 50 delivers
button presses to ``Clutter.ClickGesture`` on a ``PanelMenu.Button`` and no
longer forwards them to the legacy tray client, so both buttons do nothing.
The AppIndicator path depends on system ``gi`` typelibs. A frozen AppImage
often cannot load those, and libayatana's ``Activate`` is a silent no-op on
Wayland, which looks the same: the icon is visible and clicks are dead.

This module speaks the protocol the shell actually uses. The icon is a
``org.kde.StatusNotifierItem`` whose ``Menu`` points at a
``com.canonical.dbusmenu`` object. GNOME's AppIndicator support fills a real
panel menu from that object and toggles it on left or right click.

``WindowId`` is a signed int (``i``). GNOME Shell rejects the unsigned ``u``
some libraries advertise and then fails the click path. ``IconPixmap`` is a
real ARGB image so property ``Get`` succeeds; an error from that property
aborts the shell's proxy and clicks die inside the extension.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Callable

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import (
    AuthError,
    DBusError,
    InterfaceNotFoundError,
    InvalidAddressError,
    InvalidIntrospectionError,
    InvalidMessageError,
)
from dbus_next.service import ServiceInterface, dbus_property, method, signal
from dbus_next.constants import PropertyAccess

from .log import logger

_BUS_ERRORS = (
    AuthError,
    DBusError,
    InterfaceNotFoundError,
    InvalidAddressError,
    InvalidIntrospectionError,
    InvalidMessageError,
    OSError,
    ConnectionError,
    TimeoutError,
    EOFError,
)

_ITEM_PATH = "/StatusNotifierItem"
_MENU_PATH = "/StatusNotifierItem/Menu"
_WATCHER_NAME = "org.kde.StatusNotifierWatcher"
_WATCHER_PATH = "/StatusNotifierWatcher"

BeforeShow = Callable[[], None]


def use_native_sni() -> bool:
    """Return True when the tray should register a StatusNotifierItem itself.

    GNOME on Wayland is the session where pystray clicks do not arrive.
    ``KIRO_GATEWAY_TRAY_BACKEND=pystray`` keeps the previous backend.
    ``KIRO_GATEWAY_TRAY_BACKEND=sni`` forces this path on any Linux session.

    Returns:
        Whether ``SniIcon`` should own the tray icon.
    """
    if not sys.platform.startswith("linux"):
        return False
    override = os.environ.get("KIRO_GATEWAY_TRAY_BACKEND", "").strip().lower()
    if override == "pystray":
        return False
    if override == "sni":
        return True
    session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session == "x11":
        return False
    wayland = session == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))
    if not wayland:
        return False
    desktop = " ".join(
        os.environ.get(key, "")
        for key in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")
    ).upper()
    return "GNOME" in desktop


def argb_bytes(image: Any) -> tuple[int, int, bytes]:
    """Encode a Pillow image as one StatusNotifier pixmap.

    The shell reads byte 0 as alpha, then red, green, blue
    (``pixmapsUtils.argbToRgba``).

    Args:
        image: A Pillow image. It is converted to RGBA and not resized.

    Returns:
        ``(width, height, argb_bytes)`` with ``width * height * 4`` bytes.
    """
    img = image.convert("RGBA")
    width, height = img.size
    pixels = img.load()
    raw = bytearray()
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = pixels[x, y]
            raw.extend((alpha, red, green, blue))
    return width, height, bytes(raw)


def image_to_pixmaps(image: Any) -> list[list[Any]]:
    """Build the ``a(iiay)`` value GNOME reads for ``IconPixmap``.

    Two sizes are published so a panel scale of 1 or 2 finds a pixmap at
    least as large as the slot. An empty array makes the shell throw
    ``Empty Icon found`` while resolving the icon and then drop clicks.

    Args:
        image: Source Pillow image.

    Returns:
        A list of ``[width, height, argb_bytes]`` pixmaps.
    """
    from PIL import Image

    base = image.convert("RGBA")
    pixmaps: list[list[Any]] = []
    for size in (22, 48):
        scaled = base.resize((size, size), Image.Resampling.LANCZOS)
        width, height, raw = argb_bytes(scaled)
        pixmaps.append([width, height, raw])
    return pixmaps


def _variant_props(props: dict[str, Any]) -> dict[str, Variant]:
    """Wrap plain menu properties in the variants DBusMenu requires.

    Args:
        props: Property name to a ``(signature, value)`` pair.

    Returns:
        A dict suitable for an ``a{sv}`` menu node.
    """
    return {name: Variant(sig, value) for name, (sig, value) in props.items()}


class _MenuModel:
    """In-memory DBusMenu tree built from a pystray ``Menu``."""

    def __init__(self) -> None:
        self.revision = 1
        self.nodes: dict[int, dict[str, Any]] = {}
        self._next_id = 0

    def rebuild(self, menu: Any) -> bool:
        """Replace the tree from ``menu``. Return True when labels changed.

        Args:
            menu: A pystray ``Menu``. Falsy means an empty menu.

        Returns:
            Whether the visible labels or enabled flags changed.
        """
        previous = self._fingerprint()
        self.nodes = {}
        self._next_id = 0
        # Reserve id 0 before walking children. Argument evaluation would
        # otherwise hand the root the next free id.
        root = self._add({"children-display": ("s", "submenu")}, children=[])
        self.nodes[root]["children"] = self._children(menu) if menu else []
        if root != 0:
            raise RuntimeError("DBusMenu root id must be 0")
        changed = previous != self._fingerprint()
        if changed:
            self.revision = (self.revision + 1) & 0xFFFFFFFF or 1
        return changed

    def pack(self, node_id: int, depth: int, names: list[str]) -> list[Any]:
        """Pack one node as ``(ia{sv}av)``.

        Args:
            node_id: Node to pack. Unknown ids pack as an empty leaf.
            depth: Child levels to include. Negative means the whole subtree.
            names: Property names to keep. Empty keeps all of them.

        Returns:
            ``[id, props, children]`` with each child wrapped as a variant.
        """
        node = self.nodes.get(node_id)
        if node is None:
            return [node_id, {}, []]
        props = node["props"]
        if names:
            props = {key: value for key, value in props.items() if key in names}
        if depth == 0:
            children: list[Variant] = []
        else:
            next_depth = -1 if depth < 0 else depth - 1
            children = [
                Variant("(ia{sv}av)", self.pack(child, next_depth, names))
                for child in node["children"]
            ]
        return [node_id, props, children]

    def properties_for(self, node_id: int, names: list[str]) -> dict[str, Variant]:
        """Return stored properties for ``node_id``, optionally filtered.

        Args:
            node_id: Menu node id.
            names: Property names to keep. Empty keeps all of them.

        Returns:
            Variant dict, or an empty dict when the id is unknown.
        """
        node = self.nodes.get(node_id)
        if node is None:
            return {}
        props = node["props"]
        if names:
            return {key: value for key, value in props.items() if key in names}
        return dict(props)

    def activate(self, node_id: int, icon: Any) -> None:
        """Run the menu callback for ``node_id`` when that row is enabled.

        Args:
            node_id: Id from a DBusMenu ``clicked`` event.
            icon: Icon passed through to the pystray callback.
        """
        node = self.nodes.get(node_id)
        if node is None or node["action"] is None:
            return
        if not node["enabled"]:
            return
        node["action"](icon)

    def _add(self, plain: dict[str, Any], *, children: list[int], action: Any = None, enabled: bool = True) -> int:
        node_id = self._next_id
        self._next_id += 1
        self.nodes[node_id] = {
            "props": _variant_props(plain),
            "children": children,
            "action": action,
            "enabled": enabled,
        }
        return node_id

    def _children(self, menu: Any) -> list[int]:
        ids: list[int] = []
        try:
            items = list(menu)
        except TypeError:
            return ids
        for item in items:
            if item is getattr(type(menu), "SEPARATOR", None) or _is_separator(item):
                ids.append(self._add({"type": ("s", "separator")}, children=[]))
                continue
            submenu = getattr(item, "submenu", None)
            label = _text(item)
            enabled = bool(getattr(item, "enabled", True))
            if submenu is not None:
                ids.append(
                    self._add(
                        {
                            "label": ("s", label),
                            "enabled": ("b", enabled),
                            "children-display": ("s", "submenu"),
                        },
                        children=self._children(submenu),
                        enabled=enabled,
                    )
                )
                continue
            ids.append(
                self._add(
                    {"label": ("s", label), "enabled": ("b", enabled)},
                    children=[],
                    action=item,
                    enabled=enabled,
                )
            )
        return ids

    def _fingerprint(self) -> tuple[tuple[Any, ...], ...]:
        rows = []
        for node_id in sorted(self.nodes):
            node = self.nodes[node_id]
            label = node["props"].get("label")
            kind = node["props"].get("type")
            rows.append(
                (
                    node_id,
                    None if label is None else label.value,
                    None if kind is None else kind.value,
                    node["enabled"],
                    tuple(node["children"]),
                )
            )
        return tuple(rows)


def _is_separator(item: Any) -> bool:
    """Return True for pystray's separator marker.

    The shared ``Menu.SEPARATOR`` item is a normal ``MenuItem`` whose label is
    ``- - - -``. Identity is checked by the caller; this catches that label
    when the menu implementation yields an equal but distinct object.

    Args:
        item: A menu entry.

    Returns:
        Whether the entry should be exported as a DBusMenu separator.
    """
    return _text(item) == "- - - -" and getattr(item, "submenu", None) is None


def _text(item: Any) -> str:
    """Read a menu label, tolerating a missing or failing callable.

    Args:
        item: A pystray menu item.

    Returns:
        The label, or an empty string when it cannot be read.
    """
    try:
        return str(getattr(item, "text", "") or "")
    except (TypeError, ValueError, AttributeError):
        return ""


class DBusMenu(ServiceInterface):
    """``com.canonical.dbusmenu`` for one pystray menu."""

    def __init__(self, icon: Any, before_show: BeforeShow | None = None) -> None:
        super().__init__("com.canonical.dbusmenu")
        self._icon = icon
        self._before_show = before_show
        self._model = _MenuModel()
        self._model.rebuild(getattr(icon, "menu", None))

    @method()
    def GetLayout(self, parent_id: "i", recursion_depth: "i", property_names: "as") -> "u(ia{sv}av)":
        """Return the menu tree from ``parent_id``.

        Args:
            parent_id: Node to start from. ``0`` is the root.
            recursion_depth: Levels of children. ``-1`` is the whole tree.
            property_names: Properties to include. Empty includes all.

        Returns:
            ``[revision, (id, props, children)]``.
        """
        return [
            self._model.revision,
            self._model.pack(parent_id, recursion_depth, list(property_names)),
        ]

    @method()
    def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
        """Return properties for each known id.

        Args:
            ids: Node ids requested by the shell.
            property_names: Properties to include. Empty includes all.

        Returns:
            A list of ``[id, props]``.
        """
        names = list(property_names)
        return [
            [node_id, self._model.properties_for(node_id, names)]
            for node_id in ids
            if node_id in self._model.nodes
        ]

    @method()
    def Event(self, node_id: "i", event_id: "s", data: "v", timestamp: "u"):
        """Handle a menu event. Only ``clicked`` activates a row.

        Args:
            node_id: The row that was activated.
            event_id: DBusMenu event name.
            data: Event payload. Unused.
            timestamp: Event time. Unused.
        """
        del data, timestamp
        if event_id != "clicked":
            return
        try:
            self._model.activate(node_id, self._icon)
        except Exception:
            # Menu callbacks are arbitrary app code (dialogs, subprocesses,
            # httpx). The bus loop must stay up when one of them fails.
            logger.exception("tray menu action failed")

    @method()
    def AboutToShow(self, node_id: "i") -> "b":
        """Rebuild dynamic labels before the shell displays the menu.

        Args:
            node_id: The submenu about to open. The whole tree is refreshed.

        Returns:
            True so the shell calls ``GetLayout`` again with fresh labels.
        """
        del node_id
        if self._before_show is not None:
            try:
                self._before_show()
            except (OSError, RuntimeError, ValueError, TypeError):
                logger.debug("tray menu before-show failed", exc_info=True)
        self.refresh(emit=False)
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        """DBusMenu protocol version."""
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        """Menu status. ``normal`` means the layout is usable."""
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        """Text direction hint."""
        return "ltr"

    @signal()
    def LayoutUpdated(self) -> "ui":
        """Tell the shell the layout revision changed."""
        return [self._model.revision, 0]

    def refresh(self, *, emit: bool) -> None:
        """Rebuild from the icon's current menu.

        Args:
            emit: When True, broadcast ``LayoutUpdated``. AboutToShow leaves
                this False because it already asks the shell to fetch again.
        """
        self._model.rebuild(getattr(self._icon, "menu", None))
        if emit:
            self.LayoutUpdated()


class StatusNotifierItem(ServiceInterface):
    """``org.kde.StatusNotifierItem`` with the property types GNOME 50 requires.

    ``Activate`` is intentionally absent. The shell treats a missing
    ``Activate`` as "this item is menu-only" and opens the menu on the first
    click instead of waiting for a double-click.
    """

    def __init__(self, item_id: str, title: str) -> None:
        super().__init__("org.kde.StatusNotifierItem")
        self._item_id = item_id
        self._title = title
        self._pixmap: list[list[Any]] = []
        self._status = "Active"

    def set_pixmap(self, pixmap: list[list[Any]]) -> None:
        """Replace the icon pixmap and notify listeners when exported.

        Args:
            pixmap: ``a(iiay)`` value from ``image_to_pixmaps``.
        """
        self._pixmap = pixmap
        if ServiceInterface._get_buses(self):
            self.emit_properties_changed({"IconPixmap": pixmap})

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        """SNI category. Application status icons live in the panel."""
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        """Stable id. The shell will not mark the item ready without one."""
        return self._item_id

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        """Tooltip title."""
        return self._title

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        """``Active`` keeps the icon visible. ``Passive`` hides it."""
        return self._status

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "i":
        """Window id. The spec says signed int; ``u`` makes GNOME drop the item."""
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "s":
        """Unused. The pixmap is the icon, so the theme path stays empty."""
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        """Empty so the shell uses ``IconPixmap`` instead of a theme lookup."""
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        """ARGB pixmaps. Must be non-empty once the icon is registered."""
        return self._pixmap

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> "s":
        """No overlay icon."""
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconPixmap(self) -> "a(iiay)":
        """No overlay pixmap."""
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":
        """No attention icon."""
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> "a(iiay)":
        """No attention pixmap."""
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionMovieName(self) -> "s":
        """No attention movie."""
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconAccessibleDesc(self) -> "s":
        """Accessibility label queried by newer GNOME AppIndicator hosts."""
        return self._title

    @dbus_property(access=PropertyAccess.READ)
    def AttentionAccessibleDesc(self) -> "s":
        """Attention accessibility label. Empty when we are not requesting it."""
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconAccessibleDesc(self) -> "s":
        """Overlay accessibility label. We do not draw an overlay."""
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        """Ask hosts that honor this hint to open the menu on left click."""
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        """Object path of the DBusMenu exported beside this item."""
        return _MENU_PATH

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        """Tooltip some hosts still query. Icon image is omitted."""
        return [self._title, [], self._title, ""]

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        """Ignore scroll. Present so the shell does not log an unknown method.

        Args:
            delta: Scroll amount. Unused.
            orientation: ``horizontal`` or ``vertical``. Unused.
        """
        del delta, orientation

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        """Right-click hook. The shell opens ``Menu`` itself.

        Args:
            x: Pointer x. Unused.
            y: Pointer y. Unused.
        """
        del x, y

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        """Middle-click hook. No secondary action.

        Args:
            x: Pointer x. Unused.
            y: Pointer y. Unused.
        """
        del x, y


class SniIcon:
    """pystray-shaped icon that registers with ``StatusNotifierWatcher``.

    ``run`` blocks on the asyncio session bus. If the watcher is missing, it
    falls back to pystray so a misconfigured session still shows an icon.
    """

    def __init__(
        self,
        name: str,
        icon: Any,
        title: str,
        menu: Any,
        before_show: BeforeShow | None = None,
    ) -> None:
        self.name = name
        self._image = icon
        self._title = title or ""
        self.menu = menu
        self._before_show = before_show
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped: asyncio.Event | None = None
        self._fallback: Any = None
        self._item = StatusNotifierItem(name, self._title)
        self._menu_iface = DBusMenu(self, before_show)
        if icon is not None:
            self._item.set_pixmap(image_to_pixmaps(icon))

    @property
    def icon(self) -> Any:
        """Current Pillow image."""
        return self._image

    @icon.setter
    def icon(self, value: Any) -> None:
        self._image = value
        if value is None or self._fallback is not None:
            if self._fallback is not None:
                self._fallback.icon = value
            return
        self._schedule(self._publish_icon)

    @property
    def title(self) -> str:
        """Icon title."""
        return self._title

    def update_menu(self) -> None:
        """Rebuild the DBus menu from the current pystray menu."""
        if self._fallback is not None:
            self._fallback.update_menu()
            return
        self._schedule(lambda: self._menu_iface.refresh(emit=True))

    def notify(self, message: str, title: str | None = None) -> None:
        """Show a desktop notification.

        Args:
            message: Notification body. Matches pystray's argument order.
            title: Notification title. Falls back to the icon title.
        """
        if self._fallback is not None:
            self._fallback.notify(message, title)
            return
        summary = title or self._title or self.name
        self._schedule_coro(self._notify(summary, message))

    def stop(self) -> None:
        """Leave the tray loop. Safe to call from a menu callback."""
        if self._fallback is not None:
            self._fallback.stop()
            return
        loop = self._loop
        event = self._stopped
        if loop is None or event is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(event.set)

    def run(self) -> None:
        """Block until ``stop``. Falls back to pystray if the bus is unusable."""
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._serve())
        except _BUS_ERRORS:
            logger.exception("StatusNotifierItem registration failed; falling back to pystray")
            self._run_pystray_fallback()
        finally:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.close()
            self._loop = None

    async def _serve(self) -> None:
        self._stopped = asyncio.Event()
        bus = await MessageBus().connect()
        bus.export(_ITEM_PATH, self._item)
        bus.export(_MENU_PATH, self._menu_iface)
        introspection = await bus.introspect(_WATCHER_NAME, _WATCHER_PATH)
        proxy = bus.get_proxy_object(_WATCHER_NAME, _WATCHER_PATH, introspection)
        watcher = proxy.get_interface(_WATCHER_NAME)
        # A leading slash means "object path on the sender's unique name".
        # Passing a well-known name here is what breaks GNOME 50's proxy.
        await watcher.call_register_status_notifier_item(_ITEM_PATH)
        logger.info("tray icon registered with StatusNotifierWatcher at {}", _ITEM_PATH)
        await self._stopped.wait()
        bus.disconnect()

    def _publish_icon(self) -> None:
        if self._image is None:
            return
        self._item.set_pixmap(image_to_pixmaps(self._image))

    async def _notify(self, summary: str, message: str) -> None:
        try:
            bus = self._item_bus()
            if bus is None:
                return
            path = "/org/freedesktop/Notifications"
            name = "org.freedesktop.Notifications"
            introspection = await bus.introspect(name, path)
            proxy = bus.get_proxy_object(name, path, introspection)
            iface = proxy.get_interface(name)
            await iface.call_notify("Kiro Gateway Tray", 0, "", summary, message, [], {}, -1)
        except _BUS_ERRORS:
            logger.debug("desktop notification failed", exc_info=True)

    def _item_bus(self) -> Any:
        buses = ServiceInterface._get_buses(self._item)
        if not buses:
            return None
        return next(iter(buses))

    def _schedule(self, fn: Callable[[], None]) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            fn()
            return
        loop.call_soon_threadsafe(fn)

    def _schedule_coro(self, coro: Any) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, loop)

    def _run_pystray_fallback(self) -> None:
        import pystray

        self._fallback = pystray.Icon(self.name, self._image, self._title, self.menu)
        self._fallback.run()
