# -*- coding: utf-8 -*-
"""
app.py
------
Servidor web (Flask) para o dashboard "Service Desk por Faixa".

- GET /            -> serve o dashboard (HTML estático, busca dados via JS)
- GET /api/data    -> retorna os dados mais recentes em JSON, atualizando
                       o cache automaticamente a cada REFRESH_SECONDS
                       (padrão: 300s = 5 min) para não sobrecarregar o Jira.
- GET /api/health  -> healthcheck simples (usado pelo Render)

O token do Jira fica só nas variáveis de ambiente do servidor — nunca
é enviado ao navegador de quem acessa o dashboard.
"""
from __future__ import annotations

import os
import threading
import time
import traceback

from flask import Flask, jsonify, send_from_directory

from jira_sync import fetch_dashboard_data

app = Flask(__name__, static_folder="static")

REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "300"))

_cache_lock = threading.Lock()
_cache = {"data": None, "fetched_at": 0.0, "error": None}


def _refresh_cache():
    try:
        data = fetch_dashboard_data()
        with _cache_lock:
            _cache["data"] = data
            _cache["fetched_at"] = time.time()
            _cache["error"] = None
    except Exception as e:
        traceback.print_exc()
        with _cache_lock:
            _cache["error"] = str(e)


def _background_refresher():
    while True:
        _refresh_cache()
        time.sleep(REFRESH_SECONDS)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/data")
def api_data():
    with _cache_lock:
        data = _cache["data"]
        error = _cache["error"]
        fetched_at = _cache["fetched_at"]

    # Primeira chamada, ainda sem cache: busca na hora (bloqueante) uma vez.
    if data is None and error is None:
        _refresh_cache()
        with _cache_lock:
            data = _cache["data"]
            error = _cache["error"]
            fetched_at = _cache["fetched_at"]

    if data is None:
        return jsonify({"error": error or "Sem dados ainda"}), 503

    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


# Inicia o refresh em background assim que o processo sobe (Gunicorn ou
# `python app.py`), para o cache já vir quente na primeira visita.
_refresher_thread = threading.Thread(target=_background_refresher, daemon=True)
_refresher_thread.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
