"""Entry point: `python -m cross_agent_mcp`.

`--reload` runs the front instead of the server, and `reload` asks the running fronts to load
the current source. Both are dispatched before the server is imported: the front stays alive
for the whole editor session, and nothing of the bridge it never uses should live in it.
"""

import sys


if __name__ == '__main__':
    if sys.argv[1:2] in (['--reload'], ['reload']):
        from .front import main
    else:
        from .server import main
    main()
