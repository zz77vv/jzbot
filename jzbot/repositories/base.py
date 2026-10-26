"""Repositories share one connection and one transaction lock per unit of work."""


class BaseRepository:
    def __init__(self, store):
        self.store = store

    @property
    def conn(self):
        return self.store.database.conn

    @property
    def write_lock(self):
        return self.store.database.write_lock

    def _require_conn(self):
        return self.store.database._require_conn()
