"""Durable artifact writes and byte-compatible legacy JSON serialization."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def json_bytes(value, *, compact=False, sort_keys=False):
    """Match jq's UTF-8 output for persisted application objects."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=sort_keys,
                       indent=None if compact else 2,
                       separators=(",", ":") if compact else (",", ": "),
                       allow_nan=False) + "\n").encode("utf-8")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".review-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value, *, sort_keys=False):
    atomic_bytes(path, json_bytes(value, sort_keys=sort_keys))


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def hash_file(path):
    return hash_bytes(Path(path).read_bytes())


def pointer(root, name, target):
    root = Path(root)
    temporary = root / (name + ".tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, root / name)
    sync_directory(root)
