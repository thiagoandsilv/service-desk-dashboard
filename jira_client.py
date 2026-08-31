# -*- coding: utf-8 -*-
"""
jira_client.py
---------------
Busca chamados no Jira Cloud (API REST v3).

Autenticação: e-mail + API Token do Jira Cloud
  → Gerar em: https://id.atlassian.com/manage-profile/security/api-tokens

Uso típico (ver jira_sync.py):
    client = JiraClient(base_url, email, api_token)
    issues = client.fetch_open_issues(project_key="SUPORTE")
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterator

import requests

FIELDS = [
    "summary", "issuetype", "status", "priority",
    "assignee", "reporter", "created", "updated",
]


class JiraAuthError(RuntimeError):
    pass


@dataclass
class JiraClient:
    base_url: str          # ex: https://suaempresa.atlassian.net
    email: str
    api_token: str
    timeout: int = 30

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._session = requests.Session()
        self._session.auth = (self.email, self.api_token)
        self._session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------
    def fetch_open_issues(self, project_key: str,
                           extra_jql: str = "") -> Iterator[dict[str, Any]]:
        """Gera (yield) cada issue em aberto do projeto, paginando
        automaticamente via nextPageToken (API de busca atual do Jira Cloud).
        """
        jql = f"project = {project_key} AND resolution = Unresolved"
        if extra_jql:
            jql += f" AND {extra_jql}"
        jql += " ORDER BY created ASC"

        url = f"{self.base_url}/rest/api/3/search/jql"
        payload = {
            "jql": jql,
            "maxResults": 100,
            "fields": FIELDS,
        }

        next_token = None
        while True:
            body = dict(payload)
            if next_token:
                body["nextPageToken"] = next_token
            resp = self._session.post(url, json=body, timeout=self.timeout)
            if resp.status_code == 401:
                raise JiraAuthError(
                    "Falha de autenticação (401). Confira e-mail e API token.")
            resp.raise_for_status()
            data = resp.json()
            for issue in data.get("issues", []):
                yield issue
            next_token = data.get("nextPageToken")
            if not next_token or data.get("isLast", not next_token):
                break


    # ------------------------------------------------------------------
    def fetch_changelog(self, issue_key: str) -> list[dict[str, Any]]:
        """Busca o histórico completo (changelog) de um chamado, paginando
        se necessário. Cada história tem 'created' (timestamp) e 'items'
        (lista de mudanças de campo, ex: assignee, status).
        """
        histories: list[dict[str, Any]] = []
        start_at = 0
        page_size = 100
        while True:
            url = f"{self.base_url}/rest/api/3/issue/{issue_key}/changelog"
            resp = self._session.get(
                url, params={"startAt": start_at, "maxResults": page_size},
                timeout=self.timeout)
            if resp.status_code == 401:
                raise JiraAuthError(
                    "Falha de autenticação (401). Confira e-mail e API token.")
            resp.raise_for_status()
            data = resp.json()
            histories.extend(data.get("values", []))
            start_at += page_size
            if data.get("isLast", True) or start_at >= data.get("total", 0):
                break
        return histories


def _parse_jira_datetime(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now()
    # Jira retorna algo como "2026-07-09T14:32:10.000-0300"
    return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")


@dataclass
class ResponseTimeMetrics:
    """Métricas de tempo calculadas a partir do changelog de um chamado."""
    tempo_ate_atribuir: dt.timedelta | None       # criado -> 1ª atribuição de responsável
    tempo_ate_trocar_responsavel: dt.timedelta | None  # 1ª atribuição -> 1ª TROCA de responsável
    tempo_ate_finalizar: dt.timedelta | None       # criado -> resolutiondate (ou 1ª entrada em status "Done")
    data_1a_atribuicao: dt.datetime | None
    data_1a_troca_responsavel: dt.datetime | None
    data_finalizacao: dt.datetime | None


def compute_response_time_metrics(created: dt.datetime,
                                   resolutiondate: str | None,
                                   histories: list[dict[str, Any]]) -> ResponseTimeMetrics:
    """A partir do changelog (histories, mais recente primeiro ou não —
    tratamos ambos), calcula:
      - tempo até o chamado ser atribuído pela 1ª vez a alguém
      - tempo até o responsável ser TROCADO pela 1ª vez (reatribuição)
      - tempo até finalizar (resolutiondate, com fallback no changelog de status)
    """
    assignee_events = []  # (timestamp, from, to)
    status_done_events = []  # timestamp de transições para categoria "done"

    for h in histories:
        h_created = _parse_jira_datetime(h.get("created"))
        for item in h.get("items", []):
            if item.get("field") == "assignee":
                assignee_events.append((h_created, item.get("from"), item.get("to")))
            if item.get("field") == "status":
                # heurística: nomes de status finais comuns em service desk
                to_str = (item.get("toString") or "").strip().lower()
                if to_str in ("fechado", "resolvido", "concluído", "concluido",
                              "cancelado", "done", "closed", "resolved"):
                    status_done_events.append(h_created)

    assignee_events.sort(key=lambda e: e[0])

    # sequência de "para quem foi atribuído" (ignora eventos de
    # desatribuição, onde to é None), na ordem em que aconteceram
    atribuicoes = [(ts, to) for ts, frm, to in assignee_events if to is not None]

    data_1a_atribuicao = atribuicoes[0][0] if atribuicoes else None
    data_1a_troca = None
    if len(atribuicoes) >= 2:
        primeiro_responsavel = atribuicoes[0][1]
        for ts, to in atribuicoes[1:]:
            if to != primeiro_responsavel:
                data_1a_troca = ts
                break

    if resolutiondate:
        data_final = _parse_jira_datetime(resolutiondate)
    elif status_done_events:
        data_final = min(status_done_events)
    else:
        data_final = None

    return ResponseTimeMetrics(
        tempo_ate_atribuir=(data_1a_atribuicao - created) if data_1a_atribuicao else None,
        tempo_ate_trocar_responsavel=(data_1a_troca - data_1a_atribuicao)
            if (data_1a_troca and data_1a_atribuicao) else None,
        tempo_ate_finalizar=(data_final - created) if data_final else None,
        data_1a_atribuicao=data_1a_atribuicao,
        data_1a_troca_responsavel=data_1a_troca,
        data_finalizacao=data_final,
    )
