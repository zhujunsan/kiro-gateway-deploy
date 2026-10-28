import errno
import io

import pytest
from loguru import logger as loguru_logger

from kiro_gateway_tray import log, paths, sentry_setup


def test_setup_idempotent_and_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    monkeypatch.setattr(log, "_READY", False, raising=False)
    # should not raise, and a second call is a no-op
    first = log.setup()
    second = log.setup()
    assert log._READY is True
    assert first is not None and first.mode == "enqueued"
    assert second is None
    # the log dir/file should have been created
    assert (paths.log_dir() / "tray.log").exists()
    loguru_logger.remove(first.handler_id)


def test_logger_is_exposed():
    assert hasattr(log, "logger")
    log.logger.debug("smoke")


# --- add_file_sink degradation ladder ----------------------------------------


class _FakeLogger:
    """Stand-in for loguru's logger that fails ``add`` on a script.

    ``script`` lists, per add() call, either an exception to raise or a
    handler id to return. WARNING calls (via bind().warning) are recorded.
    """

    def __init__(self, script):
        self.script = list(script)
        self.add_calls: list[dict] = []
        self.warnings: list[tuple[dict, str, tuple]] = []
        self._bound: dict = {}

    def add(self, sink, **kwargs):
        self.add_calls.append({"sink": sink, **kwargs})
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def bind(self, **extra):
        bound = _FakeLogger([])
        bound.warnings = self.warnings
        bound._bound = extra
        return bound

    def warning(self, message, *args):
        self.warnings.append((self._bound, message, args))


