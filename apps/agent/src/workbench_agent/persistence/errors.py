"""Safe, driver-independent storage failures; never include SQL or parameters."""


class StorageError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class StaleExecution(StorageError):
    def __init__(self):
        super().__init__('STALE_EXECUTION')
