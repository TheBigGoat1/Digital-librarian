# Digital Librarian - Automated File Organizer

This script implements a CEO-Grade Automated Information Pipeline for organizing downloaded files on Windows.

## Features

- Monitors the Downloads folder for new files.
- Calculates MD5 hashes to detect duplicates.
- Classifies files by extension into categories: Images, Documents, Videos, Code, Installers, Archives.
- Moves files to organized library folders with timestamps.
- Handles locked files by waiting for release.
- Logs all actions to `librarian_audit.log`.

## Setup

1. Install Python 3.x if not already installed.
2. Install dependencies: `pip install -r requirements.txt`
3. Ensure the script path is correct in `librarian_start.vbs` (update to `C:\DigitalLibrarian\librarian_engine.py` if needed).
4. Run `librarian_start.vbs` to start in background, or use `run.bat`.

## Configuration Checklist

| Task | Action | Why? |
| --- | --- | --- |
| **Dependencies** | Run `pip install watchdog` | Enables the kernel-level listener. |
| **WhatsApp** | Change download folder to `Downloads` | Directs client files into the Librarian's path. |
| **Telegram** | Change download folder to `Downloads` | Directs developer files into the Librarian's path. |
| **Storage** | Use Lenovo Vantage to set SSD to Performance | Instant hashing and moving of files. |
| **Startup** | Place `librarian_start.vbs` in Startup folder | Ensures the Librarian runs on boot. |

## Integration

- Set browser download paths to the Downloads folder.
- Configure messaging apps to download to the same folder.

## Notes

- Files are timestamped to avoid conflicts.
- Duplicates are deleted based on hash.
- Logs are in the home directory.

## Global Search

Use the provided `search_library.bat` to search the library by category. Run it and enter the subject (e.g., Images).
- Configure messaging apps (WhatsApp, Telegram) to download to the same folder.

## Notes

- The script runs indefinitely, monitoring for file events.
- Logs to console; redirect if needed.
- For production, consider running as a Windows service.

## Troubleshooting

- Ensure Python and watchdog are installed.
- Check permissions for file operations.
- If files are not moving, check if they are locked by other apps.