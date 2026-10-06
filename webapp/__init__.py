"""webapp - the hosted, converter-only half of yt2disc.

``converter.py`` turns a file into another file; ``jobs.py`` runs that work in
the background so a slow conversion cannot hang a request; ``app.py`` is the
thin Flask layer over the two.

Nothing in here plays audio, keeps a library, or touches Minecraft - that is
all the local add-on's job (``core.py`` + ``player.py`` + ``gui.py``).
"""

from __future__ import annotations

__version__ = "1.0.0"