@pytest.fixture
def crumbs(monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(
        sentry_setup, "add_breadcrumb", lambda **kw: recorded.append(kw)
    )
    return recorded


def _enospc() -> OSError:
    return OSError(errno.ENOSPC, "No space left on device")


def test_add_file_sink_happy_path_is_enqueued_and_silent(tmp_path, crumbs):
    fake = _FakeLogger([7])
    path = tmp_path / "logs" / "x.log"

    result = log.add_file_sink(fake, path, component="tray", rotation="2 MB")

    assert result == log.FileSinkResult(mode="enqueued", handler_id=7, errors=())
    assert result.degraded is False
    assert fake.add_calls == [{"sink": str(path), "enqueue": True, "rotation": "2 MB"}]
    assert path.parent.is_dir()
    assert fake.warnings == []
    assert crumbs == []


def test_add_file_sink_enospc_falls_back_to_synchronous(tmp_path, crumbs):
    fake = _FakeLogger([_enospc(), 11])
    path = tmp_path / "gateway.log"

    result = log.add_file_sink(
        fake, path, component="gateway", rotation="2 MB", retention=5, encoding="utf-8"
    )

    assert result.mode == "synchronous"
    assert result.handler_id == 11
    assert result.degraded is True
    assert len(result.errors) == 1 and "No space left on device" in result.errors[0]
    # Same file and options; only enqueue flips.
    assert [c["enqueue"] for c in fake.add_calls] == [True, False]
    for call in fake.add_calls:
        assert call["sink"] == str(path)
        assert call["rotation"] == "2 MB" and call["retention"] == 5
    assert len(fake.warnings) == 1
    assert len(crumbs) == 1
    assert crumbs[0]["data"]["mode"] == "synchronous"


def test_add_file_sink_both_fail_leaves_stderr_only(tmp_path, crumbs):
    fake = _FakeLogger([_enospc(), PermissionError(errno.EACCES, "denied")])

    result = log.add_file_sink(fake, tmp_path / "gateway.log", component="gateway")

    assert result.mode == "stderr_only"
    assert result.handler_id is None
    assert len(result.errors) == 2
    assert result.errors[0].startswith("OSError")
    assert result.errors[1].startswith("PermissionError")
    assert len(fake.add_calls) == 2
    # Exactly one WARNING and one breadcrumb for the whole degradation.
    assert len(fake.warnings) == 1
    assert len(crumbs) == 1
    assert crumbs[0]["data"]["mode"] == "stderr_only"
    assert crumbs[0]["data"]["errors"] == list(result.errors)


def test_add_file_sink_mkdir_failure_also_degrades(tmp_path, crumbs):
    # A regular file where the log directory should be: mkdir raises an OSError
    # (FileExistsError / NotADirectoryError) on both attempts.
    blocker = tmp_path / "logs"
    blocker.write_text("not a dir")
    fake = _FakeLogger([1, 2])

    result = log.add_file_sink(fake, blocker / "tray.log", component="tray")

    assert result.mode == "stderr_only"
    assert fake.add_calls == []
    assert len(crumbs) == 1


@pytest.mark.parametrize("exc", [TypeError("bad option"), ValueError("bad rotation"), RuntimeError("bug")])
def test_add_file_sink_does_not_swallow_non_oserror(tmp_path, crumbs, exc):
    fake = _FakeLogger([exc])

    with pytest.raises(type(exc)):
        log.add_file_sink(fake, tmp_path / "x.log", component="tray")

    assert len(fake.add_calls) == 1  # no fallback attempt for programming errors
    assert fake.warnings == []
    assert crumbs == []


def test_add_file_sink_non_oserror_on_fallback_still_raises(tmp_path, crumbs):
    fake = _FakeLogger([_enospc(), ValueError("bad")])

    with pytest.raises(ValueError):
        log.add_file_sink(fake, tmp_path / "x.log", component="tray")
    assert crumbs == []


def test_degradation_warning_is_local_only_and_names_the_file(tmp_path, crumbs):
    fake = _FakeLogger([_enospc(), 3])
    path = tmp_path / "tray.log"

    log.add_file_sink(fake, path, component="tray")

    bound, message, args = fake.warnings[0]
    assert bound == {sentry_setup.LOCAL_ONLY_EXTRA: True}
    rendered = message.format(*args)
    assert "tray log sink degraded to synchronous" in rendered
    assert str(path) in rendered
    assert "No space left on device" in rendered


def test_degradation_breadcrumb_omits_full_path(tmp_path, crumbs):
    fake = _FakeLogger([_enospc(), _enospc()])
    path = tmp_path / "secret-user" / "gateway.log"

    log.add_file_sink(fake, path, component="gateway")

    crumb = crumbs[0]
    assert crumb["category"] == log.FALLBACK_BREADCRUMB_CATEGORY
    assert crumb["level"] == "warning"
    assert crumb["data"]["file"] == "gateway.log"
    assert crumb["data"]["component"] == "gateway"
    assert "secret-user" not in repr(crumb)


def test_setup_survives_enospc_on_every_attempt(tmp_path, monkeypatch, crumbs):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    monkeypatch.setattr(log, "_READY", False)
    fake = _FakeLogger([_enospc(), _enospc()])
    monkeypatch.setattr(log, "logger", fake)

    result = log.setup()

    assert result is not None and result.mode == "stderr_only"
    assert log._READY is True
    assert log.setup() is None  # no retry storm on later calls
    assert len(crumbs) == 1
    assert [c["level"] for c in fake.add_calls] == ["DEBUG", "DEBUG"]


def test_setup_falls_back_to_synchronous_with_real_loguru(tmp_path, monkeypatch, crumbs):
    """End-to-end with real loguru: only the enqueue attempt fails."""
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    monkeypatch.setattr(log, "_READY", False)
    real_add = loguru_logger.add

    def _add(sink, **kwargs):
        if kwargs.get("enqueue"):
            raise _enospc()
        return real_add(sink, **kwargs)

    loguru_logger.add = _add  # instance attribute shadows the bound method
    try:
        result = log.setup()
    finally:
        del loguru_logger.add  # restore loguru's own add before logging
    try:
        assert result is not None and result.mode == "synchronous"
        loguru_logger.info("written synchronously")
        text = (tmp_path / "logs" / "tray.log").read_text(encoding="utf-8")
        assert "written synchronously" in text
        assert "degraded to synchronous" in text
    finally:
        loguru_logger.remove(result.handler_id)


def test_stderr_only_still_emits_the_warning_to_existing_sinks(tmp_path, crumbs):
    """With no file sink, the WARNING must reach the already-installed sink
    (stderr / gateway-bootstrap.log), so the degradation is still visible."""
    buf = io.StringIO()
    sink_id = loguru_logger.add(buf, level="WARNING", format="{message}")

    class _Failing:
        def add(self, sink, **kwargs):
            raise _enospc()

        def bind(self, **extra):
            return loguru_logger.bind(**extra)

    try:
        result = log.add_file_sink(_Failing(), tmp_path / "gateway.log", component="gateway")
    finally:
        loguru_logger.remove(sink_id)
    assert result.mode == "stderr_only"
    assert "gateway log sink degraded to stderr_only" in buf.getvalue()
