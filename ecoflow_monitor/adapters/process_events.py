import ctypes
import logging
import math
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass

from PySide6 import QtCore
from shiboken6 import VoidPtr

from ecoflow_monitor.adapters.elevation_windows import close_handle, kernel32
from ecoflow_monitor.adapters.integrations import ProcessEntry, ProcessSnapshot, integration_process_names, process_entry, running_processes
from ecoflow_monitor.diagnostics import event


FILETIME_EPOCH = 116444736000000000
SYNCHRONIZE = 0x100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_NO_MORE_FILES = 18
WBEM_FORWARD_ONLY = 0x20
WBEM_RETURN_IMMEDIATELY = 0x10
WBEM_TIMED_OUT = 0x80043001
WMI_WAIT_MS = 5000
MAX_PENDING_EVENTS = 256


class ProcessEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def read_named_process(pid: int, name: str) -> ProcessEntry | None:
    entry = process_entry(pid)
    if entry is not None and not entry.name:
        return ProcessEntry(pid, name, "", entry.created)
    return entry if entry is not None and entry.name == name else None


def relevant_processes(names: frozenset[str]) -> tuple[ProcessEntry, ...]:
    if not names:
        return ()
    if sys.platform != "win32":
        return tuple(entry for entry in running_processes().entries if entry.name in names)
    api = kernel32()
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    first, advance = api.Process32FirstW, api.Process32NextW
    first.argtypes = advance.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
    first.restype = advance.restype = wintypes.BOOL
    snapshot = api.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    candidates = []
    try:
        record = ProcessEntry32(dwSize=ctypes.sizeof(ProcessEntry32))
        found = first(snapshot, ctypes.byref(record))
        while found:
            name = record.szExeFile.casefold()
            if name in names:
                candidates.append((record.th32ProcessID, name))
            found = advance(snapshot, ctypes.byref(record))
        error = ctypes.get_last_error()
        if error != ERROR_NO_MORE_FILES:
            raise ctypes.WinError(error)
    finally:
        close_handle(snapshot)
    return tuple(entry for pid, name in candidates if (entry := read_named_process(pid, name)) is not None)


def filetime_seconds(value: int) -> float:
    return (value - FILETIME_EPOCH) / 10_000_000


def open_process(entry: ProcessEntry):
    api = kernel32()
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    handle = api.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, entry.pid)
    if not handle:
        return None
    timestamps = [wintypes.FILETIME() for _ in range(4)]
    api.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    api.GetProcessTimes.restype = wintypes.BOOL
    if api.GetProcessTimes(handle, *(ctypes.byref(stamp) for stamp in timestamps)):
        created = timestamps[0]
        seconds = filetime_seconds((created.dwHighDateTime << 32) | created.dwLowDateTime)
        if entry.created is not None and abs(seconds - entry.created) < 0.00001:
            return handle
    close_handle(handle)
    return None


def duplicate_wait_handle(handle):
    api = kernel32()
    api.GetCurrentProcess.argtypes = []
    api.GetCurrentProcess.restype = wintypes.HANDLE
    process = api.GetCurrentProcess()
    copied = wintypes.HANDLE()
    api.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                    ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.DuplicateHandle.restype = wintypes.BOOL
    if api.DuplicateHandle(process, handle, process, ctypes.byref(copied), SYNCHRONIZE, False, 0):
        return copied.value
    return None


class ProcessExitNotifications(QtCore.QObject):
    exited = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._notifiers = {}

    def reconcile(self, keys, open_handle):
        if sys.platform != "win32":
            return
        keys = set(keys)
        for key in self._notifiers.keys() - keys:
            self._remove(key)
        for key in keys - self._notifiers.keys():
            handle = open_handle(key)
            if handle:
                notifier = QtCore.QWinEventNotifier(parent=self)
                try:
                    notifier.setHandle(VoidPtr(handle))
                except (TypeError, ValueError):
                    notifier.deleteLater()
                    close_handle(handle)
                    event("process_exit_watch_failed", level=logging.WARNING, reason="native_handle_binding")
                    continue
                notifier.activated.connect(self._activated)
                self._notifiers[key] = notifier, handle
                notifier.setEnabled(True)

    @QtCore.Slot()
    def _activated(self):
        sender = self.sender()
        for key, (notifier, _) in self._notifiers.items():
            if notifier is sender:
                self._remove(key)
                self.exited.emit(key)
                return

    def _remove(self, key):
        notifier, handle = self._notifiers.pop(key)
        notifier.setEnabled(False)
        notifier.deleteLater()
        close_handle(handle)

    def clear(self):
        for key in tuple(self._notifiers):
            self._remove(key)


