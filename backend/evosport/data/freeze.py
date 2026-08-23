import ctypes
import hashlib
import os
import secrets
import shutil
import stat
import sys
import tempfile
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISREG
from urllib.parse import unquote, urlparse

from evosport.data.manifest import (
    CATALOG_SCHEMA_VERSION,
    DatasetFile,
    DatasetManifest,
    manifest_id_for,
    sha256_file,
)
from evosport.semantics.football_binding import FootballDatasetBinding


_LINUX_O_TMPFILE = getattr(os, "O_TMPFILE", None)
_LINUX_FD_ROOT = Path("/proc/self/fd")


def _open_linux_tmpfile(directory: Path, flags: int, mode: int) -> int:
    return os.open(directory, flags, mode)


_LINUX_OPEN_TMPFILE = _open_linux_tmpfile


def _open_linux_readonly(path: Path, flags: int) -> int:
    return os.open(path, flags)


_LINUX_OPEN_READONLY = _open_linux_readonly


class _WindowsFileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class _WindowsFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _WindowsFileTime),
        ("ftLastAccessTime", _WindowsFileTime),
        ("ftLastWriteTime", _WindowsFileTime),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _WindowsFileDispositionInformation(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WINDOWS_FILE_ATTRIBUTE_TEMPORARY = 0x00000100
_WINDOWS_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
_WINDOWS_DELETE = 0x00010000
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_GENERIC_WRITE = 0x40000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_DELETE = 0x00000004
_WINDOWS_CREATE_NEW = 1
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_DISPOSITION_INFO = 4
_WINDOWS_GET_FILE_INFORMATION_BY_HANDLE = None
_WINDOWS_SET_FILE_INFORMATION_BY_HANDLE = None
_WINDOWS_CREATE_FILE = None
_WINDOWS_CLOSE_HANDLE = None
_WINDOWS_DUPLICATE_HANDLE = None
_WINDOWS_GET_CURRENT_PROCESS = None
_WINDOWS_WRITE_FILE = None
_WINDOWS_READ_FILE = None
_WINDOWS_FLUSH_FILE_BUFFERS = None
if sys.platform == "win32":
    try:
        _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _WINDOWS_CREATE_FILE = _KERNEL32.CreateFileW
        _WINDOWS_GET_FILE_INFORMATION_BY_HANDLE = _KERNEL32.GetFileInformationByHandle
        _WINDOWS_SET_FILE_INFORMATION_BY_HANDLE = _KERNEL32.SetFileInformationByHandle
        _WINDOWS_CLOSE_HANDLE = _KERNEL32.CloseHandle
        _WINDOWS_DUPLICATE_HANDLE = _KERNEL32.DuplicateHandle
        _WINDOWS_GET_CURRENT_PROCESS = _KERNEL32.GetCurrentProcess
        _WINDOWS_WRITE_FILE = _KERNEL32.WriteFile
        _WINDOWS_READ_FILE = _KERNEL32.ReadFile
        _WINDOWS_FLUSH_FILE_BUFFERS = _KERNEL32.FlushFileBuffers
    except (AttributeError, OSError):
        _KERNEL32 = None
    else:
        _WINDOWS_CREATE_FILE.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        _WINDOWS_CREATE_FILE.restype = ctypes.c_void_p
        _WINDOWS_GET_FILE_INFORMATION_BY_HANDLE.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_WindowsFileInformation),
        ]
        _WINDOWS_GET_FILE_INFORMATION_BY_HANDLE.restype = ctypes.c_int
        _WINDOWS_SET_FILE_INFORMATION_BY_HANDLE.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        _WINDOWS_SET_FILE_INFORMATION_BY_HANDLE.restype = ctypes.c_int
        _WINDOWS_CLOSE_HANDLE.argtypes = [ctypes.c_void_p]
        _WINDOWS_CLOSE_HANDLE.restype = ctypes.c_int
        _WINDOWS_DUPLICATE_HANDLE.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        _WINDOWS_DUPLICATE_HANDLE.restype = ctypes.c_int
        _WINDOWS_GET_CURRENT_PROCESS.argtypes = []
        _WINDOWS_GET_CURRENT_PROCESS.restype = ctypes.c_void_p
        _WINDOWS_WRITE_FILE.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        _WINDOWS_WRITE_FILE.restype = ctypes.c_int
        _WINDOWS_READ_FILE.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        _WINDOWS_READ_FILE.restype = ctypes.c_int
        _WINDOWS_FLUSH_FILE_BUFFERS.argtypes = [ctypes.c_void_p]
        _WINDOWS_FLUSH_FILE_BUFFERS.restype = ctypes.c_int


class _DarwinFSRef(ctypes.Structure):
    _fields_ = [("hidden", ctypes.c_ubyte * 80)]


_FS_PATH_MAKE_REF_WITH_OPTIONS = None
_FS_DELETE_OBJECT = None
_CF_URL_CREATE_FROM_FS_REF = None
_CF_URL_CREATE_FROM_FILE_SYSTEM_REPRESENTATION = None
_CF_URL_COPY_RESOURCE_PROPERTY_FOR_KEY = None
_CF_DATA_GET_LENGTH = None
_CF_DATA_GET_BYTE_PTR = None
_CF_GET_TYPE_ID = None
_CF_DATA_GET_TYPE_ID = None
_CF_RELEASE = None
_CF_URL_FILE_RESOURCE_IDENTIFIER_KEY = None
if sys.platform == "darwin":
    try:
        _CORE_SERVICES = ctypes.CDLL(
            "/System/Library/Frameworks/CoreServices.framework/CoreServices"
        )
        _CORE_FOUNDATION = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        _FS_PATH_MAKE_REF_WITH_OPTIONS = _CORE_SERVICES.FSPathMakeRefWithOptions
        _FS_DELETE_OBJECT = _CORE_SERVICES.FSDeleteObject
        _CF_URL_CREATE_FROM_FS_REF = _CORE_FOUNDATION.CFURLCreateFromFSRef
        _CF_URL_CREATE_FROM_FILE_SYSTEM_REPRESENTATION = (
            _CORE_FOUNDATION.CFURLCreateFromFileSystemRepresentation
        )
        _CF_URL_COPY_RESOURCE_PROPERTY_FOR_KEY = (
            _CORE_FOUNDATION.CFURLCopyResourcePropertyForKey
        )
        _CF_DATA_GET_LENGTH = _CORE_FOUNDATION.CFDataGetLength
        _CF_DATA_GET_BYTE_PTR = _CORE_FOUNDATION.CFDataGetBytePtr
        _CF_GET_TYPE_ID = _CORE_FOUNDATION.CFGetTypeID
        _CF_DATA_GET_TYPE_ID = _CORE_FOUNDATION.CFDataGetTypeID
        _CF_RELEASE = _CORE_FOUNDATION.CFRelease
        _CF_URL_FILE_RESOURCE_IDENTIFIER_KEY = ctypes.c_void_p.in_dll(
            _CORE_FOUNDATION,
            "kCFURLFileResourceIdentifierKey",
        ).value
    except (AttributeError, OSError, ValueError):
        _CORE_SERVICES = None
        _CORE_FOUNDATION = None
    else:
        _FS_PATH_MAKE_REF_WITH_OPTIONS.argtypes = [
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DarwinFSRef),
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        _FS_PATH_MAKE_REF_WITH_OPTIONS.restype = ctypes.c_int32
        _FS_DELETE_OBJECT = _CORE_SERVICES.FSDeleteObject
        _FS_DELETE_OBJECT.argtypes = [ctypes.POINTER(_DarwinFSRef)]
        _FS_DELETE_OBJECT.restype = ctypes.c_int32
        _CF_URL_CREATE_FROM_FS_REF.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_DarwinFSRef),
        ]
        _CF_URL_CREATE_FROM_FS_REF.restype = ctypes.c_void_p
        _CF_URL_CREATE_FROM_FILE_SYSTEM_REPRESENTATION.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_bool,
        ]
        _CF_URL_CREATE_FROM_FILE_SYSTEM_REPRESENTATION.restype = ctypes.c_void_p
        _CF_URL_COPY_RESOURCE_PROPERTY_FOR_KEY.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        _CF_URL_COPY_RESOURCE_PROPERTY_FOR_KEY.restype = ctypes.c_bool
        _CF_DATA_GET_LENGTH.argtypes = [ctypes.c_void_p]
        _CF_DATA_GET_LENGTH.restype = ctypes.c_long
        _CF_DATA_GET_BYTE_PTR.argtypes = [ctypes.c_void_p]
        _CF_DATA_GET_BYTE_PTR.restype = ctypes.POINTER(ctypes.c_ubyte)
        _CF_GET_TYPE_ID.argtypes = [ctypes.c_void_p]
        _CF_GET_TYPE_ID.restype = ctypes.c_ulong
        _CF_DATA_GET_TYPE_ID.argtypes = []
        _CF_DATA_GET_TYPE_ID.restype = ctypes.c_ulong
        _CF_RELEASE.argtypes = [ctypes.c_void_p]
        _CF_RELEASE.restype = None


