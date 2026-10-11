# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.12,<2", "tiktoken>=0.12,<1", "tree-sitter>=0.25,<0.26", "tree-sitter-bash>=0.25,<0.26"]
# ///
"""Exercise suggestions through real MCP, SQLite, and systemd execution."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scratch_space import ScratchSpace
from test_environment import call
from test_patterns import python_command


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plugin', type=Path, default=Path(__file__).resolve().parents[1])
    plugin = parser.parse_args().plugin.resolve()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        space = ScratchSpace(root, 'one')
        scratch = space.create()
        env = dict(os.environ, SCRATCH_ROOT=directory, CODEX_THREAD_ID='one',
                   BLOCKING_SHELL_STATE_DIR=str(root/'state'))

        @asynccontextmanager
        async def connect():
            async with stdio_client(StdioServerParameters(
                    command=sys.executable, args=[str(plugin/'server.py')], env=env)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

        arguments = dict(cmd=python_command(), workdir=directory, scratch_ref=scratch['scratch_ref'],
                         shell='/bin/bash', login=False, memory_max_mib=64, max_output_tokens=30)
        async with connect() as session:
            first = await call(session, 'run', arguments)
            assert first['exit_code'] == 0 and 'suggestion' not in first, first
            await call(session, 'run', dict(arguments, workdir='relative'), error=True)
            second = await call(session, 'run', dict(arguments, cmd=python_command(1)))
            assert 'suggestion' not in second, second

        async with connect() as session:
            third = await call(session, 'rerun')
            assert third['exit_code'] == 0 and 'suggestion' in third, third
            assert await call(session, 'result') == third
            saved = json.loads(Path(third['result_path']).read_text())
            assert saved['suggestion'] == third['suggestion']
            assert saved['memory_swap_max_bytes'] == 0
            assert saved['accounting_error'] is None and saved['cleanup_error'] is None
            assert saved['io_read_bytes'] is not None and saved['io_write_bytes'] is not None
            fourth = await call(session, 'rerun')
            assert 'suggestion' not in fourth, fourth
            isolated = await call(session, 'run', arguments, owner='two')
            assert 'suggestion' not in isolated, isolated
            # Concurrent accepted calls can cross the threshold only once.
            results = await asyncio.gather(*(call(session, 'run', arguments, owner='two') for _ in range(2)))
            assert sum('suggestion' in result for result in results) == 1, results
        assert not space.delete().get('skipped_active')
    print('MCP suggestions: third call, restart, rerun, recovery, isolation and concurrency passed')


if __name__ == '__main__':
    asyncio.run(main())
