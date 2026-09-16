"""Server-side filesystem browsing for the web UI.

The browser never uploads anything - it just lets you walk the disks of the
machine running this app and hand a path to the torrent builder.
"""

from __future__ import annotations

import os
import string
from dataclasses import asdict, dataclass

__all__ = ["list_drives", "list_dir", "path_info", "home_dir", "human_size"]


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float


def human_size(num: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    num = float(num)
    while num >= 1024 and idx < len(units) - 1:
        num /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(num)} {units[idx]}"
    return f"{num:.2f} {units[idx]}"


def home_dir() -> str:
    return os.path.abspath(os.path.expanduser("~"))


def list_drives() -> list[dict]:
    """Windows drive letters, or '/' on POSIX."""
    drives = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                total = free = 0
                try:
                    usage = os.statvfs(root)  # type: ignore[attr-defined]
                    total = usage.f_blocks * usage.f_frsize
                    free = usage.f_bavail * usage.f_frsize
                except (AttributeError, OSError):
                    try:
                        import shutil

                        usage2 = shutil.disk_usage(root)
                        total, free = usage2.total, usage2.free
                    except OSError:
                        pass
                drives.append(
                    {
                        "name": root,
                        "path": root,
                        "total": total,
                        "free": free,
                        "label": f"{root}  ({human_size(free)} free)" if total else root,
                    }
                )
    else:
        drives.append({"name": "/", "path": "/", "total": 0, "free": 0, "label": "/"})
    return drives


def _safe_stat(path: str):
    try:
        return os.stat(path)
    except OSError:
        return None


def list_dir(path: str, show_hidden: bool = False) -> dict:
    """Return the contents of ``path`` split into folders and files."""
    if not path:
        path = home_dir()
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
    if not os.path.isdir(path):
        raise NotADirectoryError(f"not a folder: {path}")

    dirs: list[dict] = []
    files: list[dict] = []
    try:
        with os.scandir(path) as scanner:
            for item in scanner:
                name = item.name
                if not show_hidden and name.startswith("."):
                    continue
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not show_hidden and os.name == "nt":
                    try:
                        if item.stat(follow_symlinks=False).st_file_attributes & 0x2:
                            continue
                    except (OSError, AttributeError):
                        pass
                stat = _safe_stat(item.path) if not is_dir else None
                entry = Entry(
                    name=name,
                    path=item.path,
                    is_dir=is_dir,
                    size=stat.st_size if stat else 0,
                    mtime=stat.st_mtime if stat else 0.0,
                )
                (dirs if is_dir else files).append(asdict(entry))
    except PermissionError as exc:
        raise PermissionError(f"access denied: {path}") from exc

    dirs.sort(key=lambda e: e["name"].lower())
    files.sort(key=lambda e: e["name"].lower())

    parent = os.path.dirname(path.rstrip("\\/"))
    if os.name == "nt" and len(path.rstrip("\\/")) <= 2:
        parent = ""  # already at a drive root
    if parent == path:
        parent = ""

    return {
        "path": path,
        "parent": parent,
        "dirs": dirs,
        "files": files,
        "crumbs": breadcrumbs(path),
    }


def breadcrumbs(path: str) -> list[dict]:
    path = os.path.abspath(path)
    parts: list[dict] = []
    drive, rest = os.path.splitdrive(path)
    if drive:
        parts.append({"name": drive + os.sep, "path": drive + os.sep})
    else:
        parts.append({"name": "/", "path": "/"})
    current = parts[0]["path"]
    for chunk in rest.replace("\\", "/").split("/"):
        if not chunk:
            continue
        current = os.path.join(current, chunk)
        parts.append({"name": chunk, "path": current})
    return parts


def path_info(path: str, include_hidden: bool = False) -> dict:
    """Quick size/count preview for a chosen file or folder."""
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.path.isfile(path):
        size = os.path.getsize(path)
        return {
            "path": path,
            "name": os.path.basename(path),
            "is_dir": False,
            "file_count": 1,
            "total_size": size,
            "human_size": human_size(size),
        }

    total = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        if not include_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for filename in filenames:
            if not include_hidden and filename.startswith("."):
                continue
            full = os.path.join(dirpath, filename)
            if os.path.islink(full):
                continue
            stat = _safe_stat(full)
            if stat:
                total += stat.st_size
                count += 1
    return {
        "path": path,
        "name": os.path.basename(path.rstrip("\\/")) or path,
        "is_dir": True,
        "file_count": count,
        "total_size": total,
        "human_size": human_size(total),
    }
