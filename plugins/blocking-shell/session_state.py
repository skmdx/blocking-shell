"""Persistent conversation state for commands and environment hooks."""
from contextlib import closing, contextmanager
import hashlib
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

    def update(self, values: dict[str, str], remove: list[str]) -> dict:
        for name in (*values, *remove):
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                raise ValueError(f'invalid environment variable name: {name!r}')
        if any(not isinstance(value, str) or '\0' in value for value in values.values()):
            raise ValueError('environment values must be strings without NUL')
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS environment '
                       '(session TEXT, name TEXT, value TEXT NOT NULL, PRIMARY KEY(session, name))')
            db.execute('BEGIN IMMEDIATE')
            existing = {row[0] for row in db.execute(
                'SELECT name FROM environment WHERE session=?', (self.session,))}
            db.executemany('INSERT INTO environment VALUES (?, ?, ?) '
                           'ON CONFLICT(session, name) DO UPDATE SET value=excluded.value',
                           [(self.session, name, value) for name, value in values.items()])
            db.executemany('DELETE FROM environment WHERE session=? AND name=?',
                           [(self.session, name) for name in remove])
            if values:
                return {'set': sorted(values)}
            return {'removed': sorted(set(remove) & existing),
                    'missing': sorted(set(remove) - existing)}

    def list(self) -> dict[str, str]:
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS environment '
                       '(session TEXT, name TEXT, value TEXT NOT NULL, PRIMARY KEY(session, name))')
            return dict(db.execute('SELECT name, value FROM environment WHERE session=? ORDER BY name',
                                   (self.session,)))

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

    def script_page(self, refs: list[str], cursor: str | None) -> dict:
        with self.script_db() as db:
            rows = dict(db.execute(
                'SELECT ref, script FROM bash_scripts WHERE session=? AND ref IN (' +
                ','.join('?' for _ in refs) + ')', [self.session, *refs]))
        items = [dict(ref=ref, script=rows[ref]) if ref in rows else
                 dict(ref=ref, error='Unknown bashrc reference in this conversation') for ref in refs]
        digest = hashlib.sha256(json.dumps(items, ensure_ascii=False).encode()).hexdigest()
        index, offset = 0, 0
        if cursor is not None:
            try:
                previous, index_text, offset_text = cursor.split(':')
                index, offset = int(index_text), int(offset_text)
            except (ValueError, AttributeError):
                raise ValueError('Invalid bashrc cursor') from None
            if previous != digest:
                raise ValueError('Bashrc selection changed; restart without cursor')
            if not 0 <= index < len(items) or not 0 <= offset <= len(items[index].get('script', '')):
                raise ValueError('Invalid bashrc cursor')
        page, remaining = [], 16000
        while index < len(items):
            item = items[index]
            if 'error' in item:
                page.append(item)
            else:
                text = item['script'][offset:offset + remaining]
                page.append(dict(ref=item['ref'], script=text, offset=offset))
                remaining -= len(text)
                offset += len(text)
                if offset < len(item['script']):
                    break
            index += 1
            offset = 0
            if remaining == 0:
                break
        return {'items': page, 'next_cursor': f'{digest}:{index}:{offset}' if index < len(items) else None}

    def command(self, arguments: dict | None = None, execution: dict | None = None) -> dict:
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS last_command '
                       '(session TEXT PRIMARY KEY, arguments TEXT NOT NULL)')
            if arguments is not None:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO last_command VALUES (?, ?) '
                           'ON CONFLICT(session) DO UPDATE SET arguments=excluded.arguments',
                           (self.session, json.dumps(arguments)))
                if execution is not None:
                    db.execute('CREATE TABLE IF NOT EXISTS executions '
                               '(position INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, '
                               'run_ref TEXT UNIQUE NOT NULL, record TEXT NOT NULL)')
                    db.execute('INSERT INTO executions (session, run_ref, record) VALUES (?, ?, ?)',
                               (self.session, execution['run_ref'], json.dumps(execution)))
            row = db.execute('SELECT arguments FROM last_command WHERE session=?',
                             (self.session,)).fetchone()
            if row is None:
                raise ValueError('No previous blocking-shell command in this conversation')
            return json.loads(row[0])

    def suggestion(self, command: str, workdir: str, shell: str) -> str | None:
        from command_patterns import HISTORY_LIMIT, Pattern, SUGGESTION, patterns

        if Path(shell).name not in ('bash', 'sh'):
            return None
        candidates = patterns(command)
        if not candidates:
            return None
        scope = json.dumps([workdir, shell])
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS command_patterns ('
                       'id INTEGER PRIMARY KEY, session TEXT NOT NULL, scope TEXT NOT NULL, '
                       'tokens TEXT NOT NULL, keys TEXT NOT NULL, occurrences INTEGER NOT NULL, '
                       'notified INTEGER NOT NULL, seen INTEGER NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS command_patterns_session ON command_patterns(session)')
            db.execute('BEGIN IMMEDIATE')
            seen = db.execute('SELECT COALESCE(MAX(seen), 0) + 1 FROM command_patterns WHERE session=?',
                              (self.session,)).fetchone()[0]
            rows = db.execute('SELECT id, tokens, keys, occurrences, notified FROM command_patterns '
                              'WHERE session=? AND scope=? ORDER BY seen DESC, id DESC',
                              (self.session, scope)).fetchall()
            stored = [(row[0], Pattern(tuple(json.loads(row[1])), tuple(json.loads(row[2]))), row[3], row[4])
                      for row in rows]
            touched = set()
            notify = False
            for candidate in candidates:
                match = next((row for row in stored if row[1].matches(candidate)), None)
                if match is not None:
                    ident, _, count, notified = match
                    if ident in touched:
                        continue
                    emit = count + 1 >= 3 and not notified
                    notify |= emit
                    db.execute('UPDATE command_patterns SET occurrences=?, notified=?, seen=? WHERE id=?',
                               (count + 1, bool(notified or emit), seen, ident))
                else:
                    cursor = db.execute('INSERT INTO command_patterns '
                                        '(session, scope, tokens, keys, occurrences, notified, seen) '
                                        'VALUES (?, ?, ?, ?, 1, 0, ?)',
                                        (self.session, scope, json.dumps(candidate.tokens),
                                         json.dumps(candidate.keys), seen))
                    ident = cursor.lastrowid
                    stored.append((ident, candidate, 1, False))
                touched.add(ident)
            db.execute('DELETE FROM command_patterns WHERE session=? AND id NOT IN '
                       '(SELECT id FROM command_patterns WHERE session=? ORDER BY seen DESC, id DESC LIMIT ?)',
                       (self.session, self.session, HISTORY_LIMIT))
            return SUGGESTION if notify else None

    def execution(self, run_ref: str | None) -> dict:
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS executions '
                       '(position INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, '
                       'run_ref TEXT UNIQUE NOT NULL, record TEXT NOT NULL)')
            query = 'SELECT record FROM executions WHERE session=?'
            parameters = [self.session]
            if run_ref is not None:
                query += ' AND run_ref=?'
                parameters.append(run_ref)
            row = db.execute(query + ' ORDER BY position DESC LIMIT 1', parameters).fetchone()
            if row is None:
                raise ValueError('No matching execution in this conversation')
            return json.loads(row[0])
