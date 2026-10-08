"""Os três bancos da história, em SQLite.

- origem: onde a fila nasce (uma trigger grava um evento cada vez que um grupo muda) e onde ficam os
  itens de cada grupo. A limpeza da fila é feita aqui.
- réplica: cópia da origem que o consumidor lê, atualizada por replicação com atraso. Também guarda
  o log de envio do consumidor.
- destino: o que a API materializa a partir de cada chamada.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ORIGEM = """
CREATE TABLE IF NOT EXISTS fila (grupo INTEGER NOT NULL, atualizado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grupo_item (grupo INTEGER NOT NULL, item TEXT NOT NULL, PRIMARY KEY (grupo, item));
"""
REPLICA = """
CREATE TABLE IF NOT EXISTS fila (grupo INTEGER NOT NULL, atualizado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS envio_log (
    consumidor TEXT NOT NULL, grupo INTEGER NOT NULL, atualizado_em TEXT NOT NULL,
    situacao TEXT NOT NULL, tentativas INTEGER NOT NULL, detalhe TEXT,
    registrado_em TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (consumidor, grupo, atualizado_em));
"""
DESTINO = """
CREATE TABLE IF NOT EXISTS item (grupo INTEGER NOT NULL, item TEXT NOT NULL, PRIMARY KEY (grupo, item));
"""


def conectar(caminho: Path | str, esquema: str) -> sqlite3.Connection:
    con = sqlite3.connect(caminho, isolation_level=None, check_same_thread=False)
    con.executescript(esquema)
    return con


def replicar(origem: sqlite3.Connection, replica: sqlite3.Connection) -> int:
    """Um ciclo de replicação: a réplica passa a ver a fila como ela está agora na origem."""
    linhas = origem.execute("SELECT grupo, atualizado_em FROM fila").fetchall()
    replica.execute("BEGIN")
    replica.execute("DELETE FROM fila")
    replica.executemany("INSERT INTO fila VALUES (?, ?)", linhas)
    replica.execute("COMMIT")
    return len(linhas)
