"""Demo do repasse fila -> API, com falhas de propósito em cada etapa."""
from __future__ import annotations

import argparse
import random
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import bancos, consumidor, reparo
from .api_falsa import ApiFalsa

MARCADOR = ".fila-demo"
INICIO = datetime(2026, 3, 26, 14, 0, 0)


def semear(origem, grupos: int = 12, semente: int = 4) -> int:
    """Itens de cada grupo e de 1 a 3 eventos por grupo na fila (a trigger da origem faria isso)."""
    rng = random.Random(semente)
    origem.execute("BEGIN")
    eventos = 0
    for g in range(1, grupos + 1):
        origem.executemany("INSERT INTO grupo_item VALUES (?, ?)",
                           [(g, f"item-{g}-{i}") for i in range(1, rng.randint(3, 6) + 1)])
        for _ in range(rng.randint(1, 3)):
            momento = INICIO + timedelta(seconds=rng.randint(0, 3600))
            origem.execute("INSERT INTO fila VALUES (?, ?)", [g, momento.isoformat(sep=" ")])
            eventos += 1
    origem.execute("COMMIT")
    return eventos


def _n(qtd: int, singular: str, plural: str) -> str:
    return f"{qtd} {singular if qtd == 1 else plural}"


def _linha(rotulo: str, r: consumidor.Resumo) -> None:
    s = r.situacoes
    partes = [_n(s[consumidor.SUCESSO], "concluído", "concluídos")]
    if r.so_limpeza:
        partes.append(f"{_n(r.so_limpeza, 'só refez', 'só refizeram')} a limpeza")
    for situacao, nome in ((consumidor.FALHA_API, "falha na API"),
                           (consumidor.FALHA_MATERIALIZACAO, "sem materializar"),
                           (consumidor.FALHA_LIMPEZA, "falha na limpeza")):
        if s[situacao]:
            partes.append(f"{nome} {s[situacao]}")
    print(f"{rotulo:<13} {_n(sum(s.values()), 'grupo', 'grupos')}: " + " | ".join(partes))
    for g, d in sorted(r.detalhes.items()):
        print(f"{'':<13}   grupo {g}: {d}")


def demo(base: Path) -> int:
    if base.exists():
        if not (base / MARCADOR).exists():
            print(f"A pasta {base} já existe e não foi criada pela demo; escolha outra com --base.")
            return 2
        shutil.rmtree(base)
    base.mkdir(parents=True)
    (base / MARCADOR).write_text("pasta da demo da fila\n", encoding="utf-8")
    origem = bancos.conectar(base / "origem.sqlite", bancos.ORIGEM)
    replica = bancos.conectar(base / "replica.sqlite", bancos.REPLICA)
    destino = bancos.conectar(base / "destino.sqlite", bancos.DESTINO)
    eventos = semear(origem)
    print(f"[origem]      12 grupos e {eventos} eventos na fila; a réplica vê {bancos.replicar(origem, replica)}")

    with ApiFalsa(origem, destino) as api:
        api.programar(3, "500", "500", "500")   # fora do ar durante as três tentativas
        api.programar(5, "sem_itens")           # responde 200 e não grava nada
        evento_novo = {}

        def chega_evento_novo(grupo):
            if grupo == 10 and not evento_novo:  # alguém altera o grupo 10 enquanto ele é enviado
                evento_novo[10] = (INICIO + timedelta(hours=2)).isoformat(sep=" ")
                origem.execute("INSERT INTO fila VALUES (10, ?)", [evento_novo[10]])

        api.ao_receber = chega_evento_novo
        r1 = consumidor.processar(origem, replica, destino, api.url, espera=0.01, falhar_limpeza=frozenset({8}))
        _linha("[envio 1]", r1)
        print(f"{'':<13}   grupo 10: chegou um evento novo durante o envio; a limpeza apagou só até o enviado")

        antes = dict(api.chamadas)
        r2 = consumidor.processar(origem, replica, destino, api.url, espera=0.01)
        reenviados = [g for g in antes if api.chamadas[g] > antes[g] and g not in (3, 5)]
        _linha("[envio 2]", r2)
        print(f"{'':<13}   sem replicar: a réplica ainda mostra os eventos já enviados, e reenvios indevidos = "
              f"{len(reenviados)}")

        print(f"[replicação]  a réplica agora vê {bancos.replicar(origem, replica)} evento (o novo do grupo 10)")
        r3 = consumidor.processar(origem, replica, destino, api.url, espera=0.01)
        _linha("[envio 3]", r3)

        fila = origem.execute("SELECT count(*) FROM fila").fetchone()[0]
        certos = sum(consumidor.materializado(origem, destino, g) for g in range(1, 13))
        chamadas = ", ".join(f"{g}:{n}" for g, n in sorted(api.chamadas.items()) if n > 1)
        print(f"[final]       fila da origem com {fila} eventos | {certos} de 12 grupos certos no destino | "
              f"grupos chamados mais de uma vez: {chamadas}")

        destino.execute("DELETE FROM item WHERE grupo = 2")
        reparados = reparo.reparar(origem, replica, destino, api.url, espera=0.01)
        situacao = "reenviado e conferido" if reparados == {2: True} else f"falhou: {reparados}"
        print(f"[reparo]      o grupo 2 perdeu os itens no destino; {situacao}")
    ok = (fila == 0 and certos == 12 and not reenviados and api.chamadas[8] == 1 and reparados == {2: True})
    for con in (origem, replica, destino):
        con.close()
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="fila", description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True)
    p = sub.add_parser("demo", help="roda o cenário completo com uma API local")
    p.add_argument("--base", type=Path, default=Path("demo"))
    a = parser.parse_args(argv)
    return demo(a.base)
