"""Lifecycle timing and control semantics shared by adapters."""
WAIT_INPUT_MS=24*3600*1000
WAIT_APPROVAL_MS=30*60*1000
WALL_MS=48*3600*1000

def active_elapsed(run,now,lease_until=None):
    since=run['active_since']
    end=min(now,lease_until) if lease_until is not None else now
    return run['active_elapsed_ms']+max(0,end-since) if since is not None else run['active_elapsed_ms']
