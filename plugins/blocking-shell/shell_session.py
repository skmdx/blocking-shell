"""One noninteractive Bash and systemd cgroup per live conversation."""
import asyncio
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import tomllib
import uuid


DRIVER = r'''builtin printf '%s\0%s\0' "$BASHPID" "$PWD" >&198
while IFS= builtin read -r -d '' __blocking_shell_request <&197; do
    builtin source "$__blocking_shell_request/command.bash" >>"$__blocking_shell_request/output.log" 2>&1
    __blocking_shell_status=$?
    builtin printf '%s\0%s\0' "$__blocking_shell_status" "$PWD" >&198
done
'''
BOOTSTRAP = '''import os,socket,sys
s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1])
for fd in (197,198):os.dup2(s.fileno(),fd,inheritable=True)
s.close()
os.execv(sys.argv[2],sys.argv[2:])
'''
PROPERTIES = ('LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,'
              'ExecMainPID,CPUUsageNSec,MemoryPeak,MemoryMax,MemorySwapMax,'
              'IOReadBytes,IOWriteBytes,ControlGroup,ActiveEnterTimestampMonotonic')


def startup_environment() -> dict[str, str]:
    env = dict(os.environ)
    config = Path(env.get('CODEX_HOME', Path.home() / '.codex')) / 'config.toml'
    try:
        with config.open('rb') as stream:
            settings = tomllib.load(stream)
    except FileNotFoundError:
        settings = {}
    value = settings.get('shell_environment_policy', {}).get('set', {}).get('BASH_ENV')
    if value is not None:
        if not isinstance(value, str) or '\0' in value:
            raise ValueError('shell_environment_policy.set.BASH_ENV must be a string without NUL')
        env['BASH_ENV'] = value
    return env


