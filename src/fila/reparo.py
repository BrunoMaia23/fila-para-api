"""Reparo: sucessos recentes cujo grupo não está mais certo no destino ganham um novo envio."""
from __future__ import annotations

import sqlite3

from . import cliente_http
from .consumidor import materializado


def reparar(origem: sqlite3.Connection, replica: sqlite3.Connection, destino: sqlite3.Connection, url: str,
            consumidor: str = "padrao", dias: int = 7, espera: float = 0.5) -> dict[int, bool]:
    """{grupo: reparado?} para cada grupo com sucesso nos últimos `dias` e destino diferente do esperado."""
    candidatos = replica.execute("""
        SELECT grupo, max(atualizado_em) FROM envio_log
        WHERE consumidor = ? AND situacao = 'sucesso' AND registrado_em >= datetime('now', ?)
        GROUP BY grupo ORDER BY grupo""", [consumidor, f"-{dias} days"]).fetchall()
    resultado = {}
    for grupo, atualizado_em in candidatos:
        if materializado(origem, destino, grupo):
            continue
        ok, _ = cliente_http.enviar(url, grupo, atualizado_em, espera=espera)
        resultado[grupo] = ok and materializado(origem, destino, grupo)
    return resultado
