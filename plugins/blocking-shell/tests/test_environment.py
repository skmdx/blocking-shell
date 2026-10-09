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
        configured_env = root/'configured env'
        configured_env.write_text("env_probe() { printf 'configured\\n'; }\n")
        codex_home = root/'codex'
        codex_home.mkdir()
        codex_config = codex_home/'config.toml'
        config = json.loads((plugin/'.mcp.json').read_text())['mcpServers']['blocking-shell']
        assert 'BASH_ENV' in config['env_vars']
        space = ScratchSpace(root, 'one')
        ref = space.create()['scratch_ref']
        env = dict(os.environ, BLOCKING_SHELL_STATE_DIR=str(root/'data'), PLUGIN_ROOT=str(plugin),
                   SCRATCH_ROOT=directory, CODEX_THREAD_ID='fallback', ENV_TEST='inherited',
                   BASH_ENV=str(bash_env), CODEX_HOME=str(codex_home))

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
            assert set(t.name for t in (await session.list_tools()).tools) == {
                'run', 'rerun', 'result', 'set_env', 'unset_env', 'list_env',
                'set_bashrc', 'get_bashrc', 'list_bashrc', 'delete_bashrc'}
            assert await call(session, 'list_bashrc') == {'refs': []}
            for login in (False, True):
                request = dict(cmd='env_probe', workdir=directory, scratch_ref=ref,
                               shell='/bin/bash', login=login, memory_max_mib=64)
                inherited = await call(session, 'run', request)
                assert inherited['exit_code'] == 0 and inherited['output_tail'] == 'inherited\n', inherited
                codex_config.write_text('[shell_environment_policy.set]\nBASH_ENV = ' +
                                        json.dumps(str(configured_env)) + '\n')
                configured = await call(session, 'rerun')
                assert configured['exit_code'] == 0 and configured['output_tail'] == 'configured\n', configured
                await call(session, 'set_env', {'values': {'BASH_ENV': str(override_env)}})
                overridden = await call(session, 'rerun')
                assert overridden['exit_code'] == 0 and overridden['output_tail'] == 'override\n', overridden
                await call(session, 'unset_env', {'names': ['BASH_ENV']})
                configured = await call(session, 'rerun')
                assert configured['exit_code'] == 0 and configured['output_tail'] == 'configured\n', configured
                codex_config.unlink()
                restored = await call(session, 'rerun')
                assert restored['exit_code'] == 0 and restored['output_tail'] == 'inherited\n', restored
            script = "env_probe\nshopt -s expand_aliases\nalias session_probe='printf session'\n"
            first = (await call(session, 'set_bashrc', {'script': script}))['ref']
            second_script = "session_probe\nprintf 'second\\n'\n"
            second = (await call(session, 'set_bashrc', {'script': second_script}))['ref']
            assert first != second
            assert await call(session, 'list_bashrc') == {'refs': [first, second]}
            batch = await call(session, 'get_bashrc', {'refs': [second, 'missing', first]})
            assert batch == {'items': [dict(ref=second, script=second_script, offset=0),
                                      dict(ref='missing', error='Unknown bashrc reference in this conversation'),
                                      dict(ref=first, script=script, offset=0)], 'next_cursor': None}
            await call(session, 'set_bashrc', {'script': '\0', 'ref': first}, error=True)
            await call(session, 'set_bashrc', {'script': ':', 'ref': 'missing'}, error=True)
            assert await call(session, 'list_bashrc', owner='two') == {'refs': []}
            foreign_read = await call(session, 'get_bashrc', {'refs': [first]}, owner='two')
            assert 'error' in foreign_read['items'][0]
            for tool, arguments in (('set_bashrc', {'ref': first, 'script': ':'}),
                                    ('delete_bashrc', {'ref': first})):
                await call(session, tool, arguments, owner='two', error=True)
            request = dict(cmd='session_probe', workdir=directory, scratch_ref=ref,
                           shell='/bin/bash', memory_max_mib=64)
            for login in (False, True):
                result = await call(session, 'run', dict(request, login=login))
                assert result['exit_code'] == 0 and result['output_tail'] == 'inherited\nsessionsecond\nsession', result
            foreign = await call(session, 'run', dict(request, cmd='env_probe', login=False), owner='two')
            assert foreign['output_tail'] == 'inherited\n', foreign
            await call(session, 'run', dict(request, shell='/bin/sh'), error=True)
            await call(session, 'set_bashrc', {'ref': first, 'script': "session_probe() { printf changed; }\n"})
            assert await call(session, 'list_bashrc') == {'refs': [first, second]}
            changed = await call(session, 'rerun')
            assert changed['exit_code'] == 0 and changed['output_tail'] == 'changedsecond\nchanged', changed
            await call(session, 'set_bashrc', {'ref': first, 'script': 'return 7\n'})
            failed = await call(session, 'rerun')
            assert failed['exit_code'] == 7 and 'second' not in failed['output_tail'], failed
            assert await call(session, 'delete_bashrc', {'ref': second}) == {'deleted': second}
            assert 'error' in (await call(session, 'get_bashrc', {'refs': [second]}))['items'][0]
            await call(session, 'delete_bashrc', {'ref': second}, error=True)
            await call(session, 'set_bashrc', {'ref': first, 'script': ':\n'})
            assert await call(session, 'set_env', {'values': values}) == {'set': sorted(values)}
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
            assert await call(session, 'list_bashrc') == {'refs': [first]}
            assert await call(session, 'get_bashrc', {'refs': [first]}) == {
                'items': [dict(ref=first, script=':\n', offset=0)], 'next_cursor': None}
            large = '日本語😀\n' * 7000
            await call(session, 'set_bashrc', {'ref': first, 'script': large})
            selection = {'refs': [first, 'absent', first]}
            page = await call(session, 'get_bashrc', selection)
            stale = page['next_cursor']
            pieces = []
            while True:
                pieces.extend(page['items'])
                if page['next_cursor'] is None:
                    break
                page = await call(session, 'get_bashrc', dict(selection, cursor=page['next_cursor']))
            assert ''.join(item.get('script', '') for item in pieces) == large * 2
            assert sum('error' in item for item in pieces) == 1
            await call(session, 'set_bashrc', {'ref': first, 'script': ':\n'})
            await call(session, 'get_bashrc', dict(selection, cursor=stale), error=True)
            await call(session, 'get_bashrc', {'refs': []}, error=True)
            await call(session, 'get_bashrc', {'refs': [first], 'cursor': 'invalid'}, error=True)
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
            assert json.loads(hook.stdout.splitlines()[1]) == sorted(current)
            assert first in hook.stdout and second not in hook.stdout
            assert await call(session, 'delete_bashrc', {'ref': first}) == {'deleted': first}
            assert await call(session, 'list_bashrc') == {'refs': []}
            remaining = await call(session, 'unset_env', {'names': [*current, 'ABSENT']})
            assert remaining == {'removed': sorted(current), 'missing': ['ABSENT']}
            actual = json.loads(await run(session))
            assert actual['ENV_TEST'] == 'inherited' and actual['ENV_EMPTY'] is None, actual
            assert (await call(session, 'list_env', owner='two'))['variables'] == {'ENV_TEST': 'foreign'}
        assert space.delete()['deleted'] == [ref]
    print('PASS: Bash source registration, ordered execution, edit/delete by ref, aliases, source failure, isolation, restart, hook; BASH_ENV and environment regression')


if __name__ == '__main__':
    asyncio.run(main())
