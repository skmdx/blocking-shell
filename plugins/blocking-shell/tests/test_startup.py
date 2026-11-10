# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.12,<2", "tiktoken>=0.12,<1", "tree-sitter>=0.25,<0.26", "tree-sitter-bash>=0.25,<0.26"]
# ///
"""Start real MCP/Bash without precreated workspace or scratch directories."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_repl import call

PLUGIN = Path(__file__).resolve().parents[1]


async def check(root: Path, configured_work: bool, separate_scratch: bool, existing_work: bool):
    home = root / 'h'
    home.mkdir()
    work = root / 'w' if configured_work else home / 'codex-work'
    scratch = root / 's' if separate_scratch else work / 'tmp'
    if existing_work:
        work.mkdir(parents=True)
    env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / '.codex'), BASH_ENV='',
               BLOCKING_SHELL_STATE_DIR=str(root / 'state'), CODEX_THREAD_ID='startup-test')
    env.pop('CODEX_WORK', None)
    env.pop('SCRATCH_ROOT', None)
    if configured_work:
        env['CODEX_WORK'] = str(work)
    if separate_scratch:
        env['SCRATCH_ROOT'] = str(scratch)
    async with stdio_client(StdioServerParameters(
            command=sys.executable, args=[str(PLUGIN / 'server.py')], env=env,
            cwd=str(PLUGIN))) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            assert {tool.name for tool in (await session.list_tools()).tools} == {
                'run', 'rerun', 'result', 'reset'}
            assert work.is_dir() and scratch.is_dir()
            await call(session, 'reset', dict(login=False))
            first = await call(session, 'run', dict(cmd='n=1; echo "$PWD"'))
            assert first['exit_code'] == 0 and first['output_tail'] == str(work) + '\n', first
            assert Path(first['log_path']).is_relative_to(scratch), first
            # Private driver storage must use the selected root too.
            assert len(list(scratch.glob('blocking-shell-*/driver.bash'))) == 1
            if separate_scratch:
                assert not (work / 'tmp').exists()
            second = await call(session, 'run', dict(cmd='n=$((n+1)); echo "$n"'))
            assert second['output_tail'] == '2\n' and second['pid'] == first['pid'], second
            await call(session, 'run', dict(cmd='true', workdir=str(root / 'absent')), error=True)
            assert await call(session, 'result') == second
    assert not list(scratch.glob('blocking-shell-*')), 'runtime storage leaked'


async def main():
    for configured, separate, existing in [
        (False, False, False), (True, False, False),
        (True, False, True), (True, True, False), (True, True, True),
    ]:
        with tempfile.TemporaryDirectory(prefix='bs-') as directory:
            await check(Path(directory), configured, separate, existing)
        print(f'startup: configured_work={configured}, separate_scratch={separate}, '
              f'existing_work={existing}: passed', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
