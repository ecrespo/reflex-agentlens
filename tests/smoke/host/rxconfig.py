"""rxconfig of the smoke-test host app (plan task P6-12.1)."""

import reflex as rx

config = rx.Config(
    app_name="host_app",
    telemetry_enabled=False,
    show_built_with_reflex=False,
    state_manager_mode=rx.constants.StateManagerMode.MEMORY,
    plugins=[rx.plugins.RadixThemesPlugin()],
    disable_plugins=[rx.plugins.SitemapPlugin],
)
