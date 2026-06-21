from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from vibeframe.library import ImageLibrary, _is_image

log = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, library: ImageLibrary, on_change: Callable[[], None] | None = None) -> None:
        self.library = library
        self.on_change = on_change

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("on_change callback raised")

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        p = Path(event.src_path)
        if _is_image(p):
            self.library.add_path(p)
            self._notify()

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self.library.remove_path(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self.library.remove_path(Path(event.src_path))
        dest = Path(event.dest_path)
        if _is_image(dest):
            self.library.add_path(dest)
            self._notify()


class LibraryWatcher:
    """Watchdog observer plus a periodic-rescan fallback (NFS often misses inotify).

    on_change fires after additions/moves so the thumbnail warmer can be kicked
    without waiting for the next periodic rescan.
    """

    def __init__(
        self,
        library: ImageLibrary,
        rescan_seconds: int = 300,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.library = library
        self.rescan_seconds = rescan_seconds
        self.on_change = on_change
        self._observer = Observer()
        self._stop = threading.Event()
        self._rescan_thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.library.root.is_dir():
            log.warning("watch root %s does not exist; skipping watcher", self.library.root)
            return
        self._observer.schedule(
            _Handler(self.library, on_change=self.on_change),
            str(self.library.root),
            recursive=self.library.recursive,
        )
        self._observer.start()
        self._rescan_thread = threading.Thread(target=self._rescan_loop, daemon=True)
        self._rescan_thread.start()
        log.info(
            "watcher started on %s (recursive=%s, rescan=%ss)",
            self.library.root,
            self.library.recursive,
            self.rescan_seconds,
        )

    def _rescan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.library.scan()
                if self.on_change:
                    try:
                        self.on_change()
                    except Exception:
                        log.exception("on_change callback raised")
            except Exception:
                log.exception("periodic rescan failed")
            self._stop.wait(self.rescan_seconds)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception:
            pass
        if self._rescan_thread:
            self._rescan_thread.join(timeout=5)
