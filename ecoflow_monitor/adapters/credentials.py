import ctypes
from ctypes import wintypes
import sys

from ecoflow_monitor.adapters.ecoflow import Credentials


class CredentialError(Exception):
    pass


class NativeCredential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.c_void_p),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


class CredentialStore:
    def _api(self):
        if sys.platform != "win32":
            raise CredentialError("credentials_error")
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(NativeCredential))]
        api.CredReadW.restype = wintypes.BOOL
        api.CredWriteW.argtypes = [ctypes.POINTER(NativeCredential), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        return api

    def read(self, reference: str) -> Credentials | None:
        if not reference.startswith("EcoFlowMonitor/"):
            raise CredentialError("credentials_error")
        api = self._api()
        pointer = ctypes.POINTER(NativeCredential)()
        if not api.CredReadW(reference, 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:
                return None
            raise CredentialError("credentials_error")
        try:
            credential = pointer.contents
            secret = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode("utf-8")
            return Credentials(credential.UserName or "", secret)
        except (ValueError, UnicodeError):
            raise CredentialError("credentials_error") from None
        finally:
            api.CredFree(pointer)

    def write(self, reference: str, credentials: Credentials) -> None:
        if not reference.startswith("EcoFlowMonitor/") or not credentials.access_key or not credentials.secret_key:
            raise CredentialError("credentials_error")
        secret = credentials.secret_key.encode("utf-8")
        if len(secret) > 2560:
            raise CredentialError("credentials_error")
        buffer = ctypes.create_string_buffer(secret)
        credential = NativeCredential()
        credential.Type = 1
        credential.TargetName = reference
        credential.UserName = credentials.access_key
        credential.CredentialBlobSize = len(secret)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.c_void_p)
        credential.Persist = 2
        if not self._api().CredWriteW(ctypes.byref(credential), 0):
            raise CredentialError("credentials_error")

    def delete(self, reference: str) -> None:
        if not reference.startswith("EcoFlowMonitor/"):
            raise CredentialError("credentials_error")
        api = self._api()
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        if not api.CredDeleteW(reference, 1, 0) and ctypes.get_last_error() != 1168:
            raise CredentialError("credentials_error")
