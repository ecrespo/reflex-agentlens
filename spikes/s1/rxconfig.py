import reflex as rx

config = rx.Config(
    app_name="agentlens_webapp",
    app_module_import="reflex_agentlens._webapp.app",
    telemetry_enabled=False,
    show_built_with_reflex=False,
    plugins=[],
)
