"""Shared scratch references and cross-process leases. Canonical source: tools/scratch."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import uuid


WORDS = ('ash bay bee birch bird blue boat brook calm cave clay cloud coast coral '
         'dawn deer dew dove dusk elm fern field finch fir fish flint fog fox frog '
         'frost glen gold grass green grove gull hill lake leaf lime maple mist moon '
         'moss oak owl palm peak pine plum pond rain reed ridge river rock rose sage '
         'sand sea sky snow star stone sun swan teal tide tree vale wave west wind wood').split()


class ScratchSpace:
    def __init__(self, root: Path | None = None, session: str | None = None):
        default = Path(os.environ.get('CODEX_WORK', str(Path.home() / 'codex-work'))) / 'tmp'
        self.root = Path(root or os.environ.get('SCRATCH_ROOT', default)).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('scratch root must be an existing directory')
        self.session = session or os.environ.get('CODEX_THREAD_ID') or uuid.uuid4().hex
        self.state = self.root / '.scratch'
        self.state.mkdir(mode=0o700, exist_ok=True)
        if self.state.is_symlink():
            raise ValueError('scratch state must not be a symlink')

    @contextmanager
    def _registry(self):
        fd = os.open(self.state / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            file = self.state / 'registry.json'
            records = json.loads(file.read_text()) if file.exists() else {}
            yield records
        finally:
            os.close(fd)

    def _save(self, records: dict):
        pending = self.state / 'registry.new'
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(records, stream)
        pending.replace(self.state / 'registry.json')

    def _path(self, record: dict) -> Path:
        name = record['name']
        if not isinstance(name, str) or Path(name).name != name or name in ('.', '..'):
            raise ValueError('invalid scratch directory record')
        path = self.root / name
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != tuple(record['identity']):
            raise ValueError('scratch directory was replaced; refusing access')
        return path

    def create(self) -> dict:
        with self._registry() as records:
            for _ in range(100):
                path = self.root / '-'.join(secrets.choice(WORDS) for _ in range(3))
                if path.name in records:
                    continue
                try:
                    path.mkdir(mode=0o700)
                    break
                except FileExistsError:
                    continue
            else:
                raise RuntimeError('could not allocate a unique scratch name')
            ref = path.name
            try:
                (self.state / f'{ref}.lock').touch(mode=0o600)
                info = path.stat()
                records[ref] = dict(name=path.name, session=self.session,
                                    identity=[info.st_dev, info.st_ino])
                self._save(records)
            except BaseException:
                shutil.rmtree(path)
                (self.state / f'{ref}.lock').unlink(missing_ok=True)
                raise
        return dict(scratch_ref=ref, path=str(path))

    @contextmanager
    def lease(self, scratch_ref: str):
        """Pin the directory until all writes and descendant cleanup have finished."""
        if not isinstance(scratch_ref, str) or not scratch_ref:
            raise ValueError('scratch_ref must be an ID returned by scratch.create')
        with self._registry() as records:
            if scratch_ref not in records or records[scratch_ref].get('deleted'):
                raise ValueError(f'unknown scratch reference: {scratch_ref}')
            path = self._path(records[scratch_ref])
            fd = os.open(self.state / f'{scratch_ref}.lock', os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BaseException:
                os.close(fd)
                raise
        try:
            yield path
        finally:
            os.close(fd)

    @contextmanager
    def lease_path(self, path: str | Path):
        """Pin the managed directory containing an existing result or argument file."""
        if not Path(path).is_absolute():
            raise ValueError('scratch file path must be absolute')
        resolved = Path(path).resolve(strict=True)
        with self._registry() as records:
            refs = [ref for ref, record in records.items()
                    if not record.get('deleted') and self.root / record['name'] in (resolved, *resolved.parents)]
        if len(refs) != 1:
            raise ValueError('path must be inside a directory created by scratch.create')
        with self.lease(refs[0]) as directory:
            # Resolve again after acquiring the lease: deletion/replacement may race lookup.
            if not resolved.is_relative_to(directory) or Path(path).resolve(strict=True) != resolved:
                raise ValueError('scratch path changed during lookup')
            yield resolved

    def delete(self, refs: list[str] | None = None) -> dict:
        result: dict = dict(deleted=[], missing=[], skipped_active=[], errors={})
        with self._registry() as records:
            selected = list(dict.fromkeys(refs)) if refs is not None else [
                ref for ref, record in records.items() if record['session'] == self.session and not record.get('deleted')]
            for ref in selected:
                record = records.get(ref)
                if record is None or record.get('deleted'):
                    result['missing'].append(ref)
                    continue
                if record['session'] != self.session:
                    result['errors'][ref] = 'reference belongs to another session'
                    continue
                fd = None
                try:
                    path = self._path(record)
                    fd = os.open(self.state / f'{ref}.lock', os.O_RDONLY | os.O_NOFOLLOW)
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    shutil.rmtree(path)
                except BlockingIOError:
                    result['skipped_active'].append(ref)
                    continue
                except FileNotFoundError:
                    # Missing lock files do not authorize deleting an unprotected directory.
                    if (self.root / record['name']).exists():
                        result['errors'][ref] = 'scratch lease file is missing'
                        continue
                    result['missing'].append(ref)
                except (OSError, ValueError) as error:
                    result['errors'][ref] = str(error)
                    continue
                else:
                    result['deleted'].append(ref)
                finally:
                    if fd is not None:
                        os.close(fd)
                records[ref] = dict(session=self.session, deleted=True)
                (self.state / f'{ref}.lock').unlink(missing_ok=True)
            self._save(records)
        return result
