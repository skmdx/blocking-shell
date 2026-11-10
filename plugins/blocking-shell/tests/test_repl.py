# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.12,<2", "tiktoken>=0.12,<1", "tree-sitter>=0.25,<0.26", "tree-sitter-bash>=0.25,<0.26"]
# ///
"""Real stdio MCP, persistent Bash and systemd cgroup acceptance."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import CancelledNotification, CancelledNotificationParams, ClientNotification

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))
from scratch_space import ScratchSpace


async def call(session, name, arguments=None, owner='one', error=False):
    value = await session.call_tool(name, arguments or {}, meta={'threadId': owner})
    assert bool(value.isError) == error, value
    return value if error else json.loads(value.content[0].text)


async def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'tmp').mkdir()
        (root / 'codex').mkdir()
        startup = root / 'startup.bash'
        startup.write_text('startup_count=$(( ${startup_count:-0} + 1 ))\n')
        (root / 'codex/config.toml').write_text(
            '[shell_environment_policy.set]\nBASH_ENV=' + json.dumps(str(startup)) + '\n')
        env = dict(os.environ, CODEX_WORK=directory, SCRATCH_ROOT=str(root / 'tmp'),
                   CODEX_HOME=str(root / 'codex'), BLOCKING_SHELL_STATE_DIR=str(root / 'state'),
                   BASH_ENV='/nonexistent-ignored-by-config', CODEX_THREAD_ID='fallback')
        space = ScratchSpace(root / 'tmp', 'one')

        @asynccontextmanager
        async def connect():
            async with stdio_client(StdioServerParameters(
                    command=sys.executable, args=[str(PLUGIN / 'server.py')], env=env)) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    yield session

        async with connect() as s:
            tools = (await s.list_tools()).tools
            assert {t.name for t in tools} == {'run', 'rerun', 'result', 'reset'}
            assert next(t for t in tools if t.name == 'run').inputSchema['required'] == ['cmd']
            await call(s, 'reset', dict(login=False))
            source = root / 'setup.bash'
            source.write_text('from_source=loaded\n')
            command = ('v=41; export EXPORTED=ok; f(){ echo function; }; '
                       'shopt -s expand_aliases nullglob; alias hi="echo alias"\n'
                       'set -- first second; umask 027\n'
                       f'source {shlex.quote(str(source))}\nmkdir sub; cd sub\n'
                       'printf "%s\\n" "$startup_count"')
            a = await call(s, 'run', dict(cmd=command))
            assert a['exit_code'] == 0 and a['output_tail'] == '1\n', a
            b = await call(s, 'run', dict(cmd='v=$((v+1)); echo "$v:$EXPORTED:$from_source:$1:$2:$startup_count"; f; hi; umask; shopt -q nullglob'))
            assert b['output_tail'] == '42:ok:loaded:first:second:1\nfunction\nalias\n0027\n', b
            assert b['pid'] == a['pid'] and b['unit'] == a['unit'] and b['scratch_ref'] == a['scratch_ref']
            assert b['workdir'] == str(root / 'sub')
            assert 'memory_peak_bytes' not in b and b['session_memory_peak_bytes'] > 0
            assert b['cpu_seconds'] is not None and b['io_write_bytes'] is not None
            assert await call(s, 'result') == b
            c = await call(s, 'run', dict(cmd='false'))
            assert c['exit_code'] == 1 and 'state_lost' not in c, c
            c = await call(s, 'run', dict(cmd='echo "$v"; printf err >&2; cat <<\'EOF\'\n日本語\nEOF'))
            assert c['output_tail'] == '42\nerr日本語\n', c
            other = await call(s, 'run', dict(cmd='echo "${v-unset}"'), owner='two')
            assert other['output_tail'] == 'unset\n' and other['pid'] != a['pid']
            increment = await call(s, 'run', dict(cmd='v=$((v+1)); echo "$v"'))
            assert increment['output_tail'] == '43\n'
            again = await call(s, 'rerun')
            assert again['output_tail'] == '44\n'
            await call(s, 'run', dict(cmd='touch should-not-exist', workdir='/missing-workdir'), error=True)
            assert await call(s, 'result') == again
            again = await call(s, 'rerun', dict(run_ref=increment['run_ref']))
            assert again['output_tail'] == '45\n'
            before = again
            task = asyncio.create_task(call(s, 'run', dict(cmd='echo busy > "$SCRATCH_DIR/ready"; sleep 1; echo done')))
            ready = Path(a['log_path']).parent.parent / 'ready'
            for _ in range(200):
                if ready.exists():
                    break
                await asyncio.sleep(.01)
            assert ready.exists()
            active = await call(s, 'result')
            assert active['state'] == 'running' and 'exit_code' not in active, active
            assert space.delete([a['scratch_ref']])['skipped_active'] == [a['scratch_ref']]
            assert await call(s, 'result', dict(run_ref=before['run_ref'])) == before
            queued = asyncio.create_task(call(s, 'run', dict(cmd='echo serialized')))
            first, second = await asyncio.gather(task, queued)
            assert first['output_tail'] == 'done\n' and second['output_tail'] == 'serialized\n'
            large = await call(s, 'run', dict(cmd="python3 -c 'print(\"日本語🐍\"*10000)'", max_output_tokens=8))
            assert large['output_truncated'] and len(Path(large['log_path']).read_text()) > 10000
            saved = json.loads(Path(large['result_path']).read_text())
            assert saved['output_tokens'] <= 8 and '�' not in large['output_tail']
            timed = await call(s, 'run', dict(cmd='echo started; (sleep 3; touch escaped) & wait', timeout_seconds=1))
            assert timed['status'] == 'timeout' and timed['state_lost'], timed
            await call(s, 'run', dict(cmd='true'), error=True)
            assert await call(s, 'result') == timed
            await call(s, 'reset')
            fresh = await call(s, 'run', dict(cmd='echo "${v-unset}:$startup_count"'))
            assert fresh['output_tail'] == 'unset:1\n', fresh
            assert fresh['pid'] != a['pid']
            dead = await call(s, 'run', dict(cmd='exit 7'))
            assert dead['state_lost'] and dead['exit_code'] == 7, dead
            await call(s, 'reset')
            dead = await call(s, 'run', dict(cmd='set -e; false; echo unreachable'))
            assert dead['state_lost'] and dead['exit_code'] == 1 and not dead['output_tail'], dead
            await call(s, 'reset')
            start = await call(s, 'run', dict(cmd='sleep 60 & echo "$!"'))
            child = int(start['output_tail'])
            assert Path(f'/proc/{child}').exists()
            await call(s, 'reset')
            assert not Path(f'/proc/{child}').exists(), child
            oom = await call(s, 'run', dict(
                cmd='unit=$(basename "$(sed -n "s/^0:://p" /proc/self/cgroup)"); '
                    'systemctl --user set-property --runtime "$unit" MemorySwapMax=16M || exit; '
                    "python3 -c 'a=bytearray(256*1024*1024)'; echo unreachable",
                memory_max_mib=64))
            assert oom['state_lost'] and oom['unit_result'] == 'oom-kill', oom
            assert 'unreachable' not in oom['output_tail'], oom
            await call(s, 'reset', dict(memory_max_mib=8192))
            request_id = s._request_id
            task = asyncio.create_task(call(s, 'run', dict(cmd='sleep 60 & echo "$!" > cancel-ready; wait')))
            marker = root / 'sub/cancel-ready'
            async with asyncio.timeout(5):
                while not marker.exists():
                    await asyncio.sleep(.01)
            child = int(marker.read_text())
            await s.send_notification(ClientNotification(CancelledNotification(
                method='notifications/cancelled', params=CancelledNotificationParams(requestId=request_id))))
            try:
                await task
            except McpError as error:
                assert 'cancel' in str(error).lower(), error
            else:
                raise AssertionError('cancelled request unexpectedly succeeded')
            # Protocol cancellation acknowledges before shielded cleanup finishes.
            await call(s, 'run', dict(cmd='true'), error=True)
            cancelled = await call(s, 'result')
            assert cancelled['status'] == 'cancelled' and cancelled['state_lost'], cancelled
            assert not Path(f'/proc/{child}').exists()
            await call(s, 'reset')
            retained = await call(s, 'run', dict(cmd='echo restart-history'))
            assert not (root / 'sub/escaped').exists()
            print('persistence, isolation, serialization, output, timeout, exit, reset: passed', flush=True)

        assert not list((root / 'tmp').glob('blocking-shell-*')), 'runtime directories leaked'
        async with connect() as s:
            assert await call(s, 'result') == retained
            repeat = await call(s, 'rerun')
            assert repeat['output_tail'] == 'restart-history\n'
            space.delete([repeat['scratch_ref']])
            assert (await call(s, 'result'))['state'] == 'expired'
            new = await call(s, 'run', dict(cmd='echo new-storage'))
            assert new['scratch_ref'] != repeat['scratch_ref']
        print('restart recovery, rerun, scratch expiry and shutdown: passed', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
