"""The local console: a checklist you can act on, over loopback.

Importing this package opens nothing -- no socket, no model, no database. That is
the same rule ``core/tools`` and ``core/memory`` follow, and it is what lets the
test suite import everything without side effects.

``ConsoleApi`` is what each endpoint means, ``ConsoleServer`` is the socket and the
token, and ``static/index.html`` is the whole front end in one file.
``scripts/run_console.py`` is the command line that puts the three together with a
``VoiceRuntime``.
"""

from __future__ import annotations

from .audio import AudioDecodeError, decode_wav_base64, quality
from .routes import EDITABLE, ApiError, ConsoleApi
from .server import ConsoleError, ConsoleServer, loopback_problem

__all__ = [
    "EDITABLE",
    "ApiError",
    "AudioDecodeError",
    "ConsoleApi",
    "ConsoleError",
    "ConsoleServer",
    "decode_wav_base64",
    "loopback_problem",
    "quality",
]
