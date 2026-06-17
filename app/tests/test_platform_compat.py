# app/tests/test_platform_compat.py
from kiro_gateway_tray.platform_compat import SingleInstanceLock


def test_single_instance_lock_blocks_second_acquire(tmp_path):
    lock_path = tmp_path / "test.lock"
    a = SingleInstanceLock(lock_path)
    assert a.acquire() is True

    b = SingleInstanceLock(lock_path)
    assert b.acquire() is False  # held by `a`


def test_single_instance_lock_reacquire_after_release(tmp_path):
    lock_path = tmp_path / "test.lock"
    a = SingleInstanceLock(lock_path)
    assert a.acquire() is True
    # drop the handle to release the OS lock
    a._fd.close()

    b = SingleInstanceLock(lock_path)
    assert b.acquire() is True