def _normalize_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be aware")
    return value.astimezone(timezone.utc)


def _file_uri_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError("provider dataset storage_uri must be a file URI")
    path = unquote(parsed.path)
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path)


def _verify_payload(path: Path, file_record: DatasetFile) -> None:
    if (
        not path.is_file()
        or path.is_symlink()
        or path.stat().st_size != file_record.size_bytes
        or sha256_file(path) != file_record.sha256
    ):
        raise RuntimeError(f"snapshot integrity verification failed for {path}")


def _verify_snapshot_tree(target: Path, manifest: DatasetManifest) -> DatasetManifest:
    try:
        if target.is_symlink() or not target.is_dir():
            raise RuntimeError("snapshot target is not a regular directory")
        entries = {path.name: path for path in target.iterdir()}
        if set(entries) != {"manifest.json"}:
            raise RuntimeError("snapshot tree has undeclared entries")
        manifest_path = entries["manifest.json"]
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RuntimeError("snapshot manifest is not a regular file")
        for file_record in manifest.files:
            _verify_payload(Path(file_record.path), file_record)
        return manifest
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"snapshot integrity verification failed for {target}") from exc


def load_verified_snapshot(manifest_path: Path) -> tuple[DatasetManifest, bytes]:
    target = manifest_path.parent
    try:
        if manifest_path.name != "manifest.json":
            raise RuntimeError("snapshot manifest is not a regular file")
        descriptor = os.open(manifest_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            captured_state = os.fstat(descriptor)
            if not S_ISREG(captured_state.st_mode):
                raise RuntimeError("snapshot manifest is not a regular file")
            with os.fdopen(os.dup(descriptor), "rb") as manifest_file:
                manifest_bytes = manifest_file.read()
            manifest = DatasetManifest.model_validate_json(manifest_bytes)
            _verify_snapshot_tree(target, manifest)
            current_state = os.stat(manifest_path, follow_symlinks=False)
            if not _same_regular_file_state(captured_state, current_state):
                raise RuntimeError("snapshot manifest changed during integrity verification")
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"snapshot integrity verification failed for {target}") from exc
    return manifest, manifest_bytes


def _same_regular_file_state(captured: os.stat_result, current: os.stat_result) -> bool:
    return (
        S_ISREG(current.st_mode)
        and captured.st_dev == current.st_dev
        and captured.st_ino == current.st_ino
        and captured.st_mode == current.st_mode
        and captured.st_size == current.st_size
        and captured.st_mtime_ns == current.st_mtime_ns
        and captured.st_ctime_ns == current.st_ctime_ns
        and captured.st_nlink == current.st_nlink
    )


def _same_regular_file_identity(captured: os.stat_result, current: os.stat_result) -> bool:
    return (
        S_ISREG(current.st_mode)
        and captured.st_dev == current.st_dev
        and captured.st_ino == current.st_ino
    )


def _same_directory_identity(captured: os.stat_result, current: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(current.st_mode)
        and captured.st_dev == current.st_dev
        and captured.st_ino == current.st_ino
    )


def _same_descriptor_exposure(captured: os.stat_result, exposed: os.stat_result) -> bool:
    return (
        S_ISREG(exposed.st_mode)
        and captured.st_ino == exposed.st_ino
        and captured.st_mode == exposed.st_mode
        and captured.st_size == exposed.st_size
        and captured.st_mtime_ns == exposed.st_mtime_ns
        and captured.st_ctime_ns == exposed.st_ctime_ns
    )


@dataclass(frozen=True)
class _OwnedObjectReference:
    backend: str
    native_reference: bytes | int
    is_directory: bool
    expected_identity: tuple[int, int]
    native_identity: bytes | tuple[int, int]


def _darwin_cleanup_is_available() -> bool:
    return all(
        capability is not None
        for capability in (
            _FS_PATH_MAKE_REF_WITH_OPTIONS,
            _FS_DELETE_OBJECT,
            _CF_URL_CREATE_FROM_FS_REF,
            _CF_URL_CREATE_FROM_FILE_SYSTEM_REPRESENTATION,
            _CF_URL_COPY_RESOURCE_PROPERTY_FOR_KEY,
            _CF_DATA_GET_LENGTH,
            _CF_DATA_GET_BYTE_PTR,
            _CF_GET_TYPE_ID,
            _CF_DATA_GET_TYPE_ID,
            _CF_RELEASE,
            _CF_URL_FILE_RESOURCE_IDENTIFIER_KEY,
        )
    )


def _darwin_resource_identifier(url: int | None) -> bytes:
    if not _darwin_cleanup_is_available() or not url:
        raise RuntimeError(
            "identity-bound selected data cleanup is unavailable on this platform"
        )
    value = ctypes.c_void_p()
    error = ctypes.c_void_p()
    try:
        copied = _CF_URL_COPY_RESOURCE_PROPERTY_FOR_KEY(
            url,
            _CF_URL_FILE_RESOURCE_IDENTIFIER_KEY,
            ctypes.byref(value),
            ctypes.byref(error),
        )
        if not copied or not value.value:
            raise RuntimeError("selected data ownership identity could not be read")
        if _CF_GET_TYPE_ID(value) != _CF_DATA_GET_TYPE_ID():
            raise RuntimeError("selected data ownership identity has an unsupported type")
        length = _CF_DATA_GET_LENGTH(value)
        if length <= 0:
            raise RuntimeError("selected data ownership identity is empty")
        return ctypes.string_at(_CF_DATA_GET_BYTE_PTR(value), length)
    finally:
        if value.value:
            _CF_RELEASE(value)
        if error.value:
            _CF_RELEASE(error)


def _darwin_reference_identity(native_reference: _DarwinFSRef) -> bytes:
    if _CF_URL_CREATE_FROM_FS_REF is None or _CF_RELEASE is None:
        raise RuntimeError(
            "identity-bound selected data cleanup is unavailable on this platform"
        )
    url = _CF_URL_CREATE_FROM_FS_REF(None, ctypes.byref(native_reference))
    try:
        return _darwin_resource_identifier(url)
    finally:
        if url:
            _CF_RELEASE(url)


