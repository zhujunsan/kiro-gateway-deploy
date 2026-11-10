# app/tests/test_linux_sni.py
"""GNOME Wayland tray clicks go through StatusNotifierItem, not Gtk.StatusIcon."""
from __future__ import annotations

from dbus_next.constants import MessageType
from dbus_next.message import Message
from PIL import Image
import pytest

from kiro_gateway_tray import linux_sni


def _dbus_call(interface, name: str, *args):
    """Invoke the undecorated D-Bus method (the wrapper drops the return)."""
    methods = interface._ServiceInterface__methods
    fn = next(method.fn for method in methods if method.name == name)
    return fn(interface, *args)


def _marshall(signature: str, body: list) -> None:
    Message(
        message_type=MessageType.METHOD_RETURN,
        reply_serial=1,
        signature=signature,
        body=body,
    )._marshall(False)


class _Icon:
    def __init__(self, menu) -> None:
        self.menu = menu


class _Item:
    """Enough of a pystray menu row for the DBusMenu walker."""

    def __init__(self, text, action=None, *, enabled=True, visible=True) -> None:
        self._text = text
        self._action = action
        self.enabled = enabled
        self.visible = visible

    @property
    def text(self) -> str:
        return self._text() if callable(self._text) else self._text

    @property
    def submenu(self):
        return self._action if isinstance(self._action, _Menu) else None

    def __call__(self, icon) -> None:
        if self.submenu is None and self._action is not None:
            self._action(icon, self)


class _Menu:
    SEPARATOR = object()

    def __init__(self, *items) -> None:
        self._items = items

    def __iter__(self):
        visible = [item for item in self._items if getattr(item, "visible", True)]
        while visible and visible[0] is self.SEPARATOR:
            visible.pop(0)
        while visible and visible[-1] is self.SEPARATOR:
            visible.pop()
        return iter(visible)


def test_argb_bytes_are_alpha_then_rgb() -> None:
    image = Image.new("RGBA", (1, 1), (10, 20, 30, 40))
    width, height, raw = linux_sni.argb_bytes(image)
    assert (width, height) == (1, 1)
    assert raw == bytes((40, 10, 20, 30))


def test_image_to_pixmaps_publishes_two_non_empty_sizes() -> None:
    image = Image.new("RGBA", (8, 8), (1, 2, 3, 255))
    pixmaps = linux_sni.image_to_pixmaps(image)
    assert [size for size, _height, _raw in ((p[0], p[1], p[2]) for p in pixmaps)] == [22, 48]
    for width, height, raw in pixmaps:
        assert width == height
        assert len(raw) == width * height * 4
    _marshall("a(iiay)", [pixmaps])


def test_sni_properties_match_gnome_50_expectations() -> None:
    item = linux_sni.StatusNotifierItem("kiro-gateway-tray", "Kiro Gateway")
    info = item.introspect()
    props = {prop.name: prop.signature for prop in info.properties}
    methods = {method.name for method in info.methods}

    assert props["WindowId"] == "i"
    assert props["ItemIsMenu"] == "b"
    assert props["Menu"] == "o"
    assert props["IconPixmap"] == "a(iiay)"
    assert props["IconAccessibleDesc"] == "s"
    assert props["AttentionAccessibleDesc"] == "s"
    assert props["OverlayIconAccessibleDesc"] == "s"
    assert item.WindowId == 0
    assert item.ItemIsMenu is True
    assert item.Menu == "/StatusNotifierItem/Menu"
    # Activate would make GNOME wait for a double-click before showing the menu.
    assert "Activate" not in methods


