#!/usr/bin/env python3
import errno
import fcntl
import os
import sys
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq


if not sys.platform.startswith("linux"):
    raise SystemExit("native Linux verification requires Linux")
if not isinstance(getattr(os, "O_TMPFILE", None), int):
    raise SystemExit("native Linux verification requires O_TMPFILE")
if not os.path.isdir("/proc/self/fd"):
    raise SystemExit("native Linux verification requires /proc/self/fd")

sink = pa.BufferOutputStream()
pq.write_table(pa.table({"token_id": ["YES", "YES"], "price": [0.41, 0.42]}), sink)
payload = sink.getvalue().to_pybytes()
writer = os.open(
    tempfile.gettempdir(),
    os.O_TMPFILE | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
    0o400,
)
reader = None
try:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(writer, remaining)
        if written <= 0:
            raise RuntimeError("anonymous materialization write made no progress")
        remaining = remaining[written:]
    os.fsync(writer)
    writer_state = os.fstat(writer)
    reader = os.open(
        f"/proc/self/fd/{writer}",
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    reader_state = os.fstat(reader)
    assert (writer_state.st_dev, writer_state.st_ino, writer_state.st_size) == (
        reader_state.st_dev,
        reader_state.st_ino,
        reader_state.st_size,
    )
    os.close(writer)
    writer = -1
    assert fcntl.fcntl(reader, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
    try:
        os.write(reader, b"mutation")
    except OSError as exc:
        assert exc.errno == errno.EBADF
    else:
        raise AssertionError("retained descriptor remained writable")
    exposed = f"/proc/self/fd/{reader}"
    first = pq.read_table(exposed)
    second = pq.read_table(exposed)
    assert first.equals(second)
    assert first.num_rows == 2
    print("backend=O_TMPFILE")
    print("writer_closed_before_exposure=true")
    print("retained_access=O_RDONLY")
    print("retained_write_errno=EBADF")
    print("pyarrow_reads=2 rows=2")
    print("named_materialization=false")
finally:
    if writer >= 0:
        os.close(writer)
    if reader is not None:
        exposed = f"/proc/self/fd/{reader}"
        os.close(reader)
        assert not os.path.exists(exposed)

print("cleanup=descriptor_closed path_absent=true")
print("LINUX_O_TMPFILE_PROTOCOL_OK")
