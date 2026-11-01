"""Build-only Reflex app module: the export target of :mod:`reflex_agentlens._webapp.build`
(plan task P6-11.1; DD-002). ``serve`` builds the same app with ``build_standalone`` instead of
importing this module, so the compiled frontend and the running backend share pages and states.
"""

from reflex_agentlens.app.standalone import standalone_app

app = standalone_app()