def _darwin_descriptor_identity(descriptor: int, *, is_directory: bool) -> bytes:
    if _CF_URL_CREATE_FROM_FILE_SYSTEM_REPRESENTATION is None or _CF_RELEASE is None:
        raise RuntimeError(
            "identity-bound selected data cleanup is unavailable on this platform"
        )
    descriptor_path = os.fsencode(f"/dev/fd/{descriptor}")
    url = _CF_URL_CREATE_FROM_FILE_SYSTEM_REPRESENTATION(
        None,
        descriptor_path,
        len(descriptor_path),
        is_directory,
    )
    try:
        return _darwin_resource_identifier(url)
    finally:
        if url:
            _CF_RELEASE(url)


def _resolve_cleanup_backend() -> str:
    if sys.platform == "darwin" and _darwin_cleanup_is_available():
        return "darwin"
    if (
        sys.platform.startswith("linux")
        and isinstance(_LINUX_O_TMPFILE, int)
        and callable(_LINUX_OPEN_TMPFILE)
        and callable(_LINUX_OPEN_READONLY)
        and _LINUX_FD_ROOT.is_dir()
    ):
        return "linux"
    if sys.platform == "win32" and all(
        callable(capability)
        for capability in (
            _WINDOWS_CREATE_FILE,
            _WINDOWS_GET_FILE_INFORMATION_BY_HANDLE,
            _WINDOWS_SET_FILE_INFORMATION_BY_HANDLE,
            _WINDOWS_CLOSE_HANDLE,
            _WINDOWS_DUPLICATE_HANDLE,
            _WINDOWS_GET_CURRENT_PROCESS,
            _WINDOWS_WRITE_FILE,
            _WINDOWS_READ_FILE,
            _WINDOWS_FLUSH_FILE_BUFFERS,
        )
    ):
        return "windows"
    raise RuntimeError(
        "identity-bound selected data cleanup is unavailable on this platform"
    )


def _capture_owned_object_reference(
    path: Path,
    state: os.stat_result,
    *,
    descriptor: int,
    is_directory: bool,
) -> _OwnedObjectReference:
    if not _darwin_cleanup_is_available():
        raise RuntimeError(
            "identity-bound selected data cleanup is unavailable on this platform"
        )
    before = os.stat(path, follow_symlinks=False)
    same_identity = _same_directory_identity if is_directory else _same_regular_file_identity
    if not same_identity(state, before):
        raise RuntimeError("selected data ownership changed while it was captured")
    native_reference = _DarwinFSRef()
    captured_is_directory = ctypes.c_ubyte()
    status = _FS_PATH_MAKE_REF_WITH_OPTIONS(
        os.fsencode(path),
        1,
        ctypes.byref(native_reference),
        ctypes.byref(captured_is_directory),
    )
    if status != 0:
        raise RuntimeError(f"selected data ownership capture failed with status {status}")
    descriptor_identity = _darwin_descriptor_identity(
        descriptor,
        is_directory=is_directory,
    )
    native_identity = _darwin_reference_identity(native_reference)
    after = os.stat(path, follow_symlinks=False)
    if (
        bool(captured_is_directory.value) != is_directory
        or not same_identity(state, after)
        or native_identity != descriptor_identity
    ):
        raise RuntimeError("selected data ownership changed while it was captured")
    return _OwnedObjectReference(
        backend="darwin",
        native_reference=bytes(native_reference.hidden),
        is_directory=is_directory,
        expected_identity=(state.st_dev, state.st_ino),
        native_identity=native_identity,
    )


def _windows_handle_identity(handle: int, *, is_directory: bool) -> tuple[int, int]:
    if not callable(_WINDOWS_GET_FILE_INFORMATION_BY_HANDLE):
        raise RuntimeError(
            "identity-bound selected data cleanup is unavailable on this platform"
        )
    information = _WindowsFileInformation()
    if not _WINDOWS_GET_FILE_INFORMATION_BY_HANDLE(handle, ctypes.byref(information)):
        error_code = getattr(ctypes, "get_last_error", lambda: 0)()
        raise RuntimeError(
            f"selected data ownership identity failed with Windows error {error_code}"
        )
    captured_is_directory = bool(
        information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
    )
    if captured_is_directory != is_directory:
        raise RuntimeError("selected data ownership changed while it was captured")
    file_id = (information.nFileIndexHigh << 32) | information.nFileIndexLow
    return information.dwVolumeSerialNumber, file_id


def _capture_windows_owned_handle(
    handle: int,
    *,
    is_directory: bool,
) -> _OwnedObjectReference:
    identity = _windows_handle_identity(handle, is_directory=is_directory)
    return _OwnedObjectReference(
        backend="windows",
        native_reference=handle,
        is_directory=is_directory,
        expected_identity=identity,
        native_identity=identity,
    )


def _close_windows_handle(handle: int) -> None:
    if not callable(_WINDOWS_CLOSE_HANDLE) or not _WINDOWS_CLOSE_HANDLE(handle):
        error_code = getattr(ctypes, "get_last_error", lambda: 0)()
        raise RuntimeError(
            f"selected data native handle cleanup failed with Windows error {error_code}"
        )


def _duplicate_windows_read_delete_handle(handle: int) -> int:
    if not callable(_WINDOWS_DUPLICATE_HANDLE) or not callable(
        _WINDOWS_GET_CURRENT_PROCESS
    ):
        raise RuntimeError(
            "identity-bound selected data cleanup is unavailable on this platform"
        )
    process = _WINDOWS_GET_CURRENT_PROCESS()
    duplicate = ctypes.c_void_p()
    if not _WINDOWS_DUPLICATE_HANDLE(
        process,
        handle,
        process,
        ctypes.byref(duplicate),
        _WINDOWS_GENERIC_READ | _WINDOWS_DELETE,
        False,
        0,
    ):
        error_code = getattr(ctypes, "get_last_error", lambda: 0)()
        raise RuntimeError(
            f"selected data owner handle reduction failed with Windows error {error_code}"
        )
    if duplicate.value in (None, ctypes.c_void_p(-1).value):
        raise RuntimeError("selected data owner handle reduction returned an invalid handle")
    return int(duplicate.value)


def _write_windows_handle(handle: int, chunk: bytes) -> None:
    if not callable(_WINDOWS_WRITE_FILE):
        raise RuntimeError(
            "identity-bound selected data cleanup is unavailable on this platform"
        )
    offset = 0
    while offset < len(chunk):
        remaining = chunk[offset:]
        buffer = ctypes.create_string_buffer(remaining)
        written = ctypes.c_uint32()
        if not _WINDOWS_WRITE_FILE(
            handle,
            buffer,
            len(remaining),
            ctypes.byref(written),
            None,
        ):
            error_code = getattr(ctypes, "get_last_error", lambda: 0)()
            raise OSError(error_code, "selected data materialization write failed")
        if written.value <= 0:
            raise OSError("selected data materialization write made no progress")
        offset += written.value


def _open_windows_read_handle(path: Path) -> int:
    if not callable(_WINDOWS_CREATE_FILE):
        raise RuntimeError(
            "identity-bound selected data cleanup is unavailable on this platform"
        )
    opened = _WINDOWS_CREATE_FILE(
        str(path),
        _WINDOWS_GENERIC_READ,
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_DELETE,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_ATTRIBUTE_TEMPORARY | _WINDOWS_FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )
    handle = int(opened) if opened is not None else None
    if handle in (None, ctypes.c_void_p(-1).value):
        error_code = getattr(ctypes, "get_last_error", lambda: 0)()
        raise RuntimeError(
            f"selected data verification open failed with Windows error {error_code}"
        )
    return handle


