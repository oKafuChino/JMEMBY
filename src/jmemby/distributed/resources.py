"""Small Linux memory probe; no psutil dependency or privileged operations."""
from pathlib import Path, PurePosixPath


def memory_limit_mb(proc=Path('/proc'), cgroup=Path('/sys/fs/cgroup')):
    limits = []
    try:
        for line in (proc / 'meminfo').read_text().splitlines():
            if line.startswith('MemTotal:'):
                limits.append(int(line.split()[1]) * 1024)
                break
    except (OSError, ValueError, IndexError):
        pass
    candidates = {cgroup / 'memory.max', cgroup / 'memory/memory.limit_in_bytes'}
    try:
        for line in (proc / 'self/cgroup').read_text().splitlines():
            _, controllers, path = line.split(':', 2)
            relative = PurePosixPath(path.lstrip('/'))
            if '..' in relative.parts:
                continue
            if controllers == '':
                root, name = cgroup, 'memory.max'
            elif 'memory' in controllers.split(','):
                root, name = cgroup / 'memory', 'memory.limit_in_bytes'
            else:
                continue
            directory = root.joinpath(*relative.parts)
            while True:
                candidates.add(directory / name)
                if directory == root:
                    break
                directory = directory.parent
    except (OSError, ValueError):
        pass
    for path in candidates:
        try:
            value = int(path.read_text().strip())
            if 0 < value < 2**60:  # v1's unlimited sentinel is not a real limit.
                limits.append(value)
        except (OSError, ValueError):
            pass
    return max(1, min(limits) // (1024**2)) if limits else None


def use_low_memory(mode, limit_mb):
    return mode == 'on' or mode == 'auto' and limit_mb is not None and limit_mb <= 256
