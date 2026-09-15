# -*- coding: utf-8 -*-
"""
backfill.py
------------
Reconstrói snapshots de dias PASSADOS a partir do histórico que o Jira
já guarda (data de criação e de resolução de cada chamado) — sem
precisar ter capturado nada em tempo real naqueles dias.

Lógica: um chamado estava "aberto" no fim do dia D se:
  criado <= D  E  (não tem resolutiondate OU resolutiondate > D)

Busca-se então: todo chamado ainda aberto hoje (regardless de quando
foi criado) + todo chamado resolvido nos últimos N dias (pode ter sido
criado há muito tempo, mas ainda contava no backlog em dias recentes).
"""
from __future__ import annotations

import datetime as dt
import os

from jira_client import JiraClient
import history_store

BAND_ORDER = ["green", "yellow", "orange", "red"]


def _band_for(dias: int) -> str:
    if dias <= 7:
        return "green"
    if dias <= 15:
        return "yellow"
    if dias <= 50:
        return "orange"
    return "red"


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").date()


def compute_daily_backlog(issues: list[dict], days: int, today: dt.date | None = None
                           ) -> list[dict]:
    """A partir de uma lista de issues (com campos created/resolutiondate),
    calcula o snapshot de cada um dos últimos `days` dias ANTERIORES a
    hoje (não inclui hoje — esse já é coberto pelo tracking em tempo real).
    Retorna em ordem cronológica (mais antigo primeiro).
    """
    today = today or dt.date.today()

    parsed = []
    for issue in issues:
        f_ = issue.get("fields", {})
        created = _parse_date(f_.get("created"))
        if created is None:
            continue
        resolved = _parse_date(f_.get("resolutiondate"))
        parsed.append((created, resolved))

    results = []
    for offset in range(days, 0, -1):  # days ... 1 (não inclui 0 = hoje)
        day = today - dt.timedelta(days=offset)
        counts = {"green": 0, "yellow": 0, "orange": 0, "red": 0}
        abertos_no_dia = 0
        for created, resolved in parsed:
            if created > day:
                continue
            if resolved is not None and resolved <= day:
                continue
            dias_aberto = (day - created).days
            counts[_band_for(dias_aberto)] += 1
            if created == day:
                abertos_no_dia += 1

        total = sum(counts.values())
        results.append({
            "date": day.isoformat(),
            "total": total,
            "green": counts["green"],
            "yellow": counts["yellow"],
            "orange": counts["orange"],
            "red": counts["red"],
            "abertos_hoje": abertos_no_dia,
            "updated_at": "backfill",
            "source": "backfill",
        })
    return results


def run_backfill(days: int = 30, today: dt.date | None = None) -> dict:
    """Busca no Jira tudo que é necessário e grava os snapshots dos
    últimos `days` dias (exceto hoje) no histórico. Retorna um resumo.

    O parâmetro `today` existe pra facilitar testes; em produção
    sempre usa a data real.
    """
    base_url = os.environ["JIRA_BASE_URL"]
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_API_TOKEN"]
    project = os.environ.get("JIRA_PROJECT", "SUPORTE")

    client = JiraClient(base_url=base_url, email=email, api_token=token)

    jql = (f"project = {project} AND "
           f"(statusCategory != Done OR resolutiondate >= -{days}d)")

    issues = list(client.search(
        jql, fields=["created", "resolutiondate"], max_pages=200, page_size=100))

    daily_snapshots = compute_daily_backlog(issues, days=days, today=today)


    written = 0
    for snap in daily_snapshots:
        if history_store._store_snapshot(snap):
            written += 1

    return {
        "issues_analisados": len(issues),
        "dias_calculados": len(daily_snapshots),
        "dias_gravados": written,
        "periodo": {
            "de": daily_snapshots[0]["date"] if daily_snapshots else None,
            "ate": daily_snapshots[-1]["date"] if daily_snapshots else None,
        },
    }
