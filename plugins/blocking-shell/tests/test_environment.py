# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.12,<2", "tiktoken>=0.12,<1"]
# ///
"""Exercise session environment tools, real commands, restart and hook output."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scratch_space import ScratchSpace


async def call(session, name, arguments=None, owner: str | int = 'one', error=False):
    result = await session.call_tool(name, arguments or {}, meta={'threadId': owner})
    assert bool(result.isError) == error, result
    return result if error else json.loads(result.content[0].text)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plugin', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    plugin = args.plugin.resolve()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        bash_env = root/'bash env'
        bash_env.write_text("env_probe() { printf 'inherited\\n'; }\n")
        override_env = root/'override env'
        override_env.write_text("env_probe() { printf 'override\\n'; }\n")
        config = json.loads((plugin/'.mcp.json').read_text())['mcpServers']['blocking-shell']
        assert 'BASH_ENV' in config['env_vars']
        space = ScratchSpace(root, 'one')
        ref = space.create()['scratch_ref']
        env = dict(os.environ, BLOCKING_SHELL_STATE_DIR=str(root/'data'), PLUGIN_ROOT=str(plugin),
                   SCRATCH_ROOT=directory, CODEX_THREAD_ID='fallback', ENV_TEST='inherited',
                   BASH_ENV=str(bash_env))

        @asynccontextmanager
        async def connect():
            async with stdio_client(StdioServerParameters(
                    command=sys.executable, args=[str(plugin/'server.py')], env=env)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

        async def run(session, owner='one', cmd=None):
            command = cmd or "python3 -c 'import os,json; print(json.dumps({k:os.environ.get(k) for k in [\"ENV_TEST\",\"ENV_EMPTY\",\"XDG_RUNTIME_DIR\"]}))'"
            r = await call(session, 'run', dict(cmd=command, workdir=directory,
                           scratch_ref=ref, login=False, memory_max_mib=64), owner)
            assert r['status'] == 'completed' and r['exit_code'] == 0, r
            assert 'cleanup_error' not in r and 'accounting_error' not in r, r
            return r['output_tail']

        values = {'ENV_TEST': 'literal $HOME %u "quotes" \\ slash\n日本語',
                  'ENV_EMPTY': '', 'XDG_RUNTIME_DIR': '/job-only-runtime'}
        async with connect() as session:
            assert set(t.name for t in (await session.list_tools()).tools) == {'run', 'rerun', 'set_env', 'unset_env', 'list_env'}
            for login in (False, True):
                request = dict(cmd='env_probe', workdir=directory, scratch_ref=ref,
                               shell='/bin/bash', login=login, memory_max_mib=64)
                inherited = await call(session, 'run', request)
                assert inherited['exit_code'] == 0 and inherited['output_tail'] == 'inherited\n', inherited
                await call(session, 'set_env', {'values': {'BASH_ENV': str(override_env)}})
                overridden = await call(session, 'rerun')
                assert overridden['exit_code'] == 0 and overridden['output_tail'] == 'override\n', overridden
                await call(session, 'unset_env', {'names': ['BASH_ENV']})
                restored = await call(session, 'rerun')
                assert restored['exit_code'] == 0 and restored['output_tail'] == 'inherited\n', restored
            assert (await call(session, 'set_env', {'values': values}))['variables'] == values
            assert json.loads(await run(session)) == values
            await call(session, 'set_env', {'values': {'ENV_TEST': 'foreign'}}, 'two')
            assert json.loads(await run(session, 'two'))['ENV_TEST'] == 'foreign'
            assert (await call(session, 'list_env'))['variables'] == values
            assert await run(session, cmd='ENV_TEST=once python3 -c \'import os; print(os.environ["ENV_TEST"])\'') == 'once\n'
            for invalid in ({'BAD=NAME': 'x'}, {'OK': 'new', 'BAD-NAME': 'x'}, {'ENV_TEST': '\0'}):
                await call(session, 'set_env', {'values': invalid}, error=True)
            assert (await call(session, 'list_env'))['variables'] == values
            for owner in ('', ' ', 1):
                await call(session, 'list_env', owner=owner, error=True)
            assert (await session.call_tool('list_env')).isError is False
            assert (root/'data/environment.sqlite3').stat().st_mode & 0o777 == 0o600

        # New MCP process and a different runtime session retain conversation ownership.
        async with connect() as session:
            assert json.loads(await run(session)) == values
            await asyncio.gather(call(session, 'set_env', {'values': {'A': 'a'}}),
                                 call(session, 'set_env', {'values': {'B': 'b'}}))
            current = (await call(session, 'list_env'))['variables']
            assert current == dict(values, A='a', B='b')
            config = json.loads((plugin/'hooks/hooks.json').read_text())['hooks']['SessionStart'][0]
            assert config['matcher'] == '^compact$'
            event = dict(hook_event_name='SessionStart', source='compact', session_id='one')
            hook = subprocess.run(config['hooks'][0]['command'], shell=True, env=env,
                                  input=json.dumps(event), text=True, capture_output=True, check=True)
            assert json.loads(hook.stdout.splitlines()[1]) == current
            remaining = await call(session, 'unset_env', {'names': [*current, 'ABSENT']})
            assert remaining == {'variables': {}}
            actual = json.loads(await run(session))
            assert actual['ENV_TEST'] == 'inherited' and actual['ENV_EMPTY'] is None, actual
            assert (await call(session, 'list_env', owner='two'))['variables'] == {'ENV_TEST': 'foreign'}
        assert space.delete()['deleted'] == [ref]
    print('PASS: BASH_ENV inheritance/override/rerun in both login modes, real commands, literal values, session isolation, atomic updates, deletion, restart, hook')


if __name__ == '__main__':
    asyncio.run(main())
