"""Actual HTTP server composition; ordinary startup never migrates."""


def run_api(settings, *, host='127.0.0.1', port=8000):
    import uvicorn
    from workbench_agent.api.app import create_app

    uvicorn.run(
        create_app(settings), host=host, port=port, timeout_graceful_shutdown=15, access_log=False
    )
