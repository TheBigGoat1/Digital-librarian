import os
import time
import shutil
import hashlib
import logging
import json
from pathlib import Path
from datetime import datetime
from collections import deque
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- ARCHITECTURE CONFIG ---
BASE_DIR = Path.home() / "Downloads"
LOG_FILE = Path.home() / "librarian_audit.log"
LOCK_FILE = BASE_DIR / ".librarian.lock"
CLEANUP_MARKER_FILE = BASE_DIR / ".cleanup_done"
MANIFEST_FILE = BASE_DIR / "librarian_manifest.jsonl"

LIBRARY_MAP = {
    "Images": [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico"],
    "Documents": [".pdf", ".docx", ".doc", ".txt", ".md", ".xlsx", ".csv", ".pptx"],
    "Videos": [".mp4", ".mov", ".avi", ".mkv", ".webm"],
    "Code": [".py", ".js", ".ts", ".json", ".html", ".css", ".cpp", ".sql", ".sh"],
    "Installers": [".exe", ".msi"],
    "Archives": [".zip", ".tar", ".gz", ".7z", ".rar"]
}

UNSORTED_DIR = BASE_DIR / "Unsorted"
EXTRACTED_PREFIX = "Extracted_"
FOLDER_PREFIX = "Folder_"
PROJECT_PREFIX = "Project_"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)

class LibrarianHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        # Items: (archive_moved_time, archive_stem, extracted_bundle_path)
        self._recent_archives = deque(maxlen=20)
        self._hash_cache = {}  # (path, size, mtime_ns) -> md5

    def _now_stamp(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _is_library_path(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(BASE_DIR.resolve())
        except Exception:
            return False
        top = rel.parts[0] if rel.parts else ""
        # Ignore anything inside our managed library folders.
        # We keep the layout flat, using prefixes to avoid deep folder trees.
        return (
            top in set(LIBRARY_MAP.keys())
            or top == "Unsorted"
            or top.startswith(EXTRACTED_PREFIX)
            or top.startswith(FOLDER_PREFIX)
            or top.startswith(PROJECT_PREFIX)
        )

    def _write_manifest(self, action: str, src: Path, dst: Path, category: str | None = None):
        try:
            entry = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "action": action,
                "src": str(src),
                "dst": str(dst),
            }
            if category:
                entry["category"] = category
            with open(MANIFEST_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            # Manifest should never break file processing.
            pass

    def get_file_hash(self, path: Path) -> str:
        hasher = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def get_file_hash_cached(self, path: Path) -> str:
        try:
            st = path.stat()
            mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
            key = (str(path), st.st_size, mtime_ns)
            cached = self._hash_cache.get(key)
            if cached:
                return cached
            computed = self.get_file_hash(path)
            self._hash_cache[key] = computed
            return computed
        except Exception:
            # Fall back to uncached if stat/hash fails for any reason.
            return self.get_file_hash(path)

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

        stamp = self._now_stamp()
        prefix = FOLDER_PREFIX
        if self._is_project_folder(folder_path):
            prefix = PROJECT_PREFIX
        dest = BASE_DIR / f"{prefix}{stamp}_{folder_path.name}"
        dest = self._unique_destination(dest)

        try:
            shutil.move(str(folder_path), str(dest))
            category = "Projects" if prefix == PROJECT_PREFIX else "Folders"
            logging.info(f"{category.upper()} SAVED: {folder_path.name} -> {dest}")
            self._write_manifest("folder_move", folder_path, dest, category=category)
        except Exception as e:
            logging.error(f"SYSTEM ERROR (folder move): {e}")

    def _is_project_folder(self, folder_path: Path) -> bool:
        # Lightweight heuristic: check for common project files/folders at the root of the folder.
        # This avoids scanning entire trees.
        project_markers = [
            "package.json",
            "pyproject.toml",
            "requirements.txt",
            "setup.py",
            "manage.py",
            "composer.json",
            "Cargo.toml",
            "go.mod",
            "pom.xml",
            "build.gradle",
            "Gemfile",
            ".sln",
            "Dockerfile",
        ]
        project_dirs = [".git", ".hg", ".svn", ".idea", ".vscode"]

        for name in project_markers:
            if (folder_path / name).exists():
                return True
        for name in project_dirs:
            if (folder_path / name).exists():
                return True
        return False

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

        # Heuristic: if a bunch of files get extracted loose into Downloads right after an archive,
        # bundle them into a single folder instead of scattering across categories.
        if self._recent_archives:
            now = time.time()
            recent_ts, recent_stem, recent_bundle = self._recent_archives[-1]
            if now - recent_ts <= 120:
                # Use one bundle folder per archive to avoid "replication-looking" folder spam.
                bundle = recent_bundle
                bundle.mkdir(exist_ok=True, parents=True)
                try:
                    dest = self._unique_destination(bundle / file_path.name)
                    shutil.move(str(file_path), str(dest))
                    logging.info(f"EXTRACT BUNDLE: {file_path.name} -> {bundle}")
                    self._write_manifest("extract_bundle_file", file_path, dest, category="Extracted")
                except Exception as e:
                    logging.error(f"SYSTEM ERROR (bundle move): {e}")
                return

        # Normal routing: categorize by extension; unknown types go to Unsorted.
        if not target_folder:
            target_folder = UNSORTED_DIR
        target_folder.mkdir(parents=True, exist_ok=True)

        current_size = None
        try:
            current_size = file_path.stat().st_size
        except Exception:
            current_size = None
        current_hash = self.get_file_hash_cached(file_path)
            
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
                    if self.get_file_hash_cached(existing) == current_hash:
                        logging.info(f"REDUNDANCY: Deleted duplicate {file_path.name}")
                        os.remove(file_path)
                        return
                except:
                    continue

        # Final Move (flat structure):
        # Put the file directly under the category folder to keep navigation simple:
        # Downloads/<Category>/<timestamp>_<filename>
        timestamp = self._now_stamp()
        final_path = target_folder / f"{timestamp}_{file_path.name}"
        final_path = self._unique_destination(final_path)
        try:
            shutil.move(str(file_path), str(final_path))
            logging.info(f"ARCHIVED: {file_path.name} -> {target_folder.name} (wrapped)")
            self._write_manifest("file_move", file_path, final_path, category=target_folder.name)

            if target_folder.name == "Archives":
                # Remember most recent archive so loose extractions can be bundled.
                archive_stamp = self._now_stamp()
                archive_stem = Path(file_path.name).stem
                extracted_bundle = BASE_DIR / f"{EXTRACTED_PREFIX}{archive_stem}_{archive_stamp}"
                self._recent_archives.append((time.time(), archive_stem, extracted_bundle))
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
    - Folders: moved whole into Downloads/Folder_<stamp>_<name>/...
    - Files: deleted (user requested hard cleanup)
    """
    try:
        base_resolved = BASE_DIR.resolve()
    except Exception:
        base_resolved = BASE_DIR

    try:
        for item in BASE_DIR.iterdir():
            try:
                # Never touch our lock/marker/manifest artifacts.
                if item.name in {LOCK_FILE.name, CLEANUP_MARKER_FILE.name, MANIFEST_FILE.name, LOG_FILE.name}:
                    continue
                if item.name.startswith("."):
                    continue

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
                        handler._write_manifest("cleanup_delete", item, item, category="DownloadsRoot")
                    except Exception as e:
                        logging.error(f"SYSTEM ERROR (cleanup delete): {e}")
            except Exception as e:
                logging.error(f"SYSTEM ERROR (cleanup item): {e}")
    except Exception as e:
        logging.error(f"SYSTEM ERROR (cleanup): {e}")

def acquire_lock() -> bool:
    try:
        # Create exclusively; if it exists, another instance is running.
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    except FileExistsError:
        return False
    except Exception:
        return False

def release_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass

if __name__ == "__main__":
    # Single-instance guard
    if not acquire_lock():
        logging.info("LOCK ACTIVE: librarian already running; exiting this instance.")
        raise SystemExit(0)

    handler = LibrarianHandler()
    observer = Observer()

    try:
        for folder in LIBRARY_MAP.keys():
            (BASE_DIR / folder).mkdir(exist_ok=True)
        UNSORTED_DIR.mkdir(parents=True, exist_ok=True)

        # Clean up any existing standalone items before watching (only once).
        if not CLEANUP_MARKER_FILE.exists():
            cleanup_downloads_root(handler)
            try:
                CLEANUP_MARKER_FILE.write_text("done", encoding="utf-8")
            except Exception:
                pass

        observer.schedule(handler, str(BASE_DIR), recursive=True)
        observer.start()
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()
    finally:
        try:
            observer.join(timeout=5)
        except Exception:
            pass
        release_lock()