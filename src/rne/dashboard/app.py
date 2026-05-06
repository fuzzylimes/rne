from __future__ import annotations

from flask import Flask, g

from rne import config, db


def create_app() -> Flask:
    app = Flask(__name__)

    @app.before_request
    def open_db() -> None:
        g.conn = db.connect()

    @app.teardown_appcontext
    def close_conn(exc: BaseException | None) -> None:
        conn = getattr(g, "conn", None)
        if conn is not None:
            conn.close()

    from rne.dashboard import routes
    routes.register(app)

    return app


def main() -> None:
    app = create_app()
    app.run(host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT)


if __name__ == "__main__":
    main()
