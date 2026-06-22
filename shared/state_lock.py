# Guards against lost-update races on JSON state files.

import fcntl, os
class FileLock:
    def __init__(self, path):
        self._lockpath = str(path) + '.lock'
        self._fh = None

    def __enter__(self):
        self._fh = open(self._lockpath, 'w')
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()