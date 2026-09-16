# -*- coding: utf-8 -*-
"""
contracts.py
-------------
Busca todos os "Contrato sustentação" no Jira Assets e extrai a
franquia de horas contratada, horas consumidas e horas restantes de
cada um — dados que a própria automação da Accerte já mantém
atualizados nesses objetos, sem precisar recalcular nada aqui.
"""
from __future__ import annotations

import datetime as dt
import os

from jira_client import JiraClient

# IDs dos atributos do tipo de objeto "Contrato sustentação" (typeId=11,
# schema "Esquema Geral Accerte") — descobertos inspecionando objetos
# reais. Configuráveis via env var caso o schema mude.
ATTR_FRANQUIA = os.environ.get("ASSETS_ATTR_FRANQUIA", "138")
ATTR_CONSUMIDAS = os.environ.get("ASSETS_ATTR_CONSUMIDAS", "143")
ATTR_RESTANTES = os.environ.get("ASSETS_ATTR_RESTANTES", "144")
ATTR_ATIVO = os.environ.get("ASSETS_ATTR_ATIVO", "140")


def _attr_value(obj: dict, attr_id: str) -> str | None:
    for attr in obj.get("attributes", []):
        if str(attr.get("objectTypeAttributeId")) == str(attr_id):
            values = attr.get("objectAttributeValues", [])
            if values:
                return values[0].get("value")
    return None


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cliente_from_label(label: str) -> str:
    # Os nomes seguem o padrão "CLIENTE - DESCRIÇÃO DO CONTRATO - N HORAS"
    return (label or "").split(" - ")[0].strip() or label


def build_contracts(workspace_id: str | None = None) -> dict:
    base_url = os.environ["JIRA_BASE_URL"]
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_API_TOKEN"]
    # workspace do Jira Assets da Accerte — pode ser sobrescrito via env
    # var ASSETS_WORKSPACE_ID se um dia mudar de conta/instância.
    workspace_id = workspace_id or os.environ.get(
        "ASSETS_WORKSPACE_ID", "67cc6f9e-3376-46e9-b05b-4454d7f219ce")

    client = JiraClient(base_url=base_url, email=email, api_token=token)

    objects = client.search_assets_objects_aql(
        workspace_id, 'objectType = "Contrato sustentação"')

    rows = []
    for obj in objects:
        label = obj.get("label") or obj.get("name") or ""
        ativo = _attr_value(obj, ATTR_ATIVO)
        franquia = _to_float(_attr_value(obj, ATTR_FRANQUIA)) or 0.0
        consumidas = _to_float(_attr_value(obj, ATTR_CONSUMIDAS)) or 0.0
        restantes_raw = _to_float(_attr_value(obj, ATTR_RESTANTES))
        restantes = restantes_raw if restantes_raw is not None else (franquia - consumidas)
        pct = (consumidas / franquia) if franquia > 0 else None

        rows.append({
            "objectId": obj.get("id"),
            "cliente": _cliente_from_label(label),
            "contrato": label,
            "ativo": ativo,
            "franquia": franquia,
            "consumidas": consumidas,
            "restantes": restantes,
            "pct_consumido": pct,
            "estourado": restantes is not None and restantes < 0,
        })

    # só contratos ativos (ou sem info de "ativo" — mantém por segurança)
    rows_ativos = [r for r in rows if r["ativo"] != "Não"]

    # ordena: primeiro quem estourou (mais negativo), depois por % consumido desc
    def sort_key(r):
        if r["estourado"]:
            return (0, r["restantes"])
        return (1, -(r["pct_consumido"] or 0))
    rows_ativos.sort(key=sort_key)

    total_franquia = sum(r["franquia"] for r in rows_ativos)
    total_consumidas = sum(r["consumidas"] for r in rows_ativos)
    n_estourados = sum(1 for r in rows_ativos if r["estourado"])

    return {
        "generated_at": dt.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "total_contratos": len(rows_ativos),
        "total_franquia": round(total_franquia, 1),
        "total_consumidas": round(total_consumidas, 1),
        "n_estourados": n_estourados,
        "contratos": rows_ativos,
    }
