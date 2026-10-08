"""Consumidor da fila: lê na réplica, chama a API, confere o destino e só então limpa a origem.

O log de envio (na réplica) tem uma linha por grupo e evento, e é ele que impede reenvio: depois do
sucesso a origem é limpa, mas a réplica ainda mostra o evento até o próximo ciclo de replicação. Cada
etapa que pode falhar tem a sua situação, para a próxima execução retomar do ponto certo:

- falha_api: chama a API de novo;
- falha_materializacao: a API respondeu 200 mas o destino não ficou certo; chama de novo;
- falha_limpeza: a API e o destino já estão certos; só tenta limpar a origem de novo.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from . import cliente_http

SUCESSO, FALHA_API, FALHA_MATERIALIZACAO, FALHA_LIMPEZA = (
    "sucesso", "falha_api", "falha_materializacao", "falha_limpeza")


@dataclass(frozen=True)
class Pendente:
    grupo: int
    atualizado_em: str
    situacao_anterior: str | None
    tentativas: int


@dataclass
class Resumo:
    situacoes: Counter = field(default_factory=Counter)
    detalhes: dict[int, str] = field(default_factory=dict)
    so_limpeza: int = 0
    planejados: int = 0


def pendentes(replica: sqlite3.Connection, consumidor: str, limite: int = 500,
              max_tentativas: int = 5) -> list[Pendente]:
    """O evento mais recente de cada grupo que ainda não foi concluído."""
    linhas = replica.execute("""
        WITH ultimo AS (SELECT grupo, max(atualizado_em) AS atualizado_em FROM fila GROUP BY grupo)
        SELECT u.grupo, u.atualizado_em, l.situacao, coalesce(l.tentativas, 0)
        FROM ultimo u
        LEFT JOIN envio_log l ON l.consumidor = ? AND l.grupo = u.grupo AND l.atualizado_em = u.atualizado_em
        WHERE (l.situacao IS NULL OR l.situacao <> 'sucesso') AND coalesce(l.tentativas, 0) < ?
        ORDER BY u.atualizado_em, u.grupo LIMIT ?""", [consumidor, max_tentativas, limite]).fetchall()
    return [Pendente(*l) for l in linhas]


def materializado(origem: sqlite3.Connection, destino: sqlite3.Connection, grupo: int) -> bool:
    esperado = {r[0] for r in origem.execute("SELECT item FROM grupo_item WHERE grupo = ?", [grupo])}
    no_destino = {r[0] for r in destino.execute("SELECT item FROM item WHERE grupo = ?", [grupo])}
    return esperado == no_destino


def limpar(origem: sqlite3.Connection, grupo: int, atualizado_em: str) -> int:
    """Apaga o evento enviado e os anteriores do grupo. Evento mais novo, que chegou depois, fica."""
    return origem.execute("DELETE FROM fila WHERE grupo = ? AND atualizado_em <= ?", [grupo, atualizado_em]).rowcount


def registrar(replica: sqlite3.Connection, consumidor: str, p: Pendente, situacao: str, detalhe: str = "") -> None:
    replica.execute("""
        INSERT INTO envio_log (consumidor, grupo, atualizado_em, situacao, tentativas, detalhe)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT (consumidor, grupo, atualizado_em) DO UPDATE SET situacao = excluded.situacao,
            tentativas = envio_log.tentativas + 1, detalhe = excluded.detalhe, registrado_em = datetime('now')""",
                    [consumidor, p.grupo, p.atualizado_em, situacao, detalhe])


def processar(origem: sqlite3.Connection, replica: sqlite3.Connection, destino: sqlite3.Connection, url: str,
              consumidor: str = "padrao", dry_run: bool = False, espera: float = 0.5,
              falhar_limpeza: frozenset[int] = frozenset()) -> Resumo:
    """`falhar_limpeza` simula a origem recusando o DELETE de alguns grupos (testes e demo)."""
    resumo = Resumo()
    for p in pendentes(replica, consumidor):
        if dry_run:
            resumo.planejados += 1
            continue
        if p.situacao_anterior != FALHA_LIMPEZA:
            ok, detalhe = cliente_http.enviar(url, p.grupo, p.atualizado_em, espera=espera)
            if not ok:
                _fechar(replica, consumidor, p, FALHA_API, detalhe, resumo)
                continue
            if not materializado(origem, destino, p.grupo):
                _fechar(replica, consumidor, p, FALHA_MATERIALIZACAO, "API respondeu 200 e o destino não bate", resumo)
                continue
        else:
            resumo.so_limpeza += 1
        try:
            if p.grupo in falhar_limpeza:
                raise sqlite3.OperationalError("database is locked")
            limpar(origem, p.grupo, p.atualizado_em)
        except sqlite3.Error as exc:
            _fechar(replica, consumidor, p, FALHA_LIMPEZA, str(exc), resumo)
            continue
        _fechar(replica, consumidor, p, SUCESSO, "", resumo)
    return resumo


def _fechar(replica, consumidor, p, situacao, detalhe, resumo) -> None:
    registrar(replica, consumidor, p, situacao, detalhe)
    resumo.situacoes[situacao] += 1
    if detalhe:
        resumo.detalhes[p.grupo] = detalhe
