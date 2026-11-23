import ctypes
from ctypes import wintypes
from dataclasses import replace
import logging
import sys
from uuid import UUID

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QWidget

from ecoflow_monitor.diagnostics import event
from ecoflow_monitor.system_activity import SystemActivity


WM_POWERBROADCAST = 0x218
WM_TIMECHANGE = 0x001E
WM_WTSSESSION_CHANGE = 0x2B1
SESSION_DISPLAY = UUID("2b84c20e-ad23-4ddf-93db-05ffbd7efca5").bytes_le


class PowerSetting(ctypes.Structure):
    _fields_ = [("guid", ctypes.c_ubyte * 16), ("length", wintypes.DWORD), ("value", wintypes.DWORD)]


class SessionPrefix(ctypes.Structure):
    _fields_ = [("session_id", wintypes.DWORD), ("state", wintypes.DWORD), ("flags", wintypes.DWORD)]


class SessionData(ctypes.Union):
    _fields_ = [("info", SessionPrefix), ("alignment", ctypes.c_longlong)]


class SessionInfo(ctypes.Structure):
    _fields_ = [("level", wintypes.DWORD), ("data", SessionData)]


class WindowsActivity:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self.user.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        self.user.RegisterPowerSettingNotification.restype = wintypes.HANDLE
        self.user.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
        self.user.UnregisterPowerSettingNotification.restype = wintypes.BOOL
        self.wts.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.wts.WTSRegisterSessionNotification.restype = wintypes.BOOL
        self.wts.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
        self.wts.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
        self.wts.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        self.wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
        self.wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        self.wts.WTSFreeMemory.restype = None

    def register_power(self, hwnd):
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(SESSION_DISPLAY)
        handle = self.user.RegisterPowerSettingNotification(hwnd, ctypes.byref(guid), 0)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def register_session(self, hwnd):
        if not self.wts.WTSRegisterSessionNotification(hwnd, 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def locked(self):
        buffer, size = ctypes.c_void_p(), wintypes.DWORD()
        if not self.wts.WTSQuerySessionInformationW(None, 0xFFFFFFFF, 25, ctypes.byref(buffer), ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not buffer.value or size.value < ctypes.sizeof(SessionInfo):
                raise OSError("Invalid session information")
            info = ctypes.cast(buffer, ctypes.POINTER(SessionInfo)).contents
            if info.level != 1 or info.data.info.flags not in {0, 1}:
                raise OSError("Unknown session state")
            return info.data.info.flags == 0 or info.data.info.state != 0
        finally:
            self.wts.WTSFreeMemory(buffer)

    def unregister_power(self, handle):
        if not self.user.UnregisterPowerSettingNotification(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def unregister_session(self, hwnd):
        if not self.wts.WTSUnRegisterSessionNotification(hwnd):
            raise ctypes.WinError(ctypes.get_last_error())


class SystemActivityWatcher(QObject, QAbstractNativeEventFilter):
    changed = Signal(object)
    clock_changed = Signal()

    def __init__(self, parent=None, native=True, backend=None):
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self.state = SystemActivity()
        self.native = native
        self.backend = backend
        self.window = None
        self._hwnd = None
        self._power = None
        self._session = False
        self._started = False

    @staticmethod
    def _failed(operation, error):
        event("system_activity_error", level=logging.WARNING, operation=operation,
              winerror=getattr(error, "winerror", None), exception_type=type(error).__name__)

    def start(self):
        if self._started or not self.native or sys.platform != "win32":
            return
        self._started = True
        try:
            self.backend = self.backend or WindowsActivity()
        except OSError as error:
            self._failed("initialize", error)
            return
        self.window = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._hwnd = int(self.window.winId())
        QApplication.instance().installNativeEventFilter(self)
        try:
            self.backend.register_session(self._hwnd)
            self._session = True
        except OSError as error:
            self._failed("register_session", error)
        self._read_session()
        try:
            self._power = self.backend.register_power(self._hwnd)
        except OSError as error:
            self._failed("register_display", error)

    def _read_session(self):
        try:
            self._publish(replace(self.state, locked=self.backend.locked()))
        except OSError as error:
            self._failed("read_session", error)

    def transition(self, name):
        self._publish(self.state.transition(name))

    def _publish(self, state):
        if state != self.state:
            self.state = state
            self.changed.emit(state)

    def nativeEventFilter(self, event_type, message):
        if not self._started or self._hwnd is None or event_type not in {b"windows_generic_MSG", b"windows_dispatcher_MSG"}:
            return False, 0
        msg = wintypes.MSG.from_address(int(message))
        if msg.hWnd != self._hwnd:
            return False, 0
        if msg.message == WM_TIMECHANGE:
            self.clock_changed.emit()
        elif msg.message == WM_WTSSESSION_CHANGE:
            if msg.wParam in {2, 4, 7}:
                self.transition("lock")
            elif msg.wParam == 8:
                self.transition("unlock")
            elif msg.wParam in {1, 3, 5}:
                self._read_session()
        elif msg.message == WM_POWERBROADCAST:
            name = {4: "suspend", 6: "resume", 7: "resume_user", 18: "resume"}.get(msg.wParam)
            if name is not None:
                if name in {"resume", "resume_user"}:
                    self._read_session()
                self.transition(name)
                return True, 1
            if msg.wParam == 0x8013 and msg.lParam:
                setting = PowerSetting.from_address(msg.lParam)
                if bytes(setting.guid) == SESSION_DISPLAY and setting.length == 4:
                    name = {0: "display_off", 1: "display_on", 2: "display_dim"}.get(setting.value)
                    if name is not None:
                        self.transition(name)
                        return True, 1
        return False, 0

    def stop(self):
        if not self._started:
            return
        self._started = False
        QApplication.instance().removeNativeEventFilter(self)
        for operation, value in (("unregister_power", self._power), ("unregister_session", self._hwnd if self._session else None)):
            if value is not None:
                try:
                    getattr(self.backend, operation)(value)
                except OSError as error:
                    self._failed(operation, error)
        self._power = None
        self._session = False
        self._hwnd = None
        if self.window is not None:
            self.window.close()
            self.window.deleteLater()
            self.window = None
