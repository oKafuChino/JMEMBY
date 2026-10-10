class JobError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, delay: float = 0):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.delay = delay


class Cancelled(JobError):
    def __init__(self):
        super().__init__("CANCELLED", "任务已取消；已发布的文件保留")


class DiskLow(JobError):
    def __init__(self):
        super().__init__("DISK_LOW", "剩余磁盘空间不足，任务已暂停")
