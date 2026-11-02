import os
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ecoflow_monitor.adapters.instance_windows import InstanceMutex, instance_name
from ecoflow_monitor.adapters.windows import allow_foreground


class InstanceController(QObject):
    activation_requested = Signal()

    def __init__(self, name: str | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.name = name if name is not None else instance_name()
        self._mutex = InstanceMutex(self.name)
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept)
        self._clients = {}

    def start(self) -> bool:
        if not self._mutex.acquire():
            return False
        if not self._server.listen(self.name):
            self._mutex.close()
            raise OSError(self._server.errorString())
        return True

    def activate_existing(self, timeout_ms: int = 5000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        def remaining():
            return max(0, int((deadline - time.monotonic()) * 1000))
        socket = QLocalSocket()
        try:
            while remaining():
                socket.connectToServer(self.name)
                if socket.waitForConnected(min(100, remaining())):
                    break
                socket.abort()
                time.sleep(min(0.025, remaining() / 1000))
            else:
                return False
            def read_line():
                while not socket.canReadLine() and remaining():
                    if not socket.waitForReadyRead(remaining()):
                        break
                if socket.canReadLine():
                    return bytes(socket.readLine(128))
                return b""
            greeting = read_line()
            try:
                process_id = int(greeting.removeprefix(b"pid ").strip())
            except ValueError:
                return False
            if not greeting.startswith(b"pid ") or not 0 < process_id <= 0xFFFFFFFF:
                return False
            allow_foreground(process_id)
            socket.write(b"activate\n")
            if socket.bytesToWrite() and not socket.waitForBytesWritten(remaining()):
                return False
            return read_line() == b"ok\n"
        finally:
            socket.abort()

    def _accept(self):
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(socket.abort)
            timer.start(5000)
            socket.setReadBufferSize(128)
            self._clients[socket] = bytearray()
            socket.readyRead.connect(lambda connection=socket: self._receive(connection))
            socket.disconnected.connect(lambda connection=socket: self._release(connection))
            socket.write(f"pid {os.getpid()}\n".encode("ascii"))
            if socket.bytesAvailable():
                self._receive(socket)

    def _receive(self, socket):
        buffer = self._clients.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > 128:
            socket.abort()
            return
        if b"\n" not in buffer:
            return
        if bytes(buffer) == b"activate\n":
            self.activation_requested.emit()
            socket.write(b"ok\n")
            socket.disconnectFromServer()
        else:
            socket.abort()

    def _release(self, socket):
        self._clients.pop(socket, None)
        socket.deleteLater()

    def close(self) -> None:
        self._server.close()
        for socket in list(self._clients):
            socket.abort()
        self._mutex.close()
