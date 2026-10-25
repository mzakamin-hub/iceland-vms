"""HTTPS downloads with resume and mandatory sha256 verification (runs in a worker thread).

The file is written as <name>.part and only renamed to its final name after the sha256
matches the pinned value from the catalogue. A mismatch deletes the partial file.
"""
import hashlib
import os
import threading
import urllib.request

from . import VERSION

UA = "iceland-vms/%s" % VERSION


class DownloadError(Exception):
    pass


def sha256_file(path, progress=None, cancel=None):
    h = hashlib.sha256()
    done, total = 0, os.path.getsize(path)
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise DownloadError("cancelled")
            b = f.read(4 << 20)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress("verify", done, total)
    return h.hexdigest()


class Download:
    def __init__(self, url, dest, sha256, size=None, progress=None, done=None):
        if not url.startswith("https://"):
            raise DownloadError("only https downloads are allowed")
        self.url, self.dest, self.sha256, self.size = url, dest, sha256.lower(), size
        self.progress, self.done = progress, done
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        try:
            self._fetch()
            if self.done:
                self.done(None)
        except Exception as e:   # reported to the GUI
            if self.done:
                self.done(str(e) or e.__class__.__name__)

    def _fetch(self):
        if os.path.exists(self.dest):
            if sha256_file(self.dest, self.progress, self.cancel) == self.sha256:
                return
            os.unlink(self.dest)
        part = self.dest + ".part"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if self.size and have > self.size:
            os.unlink(part)
            have = 0
        req = urllib.request.Request(self.url, headers={"User-Agent": UA})
        if have:
            req.add_header("Range", "bytes=%d-" % have)
        with urllib.request.urlopen(req, timeout=60) as r:
            if have and r.status != 206:
                have = 0
            total = self.size or (int(r.headers.get("Content-Length", 0)) + have)
            with open(part, "ab" if have else "wb") as f:
                done = have
                while True:
                    if self.cancel is not None and self.cancel.is_set():
                        raise DownloadError("cancelled")
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
                    done += len(b)
                    if self.progress:
                        self.progress("download", done, total)
        got = sha256_file(part, self.progress, self.cancel)
        if got != self.sha256:
            os.unlink(part)
            raise DownloadError("checksum mismatch: expected %s, got %s - the file was deleted" % (self.sha256, got))
        os.replace(part, self.dest)
