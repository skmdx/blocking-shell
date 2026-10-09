# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.12,<2", "tiktoken>=0.12,<1"]
# ///
"""Recover real executions after response loss, server death, and concurrent runs."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scratch_space import ScratchSpace
from test_environment import call


def drop_response(server):
    """Forward real MCP traffic except the completed run response."""
    with subprocess.Popen([sys.executable, server], stdin=sys.stdin, stdout=subprocess.PIPE) as child:
        try:
            assert child.stdout is not None
            for line in child.stdout:
                message = json.loads(line)
                content = message.get('result', {}).get('content', [])
                if content and content[0].get('type') == 'text':
                    try:
                        result = json.loads(content[0]['text'])
                    except json.JSONDecodeError:
                        result = {}
                    if 'run_ref' in result:
                        return
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        finally:
            child.terminate()
            child.wait(timeout=5)


async def main():
    plugin = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        space = ScratchSpace(root, 'one')
        scratch = space.create()
        env = dict(os.environ, SCRATCH_ROOT=directory, CODEX_THREAD_ID='one',
                   BLOCKING_SHELL_STATE_DIR=str(root/'state'))
        pidfile = root/'server.pid'

        @asynccontextmanager
        async def connect(drop=False):
            if drop:
                command, args = sys.executable, [__file__, '--drop-response', str(plugin/'server.py')]
            else:
                command = '/bin/bash'
                args = ['-c', 'echo $$ > ' + shlex.quote(str(pidfile)) + '; exec ' +
                        shlex.join([sys.executable, str(plugin/'server.py')])]
            async with stdio_client(StdioServerParameters(command=command, args=args, env=env)) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    yield session

        async def run(session, cmd):
            return await call(session, 'run', dict(cmd=cmd, workdir=directory,
                              scratch_ref=scratch['scratch_ref'], login=False,
                              memory_max_mib=64, timeout_seconds=15))

        async def ready(name):
            async with asyncio.timeout(10):
                while not (root/name).exists():
                    await asyncio.sleep(0.02)

        async with connect(drop=True) as session:
            try:
                await run(session, 'echo once >> count; echo complete')
            except McpError:
                pass
            else:
                raise AssertionError('proxy failed to drop the response')
        async with connect() as session:
            recovered = await call(session, 'result')
            assert recovered['state'] == 'finished' and recovered['exit_code'] == 0, recovered
            assert recovered['output_tail'] == 'complete\n'
            assert (root/'count').read_text() == 'once\n'
            first = asyncio.create_task(run(session, 'touch first; while [ ! -f release ]; do sleep .05; done'))
            await ready('first')
            active = await call(session, 'result')
            assert active['state'] == 'running', active
            assert space.delete()['skipped_active'] == [scratch['scratch_ref']]
            second = await run(session, 'echo second')
            assert await call(session, 'result') == second
            (root/'release').touch()
            ended = await first
            assert await call(session, 'result', {'run_ref': active['run_ref']}) == ended
            assert await call(session, 'result') == second
            await call(session, 'result', {'run_ref': active['run_ref']}, owner='two', error=True)

        # Kill the MCP process while its independent systemd service is running.
        unit = None
        try:
            async with connect() as session:
                task = asyncio.create_task(run(session, 'touch crash; while [ ! -f crash-release ]; do sleep .05; done'))
                await ready('crash')
                crashed = await call(session, 'result')
                unit = 'blocking-shell-' + crashed['run_ref'] + '.service'
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
                try:
                    await task
                except McpError:
                    pass
                else:
                    raise AssertionError('server death unexpectedly returned a result')
            async with connect() as session:
                active = await call(session, 'result')
                assert active == crashed and active['state'] == 'running', active
                # The job finishes, but the dead server cannot save its result.
                (root/'crash-release').touch()
                async with asyncio.timeout(10):
                    while True:
                        check = await asyncio.create_subprocess_exec(
                            'systemctl', '--user', 'show', unit, '--property=SubState', '--value',
                            stdout=asyncio.subprocess.PIPE)
                        output, _ = await check.communicate()
                        if output.strip() == b'exited':
                            break
                        await asyncio.sleep(.02)
                unknown = await call(session, 'result')
                assert unknown['state'] == 'unknown' and 'exit_code' not in unknown, unknown
                proc = await asyncio.create_subprocess_exec('systemctl', '--user', 'stop', unit)
                assert await proc.wait() == 0
                unknown = await call(session, 'result')
                assert unknown['state'] == 'unknown' and 'exit_code' not in unknown, unknown
        finally:
            if unit:
                subprocess.run(['systemctl', '--user', 'stop', unit], check=False, capture_output=True)
                subprocess.run(['systemctl', '--user', 'reset-failed', unit], check=False, capture_output=True)
        assert space.delete()['deleted'] == [scratch['scratch_ref']]
        async with connect() as session:
            assert await call(session, 'result') == dict(run_ref=crashed['run_ref'], state='expired')
    print('PASS: lost response, restart, concurrent acceptance order, running, server death, unknown, expired')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--drop-response':
        drop_response(sys.argv[2])
    else:
        asyncio.run(main())
