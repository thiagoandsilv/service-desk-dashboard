# -*- coding: utf-8 -*-
"""
history_store.py
-----------------
Guarda um "retrato" diário do backlog (total de chamados abertos e a
distribuição por faixa) num banco Redis externo (Render Key Value),
para sobreviver a deploys e reinícios do serviço web — que tem disco
temporário.

Se a variável de ambiente REDIS_URL não estiver configurada, todas as
funções aqui viram no-op silencioso: o dashboard principal continua
funcionando normalmente, só a aba de Tendência fica vazia.
"""
from __future__ import annotations

import datetime as dt
import json
import os

try:
    import redis
except ImportError:  # pragma: no cover - só acontece se esquecerem de instalar
    redis = None

_client = None
_client_checked = False

SNAPSHOT_PREFIX = "snapshot:"
INDEX_KEY = "snapshot:index"


def _get_client():
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True
    url = os.environ.get("REDIS_URL")
    if not url or redis is None:
        _client = None
        return None
    try:
        c = redis.from_url(url, decode_responses=True, socket_connect_timeout=5)
        c.ping()
        _client = c
    except Exception:
        _client = None
    return _client


def record_snapshot(dashboard_data: dict, today: dt.date | None = None) -> None:
    """Grava (ou atualiza) o snapshot de HOJE com os números atuais do
    backlog. Chamado toda vez que o cache principal é atualizado, então
    o snapshot do dia vai sendo refinado ao longo do dia e "congela"
    no último valor observado quando a data virar.

    O parâmetro `today` existe principalmente para facilitar testes
    (injetar uma data fixa); em produção sempre usa a data real.
    """
    client = _get_client()
    if client is None:
        return

    today = today or dt.date.today()
    date_str = today.isoformat()

    counts = {b["band"]: b["qtd"] for b in dashboard_data.get("overview_rows", [])}

    # abertos_hoje: soma só as 4 faixas reais (green/yellow/orange/red),
    # sem contar "n1" — que é uma visão que repete chamados já contados
    # nas faixas, pra não duplicar.
    abertos_hoje = 0
    for band in ("green", "yellow", "orange", "red"):
        for r in dashboard_data.get("band_sheets", {}).get(band, []):
            criado_data = (r.get("criado_em") or "").split(" ")[0]  # "DD/MM/YYYY"
            if criado_data:
                try:
                    d, m, y = criado_data.split("/")
                    if dt.date(int(y), int(m), int(d)) == today:
                        abertos_hoje += 1
                except ValueError:
                    pass

    snapshot = {
        "date": date_str,
        "total": dashboard_data.get("total", 0),
        "green": counts.get("green", 0),
        "yellow": counts.get("yellow", 0),
        "orange": counts.get("orange", 0),
        "red": counts.get("red", 0),
        "abertos_hoje": abertos_hoje,
        "updated_at": dt.datetime.now().strftime("%H:%M:%S"),
    }

    try:
        client.set(f"{SNAPSHOT_PREFIX}{date_str}", json.dumps(snapshot))
        client.zadd(INDEX_KEY, {date_str: today.toordinal()})
    except Exception:
        pass


def get_history(days: int = 30) -> list[dict]:
    """Retorna os últimos N snapshots diários, em ordem cronológica
    (mais antigo primeiro) — pronto pra virar um gráfico de linha.
    """
    client = _get_client()
    if client is None:
        return []
    try:
        date_strs = client.zrevrange(INDEX_KEY, 0, days - 1)
        if not date_strs:
            return []
        keys = [f"{SNAPSHOT_PREFIX}{d}" for d in date_strs]
        raw_values = client.mget(keys)
        snapshots = [json.loads(v) for v in raw_values if v]
        snapshots.sort(key=lambda s: s["date"])
        return snapshots
    except Exception:
        return []


def is_enabled() -> bool:
    return _get_client() is not None
