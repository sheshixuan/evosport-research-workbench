#!/usr/bin/env python3
import ctypes
import os
import sys
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


if sys.platform != "win32":
    raise SystemExit("native Windows verification requires Windows")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
DELETE = 0x00010000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_DELETE = 0x00000004
CREATE_NEW = 1
FILE_ATTRIBUTE_TEMPORARY = 0x00000100
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_DISPOSITION_INFO = 4
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32),
        ("creation", FileTime),
        ("access", FileTime),
        ("write", FileTime),
        ("volume", ctypes.c_uint32),
        ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32),
        ("links", ctypes.c_uint32),
        ("index_high", ctypes.c_uint32),
        ("index_low", ctypes.c_uint32),
    ]


class Disposition(ctypes.Structure):
    _fields_ = [("delete", ctypes.c_ubyte)]


CreateFileW = kernel32.CreateFileW
CreateFileW.argtypes = [
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
]
CreateFileW.restype = ctypes.c_void_p
DuplicateHandle = kernel32.DuplicateHandle
DuplicateHandle.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_uint32,
    ctypes.c_int,
    ctypes.c_uint32,
]
DuplicateHandle.restype = ctypes.c_int
GetCurrentProcess = kernel32.GetCurrentProcess
GetCurrentProcess.argtypes = []
GetCurrentProcess.restype = ctypes.c_void_p
GetFileInformationByHandle = kernel32.GetFileInformationByHandle
GetFileInformationByHandle.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(FileInformation),
]
GetFileInformationByHandle.restype = ctypes.c_int
WriteFile = kernel32.WriteFile
WriteFile.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_void_p,
]
WriteFile.restype = ctypes.c_int
FlushFileBuffers = kernel32.FlushFileBuffers
FlushFileBuffers.argtypes = [ctypes.c_void_p]
FlushFileBuffers.restype = ctypes.c_int
SetFileInformationByHandle = kernel32.SetFileInformationByHandle
SetFileInformationByHandle.argtypes = [
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_uint32,
]
SetFileInformationByHandle.restype = ctypes.c_int
CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [ctypes.c_void_p]
CloseHandle.restype = ctypes.c_int


def identity(handle: int) -> tuple[int, int]:
    information = FileInformation()
    if not GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    return information.volume, (information.index_high << 32) | information.index_low


def create_owner(path: Path, payload: bytes) -> int:
    writer = CreateFileW(
        str(path),
        GENERIC_READ | GENERIC_WRITE | DELETE,
        FILE_SHARE_READ | FILE_SHARE_DELETE,
        None,
        CREATE_NEW,
        FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )
    if writer in (None, INVALID_HANDLE_VALUE):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        offset = 0
        while offset < len(payload):
            chunk = payload[offset:]
            buffer = ctypes.create_string_buffer(chunk)
            written = ctypes.c_uint32()
            if not WriteFile(writer, buffer, len(chunk), ctypes.byref(written), None):
                raise ctypes.WinError(ctypes.get_last_error())
            offset += written.value
        if not FlushFileBuffers(writer):
            raise ctypes.WinError(ctypes.get_last_error())
        owner = ctypes.c_void_p()
        process = GetCurrentProcess()
        if not DuplicateHandle(
            process,
            writer,
            process,
            ctypes.byref(owner),
            GENERIC_READ | DELETE,
            False,
            0,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        assert identity(writer) == identity(owner.value)
    finally:
        if not CloseHandle(writer):
            raise ctypes.WinError(ctypes.get_last_error())
    return int(owner.value)


def dispose(handle: int) -> None:
    value = Disposition(1)
    if not SetFileInformationByHandle(
        handle,
        FILE_DISPOSITION_INFO,
        ctypes.byref(value),
        ctypes.sizeof(value),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


sink = pa.BufferOutputStream()
pq.write_table(pa.table({"token_id": ["YES", "YES"], "price": [0.41, 0.42]}), sink)
payload = sink.getvalue().to_pybytes()
root = Path(tempfile.mkdtemp(prefix="evosport-native-windows-"))
try:
    normal = root / "selected.parquet"
    owner = create_owner(normal, payload)
    assert pq.read_table(normal).num_rows == 2
    assert pq.read_table(normal).num_rows == 2
    try:
        with normal.open("r+b"):
            pass
    except OSError:
        pass
    else:
        raise AssertionError("external write open was not refused")
    dispose(owner)
    assert not normal.exists()

    original = root / "replacement.parquet"
    moved = root / "owned-moved.parquet"
    owner = create_owner(original, payload)
    os.replace(original, moved)
    original.write_bytes(b"external replacement")
    dispose(owner)
    assert not moved.exists()
    assert original.read_bytes() == b"external replacement"
    original.unlink()
finally:
    root.rmdir()

print("pyarrow_reads=2 rows=2")
print("mutation_refused=true")
print("delete_pending_final_close_disappearance=true")
print("replacement_preserved=true")
print("WINDOWS_NATIVE_HANDLE_PROTOCOL_OK")
