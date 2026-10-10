"""Persistent queue counters stay correct across upgrades and failures."""
import sqlite3
import time

import pytest

import track_agent as ta


def assert_size(queue, count, size):
    assert queue.ceka() == count
    assert queue.db.execute(
        'SELECT queue_count.frames, queue_size.bytes FROM queue_count '
        'JOIN queue_size ON queue_size.id = queue_count.id WHERE queue_count.id = 1'
    ).fetchone() == (count, size)


def test_upgrade_legacy_queue_and_reopen(tmp_path):
    path = tmp_path / 'queue.db'
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE frames (id INTEGER PRIMARY KEY AUTOINCREMENT,
                received REAL NOT NULL, frame TEXT NOT NULL);
            CREATE TABLE queue_size (id INTEGER PRIMARY KEY, bytes INTEGER NOT NULL);
            INSERT INTO queue_size VALUES (1, 0);
            CREATE TRIGGER frame_added AFTER INSERT ON frames BEGIN
                UPDATE queue_size SET bytes = bytes + LENGTH(NEW.frame) WHERE id = 1;
            END;
            CREATE TRIGGER frame_removed AFTER DELETE ON frames BEGIN
                UPDATE queue_size SET bytes = bytes - LENGTH(OLD.frame) WHERE id = 1;
            END;
        ''')
        db.execute('INSERT INTO frames(received, frame) VALUES (?, ?)', (time.time(), 'old'))
    with ta.FrameQueue(path) as queue:
        assert_size(queue, 1, 3)
        assert queue.dalsi(1) == ['old']
        assert queue.pridej(['new']) == 0
        queue.potvrd()
        assert_size(queue, 1, 3)
    with ta.FrameQueue(path) as queue:
        assert_size(queue, 1, 3)
        assert queue.dalsi(1) == ['new']
        queue.potvrd()
        queue.potvrd()
        assert_size(queue, 0, 0)


def test_counter_expiry_capacity_and_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, 'PRELIV_MAX_BAJTU', 6)
    with ta.FrameQueue(tmp_path / 'queue.db') as queue:
        assert queue.pridej(['old'], received=[time.time() - 3 * 86400]) == 0
        assert queue.pridej(['new']) == 0
        assert queue.pridej(['full']) == 1
        assert_size(queue, 2, 6)
        assert queue.dalsi(50) == ['new']
        assert_size(queue, 1, 3)
        # First INSERT succeeds, second fails: both counters must roll back.
        assert queue.pridej(['ok', 'x'], received=[time.time(), None]) == 2
        assert_size(queue, 1, 3)
        with pytest.raises(RuntimeError), queue.db:
            queue.db.execute('DELETE FROM frames')
            raise RuntimeError('interrupted acknowledgement')
        assert_size(queue, 1, 3)
        queue.potvrd()
        assert_size(queue, 0, 0)


def test_status_does_not_read_frame_table(tmp_path):
    with ta.FrameQueue(tmp_path / 'queue.db') as queue:
        queue.pridej(['frame'] * 1000)
        def authorize(action, table, *_):
            if action == sqlite3.SQLITE_READ and table == 'frames':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        queue.db.set_authorizer(authorize)
        assert queue.ceka() == 1000
