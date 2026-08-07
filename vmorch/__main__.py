"""`python3 -m vmorch` -- run from a clone without installing anything.

The repository used to carry `vm` and `vmtui` scripts that inserted their own
directory into sys.path and were meant to be symlinked into ~/.local/bin. That
worked, but it is a local trick rather than a way to ship software: it made
installation "clone, then symlink two files" and left nothing for pip to do.

There are two supported ways in now, and this is the one that needs no install.
"""

import sys

from .cli import main

sys.exit(main())