@dataclass(frozen=True)
class ProcessNotice:
    pid: int
    name: str
    at: float
    started: bool
    entry: ProcessEntry | None = None


class ProcessSubscriptionError(Exception):
    pass


def wmi_error_code(error):
    details = error.excepinfo
    return (details[5] if details and details[5] else error.hresult) & 0xFFFFFFFF


def watch_wmi(modules, cancelled, publish):
    import pythoncom
    import pywintypes
    from win32com.client import GetObject

    names = integration_process_names(modules)
    pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
    services = source = indication = None
    failure = None
    try:
        services = GetObject(r"winmgmts:{authenticationLevel=pktPrivacy,impersonationLevel=impersonate}!\\.\root\cimv2")
        source = services.ExecNotificationQuery("SELECT * FROM Win32_ProcessTrace", "WQL",
                                                WBEM_FORWARD_ONLY | WBEM_RETURN_IMMEDIATELY)
        pending = True
        while not cancelled.is_set():
            try:
                indication = source.NextEvent(0 if pending else WMI_WAIT_MS)
            except pywintypes.com_error as error:
                if wmi_error_code(error) != WBEM_TIMED_OUT:
                    raise
                indication = None
            if pending:
                publish("ready", None)
                pending = False
            if indication is None:
                continue
            name = str(indication.ProcessName).casefold()
            if name not in names:
                continue
            pid = int(indication.ProcessID)
            observed = filetime_seconds(int(indication.TIME_CREATED))
            started = indication.Path_.Class == "Win32_ProcessStartTrace"
            entry = read_named_process(pid, name) if started else None
            publish("process", ProcessNotice(pid, name, observed, started, entry))
    except pywintypes.com_error as error:
        failure = wmi_error_code(error)
    finally:
        indication = source = services = None
        pythoncom.CoUninitialize()
    if failure is not None:
        raise ProcessSubscriptionError(f"0x{failure:08X}")


