"""Read-only checks of the configured daemon, capacity and pinned images."""
import asyncio
import os
import re
import signal


async def _query(runner, *args):
    try:
        process = await asyncio.create_subprocess_exec(
            runner.docker_binary, *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=runner.environment, start_new_session=True,
        )
    except OSError as error:
        raise ValueError('runner unavailable') from error
    async def read():
        data = await process.stdout.read(1025)
        if len(data) > 1024 or await process.wait() != 0:
            raise ValueError('runner query failed')
        return data.decode('ascii').strip()
    try:
        return await asyncio.wait_for(read(), 5)
    except (TimeoutError, UnicodeError) as error:
        raise ValueError('runner query failed') from error
    finally:
        if process.returncode is None:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            await process.wait()


async def runner_readiness(runner, daemon_id, images):
    """Report provisioned capacity, not actual available resources or isolation."""
    report = {'ready': False, 'daemon': 'unavailable', 'capacity': 'unknown', 'images': 'unknown'}
    if not images or any(not isinstance(v, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', v) for v in images):
        return report
    try:
        await runner.require_daemon(daemon_id)
        report['daemon'] = 'ready'
        value = await _query(runner, 'info', '--format', '{{.NCPU}} {{.MemTotal}}')
        cpu, memory = map(int, value.split())
        report['capacity'] = 'ready' if cpu >= 2 and memory >= 2 * 1024 ** 3 else 'insufficient'
        if report['capacity'] != 'ready': return report
        for image in sorted(set(images)):
            actual = await _query(runner, 'image', 'inspect', '--format', '{{.Id}}', image)
            if actual != image:
                report['images'] = 'unavailable'
                return report
        report['images'] = 'ready'
    except (RuntimeError, ValueError, OSError):
        # Do not expose daemon, image, or subprocess output in the diagnostic.
        if report['daemon'] == 'ready' and report['capacity'] == 'ready': report['images'] = 'unavailable'
        return report
    try:
        await runner.require_daemon(daemon_id)
    except (RuntimeError, ValueError, OSError):
        report['daemon'] = 'unavailable'
        return report
    report['ready'] = True
    return report
