# app/tests/test_log.py
from kiro_gateway_tray import log


def test_setup_idempotent_and_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    monkeypatch.setattr(log, "_READY", False, raising=False)
    # should not raise, and a second call is a no-op
    log.setup()
    log.setup()
    assert log._READY is True
    # the log dir/file should have been created
    from kiro_gateway_tray import paths
    assert (paths.log_dir() / "tray.log").exists()


def test_logger_is_exposed():
    assert hasattr(log, "logger")
    log.logger.debug("smoke")
