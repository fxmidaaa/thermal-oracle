"""Имя процесса активного окна через чистый Win32/ctypes — без psutil.

GetForegroundWindow → pid → OpenProcess(QUERY_LIMITED_INFORMATION) →
QueryFullProcessImageNameW. Кэш pid→имя (TTL 60с) сводит накладные расходы
к одному дешёвому вызову GetForegroundWindow в секунду; новый handle процесса
открывается только при смене pid.
"""
import sys
import time
from pathlib import PureWindowsPath

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_CACHE_TTL_S = 60.0
_cache: dict[int, tuple[str, float]] = {}


def get_foreground_process() -> str | None:
    if sys.platform != "win32":  # на других ОС (тесты, CI) — просто нет данных
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd = user32.GetForegroundWindow()
    if not hwnd:  # экран блокировки, переключение сессий
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None

    now = time.monotonic()
    cached = _cache.get(pid.value)
    if cached is not None and now - cached[1] < _CACHE_TTL_S:
        return cached[0]

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:  # защищённый/elevated процесс — не наша забота
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        name = PureWindowsPath(buf.value).name
    finally:
        kernel32.CloseHandle(handle)

    if len(_cache) > 256:
        _cache.clear()
    _cache[pid.value] = (name, now)
    return name
