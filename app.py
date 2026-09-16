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
from jira_client import JiraClient
import history_store
import insights

app = Flask(__name__, static_folder="static")

REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "300"))
# Cooldown mínimo entre buscas forçadas (botão "Atualizar"), pra ninguém
# conseguir martelar o botão e sobrecarregar a API do Jira.
FORCE_REFRESH_COOLDOWN = int(os.environ.get("FORCE_REFRESH_COOLDOWN", "15"))
# Os insights de reunião olham 30 dias pra trás — não precisam ser
# recalculados a cada 5 minutos como o resto do dashboard.
INSIGHTS_REFRESH_SECONDS = int(os.environ.get("INSIGHTS_REFRESH_SECONDS", "1800"))

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
AUTH_ENABLED = bool(DASHBOARD_USER and DASHBOARD_PASSWORD)

_cache_lock = threading.Lock()
_cache = {"data": None, "fetched_at": 0.0, "error": None}
_refresh_in_progress = threading.Lock()

_insights_lock = threading.Lock()
_insights_cache = {"data": None, "fetched_at": 0.0, "error": None}
_insights_refresh_in_progress = threading.Lock()


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
            # grava o retrato do dia no histórico; nunca deixa isso derrubar
            # o dashboard principal se o Redis estiver fora do ar.
            try:
                history_store.record_snapshot(data)
            except Exception:
                traceback.print_exc()
        except Exception as e:
            traceback.print_exc()
            with _cache_lock:
                _cache["error"] = str(e)


def _background_refresher():
    while True:
        _refresh_cache()
        time.sleep(REFRESH_SECONDS)


def _refresh_insights_cache():
    with _insights_refresh_in_progress:
        try:
            data = insights.build_insights(days=30)
            with _insights_lock:
                _insights_cache["data"] = data
                _insights_cache["fetched_at"] = time.time()
                _insights_cache["error"] = None
        except Exception as e:
            traceback.print_exc()
            with _insights_lock:
                _insights_cache["error"] = str(e)


def _background_insights_refresher():
    while True:
        _refresh_insights_cache()
        time.sleep(INSIGHTS_REFRESH_SECONDS)


@app.route("/")
@require_auth
def index():
    # conditional=False + no-store: nunca deixa o navegador (ou algum proxy
    # no meio do caminho) servir uma versão em cache do index.html via
    # ETag/304 — já aconteceu de um deploy novo não aparecer por causa disso.
    resp = send_from_directory("static", "index.html", conditional=False)
    resp.headers["Cache-Control"] = "no-store"
    return resp


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


@app.route("/api/history")
@require_auth
def api_history():
    days = request.args.get("days", "30")
    try:
        days = max(1, min(int(days), 90))
    except ValueError:
        days = 30
    history = history_store.get_history(days)
    resp = jsonify({"history": history, "enabled": history_store.is_enabled()})
    resp.headers["Cache-Control"] = "no-store"
    return resp


_backfill_lock = threading.Lock()
_backfill_in_progress = False


@app.route("/api/backfill", methods=["POST"])
@require_auth
def api_backfill():
    global _backfill_in_progress

    if not history_store.is_enabled():
        return jsonify({"error": "Histórico não está configurado (REDIS_URL ausente)."}), 400

    if _backfill_in_progress:
        return jsonify({"error": "Já existe um preenchimento retroativo em andamento."}), 409

    days = request.args.get("days", "30")
    try:
        days = max(1, min(int(days), 90))
    except ValueError:
        days = 30

    with _backfill_lock:
        if _backfill_in_progress:
            return jsonify({"error": "Já existe um preenchimento retroativo em andamento."}), 409
        _backfill_in_progress = True

    try:
        import backfill
        resumo = backfill.run_backfill(days=days)
        return jsonify({"status": "ok", **resumo})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        with _backfill_lock:
            _backfill_in_progress = False


@app.route("/api/insights")
@require_auth
def api_insights():
    force = request.args.get("force") == "1"

    with _insights_lock:
        data = _insights_cache["data"]
        error = _insights_cache["error"]
        fetched_at = _insights_cache["fetched_at"]

    should_force = force and (time.time() - fetched_at) >= FORCE_REFRESH_COOLDOWN
    if (data is None and error is None) or should_force:
        _refresh_insights_cache()
        with _insights_lock:
            data = _insights_cache["data"]
            error = _insights_cache["error"]
            fetched_at = _insights_cache["fetched_at"]

    if data is None:
        return jsonify({"error": error or "Sem dados ainda"}), 503

    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/debug/assets")
@require_auth
def api_debug_assets():
    """Rota temporária de diagnóstico: testa se dá pra ler um objeto do
    Jira Assets (CMDB) com as credenciais já configuradas (e-mail + API
    token), tentando os dois formatos de API conhecidos. Remover depois
    que descobrirmos qual funciona (ou se nenhuma funciona)."""
    workspace_id = request.args.get("workspace_id", "")
    object_id = request.args.get("object_id", "")
    if not workspace_id or not object_id:
        return jsonify({
            "error": "informe ?workspace_id=...&object_id=... na URL",
            "exemplo": "/api/debug/assets?workspace_id=67cc6f9e-3376-46e9-b05b-4454d7f219ce&object_id=1017",
        }), 400

    client = JiraClient(
        base_url=os.environ["JIRA_BASE_URL"],
        email=os.environ["JIRA_EMAIL"],
        api_token=os.environ["JIRA_API_TOKEN"],
    )

    result: dict = {}
    try:
        result["v1_nova_api"] = client.get_assets_object_v1(workspace_id, object_id)
        result["v1_nova_api_status"] = "OK"
    except Exception as e:
        result["v1_nova_api_status"] = "FALHOU"
        result["v1_nova_api_erro"] = str(e)

    try:
        result["legacy_insight_api"] = client.get_assets_object_legacy(object_id)
        result["legacy_insight_api_status"] = "OK"
    except Exception as e:
        result["legacy_insight_api_status"] = "FALHOU"
        result["legacy_insight_api_erro"] = str(e)

    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/debug/assets-aql")
@require_auth
def api_debug_assets_aql():
    """Debug temporário: testa a busca em lote de objetos do Assets via AQL."""
    workspace_id = request.args.get("workspace_id", "")
    aql = request.args.get("aql", 'objectType = "Contrato sustentação"')
    if not workspace_id:
        return jsonify({"error": "informe ?workspace_id=..."}), 400

    client = JiraClient(
        base_url=os.environ["JIRA_BASE_URL"],
        email=os.environ["JIRA_EMAIL"],
        api_token=os.environ["JIRA_API_TOKEN"],
    )
    try:
        objects = client.search_assets_objects_aql(workspace_id, aql)
        resp = jsonify({"status": "OK", "total_encontrado": len(objects), "objetos": objects})
    except Exception as e:
        resp = jsonify({"status": "FALHOU", "erro": str(e)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


# Inicia o refresh em background assim que o processo sobe (Gunicorn ou
# `python app.py`), para o cache já vir quente na primeira visita.
_refresher_thread = threading.Thread(target=_background_refresher, daemon=True)
_refresher_thread.start()

_insights_refresher_thread = threading.Thread(target=_background_insights_refresher, daemon=True)
_insights_refresher_thread.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
