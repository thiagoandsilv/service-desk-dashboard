# -*- coding: utf-8 -*-
"""
jira_sync.py
-------------
Busca os chamados abertos do Jira e monta a mesma estrutura de dados
usada pelo dashboard (Visão Geral + 4 faixas), pronta para servir como
JSON em /api/data.
"""
from __future__ import annotations

import datetime as dt
import os
from collections import defaultdict

from jira_client import JiraClient

BAND_ORDER = ["green", "yellow", "orange", "red"]
BAND_LABEL = {
    "green": "🟢 Até 7 dias",
    "yellow": "🟡 8 a 15 dias",
    "orange": "🟠 16 a 50 dias",
    "red": "🔴 Acima de 50 dias",
}


def _band_for(dias: int) -> str:
    if dias <= 7:
        return "green"
    if dias <= 15:
        return "yellow"
    if dias <= 50:
        return "orange"
    return "red"


def fetch_dashboard_data() -> dict:
    base_url = os.environ["JIRA_BASE_URL"]
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_API_TOKEN"]
    project = os.environ.get("JIRA_PROJECT", "SUPORTE")

    report_date = dt.datetime.now()
    client = JiraClient(base_url=base_url, email=email, api_token=token)

    rows = []
    for issue in client.fetch_open_issues(project):
        f_ = issue["fields"]
        assignee = f_.get("assignee")
        responsavel = assignee["displayName"] if assignee else "Não atribuído"
        reporter = f_.get("reporter")
        if reporter:
            solicitante = reporter.get("displayName") or reporter.get("emailAddress") or "Desconhecido"
        else:
            solicitante = "Desconhecido"

        criado = dt.datetime.strptime(f_["created"][:19], "%Y-%m-%dT%H:%M:%S")
        atualizado = dt.datetime.strptime(f_["updated"][:19], "%Y-%m-%dT%H:%M:%S")
        dias = max((report_date - criado).days, 0)
        band = _band_for(dias)

        rows.append({
            "key": issue["key"],
            "tipo": (f_.get("issuetype") or {}).get("name", ""),
            "resumo": f_.get("summary", ""),
            "status": (f_.get("status") or {}).get("name", ""),
            "prioridade": (f_.get("priority") or {}).get("name", "—"),
            "responsavel": responsavel,
            "solicitante": solicitante,
            "criado_em": criado.strftime("%d/%m/%Y"),
            "atualizado_em": atualizado.strftime("%d/%m/%Y"),
            "dias_aberto": dias,
            "band": band,
        })

    total = len(rows)
    overview_rows = []
    for b in BAND_ORDER:
        br = [r for r in rows if r["band"] == b]
        qtd = len(br)
        dias_list = [r["dias_aberto"] for r in br]
        overview_rows.append({
            "band": b, "label": BAND_LABEL[b], "qtd": qtd,
            "pct": (qtd / total) if total else 0,
            "mais_antigo": max(dias_list) if dias_list else 0,
            "mais_recente": min(dias_list) if dias_list else 0,
            "media": round(sum(dias_list) / qtd, 1) if qtd else 0,
            "sem_resp": sum(1 for r in br if r["responsavel"] == "Não atribuído"),
        })

    sem_resp_total = sum(1 for r in rows if r["responsavel"] == "Não atribuído")
    media_total = round(sum(r["dias_aberto"] for r in rows) / total, 2) if total else 0

    resp_counter = defaultdict(lambda: {"green": 0, "yellow": 0, "orange": 0, "red": 0})
    for r in rows:
        resp_counter[r["responsavel"]][r["band"]] += 1
    top_resp = []
    for name, counts in resp_counter.items():
        t = sum(counts.values())
        top_resp.append({"name": name, **counts, "total": t,
                          "pct": (t / total) if total else 0})
    top_resp.sort(key=lambda x: -x["total"])

    band_sheets = {}
    for b in BAND_ORDER:
        band_sheets[b] = sorted(
            [r for r in rows if r["band"] == b],
            key=lambda r: -r["dias_aberto"])

    return {
        "report_date": report_date.strftime("%d/%m/%Y %H:%M:%S"),
        "jira_base_url": base_url,
        "total": total,
        "overview_rows": overview_rows,
        "sem_resp_total": sem_resp_total,
        "media_total": media_total,
        "top_responsaveis": top_resp,
        "band_sheets": band_sheets,
        "band_label": BAND_LABEL,
    }
