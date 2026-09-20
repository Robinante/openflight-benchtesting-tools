"""Progress reporting for the analysis layer.

The analyses narrate as they run. That output is genuinely useful -- it is how
you see which bin a group settled on and why a file was skipped -- but writing
it to stdout means only a terminal can consume it.

``as_progress`` wraps a caller's sink so it can stand in for the builtin
``print`` exactly: same ``*args``, same ``sep``/``end``, so the analysis bodies
need no edits and a sink that only accepts one string still works.

    lines = []
    analyze_material(opts, progress=lines.append)   # nothing reaches stdout

Passing ``progress=None`` uses the real ``print``, which is why the CLI output
is byte-for-byte what it always was.
"""

from __future__ import annotations

from builtins import print as _builtin_print


def as_progress(sink=None):
    """Return a print-compatible callable that forwards to ``sink``.

    ``sink`` may be any one-argument callable (``list.append``, a logger, a
    Streamlit placeholder's write). ``None`` returns the builtin ``print``.
    """
    if sink is None or sink is _builtin_print:
        return _builtin_print
    if getattr(sink, "_is_progress_adapter", False):
        return sink

    def progress(*args, sep=" ", end="\n", **_ignored):
        sink(sep.join(str(a) for a in args))

    progress._is_progress_adapter = True
    return progress


class CollectingProgress:
    """A sink that keeps every line and can mirror it somewhere live.

    Handy for a GUI: hand ``.write`` to the analysis, render ``.lines`` when it
    finishes, and optionally pass ``mirror`` to stream lines as they arrive.
    """

    def __init__(self, mirror=None):
        self.lines: list[str] = []
        self._mirror = mirror

    def write(self, line: str) -> None:
        self.lines.append(line)
        if self._mirror is not None:
            self._mirror(line)

    __call__ = write

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def __len__(self) -> int:
        return len(self.lines)