def _hash_windows_owned_file(item: "_MaterializedFile") -> str:
    if item.deletion_reference is None or not isinstance(
        item.deletion_reference.native_reference,
        int,
    ):
        raise RuntimeError("materialized selected data changed during execution")
    owner_identity = _windows_handle_identity(
        item.deletion_reference.native_reference,
        is_directory=False,
    )
    if owner_identity != item.deletion_reference.expected_identity:
        raise RuntimeError("materialized selected data changed during execution")
    handle = _open_windows_read_handle(item.physical_path)
    try:
        if _windows_handle_identity(handle, is_directory=False) != owner_identity:
            raise RuntimeError("materialized selected data changed during execution")
        if not callable(_WINDOWS_READ_FILE):
            raise RuntimeError(
                "identity-bound selected data cleanup is unavailable on this platform"
            )
        digest = hashlib.sha256()
        while True:
            buffer = ctypes.create_string_buffer(1024 * 1024)
            read = ctypes.c_uint32()
            if not _WINDOWS_READ_FILE(
                handle,
                buffer,
                len(buffer),
                ctypes.byref(read),
                None,
            ):
                error_code = getattr(ctypes, "get_last_error", lambda: 0)()
                raise OSError(error_code, "selected data verification read failed")
            if read.value == 0:
                break
            digest.update(buffer.raw[: read.value])
        return digest.hexdigest()
    finally:
        _close_windows_handle(handle)


