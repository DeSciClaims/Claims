"""Safe transport diagnostics that never include remote stderr or key values."""


class WorkerTransportError(RuntimeError):
    REASONS = frozenset({
        "worker_busy", "worker_processes_remain", "worker_unclean",
        "guest_stop_failed", "firewall_mismatch", "gateway_mismatch", "ssh_failed",
    })

    def __init__(self, reason: str):
        if reason not in self.REASONS:
            raise ValueError("unknown worker transport failure")
        self.reason = reason
        super().__init__(reason)
