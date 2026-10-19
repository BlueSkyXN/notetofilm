"""User-facing errors with stable machine-readable codes."""


class NTFError(RuntimeError):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}
