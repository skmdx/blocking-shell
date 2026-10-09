# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.12,<2", "tiktoken>=0.12,<1"]
# ///
"""Verify persistent reruns through real MCP and systemd commands."""
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


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plugin', type=Path, default=Path(__file__).resolve().parents[1])
    plugin = parser.parse_args().plugin.resolve()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        space = ScratchSpace(root, 'one')
        scratch = space.create()
        ref = scratch['scratch_ref']
        env = dict(os.environ, SCRATCH_ROOT=directory, CODEX_THREAD_ID='one',
                   BLOCKING_SHELL_STATE_DIR=str(root/'state'), BLOCKING_SHELL_SCRATCH_DIR='inherited-wrong')

        @asynccontextmanager
        async def connect():
            async with stdio_client(StdioServerParameters(
                    command=sys.executable, args=[str(plugin/'server.py')], env=env)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

        arguments = dict(cmd='printf "%s|%s|%s\\n" "$0" "$PWD" "$RERUN_TEST"; echo x >> count',
                         workdir=directory, scratch_ref=ref, shell='/bin/bash', login=False,
                         memory_max_mib=64, timeout_seconds=9, max_output_tokens=100)
        arguments['cmd'] += '; echo x >> "$BLOCKING_SHELL_SCRATCH_DIR/artifact"'
        async with connect() as session:
            schemas = {t.name: t for t in (await session.list_tools()).tools}
            assert not schemas['rerun'].inputSchema.get('required')
            assert schemas['rerun'].annotations is not None
            assert schemas['rerun'].annotations.destructiveHint
            await call(session, 'rerun', error=True)
            await call(session, 'set_env', {'values': {'RERUN_TEST': 'before',
                                                     'BLOCKING_SHELL_SCRATCH_DIR': 'override-wrong'}})
            first = await call(session, 'run', arguments)
            assert first['exit_code'] == 0, first
            assert (Path(scratch['path'])/'artifact').read_text() == 'x\n'
            await call(session, 'run', dict(arguments, workdir='relative'), error=True)
            await call(session, 'run', dict(arguments, memory_max_mib=0), error=True)
            await call(session, 'run', dict(arguments, scratch_ref='missing-ref'), error=True)
            await call(session, 'rerun', owner='two', error=True)
            await call(session, 'run', dict(arguments, cmd='printf foreign'), owner='two')
            await call(session, 'set_env', {'values': {'RERUN_TEST': 'after'}})

        # New process, same conversation, updated environment, original settings.
        async with connect() as session:
            repeated = await call(session, 'rerun')
            assert repeated['exit_code'] == 0, repeated
            assert repeated['output_tail'] == f'/bin/bash|{directory}|after\n', repeated
            assert repeated['result_path'] != first['result_path']
            assert (root/'count').read_text() == 'x\nx\n'
            assert (Path(scratch['path'])/'artifact').read_text() == 'x\nx\n'
            result = json.loads(Path(repeated['result_path']).read_text())
            assert result['timeout_seconds'] == 9 and result['memory_max_bytes'] == 64*1024**2
            assert result['memory_swap_max_bytes'] == 0
            assert result['accounting_error'] is None and result['cleanup_error'] is None
            assert (await call(session, 'rerun', owner='two'))['output_tail'] == 'foreign'
            assert space.delete()['deleted'] == [ref]
            await call(session, 'rerun', error=True)
            assert (root/'count').read_text() == 'x\nx\n'
            scratch = space.create()
            replacement = scratch['scratch_ref']
            assert (await call(session, 'rerun', {'scratch_ref': replacement}))['exit_code'] == 0
            assert (await call(session, 'rerun'))['exit_code'] == 0
            assert (root/'count').read_text() == 'x\nx\nx\nx\n'
            assert (Path(scratch['path'])/'artifact').read_text() == 'x\nx\n'
            failed = dict(arguments, cmd='printf failed; exit 7', scratch_ref=replacement,
                          max_output_tokens=1)
            await call(session, 'run', failed)
            repeated = await call(session, 'rerun')
            assert repeated['exit_code'] == 7 and repeated['status'] == 'failed', repeated
            assert repeated['output_tail'] == 'failed'
            subdir = Path(scratch['path'])/'sub dir'
            subdir.mkdir()
            for workdir, expected in [('$BLOCKING_SHELL_SCRATCH_DIR', scratch['path']),
                                      ('${BLOCKING_SHELL_SCRATCH_DIR}/sub dir', str(subdir))]:
                cwd_args = dict(arguments, cmd='pwd -P', scratch_ref=replacement, workdir=workdir)
                actual = await call(session, 'run', cwd_args)
                assert actual['exit_code'] == 0 and actual['output_tail'].strip() == expected, actual
            for invalid in ('$BLOCKING_SHELL_SCRATCH_DIR/missing',
                            '$BLOCKING_SHELL_SCRATCH_DIR_SUFFIX', '$HOME', '$(pwd)'):
                await call(session, 'run', dict(cwd_args, workdir=invalid), error=True)
            new_scratch = space.create()
            new_subdir = Path(new_scratch['path'])/'sub dir'
            new_subdir.mkdir()
            actual = await call(session, 'rerun', {'scratch_ref': new_scratch['scratch_ref']})
            assert actual['exit_code'] == 0 and actual['output_tail'].strip() == str(new_subdir), actual
            assert set(space.delete()['deleted']) == {replacement, new_scratch['scratch_ref']}
    print('PASS: rerun, restart, isolation, settings, current environment, invalid requests, scratch replacement, failure')


if __name__ == '__main__':
    asyncio.run(main())