def _delete_owned_object(reference: _OwnedObjectReference) -> None:
    if reference.backend == "windows":
        if not isinstance(reference.native_reference, int):
            raise RuntimeError("identity-bound selected data cleanup refused invalid handle")
        current_identity = _windows_handle_identity(
            reference.native_reference,
            is_directory=reference.is_directory,
        )
        if current_identity != reference.expected_identity:
            raise RuntimeError("identity-bound selected data cleanup refused stale ownership")
        if not callable(_WINDOWS_SET_FILE_INFORMATION_BY_HANDLE):
            raise RuntimeError(
                "identity-bound selected data cleanup is unavailable on this platform"
            )
        disposition = _WindowsFileDispositionInformation(DeleteFile=1)
        if not _WINDOWS_SET_FILE_INFORMATION_BY_HANDLE(
            reference.native_reference,
            _WINDOWS_FILE_DISPOSITION_INFO,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            error_code = getattr(ctypes, "get_last_error", lambda: 0)()
            raise RuntimeError(
                f"identity-bound selected data cleanup failed with Windows error {error_code}"
            )
        return
    if not _darwin_cleanup_is_available():
        raise RuntimeError(
            "identity-bound selected data cleanup is unavailable on this platform"
        )
    if not isinstance(reference.native_reference, bytes):
        raise RuntimeError("identity-bound selected data cleanup refused invalid reference")
    native_reference = _DarwinFSRef.from_buffer_copy(reference.native_reference)
    if _darwin_reference_identity(native_reference) != reference.native_identity:
        raise RuntimeError("identity-bound selected data cleanup refused stale ownership")
    status = _FS_DELETE_OBJECT(ctypes.byref(native_reference))
    if status != 0:
        raise RuntimeError(f"identity-bound selected data cleanup failed with status {status}")


@dataclass(frozen=True)
class _MaterializedFile:
    logical_path: str
    physical_path: Path
    sha256: str
    size_bytes: int
    provider_dataset_ids: tuple[str, ...]
    token_ids: tuple[str, ...]
    state: os.stat_result
    deletion_reference: _OwnedObjectReference | None
    descriptor: int | None = None


class VerifiedMarketDataMaterialization:
    def __init__(
        self,
        *,
        files: tuple[_MaterializedFile, ...],
        manifest: DatasetManifest,
        backend: str = "darwin",
        root: Path | None = None,
        parent_descriptor: int | None = None,
        root_descriptor: int | None = None,
        root_state: os.stat_result | None = None,
        root_deletion_reference: _OwnedObjectReference | None = None,
    ) -> None:
        from services.marketdata.coverage import CoverageMap, TokenCoverage
        from services.marketdata.view import MarketDataView

        self._root = root
        self._parent_descriptor = parent_descriptor
        self._root_descriptor = root_descriptor
        self._root_state = root_state
        self._root_deletion_reference = root_deletion_reference
        self._files = files
        self._backend = backend
        physical_by_logical = {item.logical_path: str(item.physical_path) for item in files}
        start_us = int(manifest.start.timestamp() * 1_000_000)
        end_us = int(manifest.end.timestamp() * 1_000_000)
        coverage = CoverageMap(
            by_token={
                token_id: TokenCoverage(
                    token_id=token_id,
                    files=tuple(
                        physical_by_logical[item.path]
                        for item in manifest.files
                        if token_id in item.token_ids
                    ),
                    dataset_ids=tuple(
                        sorted(
                            {
                                dataset_id
                                for item in manifest.files
                                if token_id in item.token_ids
                                for dataset_id in item.provider_dataset_ids
                            }
                        )
                    ),
                    start_us=start_us,
                    end_us=end_us,
                )
                for token_id in manifest.token_ids
            },
            requested=manifest.token_ids,
            window_start_us=start_us,
            window_end_us=end_us,
            dataset_ids_by_path={
                str(item.physical_path): item.provider_dataset_ids for item in files
            },
        )
        self.view = MarketDataView(
            coverage=coverage,
            start=manifest.start,
            end=manifest.end,
            logical_paths_by_file={
                str(item.physical_path): item.logical_path for item in files
            },
            integrity_checker=self._verify_state,
        )

    def _verify_state(self) -> None:
        try:
            if self._backend == "windows":
                for item in self._files:
                    if item.deletion_reference is None or not isinstance(
                        item.deletion_reference.native_reference,
                        int,
                    ):
                        raise RuntimeError(
                            "materialized selected data changed during execution"
                        )
                    owner_identity = _windows_handle_identity(
                        item.deletion_reference.native_reference,
                        is_directory=False,
                    )
                    if owner_identity != item.deletion_reference.expected_identity:
                        raise RuntimeError(
                            "materialized selected data changed during execution"
                        )
                    reader = _open_windows_read_handle(item.physical_path)
                    try:
                        path_identity = _windows_handle_identity(
                            reader,
                            is_directory=False,
                        )
                    finally:
                        _close_windows_handle(reader)
                    current = os.stat(item.physical_path, follow_symlinks=False)
                    if (
                        path_identity != owner_identity
                        or not _same_regular_file_state(item.state, current)
                    ):
                        raise RuntimeError(
                            "materialized selected data changed during execution"
                        )
                return
            if self._backend == "linux":
                for item in self._files:
                    if item.descriptor is None:
                        raise RuntimeError(
                            "materialized selected data changed during execution"
                        )
                    current = os.fstat(item.descriptor)
                    exposed = os.stat(item.physical_path)
                    exposed_matches = _same_descriptor_exposure(current, exposed)
                    if (
                        not _same_regular_file_state(item.state, current)
                        or not exposed_matches
                    ):
                        raise RuntimeError(
                            "materialized selected data changed during execution"
                        )
                return
            if self._root_descriptor is None or self._root_state is None:
                raise RuntimeError("materialized selected data changed during execution")
            root_state = os.fstat(self._root_descriptor)
            if (
                not stat.S_ISDIR(root_state.st_mode)
                or root_state.st_dev != self._root_state.st_dev
                or root_state.st_ino != self._root_state.st_ino
                or root_state.st_mode != self._root_state.st_mode
                or root_state.st_mtime_ns != self._root_state.st_mtime_ns
                or root_state.st_ctime_ns != self._root_state.st_ctime_ns
            ):
                raise RuntimeError("materialized selected data changed during execution")
            if set(os.listdir(self._root_descriptor)) != {
                item.physical_path.name for item in self._files
            }:
                raise RuntimeError("materialized selected data changed during execution")
            for item in self._files:
                current = os.stat(
                    item.physical_path.name,
                    dir_fd=self._root_descriptor,
                    follow_symlinks=False,
                )
                if not _same_regular_file_state(item.state, current):
                    raise RuntimeError("materialized selected data changed during execution")
        except OSError as exc:
            raise RuntimeError("materialized selected data changed during execution") from exc

    def verify(self) -> None:
        self._verify_state()
        for item in self._files:
            digest = (
                _hash_windows_owned_file(item)
                if self._backend == "windows"
                else _hash_stable_materialized_file(
                    item,
                    root_descriptor=self._root_descriptor,
                )
            )
            if digest != item.sha256:
                raise RuntimeError("materialized selected data changed during execution")
        self._verify_state()

    def cleanup(self) -> None:
        if self._backend == "windows":
            cleanup_error: BaseException | None = None
            close_error: BaseException | None = None
            for item in self._files:
                reference = item.deletion_reference
                if reference is None or not isinstance(reference.native_reference, int):
                    cleanup_error = cleanup_error or RuntimeError(
                        "selected data cleanup refused incomplete ownership state"
                    )
                    continue
                try:
                    _delete_owned_object(reference)
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
                try:
                    _close_windows_handle(reference.native_reference)
                except BaseException as exc:
                    close_error = close_error or exc
                if item.physical_path.exists():
                    cleanup_error = cleanup_error or RuntimeError(
                        "selected data cleanup refused replacement content"
                    )
            if cleanup_error is not None:
                if close_error is not None:
                    _add_cleanup_note(
                        cleanup_error,
                        f"selected data native handle cleanup also failed: {close_error}",
                    )
                raise cleanup_error
            if close_error is not None:
                raise close_error
            return
        if self._backend == "linux":
            cleanup_error: BaseException | None = None
            close_error: BaseException | None = None
            for item in self._files:
                if item.descriptor is None:
                    continue
                try:
                    os.close(item.descriptor)
                except BaseException as exc:
                    close_error = close_error or exc
            if cleanup_error is not None:
                if close_error is not None:
                    _add_cleanup_note(
                        cleanup_error,
                        f"selected data descriptor cleanup also failed: {close_error}",
                    )
                raise cleanup_error
            if close_error is not None:
                raise close_error
            return
        if (
            self._root is None
            or self._parent_descriptor is None
            or self._root_descriptor is None
            or self._root_state is None
            or self._root_deletion_reference is None
        ):
            raise RuntimeError("selected data cleanup refused incomplete ownership state")
        cleanup_error: BaseException | None = None
        try:
            _cleanup_materialized_files(
                root=self._root,
                parent_descriptor=self._parent_descriptor,
                root_descriptor=self._root_descriptor,
                root_state=self._root_state,
                root_deletion_reference=self._root_deletion_reference,
                files=self._files,
            )
        except BaseException as exc:
            cleanup_error = exc
        close_error: BaseException | None = None
        for descriptor in (self._root_descriptor, self._parent_descriptor):
            try:
                os.close(descriptor)
            except BaseException as exc:
                close_error = close_error or exc
        if cleanup_error is not None:
            if close_error is not None:
                _add_cleanup_note(
                    cleanup_error,
                    f"selected data descriptor cleanup also failed: {close_error}",
                )
            raise cleanup_error
        if close_error is not None:
            raise close_error


def _hash_stable_materialized_file(
    item: _MaterializedFile,
    *,
    root_descriptor: int | None,
) -> str:
    if item.descriptor is not None:
        descriptor = os.dup(item.descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
    else:
        if root_descriptor is None:
            raise RuntimeError("materialized selected data changed during execution")
        descriptor = os.open(
            item.physical_path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
    try:
        before = os.fstat(descriptor)
        if not _same_regular_file_state(item.state, before):
            raise RuntimeError("materialized selected data changed during execution")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if item.descriptor is not None:
            current = os.fstat(item.descriptor)
        else:
            current = os.stat(
                item.physical_path.name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        if not _same_regular_file_state(before, after) or not _same_regular_file_state(after, current):
            raise RuntimeError("materialized selected data changed during execution")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _capture_manifest_file(
    *,
    file_record: DatasetFile,
    physical_path: Path,
    root_descriptor: int,
) -> _MaterializedFile:
    source_path = Path(file_record.path)
    source_descriptor = os.open(
        source_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    destination_descriptor: int | None = None
    destination_identity: tuple[int, int] | None = None
    deletion_reference: _OwnedObjectReference | None = None
    completed = False
    capture_error: BaseException | None = None
    try:
        source_before = os.fstat(source_descriptor)
        if not S_ISREG(source_before.st_mode):
            raise RuntimeError(f"snapshot integrity verification failed for {source_path}")
        destination_descriptor = os.open(
            physical_path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_descriptor,
        )
        destination_before = os.fstat(destination_descriptor)
        destination_identity = (destination_before.st_dev, destination_before.st_ino)
        deletion_reference = _capture_owned_object_reference(
            physical_path,
            destination_before,
            descriptor=destination_descriptor,
            is_directory=False,
        )
        digest = hashlib.sha256()
        size_bytes = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size_bytes += len(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination_descriptor, remaining)
                if written <= 0:
                    raise OSError("selected data materialization write made no progress")
                remaining = remaining[written:]
        os.fsync(destination_descriptor)
        os.fchmod(destination_descriptor, 0o400)
        destination_state = os.fstat(destination_descriptor)
        source_after = os.fstat(source_descriptor)
        source_current = os.stat(source_path, follow_symlinks=False)
        if (
            not _same_regular_file_state(source_before, source_after)
            or not _same_regular_file_state(source_after, source_current)
            or size_bytes != file_record.size_bytes
            or digest.hexdigest() != file_record.sha256
            or destination_state.st_size != file_record.size_bytes
        ):
            raise RuntimeError(f"snapshot integrity verification failed for {source_path}")
        materialized = _MaterializedFile(
            logical_path=file_record.path,
            physical_path=physical_path,
            sha256=file_record.sha256,
            size_bytes=file_record.size_bytes,
            provider_dataset_ids=file_record.provider_dataset_ids,
            token_ids=file_record.token_ids,
            state=destination_state,
            deletion_reference=deletion_reference,
        )
        completed = True
        return materialized
    except BaseException as exc:
        capture_error = exc
        raise
    finally:
        cleanup_error: Exception | None = None
        try:
            os.close(source_descriptor)
        except Exception as exc:
            cleanup_error = exc
        if (
            not completed
            and destination_identity is not None
            and destination_descriptor is not None
            and deletion_reference is not None
        ):
            try:
                owned = os.fstat(destination_descriptor)
                if (owned.st_dev, owned.st_ino) != destination_identity:
                    raise RuntimeError("partial selected data cleanup refused changed descriptor")
                os.fchmod(destination_descriptor, 0o600)
                os.close(destination_descriptor)
                destination_descriptor = None
                _delete_owned_object(deletion_reference)
                try:
                    os.stat(physical_path, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise RuntimeError(
                        "partial selected data cleanup refused replacement content"
                    )
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if destination_descriptor is not None:
            try:
                os.close(destination_descriptor)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            if capture_error is not None:
                _add_cleanup_note(
                    capture_error,
                    f"partial selected data cleanup failed: {cleanup_error}",
                )
            else:
                raise cleanup_error


def _capture_linux_manifest_file(
    *,
    file_record: DatasetFile,
) -> _MaterializedFile:
    if (
        not isinstance(_LINUX_O_TMPFILE, int)
        or not callable(_LINUX_OPEN_TMPFILE)
        or not callable(_LINUX_OPEN_READONLY)
    ):
        raise RuntimeError(
            "identity-bound selected data cleanup is unavailable on this platform"
        )
    source_path = Path(file_record.path)
    source_descriptor = os.open(
        source_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    writer_descriptor: int | None = None
    destination_descriptor: int | None = None
    primary_error: BaseException | None = None
    try:
        source_before = os.fstat(source_descriptor)
        if not S_ISREG(source_before.st_mode):
            raise RuntimeError(f"snapshot integrity verification failed for {source_path}")
        writer_descriptor = _LINUX_OPEN_TMPFILE(
            Path(tempfile.gettempdir()),
            _LINUX_O_TMPFILE | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
            0o400,
        )
        digest = hashlib.sha256()
        size_bytes = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size_bytes += len(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(writer_descriptor, remaining)
                if written <= 0:
                    raise OSError("selected data materialization write made no progress")
                remaining = remaining[written:]
        os.fsync(writer_descriptor)
        writer_state = os.fstat(writer_descriptor)
        source_after = os.fstat(source_descriptor)
        source_current = os.stat(source_path, follow_symlinks=False)
        if (
            not _same_regular_file_state(source_before, source_after)
            or not _same_regular_file_state(source_after, source_current)
            or size_bytes != file_record.size_bytes
            or digest.hexdigest() != file_record.sha256
            or writer_state.st_size != file_record.size_bytes
        ):
            raise RuntimeError(f"snapshot integrity verification failed for {source_path}")
        destination_descriptor = _LINUX_OPEN_READONLY(
            _LINUX_FD_ROOT / str(writer_descriptor),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        destination_state = os.fstat(destination_descriptor)
        if not _same_descriptor_exposure(writer_state, destination_state):
            raise RuntimeError("materialized selected data changed during setup")
        os.close(writer_descriptor)
        writer_descriptor = None
        physical_path = _LINUX_FD_ROOT / str(destination_descriptor)
        exposed_state = os.stat(physical_path)
        if not _same_descriptor_exposure(destination_state, exposed_state):
            raise RuntimeError("materialized selected data changed during setup")
        materialized = _MaterializedFile(
            logical_path=file_record.path,
            physical_path=physical_path,
            sha256=file_record.sha256,
            size_bytes=file_record.size_bytes,
            provider_dataset_ids=file_record.provider_dataset_ids,
            token_ids=file_record.token_ids,
            state=destination_state,
            deletion_reference=None,
            descriptor=destination_descriptor,
        )
        destination_descriptor = None
        return materialized
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        for descriptor in (source_descriptor, writer_descriptor, destination_descriptor):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            if primary_error is not None:
                _add_cleanup_note(
                    primary_error,
                    f"partial selected data cleanup failed: {cleanup_error}",
                )
            else:
                raise cleanup_error


def _create_linux_materialization(
    manifest: DatasetManifest,
) -> VerifiedMarketDataMaterialization:
    captured: list[_MaterializedFile] = []
    try:
        for file_record in manifest.files:
            captured.append(
                _capture_linux_manifest_file(
                    file_record=file_record,
                )
            )
        selected = VerifiedMarketDataMaterialization(
            backend="linux",
            files=tuple(captured),
            manifest=manifest,
        )
        selected.verify()
        return selected
    except BaseException as exc:
        cleanup_error: BaseException | None = None
        for item in captured:
            if item.descriptor is None:
                continue
            try:
                os.close(item.descriptor)
            except BaseException as close_exc:
                cleanup_error = cleanup_error or close_exc
        if cleanup_error is not None:
            _add_cleanup_note(exc, f"selected data setup cleanup failed: {cleanup_error}")
        raise


def _capture_windows_manifest_file(
    *,
    file_record: DatasetFile,
    index: int,
    manifest_id: str,
) -> _MaterializedFile:
    if not all(
        callable(capability)
        for capability in (
            _WINDOWS_CREATE_FILE,
            _WINDOWS_GET_FILE_INFORMATION_BY_HANDLE,
            _WINDOWS_DUPLICATE_HANDLE,
            _WINDOWS_GET_CURRENT_PROCESS,
            _WINDOWS_WRITE_FILE,
            _WINDOWS_FLUSH_FILE_BUFFERS,
            _WINDOWS_SET_FILE_INFORMATION_BY_HANDLE,
            _WINDOWS_CLOSE_HANDLE,
        )
    ):
        raise RuntimeError(
            "identity-bound selected data cleanup is unavailable on this platform"
        )
    source_path = Path(file_record.path)
    source_descriptor = os.open(
        source_path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0),
    )
    writer_handle: int | None = None
    writer_reference: _OwnedObjectReference | None = None
    owner_handle: int | None = None
    owner_reference: _OwnedObjectReference | None = None
    primary_error: BaseException | None = None
    physical_path = Path(tempfile.gettempdir()) / (
        f"evosport-selected-{manifest_id[:12]}-{index:04d}-{secrets.token_hex(8)}.parquet"
    )
    try:
        source_before = os.fstat(source_descriptor)
        if not S_ISREG(source_before.st_mode):
            raise RuntimeError(f"snapshot integrity verification failed for {source_path}")
        created_handle = _WINDOWS_CREATE_FILE(
            str(physical_path),
            _WINDOWS_GENERIC_READ | _WINDOWS_GENERIC_WRITE | _WINDOWS_DELETE,
            _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_DELETE,
            None,
            _WINDOWS_CREATE_NEW,
            _WINDOWS_FILE_ATTRIBUTE_TEMPORARY | _WINDOWS_FILE_FLAG_SEQUENTIAL_SCAN,
            None,
        )
        writer_handle = int(created_handle) if created_handle is not None else None
        if writer_handle in (None, ctypes.c_void_p(-1).value):
            error_code = getattr(ctypes, "get_last_error", lambda: 0)()
            raise RuntimeError(
                f"selected data ownership creation failed with Windows error {error_code}"
            )
        writer_reference = _capture_windows_owned_handle(
            writer_handle,
            is_directory=False,
        )
        digest = hashlib.sha256()
        size_bytes = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size_bytes += len(chunk)
            _write_windows_handle(writer_handle, chunk)
        if not _WINDOWS_FLUSH_FILE_BUFFERS(writer_handle):
            error_code = getattr(ctypes, "get_last_error", lambda: 0)()
            raise OSError(error_code, "selected data materialization flush failed")
        source_after = os.fstat(source_descriptor)
        source_current = os.stat(source_path, follow_symlinks=False)
        writer_identity = _windows_handle_identity(writer_handle, is_directory=False)
        if (
            not _same_regular_file_state(source_before, source_after)
            or not _same_regular_file_state(source_after, source_current)
            or size_bytes != file_record.size_bytes
            or digest.hexdigest() != file_record.sha256
            or writer_identity != writer_reference.expected_identity
        ):
            raise RuntimeError(f"snapshot integrity verification failed for {source_path}")
        owner_handle = _duplicate_windows_read_delete_handle(writer_handle)
        owner_reference = _capture_windows_owned_handle(owner_handle, is_directory=False)
        if owner_reference.expected_identity != writer_reference.expected_identity:
            raise RuntimeError("selected data ownership changed while it was captured")
        _close_windows_handle(writer_handle)
        writer_handle = None
        writer_reference = None
        destination_state = os.stat(physical_path, follow_symlinks=False)
        if destination_state.st_size != file_record.size_bytes:
            raise RuntimeError(f"snapshot integrity verification failed for {source_path}")
        materialized = _MaterializedFile(
            logical_path=file_record.path,
            physical_path=physical_path,
            sha256=file_record.sha256,
            size_bytes=file_record.size_bytes,
            provider_dataset_ids=file_record.provider_dataset_ids,
            token_ids=file_record.token_ids,
            state=destination_state,
            deletion_reference=owner_reference,
            descriptor=None,
        )
        owner_handle = None
        owner_reference = None
        return materialized
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            os.close(source_descriptor)
        except BaseException as exc:
            cleanup_error = exc
        cleanup_handle = owner_handle if owner_handle is not None else writer_handle
        cleanup_reference = (
            owner_reference if owner_handle is not None else writer_reference
        )
        if cleanup_handle is not None:
            if cleanup_reference is not None:
                try:
                    _delete_owned_object(cleanup_reference)
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
            elif primary_error is not None:
                _add_cleanup_note(
                    primary_error,
                    f"unproven selected data ownership was preserved at {physical_path}",
                )
            try:
                _close_windows_handle(cleanup_handle)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if owner_handle is not None and writer_handle is not None:
            try:
                _close_windows_handle(writer_handle)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_handle is not None and physical_path.exists():
            cleanup_error = cleanup_error or RuntimeError(
                "partial selected data cleanup refused replacement content"
            )
        if cleanup_error is not None:
            if primary_error is not None:
                _add_cleanup_note(
                    primary_error,
                    f"partial selected data cleanup failed: {cleanup_error}",
                )
            else:
                raise cleanup_error


def _create_windows_materialization(
    manifest: DatasetManifest,
) -> VerifiedMarketDataMaterialization:
    captured: list[_MaterializedFile] = []
    try:
        for index, file_record in enumerate(manifest.files):
            captured.append(
                _capture_windows_manifest_file(
                    file_record=file_record,
                    index=index,
                    manifest_id=manifest.manifest_id,
                )
            )
        selected = VerifiedMarketDataMaterialization(
            backend="windows",
            files=tuple(captured),
            manifest=manifest,
        )
        selected.verify()
        return selected
    except BaseException as exc:
        cleanup_error: BaseException | None = None
        for item in captured:
            if item.deletion_reference is not None:
                try:
                    _delete_owned_object(item.deletion_reference)
                except BaseException as delete_exc:
                    cleanup_error = cleanup_error or delete_exc
                if isinstance(item.deletion_reference.native_reference, int):
                    try:
                        _close_windows_handle(
                            item.deletion_reference.native_reference
                        )
                    except BaseException as close_exc:
                        cleanup_error = cleanup_error or close_exc
            elif item.descriptor is not None:
                try:
                    os.close(item.descriptor)
                except BaseException as close_exc:
                    cleanup_error = cleanup_error or close_exc
        if cleanup_error is not None:
            _add_cleanup_note(exc, f"selected data setup cleanup failed: {cleanup_error}")
        raise


def _cleanup_materialized_files(
    *,
    root: Path,
    parent_descriptor: int,
    root_descriptor: int,
    root_state: os.stat_result,
    root_deletion_reference: _OwnedObjectReference,
    files: tuple[_MaterializedFile, ...],
) -> None:
    pinned_root = os.fstat(root_descriptor)
    if not _same_directory_identity(root_state, pinned_root):
        raise RuntimeError("selected data cleanup refused changed materialization root")
    parent_entry = os.stat(
        root.name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if not _same_directory_identity(pinned_root, parent_entry):
        raise RuntimeError("selected data cleanup refused changed materialization root")
    expected_names = {item.physical_path.name for item in files}
    if set(os.listdir(root_descriptor)) != expected_names:
        raise RuntimeError("selected data cleanup refused changed materialization tree")
    for item in files:
        current = os.stat(
            item.physical_path.name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if not _same_regular_file_identity(item.state, current):
            raise RuntimeError("selected data cleanup refused replacement content")

    parent_entry = os.stat(
        root.name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if not _same_directory_identity(pinned_root, parent_entry):
        raise RuntimeError("selected data cleanup refused changed materialization root")
    os.fchmod(root_descriptor, 0o700)
    parent_entry = os.stat(
        root.name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if not _same_directory_identity(pinned_root, parent_entry):
        raise RuntimeError("selected data cleanup refused changed materialization root")

    for item in files:
        parent_entry = os.stat(
            root.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _same_directory_identity(pinned_root, parent_entry):
            raise RuntimeError("selected data cleanup refused changed materialization root")
        descriptor = os.open(
            item.physical_path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
        try:
            owned = os.fstat(descriptor)
            if not _same_regular_file_identity(item.state, owned):
                raise RuntimeError("selected data cleanup refused replacement content")
            os.fchmod(descriptor, 0o600)
            changed = os.fstat(descriptor)
            current = os.stat(
                item.physical_path.name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if (
                not _same_regular_file_identity(owned, changed)
                or not _same_regular_file_identity(changed, current)
            ):
                raise RuntimeError("selected data cleanup refused replacement content")
            parent_entry = os.stat(
                root.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not _same_directory_identity(pinned_root, parent_entry):
                raise RuntimeError("selected data cleanup refused changed materialization root")
        finally:
            os.close(descriptor)
        _delete_owned_object(item.deletion_reference)
        try:
            os.stat(item.physical_path, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("selected data cleanup refused replacement content")

    if os.listdir(root_descriptor):
        raise RuntimeError("selected data cleanup refused changed materialization tree")
    parent_entry = os.stat(
        root.name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if not _same_directory_identity(pinned_root, parent_entry):
        raise RuntimeError("selected data cleanup refused changed materialization root")
    _delete_owned_object(root_deletion_reference)
    try:
        os.stat(root, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError("selected data cleanup refused changed materialization root")


def _add_cleanup_note(error: BaseException, note: str) -> None:
    try:
        add_note = getattr(error, "add_note", None)
    except BaseException:
        add_note = None
    if callable(add_note):
        try:
            add_note(note)
            return
        except BaseException:
            pass
    try:
        notes = getattr(error, "__notes__", None)
    except BaseException:
        return
    if isinstance(notes, list):
        try:
            notes.append(note)
        except BaseException:
            return
    else:
        try:
            setattr(error, "__notes__", [note])
        except BaseException:
            return


@contextmanager
def materialize_verified_market_data(manifest: DatasetManifest):
    backend = _resolve_cleanup_backend()
    if backend == "linux":
        selected = _create_linux_materialization(manifest)
        try:
            yield selected
        except BaseException as exc:
            try:
                selected.cleanup()
            except Exception as cleanup_exc:
                _add_cleanup_note(exc, f"selected data cleanup also failed: {cleanup_exc}")
            raise
        else:
            selected.cleanup()
        return
    if backend == "windows":
        selected = _create_windows_materialization(manifest)
        try:
            yield selected
        except BaseException as exc:
            try:
                selected.cleanup()
            except Exception as cleanup_exc:
                _add_cleanup_note(exc, f"selected data cleanup also failed: {cleanup_exc}")
            raise
        else:
            selected.cleanup()
        return
    root = Path(tempfile.mkdtemp(prefix=f"evosport-selected-{manifest.manifest_id[:12]}-"))
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_descriptor = os.open(root.parent, directory_flags)
    try:
        root_descriptor = os.open(
            root.name,
            directory_flags,
            dir_fd=parent_descriptor,
        )
    except BaseException:
        os.close(parent_descriptor)
        raise
    created_root_state = os.fstat(root_descriptor)
    try:
        root_deletion_reference = _capture_owned_object_reference(
            root,
            created_root_state,
            descriptor=root_descriptor,
            is_directory=True,
        )
    except BaseException as exc:
        os.close(root_descriptor)
        os.close(parent_descriptor)
        _add_cleanup_note(
            exc,
            f"unproven selected data ownership was preserved at {root}",
        )
        raise
    captured: list[_MaterializedFile] = []
    try:
        for index, file_record in enumerate(manifest.files):
            captured.append(
                _capture_manifest_file(
                    file_record=file_record,
                    physical_path=root / f"selected-{index:04d}.parquet",
                    root_descriptor=root_descriptor,
                )
            )
        current_root = os.fstat(root_descriptor)
        parent_entry = os.stat(
            root.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not _same_directory_identity(created_root_state, current_root)
            or not _same_directory_identity(current_root, parent_entry)
        ):
            raise RuntimeError("materialized selected data changed during setup")
        os.fchmod(root_descriptor, 0o500)
        root_state = os.fstat(root_descriptor)
        parent_entry = os.stat(
            root.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _same_directory_identity(root_state, parent_entry):
            raise RuntimeError("materialized selected data changed during setup")
        selected = VerifiedMarketDataMaterialization(
            backend="darwin",
            root=root,
            parent_descriptor=parent_descriptor,
            root_descriptor=root_descriptor,
            root_state=root_state,
            root_deletion_reference=root_deletion_reference,
            files=tuple(captured),
            manifest=manifest,
        )
        selected.verify()
    except BaseException as exc:
        try:
            _cleanup_materialized_files(
                root=root,
                parent_descriptor=parent_descriptor,
                root_descriptor=root_descriptor,
                root_state=created_root_state,
                root_deletion_reference=root_deletion_reference,
                files=tuple(captured),
            )
        except Exception as cleanup_exc:
            _add_cleanup_note(exc, f"selected data setup cleanup was refused: {cleanup_exc}")
        for descriptor in (root_descriptor, parent_descriptor):
            try:
                os.close(descriptor)
            except Exception as close_exc:
                _add_cleanup_note(
                    exc,
                    f"selected data setup descriptor cleanup also failed: {close_exc}",
                )
        raise
    try:
        yield selected
    except BaseException as exc:
        try:
            selected.cleanup()
        except Exception as cleanup_exc:
            _add_cleanup_note(exc, f"selected data cleanup also failed: {cleanup_exc}")
        raise
    else:
        selected.cleanup()


@contextmanager
def _publication_lock(output_root: Path, manifest_id: str):
    lock_path = output_root / f".{manifest_id}.lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(
            f"snapshot publication lock exists at {lock_path}; it may be busy or stale, verify it and remove it before retrying"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
            lock_file.write(f"pid={os.getpid()}\n")
        yield
    finally:
        lock_path.unlink(missing_ok=True)


async def freeze_provider_datasets(
    *,
    provider_dataset_ids: Sequence[str],
    output_root: Path,
    start: datetime,
    end: datetime,
    football: FootballDatasetBinding,
) -> DatasetManifest:
    from sqlalchemy import select

    from models.database import AsyncSessionLocal, ProviderDataset
    from services.marketdata.coverage import resolve_coverage

    selected_ids = tuple(sorted(set(str(value) for value in provider_dataset_ids if value)))
    if not selected_ids:
        raise ValueError("provider_dataset_ids cannot be empty")
    start_utc = _normalize_utc(start, "start")
    end_utc = _normalize_utc(end, "end")
    if start_utc >= end_utc:
        raise ValueError("start must be before end")

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(select(ProviderDataset).where(ProviderDataset.id.in_(selected_ids)))
        ).scalars().all()
    rows_by_id = {str(row.id): row for row in rows}
    missing = set(selected_ids) - set(rows_by_id)
    if missing:
        raise ValueError(f"unknown provider dataset IDs: {sorted(missing)}")
    for dataset_id in selected_ids:
        football.validate_provider_dataset(rows_by_id[dataset_id])

    token_ids = tuple(sorted({str(token) for row in rows for token in (row.token_ids_json or ())}))
    coverage = await resolve_coverage(
        token_ids=token_ids,
        start=start_utc,
        end=end_utc,
        dataset_ids=selected_ids,
        ensure_scan=False,
    )
    if coverage.uncovered_tokens:
        raise ValueError(f"selected datasets have uncovered tokens: {list(coverage.uncovered_tokens)}")

    identity_by_path: dict[str, tuple[set[str], set[str]]] = {}
    for token_id in token_ids:
        token_coverage = coverage.by_token[token_id]
        for path in token_coverage.files:
            dataset_set, token_set = identity_by_path.setdefault(path, (set(), set()))
            dataset_set.update(coverage.dataset_ids_by_path.get(path, ()))
            token_set.add(token_id)

    selected_directories = {_file_uri_path(str(row.storage_uri)).resolve() for row in rows}
    actual_parquet = {
        str(path.resolve())
        for directory in selected_directories
        for path in directory.glob("*.parquet")
        if path.is_file() and not path.is_symlink()
    }
    selected_paths = {str(Path(path).resolve()) for path in identity_by_path}
    if actual_parquet != selected_paths:
        raise ValueError(
            f"selected dataset directories contain missing or extra parquet files: "
            f"expected={sorted(selected_paths)} actual={sorted(actual_parquet)}"
        )

    files = tuple(
        DatasetFile(
            path=path,
            sha256=sha256_file(Path(path)),
            size_bytes=Path(path).stat().st_size,
            provider_dataset_ids=tuple(sorted(identity_by_path[path][0])),
            token_ids=tuple(sorted(identity_by_path[path][1])),
        )
        for path in sorted(identity_by_path)
    )
    manifest = DatasetManifest(
        schema_version=CATALOG_SCHEMA_VERSION,
        manifest_id=manifest_id_for(
            schema_version=CATALOG_SCHEMA_VERSION,
            source="homerun_catalog",
            token_ids=token_ids,
            start=start_utc,
            end=end_utc,
            files=files,
            provider_dataset_ids=selected_ids,
            football=football,
        ),
        source="homerun_catalog",
        token_ids=token_ids,
        start=start_utc,
        end=end_utc,
        files=files,
        provider_dataset_ids=selected_ids,
        football=football,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / manifest.manifest_id
    with _publication_lock(output_root, manifest.manifest_id):
        if target.exists():
            existing, _ = load_verified_snapshot(target / "manifest.json")
            if existing != manifest:
                raise RuntimeError(f"snapshot collision at {target}")
            return existing
        staging = Path(tempfile.mkdtemp(prefix=f".{manifest.manifest_id}.", dir=output_root))
        try:
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            os.chmod(manifest_path, 0o444)
            verified, _ = load_verified_snapshot(manifest_path)
            if verified != manifest:
                raise RuntimeError(f"snapshot collision at {staging}")
            os.rename(staging, target)
            return manifest
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def load_manifest(path: Path) -> DatasetManifest:
    return DatasetManifest.model_validate_json(path.read_text(encoding="utf-8"))