def test_menu_layout_drops_hidden_rows_and_keeps_submenus() -> None:
    clicked: list[str] = []
    menu = _Menu(
        _Item("Hidden", lambda _icon, _item: clicked.append("hidden"), visible=False),
        _Menu.SEPARATOR,
        _Item("Go", lambda _icon, _item: clicked.append("go")),
        _Item("Nope", lambda _icon, _item: clicked.append("nope"), enabled=False),
        _Item("More", _Menu(_Item("Child", lambda _icon, _item: clicked.append("child")))),
    )
    iface = linux_sni.DBusMenu(_Icon(menu))
    revision, root = _dbus_call(iface, "GetLayout", 0, -1, [])
    _marshall("u(ia{sv}av)", [revision, root])

    labels = _labels(iface)
    assert "Hidden" not in labels
    assert "Go" in labels
    assert "Nope" in labels
    assert "More" in labels
    assert "Child" in labels

    iface.Event(labels["Go"], "clicked", linux_sni.Variant("i", 0), 0)
    iface.Event(labels["Nope"], "clicked", linux_sni.Variant("i", 0), 0)
    iface.Event(labels["Child"], "clicked", linux_sni.Variant("i", 0), 0)
    assert clicked == ["go", "child"]

    _root_id, props, children = root
    assert props["children-display"].value == "submenu"
    assert children


def test_about_to_show_picks_up_callable_labels() -> None:
    state = {"text": "old"}
    menu = _Menu(_Item(lambda: state["text"]))
    iface = linux_sni.DBusMenu(_Icon(menu))
    assert _labels(iface) == {"old": 1}

    state["text"] = "new"
    assert _dbus_call(iface, "AboutToShow", 0) is True
    assert _labels(iface) == {"new": 1}
    rows = _dbus_call(iface, "GetGroupProperties", [1], ["label"])
    _marshall("a(ia{sv})", [rows])
    assert rows == [[1, {"label": linux_sni.Variant("s", "new")}]]


def test_filtered_layout_still_marks_submenus() -> None:
    menu = _Menu(_Item("More", _Menu(_Item("Child"))))
    iface = linux_sni.DBusMenu(_Icon(menu))
    _revision, root = _dbus_call(iface, "GetLayout", 0, -1, ["type", "children-display"])
    _root_id, _props, children = root
    more = children[0].value
    _more_id, more_props, more_children = more
    assert more_props["children-display"].value == "submenu"
    assert "label" not in more_props
    child = more_children[0].value
    assert "children-display" not in child[1]


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("linux", {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, True),
        ("linux", {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"}, True),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, True),
        ("linux", {"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, False),
        ("linux", {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"}, False),
        ("darwin", {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, False),
    ],
)
def test_use_native_sni_only_on_gnome_wayland(monkeypatch, platform, env, expected) -> None:
    monkeypatch.setattr(linux_sni.sys, "platform", platform)
    for key in (
        "XDG_SESSION_TYPE",
        "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_DESKTOP",
        "DESKTOP_SESSION",
        "WAYLAND_DISPLAY",
        "KIRO_GATEWAY_TRAY_BACKEND",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert linux_sni.use_native_sni() is expected


def test_backend_override(monkeypatch) -> None:
    monkeypatch.setattr(linux_sni.sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_BACKEND", "pystray")
    assert linux_sni.use_native_sni() is False

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_BACKEND", "sni")
    assert linux_sni.use_native_sni() is True


def test_tray_uses_sni_icon_when_session_matches(monkeypatch) -> None:
    from kiro_gateway_tray import tray as tray_mod

    monkeypatch.setattr(linux_sni, "use_native_sni", lambda: True)
    monkeypatch.setattr(tray_mod, "make_icon", lambda _running: Image.new("RGBA", (4, 4), (0, 0, 0, 255)))
    app = tray_mod.TrayApp.__new__(tray_mod.TrayApp)
    app._pystray = None
    icon = app._create_tray_icon(_Menu(_Item("Quit")))
    assert isinstance(icon, linux_sni.SniIcon)
    assert icon._item.IconPixmap


def _labels(iface: linux_sni.DBusMenu) -> dict[str, int]:
    found: dict[str, int] = {}
    for node_id, node in iface._model.nodes.items():
        label = node["props"].get("label")
        if label is not None and label.value:
            found[label.value] = node_id
    return found
