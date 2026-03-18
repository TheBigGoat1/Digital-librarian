import os
import time
import shutil
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from collections import deque
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- ARCHITECTURE CONFIG ---
BASE_DIR = Path.home() / "Downloads"
LOG_FILE = Path.home() / "librarian_audit.log"

LIBRARY_MAP = {
    "Images": [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico"],
    "Documents": [".pdf", ".docx", ".doc", ".txt", ".md", ".xlsx", ".csv", ".pptx"],
    "Videos": [".mp4", ".mov", ".avi", ".mkv", ".webm"],
    "Code": [".py", ".js", ".ts", ".json", ".html", ".css", ".cpp", ".sql", ".sh"],
    "Installers": [".exe", ".msi"],
    "Archives": [".zip", ".tar", ".gz", ".7z", ".rar"]
}

FOLDERS_DIR = BASE_DIR / "Folders"
EXTRACTED_DIR = FOLDERS_DIR / "Extracted"
UNSORTED_DIR = FOLDERS_DIR / "Unsorted"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)

class LibrarianHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self._recent_archives = deque(maxlen=20)  # (timestamp, archive_stem)

    def _now_stamp(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _is_library_path(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(BASE_DIR.resolve())
        except Exception:
            return False
        top = rel.parts[0] if rel.parts else ""
        return top in set(LIBRARY_MAP.keys()) | {"Folders"}

    def get_file_hash(self, path):
        hasher = hashlib.md5()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _wait_until_stable(self, path: Path, timeout_s: int = 90, interval_s: float = 1.0, stable_checks: int = 3) -> bool:
        """
        Wait until the file size stops changing and the file can be opened.
        This avoids moving half-downloaded / still-being-written files.
        """
        start = time.time()
        last_size = -1
        stable = 0

        while time.time() - start < timeout_s:
            if not path.exists():
                return False

            try:
                size = path.stat().st_size
            except Exception:
                time.sleep(interval_s)
                continue

            if size == last_size and size > 0:
                stable += 1
            else:
                stable = 0
                last_size = size

            if stable >= stable_checks:
                try:
                    with open(path, "rb"):
                        return True
                except Exception:
                    # Still locked by another process; keep waiting.
                    pass

            time.sleep(interval_s)

        return False

    def _unique_destination(self, dest: Path) -> Path:
        if not dest.exists():
            return dest
        stem = dest.stem
        suffix = dest.suffix
        parent = dest.parent
        i = 1
        while True:
            candidate = parent / f"{stem}_{i}{suffix}"
            if not candidate.exists():
                return candidate
            i += 1

    def _move_folder_whole(self, folder_path: Path):
        if not folder_path.exists() or not folder_path.is_dir():
            return
        if self._is_library_path(folder_path):
            return

        FOLDERS_DIR.mkdir(exist_ok=True)
        stamp = self._now_stamp()
        dest = FOLDERS_DIR / f"{stamp}_{folder_path.name}"
        dest = self._unique_destination(dest)

        try:
            shutil.move(str(folder_path), str(dest))
            logging.info(f"FOLDER SAVED: {folder_path.name} -> {dest}")
        except Exception as e:
            logging.error(f"SYSTEM ERROR (folder move): {e}")

    def process_file(self, file_path):
        # Skip directories, library destinations, and temp downloads
        if file_path.is_dir() or self._is_library_path(file_path) or file_path.suffix.lower() in [".tmp", ".crdownload", ".part"]:
            return

        # Only process standalone files directly in Downloads.
        # If a file is already inside a non-library folder, leave it alone to avoid "pulling it out".
        try:
            if file_path.parent.resolve() != BASE_DIR.resolve():
                return
        except Exception:
            return

        # Wait for file stability (ensures download is finished)
        if not self._wait_until_stable(file_path):
            return

        ext = file_path.suffix.lower()
        target_folder = None

        for folder_name, extensions in LIBRARY_MAP.items():
            if ext in extensions:
                target_folder = BASE_DIR / folder_name
                break

        if not target_folder:
            # Never leave loose files in Downloads; park unknown types safely.
            target_folder = UNSORTED_DIR

        target_folder.mkdir(parents=True, exist_ok=True)

        # Heuristic: if a bunch of files get extracted loose into Downloads right after an archive,
        # bundle them into a single folder instead of scattering across categories.
        if self._recent_archives:
            now = time.time()
            recent_ts, recent_stem = self._recent_archives[-1]
            if now - recent_ts <= 120:
                EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
                stamp = self._now_stamp()
                bundle = EXTRACTED_DIR / f"{stamp}_{recent_stem}"
                bundle.mkdir(exist_ok=True)
                dest = bundle / file_path.name
                dest = self._unique_destination(dest)
                try:
                    shutil.move(str(file_path), str(dest))
                    logging.info(f"EXTRACT BUNDLE: {file_path.name} -> {bundle}")
                except Exception as e:
                    logging.error(f"SYSTEM ERROR (bundle move): {e}")
                return

        current_size = None
        try:
            current_size = file_path.stat().st_size
        except Exception:
            current_size = None
        current_hash = self.get_file_hash(file_path)
            
        # Deduplication Check (only against direct files in category root)
        for existing in target_folder.iterdir():
            if existing.is_file() and existing.suffix == ext:
                try:
                    if current_size is not None:
                        try:
                            if existing.stat().st_size != current_size:
                                continue
                        except Exception:
                            pass
                    if self.get_file_hash(existing) == current_hash:
                        logging.info(f"REDUNDANCY: Deleted duplicate {file_path.name}")
                        os.remove(file_path)
                        return
                except:
                    continue

        # Final Move:
        # Never leave standalone files in any destination; wrap each file into its own folder.
        timestamp = self._now_stamp()
        wrapper_folder = target_folder / f"{timestamp}_{file_path.stem}"
        wrapper_folder = self._unique_destination(wrapper_folder)
        try:
            wrapper_folder.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.error(f"SYSTEM ERROR (mkdir): {e}")
            return

        final_path = wrapper_folder / file_path.name
        final_path = self._unique_destination(final_path)
        try:
            shutil.move(str(file_path), str(final_path))
            logging.info(f"ARCHIVED: {file_path.name} -> {target_folder.name} (wrapped)")

            if target_folder.name == "Archives":
                # Remember most recent archive so loose extractions can be bundled.
                self._recent_archives.append((time.time(), Path(file_path.name).stem))
        except Exception as e:
            logging.error(f"SYSTEM ERROR: {e}")

    def on_created(self, event):
        p = Path(event.src_path)
        if event.is_directory:
            self._move_folder_whole(p)
        else:
            self.process_file(p)

    def on_moved(self, event):
        p = Path(event.dest_path)
        if event.is_directory:
            self._move_folder_whole(p)
        else:
            self.process_file(p)

def cleanup_downloads_root(handler: LibrarianHandler):
    """
    One-time cleanup: ensure nothing remains standalone in Downloads root.
    - Folders: moved whole into Downloads/Folders/...
    - Files: deleted (user requested hard cleanup)
    """
    try:
        base_resolved = BASE_DIR.resolve()
    except Exception:
        base_resolved = BASE_DIR

    try:
        for item in BASE_DIR.iterdir():
            try:
                # Skip the library destinations themselves
                if handler._is_library_path(item):
                    continue
                if item.is_dir():
                    handler._move_folder_whole(item)
                else:
                    # Hard cleanup: delete standalone files directly in Downloads root
                    try:
                        if item.parent.resolve() != base_resolved:
                            continue
                    except Exception:
                        continue
                    try:
                        os.remove(item)
                        logging.info(f"CLEANUP DELETE: {item.name}")
                    except Exception as e:
                        logging.error(f"SYSTEM ERROR (cleanup delete): {e}")
            except Exception as e:
                logging.error(f"SYSTEM ERROR (cleanup item): {e}")
    except Exception as e:
        logging.error(f"SYSTEM ERROR (cleanup): {e}")

if __name__ == "__main__":
    for folder in LIBRARY_MAP.keys():
        (BASE_DIR / folder).mkdir(exist_ok=True)
    FOLDERS_DIR.mkdir(exist_ok=True)
    EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
    UNSORTED_DIR.mkdir(parents=True, exist_ok=True)
    # Clean up any existing standalone items before watching.
    cleanup_downloads_root(LibrarianHandler())
    observer = Observer()
    observer.schedule(LibrarianHandler(), str(BASE_DIR), recursive=True)
    observer.start()
    try:
        while True: time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()