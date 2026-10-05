"""Readable opaque references. Canonical source: tools/word-ids."""
import secrets
import sqlite3
from collections.abc import Callable

WORDS = ('ash bay bee birch bird blue boat brook calm cave clay cloud coast coral '
         'dawn deer dew dove dusk elm fern field finch fir fish flint fog fox frog '
         'frost glen gold grass green grove gull hill lake leaf lime maple mist moon '
         'moss oak owl palm peak pine plum pond rain reed ridge river rock rose sage '
         'sand sea sky snow star stone sun swan teal tide tree vale wave west wind wood').split()


def new_id(is_used: Callable[[str], bool] = lambda _: False) -> str:
    for _ in range(100):
        candidate = '-'.join(secrets.choice(WORDS) for _ in range(3))
        if not is_used(candidate):
            return candidate
    raise RuntimeError('could not allocate a unique word ID')


def valid_id(value: str) -> bool:
    parts = value.split('-')
    return len(parts) == 3 and all(part in WORDS for part in parts)


def store_reference(db: sqlite3.Connection, table: str, payload: str) -> str:
    """Keep stable references without overwriting on allocation races or collisions."""
    if table not in {'refs', 'reads', 'reports'}:
        raise ValueError('unknown reference table')
    with db:
        row = db.execute(f'SELECT id FROM {table} WHERE payload=?', (payload,)).fetchone()
        if row is not None:
            return row[0]
        for _ in range(100):
            key = new_id()
            cursor = db.execute(f'INSERT OR IGNORE INTO {table} (id, payload) VALUES (?, ?)', (key, payload))
            if cursor.rowcount:
                return key
            row = db.execute(f'SELECT id FROM {table} WHERE payload=?', (payload,)).fetchone()
            if row is not None:
                return row[0]
    raise RuntimeError('could not allocate a unique word ID')
