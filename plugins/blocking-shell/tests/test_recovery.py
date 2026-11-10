# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.12,<2", "tiktoken>=0.12,<1", "tree-sitter>=0.25,<0.26", "tree-sitter-bash>=0.25,<0.26"]
# ///
"""Lost replies, SIGTERM cleanup and deadlines surviving a killed MCP server."""
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
from test_repl import call


def drop_response(server):
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
        (root / 'tmp').mkdir()
        (root / 'codex').mkdir()
        env = dict(os.environ, CODEX_WORK=directory, SCRATCH_ROOT=str(root / 'tmp'),
                   CODEX_HOME=str(root / 'codex'), BASH_ENV='', CODEX_THREAD_ID='one',
                   BLOCKING_SHELL_STATE_DIR=str(root / 'state'))
        pidfile = root / 'server.pid'

        @asynccontextmanager
        async def connect(drop=False):
            if drop:
                command, args = sys.executable, [__file__, '--drop-response', str(plugin / 'server.py')]
            else:
                command = '/bin/bash'
                args = ['-c', 'echo $$ > ' + shlex.quote(str(pidfile)) + '; exec ' +
                        shlex.join([sys.executable, str(plugin / 'server.py')])]
            async with stdio_client(StdioServerParameters(command=command, args=args, env=env)) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    yield session

        async with connect(drop=True) as s:
            try:
                await call(s, 'run', dict(cmd='echo once >> count; echo complete'))
            except McpError:
                pass
            else:
                raise AssertionError('proxy did not drop the response')
        assert not list((root / 'tmp').glob('blocking-shell-*')), 'SIGTERM leaked runtime'
        async with connect() as s:
            recovered = await call(s, 'result')
            assert recovered['state'] == 'finished' and recovered['exit_code'] == 0, recovered
            assert recovered['output_tail'] == 'complete\n'
            assert (root / 'count').read_text() == 'once\n'
            task = asyncio.create_task(call(s, 'run', dict(
                cmd='echo $$ > ready; sleep 60 & echo "$!" > child; wait', timeout_seconds=2)))
            async with asyncio.timeout(5):
                while not (root / 'child').exists():
                    await asyncio.sleep(.01)
            active = await call(s, 'result')
            child = int((root / 'child').read_text())
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
            try:
                await task
            except McpError:
                pass
            else:
                raise AssertionError('killed server returned a result')
        # The timer, not the dead Python process, must stop Bash and its child.
        await asyncio.sleep(4)
        assert not Path(f'/proc/{active["pid"]}').exists(), active
        assert not Path(f'/proc/{child}').exists(), child
        async with connect() as s:
            unknown = await call(s, 'result')
            assert unknown['state'] == 'unknown' and 'exit_code' not in unknown, unknown
            task = asyncio.create_task(call(s, 'run', dict(cmd='sleep 1; echo new-shell')))
            await asyncio.sleep(.1)
            unknown = await call(s, 'result', dict(run_ref=active['run_ref']))
            assert unknown['state'] == 'unknown', unknown
            await task
        print('lost response, SIGTERM cleanup, crash deadline and unknown recovery: passed')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--drop-response':
        drop_response(sys.argv[2])
    else:
        asyncio.run(main())
