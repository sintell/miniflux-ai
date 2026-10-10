"""Local transactional digest state. No network filesystem or database server required."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def normalize_record(record):
    value = dict(record)
    value.pop('record_id', None)
    entry_id = value.get('entry_id') or value.get('id')
    if entry_id is not None:
        from services.editorial import positive_id
        entry_id = positive_id(entry_id)
        if entry_id is None:
            raise ValueError('Invalid entry ID')
        value['entry_id'] = int(entry_id)
    value['summary'] = value.get('summary', value.get('content', ''))
    value.pop('content', None)
    if not isinstance(value['summary'], str) or not value['summary'].strip():
        raise ValueError('Record has no summary')
    for key in ('title', 'url', 'category', 'feed_title', 'datetime'):
        value.setdefault(key, '')
    identity = 'entry:' + entry_id if entry_id else 'legacy:' + hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    value['record_id'] = identity
    return value


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL, processed_by TEXT
                );
                CREATE TABLE IF NOT EXISTS digests (
                    id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, edition TEXT NOT NULL,
                    title TEXT NOT NULL, html TEXT NOT NULL, payload TEXT NOT NULL,
                    refresh_pending INTEGER NOT NULL DEFAULT 1, refresh_error TEXT
                );
                CREATE TABLE IF NOT EXISTS imports (name TEXT PRIMARY KEY, imported_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS records_queue ON records(processed_by);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def lock(self, name='generation', blocking=False):
        # flock is shared across processes and separate open file descriptions in threads.
        # Lock a stable sidecar, never the database or an atomically replaced file.
        path = self.path.parent / (name + '.lock')
        with path.open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def entry_lock(self, entry_id):
        # Fixed lock stripes avoid one file per article.
        stripe = int(hashlib.sha256(str(entry_id).encode()).hexdigest(), 16) % 32
        return self.lock(f'entry-{stripe}', blocking=True)

    def get_record(self, entry_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM records WHERE id=?', (f'entry:{entry_id}',)).fetchone()
        return json.loads(row['payload']) if row else None

    def enqueue(self, record):
        record = normalize_record(record)
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO records(id,payload,created_at) VALUES (?,?,?)',
                       (record['record_id'], json.dumps(record, ensure_ascii=False), timestamp()))
        return record

    def queued(self, limit=100):
        with self.connect() as db:
            rows = db.execute('SELECT payload FROM records WHERE processed_by IS NULL ORDER BY rowid LIMIT ?',
                              (limit,)).fetchall()
        return [json.loads(row['payload']) for row in rows]

    def publish(self, digest, record_ids):
        if not record_ids:
            raise ValueError('Empty editions are not published')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for record_id in record_ids:
                row = db.execute('SELECT processed_by FROM records WHERE id=?', (record_id,)).fetchone()
                if row is None or row['processed_by'] is not None:
                    raise ValueError('Batch already processed or missing')
            db.execute('INSERT INTO digests(id,generated_at,edition,title,html,payload) VALUES (?,?,?,?,?,?)',
                       (digest['id'], digest['generated_at'], digest['edition'], digest['title'],
                        digest['html'], json.dumps(digest, ensure_ascii=False)))
            db.executemany('UPDATE records SET processed_by=? WHERE id=?',
                           [(digest['id'], record_id) for record_id in record_ids])

    def digests(self):
        # Retain all editions by default: refresh acceptance does not prove ingestion.
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM digests ORDER BY julianday(generated_at) DESC,id')]

    def pending_refresh(self):
        with self.connect() as db:
            return [row['id'] for row in db.execute('SELECT id FROM digests WHERE refresh_pending=1')]

    def refreshed(self, ids, error=None):
        with self.connect() as db:
            db.executemany('UPDATE digests SET refresh_pending=?,refresh_error=? WHERE id=?',
                           [(1 if error else 0, error, identity) for identity in ids])

    def import_legacy(self, entries_path=None, digest_path=None):
        """Explicit import: validate first, import once per file content, never modify originals."""
        imports = []
        for kind, file_path in (('entries', entries_path), ('digest', digest_path)):
            if file_path is None:
                continue
            path = Path(file_path)
            raw = path.read_bytes()
            value = json.loads(raw)
            key = kind + ':' + hashlib.sha256(raw).hexdigest()
            if kind == 'entries':
                if not isinstance(value, list):
                    raise ValueError('Legacy entries must be a JSON list')
                value = [normalize_record(record) for record in value]
            elif not isinstance(value, str):
                raise ValueError('Legacy digest must be a JSON string')
            imports.append((kind, key, value, path.stat().st_mtime))
        with self.lock(blocking=True), self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for kind, key, value, mtime in imports:
                if db.execute('SELECT 1 FROM imports WHERE name=?', (key,)).fetchone():
                    continue
                if kind == 'entries':
                    for record in value:
                        db.execute('INSERT OR IGNORE INTO records(id,payload,created_at) VALUES (?,?,?)',
                                   (record['record_id'], json.dumps(record, ensure_ascii=False), timestamp()))
                elif value.strip():
                    # Legacy generation time is unknown; preserve file mtime as an explicit approximation.
                    import html
                    digest_id = 'legacy-' + key.split(':')[1]
                    generated_at = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
                    db.execute('INSERT OR IGNORE INTO digests(id,generated_at,edition,title,html,payload) VALUES (?,?,?,?,?,?)',
                               (digest_id, generated_at, 'legacy', 'Imported news briefing (legacy)',
                                '<p>Imported legacy digest; timestamp is the saved file modification time.</p><pre>'
                                + html.escape(value) + '</pre>', json.dumps({'legacy_text': value})))
                db.execute('INSERT INTO imports VALUES (?,?)', (key, timestamp()))


def main():
    parser = argparse.ArgumentParser(description='Import backed-up legacy JSON into SQLite; originals are preserved.')
    parser.add_argument('--database', required=True)
    parser.add_argument('--entries')
    parser.add_argument('--digest')
    args = parser.parse_args()
    if not args.entries and not args.digest:
        parser.error('Supply --entries and/or --digest')
    State(args.database).import_legacy(args.entries, args.digest)


if __name__ == '__main__':
    main()