class ProcessTracker(QtCore.QObject):
    changed = QtCore.Signal()
    _available = QtCore.Signal()

    def __init__(self, clock, parent=None, *, subscribe=watch_wmi):
        super().__init__(parent)
        self.clock = clock
        self._subscribe = subscribe
        self._watchers = ProcessExitNotifications(self)
        self._watchers.exited.connect(self._process_exited)
        self._available.connect(self._drain, QtCore.Qt.ConnectionType.QueuedConnection)
        self._guard = threading.Lock()
        self._pending = []
        self._worker = None
        self._cancelled = None
        self._signature = ()
        self._modules = ()
        self._ready = False
        self._snapshot_token = None
        self._snapshot_events = []
        self._entries = {}
        self._next_scan = math.inf
        self._next_retry = math.inf
        self._retry_delay = 5
        self.initialized = False
        self.revision = 0

    def configure(self, modules):
        signature = tuple((module.id, module.driver, module.executable, tuple(rule.executable for rule in module.application_rules))
                          for module in modules)
        if signature == self._signature:
            return
        self.stop()
        self._signature = signature
        self._modules = modules
        self._next_scan = self.clock() + 1 if modules else math.inf
        self._next_retry = self.clock() if modules else math.inf
        self._retry_delay = 5

    def stop(self):
        with self._guard:
            if self._cancelled is not None:
                self._cancelled.set()
                self._cancelled = None
            self._pending.clear()
        self._signature = self._modules = ()
        self._ready = self.initialized = False
        self._entries.clear()
        self._watchers.clear()
        self._invalidate_snapshot()
        self._next_scan = self._next_retry = math.inf
        self.revision += 1

    def advance(self, now):
        if now >= self._next_retry and self._worker is None:
            self._cancelled = threading.Event()
            self._next_retry = math.inf
            self._worker = threading.Thread(target=self._watch, args=(self._modules, self._cancelled),
                                            name="ecoflow-process-events", daemon=True)
            self._worker.start()

    def _watch(self, modules, cancelled):
        def publish(kind, value):
            with self._guard:
                if cancelled is not self._cancelled or cancelled.is_set():
                    return
                if len(self._pending) >= MAX_PENDING_EVENTS:
                    self._pending.clear()
                    self._pending.append(("lost", "event_queue_full"))
                    cancelled.set()
                else:
                    self._pending.append((kind, value))
                if len(self._pending) == 1:
                    self._available.emit()

        try:
            self._subscribe(modules, cancelled, publish)
            publish("lost", "subscription_ended")
        except ProcessSubscriptionError as error:
            publish("lost", str(error))
        except Exception as error:
            publish("lost", type(error).__name__)
        finally:
            with self._guard:
                self._worker = None
                if self._cancelled is not None:
                    self._available.emit()

    @QtCore.Slot()
    def _drain(self):
        with self._guard:
            pending, self._pending = self._pending, []
        for kind, value in pending:
            if kind == "ready":
                self._ready = True
                self.initialized = False
                self._retry_delay = 5
                self._invalidate_snapshot()
                self._next_scan = self.clock()
            elif kind == "lost":
                if self._ready or not self.initialized and self._snapshot_token is None:
                    self.initialized = False
                    self._invalidate_snapshot()
                    self._next_scan = self.clock()
                self._ready = False
                self._next_retry = self.clock() + self._retry_delay
                self._retry_delay = min(self._retry_delay * 2, 60)
                event("process_events_unavailable", level=logging.WARNING, reason=value)
            else:
                self._apply_notice(value)
        self.changed.emit()

    def deadline(self, *, snapshot_busy=False):
        return min(self._next_retry if self._worker is None else math.inf,
                   self._next_scan if not snapshot_busy else math.inf)

    def begin_snapshot(self, now):
        if now < self._next_scan:
            return None
        self._snapshot_token = object()
        self._snapshot_events.clear()
        self._next_scan = math.inf if self._ready else now + 2
        return self._snapshot_token

    def _invalidate_snapshot(self):
        self._snapshot_token = None
        self._snapshot_events.clear()

    def finish_snapshot(self, token, entries):
        if token is not self._snapshot_token or token is None:
            return False
        if entries is None or len(self._snapshot_events) > MAX_PENDING_EVENTS:
            self.initialized = False
            self._next_scan = self.clock() + 2
            self._invalidate_snapshot()
            return True
        self._entries = {entry.pid: entry for entry in entries}
        for notice in self._snapshot_events:
            self._update_entries(notice)
        self._invalidate_snapshot()
        self.initialized = True
        self.revision += 1
        self._update_handles()
        return True

    def snapshot(self):
        return ProcessSnapshot.from_entries(self._entries.values())

    def _update_handles(self):
        self._watchers.reconcile((entry for entry in self._entries.values() if entry.created is not None), open_process)

    def _process_exited(self, entry):
        self._apply_notice(entry)
        self.changed.emit()

    def _apply_notice(self, notice):
        if self._snapshot_token is not None and len(self._snapshot_events) <= MAX_PENDING_EVENTS:
            self._snapshot_events.append(notice)
        if self._update_entries(notice):
            self.revision += 1
            self._update_handles()

    def _update_entries(self, notice):
        current = self._entries.get(notice.pid)
        if isinstance(notice, ProcessEntry):
            if current != notice:
                return False
        elif notice.started:
            entry = notice.entry
            if (entry is None or entry.name != notice.name or current == entry
                    or (entry.created is not None and entry.created > notice.at + 0.01)):
                return False
            self._entries[entry.pid] = entry
            return True
        elif current is None or current.name != notice.name or (current.created is not None and current.created > notice.at):
            return False
        self._entries.pop(notice.pid)
        return True
