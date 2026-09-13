class SmuError(RuntimeError):
    """Base class for SMU communication errors."""


class SmuTimeout(SmuError):
    """The mailbox never reported a done status before the deadline."""

    def __init__(self, queue: int, msg_id: int):
        self.queue = queue
        self.msg_id = msg_id
        super().__init__(f"queue {queue} msg 0x{msg_id:02X} timed out - SMU wedged, cold cycle")


class SmuRejected(SmuError):
    """The SMU answered with a non-OK status."""

    def __init__(self, queue: int, msg_id: int, status: int):
        self.queue = queue
        self.msg_id = msg_id
        self.status = status
        super().__init__(f"queue {queue} msg 0x{msg_id:02X} rejected (status 0x{status:02X})")
