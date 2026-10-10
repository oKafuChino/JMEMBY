"""Repair only managed service directories, without following symlinks."""
import os
from pathlib import Path
import stat

from configure import ENV, ROOT, parse


def repair(root, uid, gid):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Not a regular directory: {root}")
    paths = [root]
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"Refusing symlink: {path}")
            paths.append(path)
    # Validate the full inventory before changing any owner.
    for path in paths:
        if not (path.is_dir() or path.is_file()):
            raise ValueError(f"Unsupported file: {path}")
    for path in paths:
        mode = stat.S_IMODE(path.stat().st_mode)
        os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(path, mode | (0o700 if path.is_dir() else 0o600))


def main():
    if os.name != "posix" or os.geteuid() != 0:
        raise SystemExit("Run on Linux through sudo bash repair.sh")
    values = parse(ENV.read_text(encoding="utf-8"))
    uid, gid = int(values.get("APP_UID", "1000")), int(values.get("APP_GID", "1000"))
    if uid <= 0 or gid <= 0:
        raise ValueError("APP_UID/APP_GID must be positive, non-root IDs")
    data = ROOT / "data"
    if data.is_symlink():
        raise ValueError("Refusing symlink data directory")
    for name in ("app", "komga"):
        path = data / name
        if not path.exists():
            path.mkdir(parents=True)
        repair(path, uid, gid)
    print("已修复 data/app 和 data/komga 所有者及所有者读写权限；未修改漫画文件。")


if __name__ == "__main__":
    main()
