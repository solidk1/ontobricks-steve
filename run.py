#!/usr/bin/env python3
"""Main entry point for OntoBricks application (FastAPI)."""
import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_ROOT, "src"))

startup_error = None
app = None

try:
    import uvicorn
    from dotenv import load_dotenv

    # Load environment variables
    load_dotenv()

    # Configure structured logging (must happen before any app import)
    from back.core.logging import setup_logging
    setup_logging()

    # Import and create the FastAPI app
    from shared.fastapi.main import create_app
    app = create_app()

except Exception as e:
    startup_error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    print(f"STARTUP ERROR: {startup_error}", flush=True)

# Fallback app if main app fails to load
if app is None:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="OntoBricks - Error")

    @app.get("/", response_class=HTMLResponse)
    def error_page():
        error_html = startup_error.replace('\n', '<br>') if startup_error else "Unknown error"
        return f"""
        <html>
        <head><title>OntoBricks - Startup Error</title></head>
        <body style="font-family: monospace; padding: 20px;">
            <h1 style="color: red;">OntoBricks Failed to Start</h1>
            <h3>Error:</h3>
            <pre style="background: #f0f0f0; padding: 15px; overflow: auto;">{error_html}</pre>
            <p>Please check the logs for more details.</p>
        </body>
        </html>
        """

    @app.get("/health")
    def health():
        return {"status": "error", "message": "App failed to start", "error": startup_error}

if __name__ == '__main__':
    import logging

    from shared.config.constants import APP_LOGGER_NAME
    from shared.config.RuntimeEnv import RuntimeEnv

    _log = logging.getLogger(APP_LOGGER_NAME)

    port = RuntimeEnv.port()
    containerized = RuntimeEnv.is_containerized()

    if containerized:
        # 0.0.0.0 is mandatory: bound to loopback the container starts, fails
        # every health check and serves nothing to the outside. Auto-reload is
        # a development tool and would restart the process on any file change,
        # killing in-flight background builds.
        _log.info("Starting uvicorn — 0.0.0.0:%d (containerized)", port)
        uvicorn.run(app, host='0.0.0.0', port=port, log_level="info", log_config=None)
    else:
        # Local development: loopback only, and auto-reload on by default.
        #
        # Reload restarts the process on any src/ save, which kills in-flight
        # background task threads (Auto-Map, KG build) and drops the in-memory
        # TaskManager state. Set ONTOBRICKS_NO_RELOAD=1 when running long live
        # jobs — notably `make scenario-campaign`.
        _no_reload = os.getenv("ONTOBRICKS_NO_RELOAD", "").strip().lower() in {
            "1", "true", "yes", "on"
        }
        _uvicorn_kwargs = dict(
            host='127.0.0.1',
            port=port,
            log_level="info",
            log_config=None,
        )
        if not _no_reload:
            _uvicorn_kwargs["reload"] = True
            _uvicorn_kwargs["reload_dirs"] = [
                "src/back", "src/front", "src/api", "src/shared", "src/agents"
            ]
        _log.info(
            "Starting uvicorn — 127.0.0.1:%d, auto-reload %s",
            port,
            "DISABLED" if _no_reload else "enabled",
        )
        # Pass env_file so reload workers see .env without re-running load_dotenv.
        _env_file = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.isfile(_env_file):
            _uvicorn_kwargs["env_file"] = _env_file
        uvicorn.run("shared.fastapi.main:app", **_uvicorn_kwargs)
