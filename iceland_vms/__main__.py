import sys

from . import limit_malloc_arenas

limit_malloc_arenas()                   # before GTK starts its threads
from .ui.app import main  # noqa: E402

sys.exit(main())