async def control(environment: dict[str, str], *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        'systemctl', '--user', *args, env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await proc.communicate()
    return proc.returncode or 0, output.decode(errors='replace')


def counter(properties: dict, name: str) -> int | None:
    value = properties.get(name, '')
    if not value.isdecimal() or int(value) == 2**64 - 1:
        return None
    return int(value)


async def snapshot(environment: dict, unit: str) -> tuple[dict, str | None]:
    code, text = await control(environment, 'show', unit, '--property=' + PROPERTIES)
    return (dict(line.split('=', 1) for line in text.splitlines() if '=' in line),
            text.strip() if code else None)


def metrics(before: dict, after: dict) -> dict:
    values = {}
    for field, prop, scale in [('cpu_seconds', 'CPUUsageNSec', 1e9),
                               ('io_read_bytes', 'IOReadBytes', 1),
                               ('io_write_bytes', 'IOWriteBytes', 1)]:
        a, b = counter(before, prop), counter(after, prop)
        delta = b - a if a is not None and b is not None and b >= a else None
        values[field] = delta / scale if delta is not None and scale != 1 else delta
    return dict(values, session_memory_peak_bytes=counter(after, 'MemoryPeak'),
                pid=counter(after, 'ExecMainPID') or None)


class ShellSession:
    def __init__(self, workdir: Path):
        self.lock = asyncio.Lock()
        self.workdir = workdir
        self.login = True
        self.memory_max_mib = 8192
        self.scratch_ref: str | None = None
        self.lost: str | None = None
        self.unit: str | None = None
        self.runtime: Path | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.reader: asyncio.StreamReader | None = None
        self.listener: asyncio.Server | None = None
        self.pidfd: int | None = None
        self.dead: asyncio.Future | None = None
        self.env: dict[str, str] = {}
        self.pid: int | None = None
        self.deadline: str | None = None

    async def start(self):
        if self.lost:
            raise ValueError(f'Bash state lost ({self.lost}); call reset before run')
        if self.unit:
            if self.dead is not None and self.dead.done():
                self.lost = 'shell exited'
                raise ValueError('Bash state lost (shell exited); call reset before run')
            return
        self.env = startup_environment()
        bash = shutil.which('bash')
        if bash is None:
            raise ValueError('Bash is required')
        root = Path(os.environ.get('CODEX_WORK', Path.home() / 'codex-work')) / 'tmp'
        self.runtime = Path(tempfile.mkdtemp(prefix='blocking-shell-', dir=root))
        self.unit = 'blocking-shell-' + uuid.uuid4().hex + '.service'
        driver = self.runtime / 'driver.bash'
        driver.write_text(DRIVER)
        connection = asyncio.get_running_loop().create_future()

        def connected(reader, writer):
            if connection.done():
                writer.close()
            else:
                connection.set_result((reader, writer))

        socket_path = self.runtime / 'control.sock'
        listener = await asyncio.start_unix_server(connected, path=socket_path)
        self.listener = listener
        try:
            with (self.runtime / 'systemd.log').open('wb') as stream:
                proc = await asyncio.create_subprocess_exec(
                    'systemd-run', '--user', '--service-type=exec', '--remain-after-exit',
                    '--unit=' + self.unit, '--expand-environment=no',
                    '--working-directory=' + str(self.workdir).replace('%', '%%'),
                    '--property=MemoryAccounting=yes',
                    '--property=IOAccounting=yes', '--property=OOMPolicy=kill',
                    '--property=MemoryMax=' + str(self.memory_max_mib * 1024**2),
                    '--property=TimeoutStopSec=2', '--property=KillMode=control-group',
                    '--property=StandardInput=null',
                    '--property=StandardOutput=append:' + str(self.runtime / 'startup.log').replace('%', '%%'),
                    '--property=StandardError=inherit',
                    *('--setenv=' + key for key in self.env),
                    sys.executable, '-c', BOOTSTRAP, str(socket_path), bash,
                    *(['--login'] if self.login else []), str(driver),
                    stdin=asyncio.subprocess.DEVNULL, stdout=stream, stderr=stream, env=self.env)
                await proc.wait()
                if proc.returncode:
                    raise RuntimeError((self.runtime / 'systemd.log').read_text())
            async with asyncio.timeout(15):
                self.reader, self.writer = await connection
                self.pid = int(await self.field())
                self.workdir = Path(await self.field())
            self.pidfd = os.pidfd_open(self.pid)
            dead = asyncio.get_running_loop().create_future()
            self.dead = dead
            pidfd = self.pidfd

            def exited():
                asyncio.get_running_loop().remove_reader(pidfd)
                dead.set_result(None)

            asyncio.get_running_loop().add_reader(self.pidfd, exited)
        except (TimeoutError, asyncio.IncompleteReadError) as error:
            startup = self.runtime / 'startup.log'
            detail = startup.read_text(errors='replace')[-4000:] if startup.exists() else ''
            raise RuntimeError('Bash startup failed: ' + (detail or str(error))) from error
        finally:
            listener.close()

    async def field(self) -> str:
        assert self.reader is not None
        return (await self.reader.readuntil(b'\0'))[:-1].decode()

    async def limits(self, memory_max_mib: int | None):
        assert self.unit is not None
        props, error = await snapshot(self.env, self.unit)
        if error:
            raise RuntimeError(error)
        memory = memory_max_mib if memory_max_mib is not None else self.memory_max_mib
        if memory != self.memory_max_mib:
            code, output = await control(self.env, 'set-property', '--runtime', self.unit,
                                        f'MemoryMax={memory * 1024**2}')
            if code:
                raise RuntimeError(output)
        self.memory_max_mib = memory
        return props

    async def arm_deadline(self, seconds: int):
        assert self.unit is not None
        # An independent systemd timer still stops the cgroup if the MCP server dies.
        self.deadline = self.unit.removesuffix('.service') + '-deadline'
        systemctl = shutil.which('systemctl')
        assert systemctl is not None
        proc = await asyncio.create_subprocess_exec(
            'systemd-run', '--user', '--quiet', '--unit=' + self.deadline,
            f'--on-active={seconds + 1}s', '--timer-property=AccuracySec=1us',
            '--timer-property=RemainAfterElapse=no',
            *('--setenv=' + key for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS')
              if key in self.env),
            systemctl, '--user', 'stop', self.unit,
            env=self.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(output.decode(errors='replace'))

    async def execute(self, out: Path) -> tuple[int, str]:
        assert self.writer is not None and self.dead is not None
        async def response():
            return int(await self.field()), await self.field()

        pending = asyncio.create_task(response())
        try:
            self.writer.write(os.fsencode(out) + b'\0')
            await self.writer.drain()
            done, _ = await asyncio.wait([pending, self.dead], return_when=asyncio.FIRST_COMPLETED)
            if pending in done:
                return await pending
            raise EOFError('Bash exited')
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def idle(self) -> str | None:
        if not self.deadline:
            return None
        code, output = await control(self.env, 'stop', self.deadline + '.timer')
        await control(self.env, 'stop', self.deadline + '.service')
        self.deadline = None
        return output.strip() if code else None

    async def close(self) -> str | None:
        error = await self.idle()
        if self.unit:
            code, text = await control(self.env, 'stop', self.unit)
            if code:
                return text.strip()
            await control(self.env, 'reset-failed', self.unit)
        if self.pidfd is not None:
            asyncio.get_running_loop().remove_reader(self.pidfd)
            os.close(self.pidfd)
            self.pidfd = None
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
        if self.listener:
            await self.listener.wait_closed()
            self.listener = None
        if self.runtime:
            shutil.rmtree(self.runtime)
        self.unit = self.runtime = self.writer = self.reader = self.dead = None
        return error


def command_source(cmd: str, workdir: Path | None, scratch: Path) -> str:
    change = f'builtin cd -- {shlex.quote(str(workdir))} || return $?\n' if workdir else ''
    return change + f'builtin export SCRATCH_DIR={shlex.quote(str(scratch))}\n' + cmd + '\n'
