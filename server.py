#!/usr/bin/env python3
"""RAT server entry point.

Usage:
    python3 server.py [--host 127.0.0.1] [--port 8000] [--data DIR]
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from flask import Flask, jsonify, send_from_directory
from werkzeug.exceptions import HTTPException

from rat import __version__
from rat.api import ApiError, api
from rat.config import DATA_DIR, MAX_UPLOAD_BYTES, WEB_DIR
from rat.store import RepoStore


def create_app(data_dir=None) -> Flask:
    app = Flask(
        __name__,
        static_folder=str(WEB_DIR),
        static_url_path="/",
    )
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.config["JSON_SORT_KEYS"] = False
    app.extensions["store"] = RepoStore(Path(data_dir).resolve() if data_dir else DATA_DIR)
    app.register_blueprint(api)

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/favicon.ico")
    def favicon():
        return ("", 204)

    @app.errorhandler(ApiError)
    def handle_api_error(exc: ApiError):
        return jsonify({"error": exc.message}), exc.status

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        if exc.code == 413:
            return jsonify({"error": "Upload exceeds the maximum archive size."}), 413
        if exc.code == 404 and not str(exc.description).startswith("The requested URL"):
            return jsonify({"error": exc.description}), 404
        return jsonify({"error": exc.description or exc.name}), exc.code or 500

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        app.logger.exception("Unhandled error")
        return jsonify({"error": f"Internal error: {exc}"}), 500

    @app.after_request
    def no_cache_api(resp):
        if resp.mimetype == "application/json":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="RAT - Repository Analysis Tool server")
    parser.add_argument("--host", default=os.environ.get("RAT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RAT_PORT", "8000")))
    parser.add_argument("--data", default=None, help="data directory (default: ./data)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app(args.data)
    print(f"RAT v{__version__} — dashboard at http://{args.host}:{args.port}/")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
