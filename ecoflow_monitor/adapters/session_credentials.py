import ctypes
from ctypes import wintypes

from ecoflow_monitor.adapters.credentials import CredentialError, CredentialStore, NativeCredential
from ecoflow_monitor.adapters.instance_windows import instance_name


class SessionStorageError(CredentialError):
    def __init__(self):
        super().__init__("onepassword_cache_error")


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]


class WindowsDataProtection:
    def _transform(self, value: bytes, entropy: bytearray, decrypt: bool) -> bytes:
        source_buffer = ctypes.create_string_buffer(value)
        entropy_buffer = (ctypes.c_ubyte * len(entropy)).from_buffer(entropy)
        source = DataBlob(len(value), ctypes.cast(source_buffer, ctypes.c_void_p))
        extra = DataBlob(len(entropy), ctypes.cast(entropy_buffer, ctypes.c_void_p))
        result = DataBlob()
        try:
            api = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree.restype = ctypes.c_void_p
            operation = api.CryptUnprotectData if decrypt else api.CryptProtectData
            operation.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.POINTER(DataBlob),
                                  ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
            operation.restype = wintypes.BOOL
            if not operation(ctypes.byref(source), None, ctypes.byref(extra), None, None, 1, ctypes.byref(result)):
                raise SessionStorageError()
            try:
                return ctypes.string_at(result.data, result.size)
            finally:
                ctypes.memset(result.data, 0, result.size)
                kernel.LocalFree(result.data)
        except (OSError, AttributeError):
            raise SessionStorageError() from None
        finally:
            ctypes.memset(source_buffer, 0, len(source_buffer))

    def protect(self, value: bytes, entropy: bytearray) -> bytes:
        return self._transform(value, entropy, False)

    def unprotect(self, value: bytes, entropy: bytearray) -> bytes:
        return self._transform(value, entropy, True)


class SessionCredentialStore:
    def __init__(self, reference: str | None = None):
        if reference is not None and not reference.startswith("EcoFlowMonitor/1PasswordSession/"):
            raise ValueError("Invalid session credential reference")
        self._reference = reference
        self._credentials = CredentialStore()

    @property
    def reference(self) -> str:
        if self._reference is None:
            try:
                self._reference = "EcoFlowMonitor/1PasswordSession/" + instance_name()
            except OSError:
                raise SessionStorageError() from None
        return self._reference

    def read(self) -> bytes | None:
        try:
            api = self._credentials._api()
            pointer = ctypes.POINTER(NativeCredential)()
            if not api.CredReadW(self.reference, 1, 0, ctypes.byref(pointer)):
                if ctypes.get_last_error() == 1168:
                    return None
                raise SessionStorageError()
            try:
                return ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
            finally:
                api.CredFree(pointer)
        except (OSError, CredentialError):
            raise SessionStorageError() from None

    def write(self, value: bytes) -> None:
        if not value or len(value) > 2560:
            raise SessionStorageError()
        buffer = ctypes.create_string_buffer(value)
        credential = NativeCredential()
        credential.Type = 1
        credential.TargetName = self.reference
        credential.UserName = "1Password session"
        credential.CredentialBlobSize = len(value)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.c_void_p)
        credential.Persist = 1
        try:
            if not self._credentials._api().CredWriteW(ctypes.byref(credential), 0):
                raise SessionStorageError()
        except (OSError, CredentialError):
            raise SessionStorageError() from None

    def delete(self) -> None:
        try:
            self._credentials.delete(self.reference)
        except (OSError, CredentialError):
            raise SessionStorageError() from None
