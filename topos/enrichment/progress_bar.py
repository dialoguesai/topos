"""Progress bar utility for enrichment jobs.

Provides a single-line updating progress bar similar to transformers library.
"""

from __future__ import annotations

import sys
import time
from typing import Optional


class ProgressBar:
    """Single-line progress bar that updates in place."""
    
    def __init__(
        self,
        total: int,
        desc: str = "",
        width: int = 50,
        file: Optional[object] = None,
    ):
        """Initialize progress bar.
        
        Args:
            total: Total number of items to process
            desc: Description prefix for the progress bar
            width: Width of the progress bar in characters
            file: File object to write to (defaults to stderr)
        """
        self.total = total
        self.desc = desc
        self.width = width
        self.file = file or sys.stderr
        self.n = 0
        self.start_time = time.time()
        self.last_update_time = self.start_time
        try:
            self._isatty = hasattr(self.file, 'isatty') and self.file.isatty()
        except ValueError:
            # Closed stream — isatty() raises rather than answering.
            self._isatty = False
        self._last_line_length = 0
        # Set once a write fails: a detached process (node launched by the
        # macOS shell, --app --no-tray) can have stderr closed out from under
        # it, and every later write would raise the same ValueError.
        self._broken = False

    def _write(self, text: str, end: str = "") -> None:
        """Emit progress, or give up permanently. NEVER raises.

        Progress display is cosmetic; the work it narrates is not. On
        2026-08-09 this exact write killed the `backfill-attention-triage-redo`
        upgrade step six seconds into a boot — before a single source had been
        touched — because the step ran in a daemon thread whose stderr had been
        closed. A progress bar must not be able to fail a data repair.
        """
        if self._broken:
            return
        try:
            print(text, end=end, file=self.file, flush=True)
        except (ValueError, OSError):
            # ValueError: I/O operation on closed file. OSError: EPIPE/EBADF.
            self._broken = True

    def update(self, n: int = 1) -> None:
        """Update progress by n items.
        
        Args:
            n: Number of items to advance (default 1)
        """
        self.n = min(self.n + n, self.total)
        self._display()
        
    def set_description(self, desc: str) -> None:
        """Update the description prefix.
        
        Args:
            desc: New description
        """
        self.desc = desc
        
    def _display(self) -> None:
        """Display/update the progress bar."""
        if not self._isatty:
            # If not a TTY, just print periodic updates
            if self.n % max(1, self.total // 10) == 0 or self.n == self.total:
                elapsed = time.time() - self.start_time
                percent = (self.n / self.total * 100) if self.total > 0 else 0
                self._write(
                    f"\r{self.desc}: {self.n}/{self.total} ({percent:.1f}%) "
                    f"[{elapsed:.1f}s]"
                )
            return
            
        # Calculate progress
        percent = (self.n / self.total * 100) if self.total > 0 else 0
        elapsed = time.time() - self.start_time
        
        # Calculate rate
        if elapsed > 0 and self.n > 0:
            rate = self.n / elapsed
            if self.n < self.total:
                eta = (self.total - self.n) / rate
                eta_str = f", ETA: {eta:.1f}s"
            else:
                eta_str = ""
        else:
            rate = 0
            eta_str = ""
        
        # Build progress bar
        filled = int(self.width * self.n / self.total) if self.total > 0 else 0
        bar = "█" * filled + "░" * (self.width - filled)
        
        # Build status string
        status = f"{self.desc}: {percent:5.1f}%|{bar}| {self.n}/{self.total} [{elapsed:.1f}s{eta_str}]"
        
        # Clear previous line and print new one
        # Use carriage return and clear to end of line
        self._write(f"\r{' ' * self._last_line_length}\r{status}")
        self._last_line_length = len(status)

    def close(self) -> None:
        """Close the progress bar (print final newline)."""
        if self._isatty:
            self._write("", end="\n")  # Newline to move past progress bar
        self._last_line_length = 0
        
    def __enter__(self):
        """Context manager entry."""
        self._display()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        
    def __del__(self):
        """Cleanup on deletion."""
        try:
            self.close()
        except Exception:
            pass
