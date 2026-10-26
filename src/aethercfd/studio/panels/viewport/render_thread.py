"""Rule 1 of ADR 0008, checked at run time: a viewport renders on the GUI thread only.

pyvistaqt's ``QtInteractor.render()`` is already safe to call from any thread (it emits a Qt signal, so the
render is queued to the GUI thread), but anything that reaches the VTK render window directly from a worker
(``render_window.Render()``, an off-screen grab, a filter's lazy update through a mapper) races the GUI
thread's OpenGL context. A StartEvent observer on each viewport's render window runs on whichever thread is
rendering; when that is not the thread that built the viewport it logs the call stack and records it in
``VIOLATIONS``, which tests and ``--sweep`` check. A Python exception would be swallowed inside a VTK
observer, so the check records instead of raising. The cost is one Python call per rendered frame.
"""

from __future__ import annotations

import logging
import threading
import traceback

log = logging.getLogger("aethercfd.studio.viewport")

VIOLATIONS: list[str] = []  # "<viewport>: <thread name>\n<stack>" per off-thread render
_MAX = 50


def guard_render_thread(render_window, name: str) -> int:
    """Observe ``render_window``: every render must run on the calling (GUI) thread. Returns the observer
    tag."""
    owner = threading.get_ident()

    def check(_obj, _event) -> None:
        if threading.get_ident() == owner:
            return
        msg = (f"{name}: rendered on thread {threading.current_thread().name!r}, not the GUI thread\n"
               + "".join(traceback.format_stack(limit=12)))
        if len(VIOLATIONS) < _MAX:
            VIOLATIONS.append(msg)
        log.error("viewport render off the GUI thread (ADR 0008 rule 1): %s", msg)

    return render_window.AddObserver("StartEvent", check)
