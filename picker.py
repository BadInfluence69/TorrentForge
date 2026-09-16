"""Native folder/file chooser, launched as a short-lived subprocess.

Used by the "Browse..." button in the web UI.  Running Tk in its own process
keeps the Flask server free of GUI event-loop problems.

    python picker.py folder [start_dir]
    python picker.py file   [start_dir]

Prints the chosen path on stdout (nothing at all if the dialog is cancelled).
"""

from __future__ import annotations

import sys


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "folder"
    start = sys.argv[2] if len(sys.argv) > 2 else ""

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - tkinter missing
        print(f"ERROR: tkinter not available: {exc}", file=sys.stderr)
        return 2

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    try:
        if mode == "file":
            chosen = filedialog.askopenfilename(
                title="Select a file to make a torrent of",
                initialdir=start or None,
            )
        else:
            chosen = filedialog.askdirectory(
                title="Select a folder to make a torrent of",
                initialdir=start or None,
                mustexist=True,
            )
    finally:
        root.destroy()

    if chosen:
        sys.stdout.write(chosen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
