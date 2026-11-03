class VoiceError(ValueError):
    def __init__(self, message: str, code: str = "invalid", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
