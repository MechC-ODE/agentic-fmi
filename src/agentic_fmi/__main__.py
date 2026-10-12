"""``python -m agentic_fmi``: the same CLI as ``afmi``. The server spawns workers this way, with
``sys.executable``, so they use the server's interpreter and environment.

Collaborators: ``cli.main``.
"""

from agentic_fmi.cli import main

raise SystemExit(main())
