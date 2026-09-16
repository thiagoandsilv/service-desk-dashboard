# -*- coding: utf-8 -*-
"""
insights.py
------------
Calcula um conjunto de análises "prontas pra reunião" a partir dos
chamados do período (abertos + resolvidos recentemente): padrão de
entrada por dia da semana, distribuição por faixa de idade, por tipo,
por responsável, por cliente, e um alerta de qualidade de dados sobre
o campo resolutiondate (que descobrimos estar inconsistente nesse Jira).
"""
from __future__ import annotations

import datetime as dt
import os
from collections import Counter

from jira_client import JiraClient

WEEKDAY_PT = {
    0: "Segunda", 1: "Terça", 2: "Quarta", 3: "Quinta",
    4: "Sexta", 5: "Sábado", 6: "Domingo",
}
WEEKDAY_ORDER = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]

BAND_ORDER = ["green", "yellow", "orange", "red"]
BAND_LABEL = {
    "green": "🟢 Até 7 dias", "yellow": "🟡 8 a 15 dias",
    "orange": "🟠 16 a 50 dias", "red": "🔴 Acima de 50 dias",
}


def _band_for(dias: int) -> str:
    if dias <= 7:
        return "green"
    if dias <= 15:
        return "yellow"
    if dias <= 50:
        return "orange"
    return "red"


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")


def build_insights(days: int = 30, today: dt.date | None = None) -> dict:
    base_url = os.environ["JIRA_BASE_URL"]
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_API_TOKEN"]
    project = os.environ.get("JIRA_PROJECT", "SUPORTE")

    client = JiraClient(base_url=base_url, email=email, api_token=token)

    jql = (f"project = {project} AND "
           f"(statusCategory != Done OR resolutiondate >= -{days}d)")

    issues = list(client.search(
        jql,
        fields=["created", "resolutiondate", "assignee", "issuetype",
                "status", "customfield_10002", "customfield_10400"],
        max_pages=100, page_size=100))

    today = today or dt.date.today()

    rows = []
    for issue in issues:
        f_ = issue.get("fields", {})
        created = _parse_dt(f_.get("created"))
        if created is None:
            continue
        resolved = _parse_dt(f_.get("resolutiondate"))
        assignee = (f_.get("assignee") or {}).get("displayName", "Não atribuído")
        tipo = (f_.get("issuetype") or {}).get("name", "")
        status = (f_.get("status") or {}).get("name", "")
        orgs = f_.get("customfield_10002") or []
        cliente = orgs[0].get("name", "—") if orgs else "—"
        horas = f_.get("customfield_10400") or 0
        rows.append(dict(key=issue["key"], created=created, resolved=resolved,
                          assignee=assignee, tipo=tipo, status=status,
                          cliente=cliente, horas=horas))

    total = len(rows)
    ainda_abertos = [r for r in rows if r["resolved"] is None]
    resolvidos_no_periodo = [r for r in rows if r["resolved"] is not None]

    # ---- qualidade de dados: quantos "resolvidos" (status categoria Done
    # não é conhecida aqui sem outra chamada, então usamos como proxy:
    # se resolutiondate nunca aparece preenchido, é sinal de problema de
    # processo — mostra isso mesmo quando 0 estão preenchidos ----
    com_resolutiondate = len(resolvidos_no_periodo)

    # ---- volume por dia da semana ----
    weekday_counter = Counter()
    for r in rows:
        weekday_counter[WEEKDAY_PT[r["created"].weekday()]] += 1
    weekday_volume = [{"day": d, "count": weekday_counter.get(d, 0)} for d in WEEKDAY_ORDER]

    # ---- faixa de idade (só dos ainda abertos) ----
    band_counts = {b: 0 for b in BAND_ORDER}
    for r in ainda_abertos:
        dias = (today - r["created"].date()).days
        band_counts[_band_for(dias)] += 1
    age_bands = [{"band": b, "label": BAND_LABEL[b], "qtd": band_counts[b],
                  "pct": (band_counts[b] / len(ainda_abertos)) if ainda_abertos else 0}
                 for b in BAND_ORDER]

    def _dist(field_name: str, top: int | None = None) -> list[dict]:
        counter = Counter(r[field_name] for r in rows)
        items = counter.most_common(top)
        return [{"label": k, "qtd": v, "pct": v / total if total else 0} for k, v in items]

    # ---- top clientes por HORAS consumidas (não por quantidade de
    # chamados) — soma customfield_10400 agrupado por cliente ----
    horas_por_cliente: dict[str, float] = {}
    for r in rows:
        if r["horas"]:
            horas_por_cliente[r["cliente"]] = horas_por_cliente.get(r["cliente"], 0) + r["horas"]
    total_horas = sum(horas_por_cliente.values())
    by_cliente_horas = [
        {"label": cliente, "horas": round(horas, 1),
         "pct": (horas / total_horas) if total_horas else 0}
        for cliente, horas in sorted(horas_por_cliente.items(), key=lambda kv: -kv[1])[:8]
    ]

    return {
        "generated_at": dt.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "period_days": days,
        "total_period": total,
        "total_ainda_abertos": len(ainda_abertos),
        "data_quality": {
            "total_periodo": total,
            "com_resolutiondate": com_resolutiondate,
            "pct_com_resolutiondate": (com_resolutiondate / total) if total else 0,
        },
        "weekday_volume": weekday_volume,
        "age_bands": age_bands,
        "by_tipo": _dist("tipo"),
        "by_responsavel": _dist("assignee"),
        "by_cliente": _dist("cliente", top=8),
        "by_cliente_horas": by_cliente_horas,
        "total_horas_periodo": round(total_horas, 1),
        "by_status": _dist("status"),
    }
