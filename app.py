# -*- coding: utf-8 -*-
"""
app.py
------
Servidor web (Flask) para o dashboard "Service Desk por Faixa".

- GET /            -> serve o dashboard (HTML estático, busca dados via JS)
- GET /api/data    -> retorna os dados mais recentes em JSON, atualizando
                       o cache automaticamente a cada REFRESH_SECONDS
                       (padrão: 300s = 5 min) para não sobrecarregar o Jira.
- GET /api/health  -> healthcheck simples (usado pelo Render, sem autenticação)

O token do Jira, e agora também o usuário/senha de acesso ao dashboard,
ficam só nas variáveis de ambiente do servidor — nunca hardcoded no
código nem enviados ao navegador de quem acessa.

Autenticação: HTTP Basic Auth simples, configurada via as variáveis de
ambiente DASHBOARD_USER e DASHBOARD_PASSWORD. Se essas duas variáveis
não estiverem definidas, o dashboard fica público sem senha (assim o
serviço não quebra caso alguém esqueça de configurar).
"""
from __future__ import annotations

import hmac
import os
import threading
import time
import traceback
from functools import wraps

from flask import Flask, Response, jsonify, request, send_from_directory

from jira_sync import fetch_dashboard_data

app = Flask(__name__, static_folder="static")

REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "300"))
# Cooldown mínimo entre buscas forçadas (botão "Atualizar"), pra ninguém
# conseguir martelar o botão e sobrecarregar a API do Jira.
FORCE_REFRESH_COOLDOWN = int(os.environ.get("FORCE_REFRESH_COOLDOWN", "15"))

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
AUTH_ENABLED = bool(DASHBOARD_USER and DASHBOARD_PASSWORD)

_cache_lock = threading.Lock()
_cache = {"data": None, "fetched_at": 0.0, "error": None}
_refresh_in_progress = threading.Lock()


def _check_credentials(user: str, password: str) -> bool:
    # hmac.compare_digest evita "timing attack" (comparar string por
    # string normal vaza informação pelo tempo de resposta).
    return (hmac.compare_digest(user, DASHBOARD_USER)
            and hmac.compare_digest(password, DASHBOARD_PASSWORD))


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not AUTH_ENABLED:
            return view(*args, **kwargs)
        auth = request.authorization
        if not auth or not _check_credentials(auth.username or "", auth.password or ""):
            return Response(
                "Login necessário para acessar o dashboard.", 401,
                {"WWW-Authenticate": 'Basic realm="Service Desk Dashboard"'})
        return view(*args, **kwargs)
    return wrapped


def _refresh_cache():
    # Evita duas buscas simultâneas (ex: refresh automático e botão
    # "Atualizar" clicados ao mesmo tempo) — a segunda só espera a
    # primeira terminar e aproveita o resultado dela.
    with _refresh_in_progress:
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
@require_auth
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/data")
@require_auth
def api_data():
    force = request.args.get("force") == "1"

    with _cache_lock:
        data = _cache["data"]
        error = _cache["error"]
        fetched_at = _cache["fetched_at"]

    # Primeira chamada, ainda sem cache: busca na hora (bloqueante) uma vez.
    # Ou: botão "Atualizar" clicado (force=1) e já passou o cooldown mínimo
    # desde a última busca — busca de novo na hora, ignorando o cache.
    should_force = force and (time.time() - fetched_at) >= FORCE_REFRESH_COOLDOWN
    if (data is None and error is None) or should_force:
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
