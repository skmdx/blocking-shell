"""Durable command history; live Bash owns execution state."""
from contextlib import closing
import json
import os
from pathlib import Path
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

    def command(self) -> dict:
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS last_command '
                       '(session TEXT PRIMARY KEY, arguments TEXT NOT NULL)')
            row = db.execute('SELECT arguments FROM last_command WHERE session=?',
                             (self.session,)).fetchone()
            if row is None:
                raise ValueError('No previous blocking-shell command in this conversation')
            return json.loads(row[0])

    def accept(self, arguments: dict, logs: Path, record: dict) -> Path:
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS last_command '
                       '(session TEXT PRIMARY KEY, arguments TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS executions '
                       '(position INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, '
                       'run_ref TEXT UNIQUE NOT NULL, record TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            while True:
                ref = new_id(lambda key: db.execute(
                    'SELECT 1 FROM executions WHERE run_ref=?', (key,)).fetchone() is not None)
                out = logs / ('blocking-shell-' + ref)
                try:
                    out.mkdir(mode=0o700)
                    break
                except FileExistsError:
                    continue
            try:
                db.execute('INSERT INTO last_command VALUES (?, ?) '
                           'ON CONFLICT(session) DO UPDATE SET arguments=excluded.arguments',
                           (self.session, json.dumps(arguments)))
                db.execute('INSERT INTO executions (session, run_ref, record) VALUES (?, ?, ?)',
                           (self.session, ref, json.dumps(dict(
                               record, run_ref=ref, arguments=arguments))))
                db.commit()
            except BaseException:
                out.rmdir()
                raise
            return out

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
