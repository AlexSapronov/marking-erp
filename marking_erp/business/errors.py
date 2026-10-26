class RemoteError(ValueError):
    """Sanitized error; request URLs and credentials are never included."""
    def __init__(self,message,*,status=None,retry_after=None):
        super().__init__(message);self.status=status;self.retry_after=retry_after
