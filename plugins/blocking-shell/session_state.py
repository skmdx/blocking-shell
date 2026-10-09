"""Persistent conversation state for commands and environment hooks."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from word_ids import new_id


class SessionState:
    def __init__(self, session: str):
        if not isinstance(session, str) or not session.strip():
            raise ValueError('blocking-shell requires a stable conversation ID')
        self.session = session
        default = Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state')) / 'blocking-shell'
        root = Path(os.environ.get('BLOCKING_SHELL_STATE_DIR', default))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / 'environment.sqlite3'
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600))

    def update(self, values: dict[str, str], remove: list[str]) -> dict[str, str]:
        for name in (*values, *remove):
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                raise ValueError(f'invalid environment variable name: {name!r}')
        if any(not isinstance(value, str) or '\0' in value for value in values.values()):
            raise ValueError('environment values must be strings without NUL')
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS environment '
                       '(session TEXT, name TEXT, value TEXT NOT NULL, PRIMARY KEY(session, name))')
            db.executemany('INSERT INTO environment VALUES (?, ?, ?) '
                           'ON CONFLICT(session, name) DO UPDATE SET value=excluded.value',
                           [(self.session, name, value) for name, value in values.items()])
            db.executemany('DELETE FROM environment WHERE session=? AND name=?',
                           [(self.session, name) for name in remove])
            return dict(db.execute('SELECT name, value FROM environment WHERE session=? ORDER BY name',
                                   (self.session,)))

    def list(self) -> dict[str, str]:
        return self.update({}, [])

    @contextmanager
    def script_db(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS bash_scripts '
                       '(position INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'session TEXT NOT NULL, ref TEXT UNIQUE NOT NULL, script TEXT NOT NULL)')
            yield db

    def scripts(self, ref: str | None = None) -> list[dict[str, str]]:
        with self.script_db() as db:
            query = 'SELECT ref, script FROM bash_scripts WHERE session=?'
            parameters = [self.session]
            if ref is not None:
                query += ' AND ref=?'
                parameters.append(ref)
            rows = db.execute(query + ' ORDER BY position', parameters).fetchall()
            if ref is not None and not rows:
                raise ValueError('Unknown bashrc reference in this conversation')
            return [dict(ref=key, script=script) for key, script in rows]

    def set_script(self, script: str, ref: str | None = None) -> str:
        if '\0' in script:
            raise ValueError('Bash scripts must not contain NUL')
        with self.script_db() as db:
            db.execute('BEGIN IMMEDIATE')
            if ref is None:
                ref = new_id(lambda key: db.execute(
                    'SELECT 1 FROM bash_scripts WHERE ref=?', (key,)).fetchone() is not None)
                db.execute('INSERT INTO bash_scripts (session, ref, script) VALUES (?, ?, ?)',
                           (self.session, ref, script))
            elif not db.execute('UPDATE bash_scripts SET script=? WHERE session=? AND ref=?',
                                (script, self.session, ref)).rowcount:
                raise ValueError('Unknown bashrc reference in this conversation')
            return ref

    def delete_script(self, ref: str) -> None:
        with self.script_db() as db:
            if not db.execute('DELETE FROM bash_scripts WHERE session=? AND ref=?',
                              (self.session, ref)).rowcount:
                raise ValueError('Unknown bashrc reference in this conversation')

    def command(self, arguments: dict | None = None) -> dict:
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS last_command '
                       '(session TEXT PRIMARY KEY, arguments TEXT NOT NULL)')
            if arguments is not None:
                db.execute('INSERT INTO last_command VALUES (?, ?) '
                           'ON CONFLICT(session) DO UPDATE SET arguments=excluded.arguments',
                           (self.session, json.dumps(arguments)))
            row = db.execute('SELECT arguments FROM last_command WHERE session=?',
                             (self.session,)).fetchone()
            if row is None:
                raise ValueError('No previous blocking-shell command in this conversation')
            return json.loads(row[0])
