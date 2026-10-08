import pytest

from fila import bancos, consumidor, reparo
from fila.api_falsa import ApiFalsa


@pytest.fixture
def ambiente(tmp_path):
    origem = bancos.conectar(tmp_path / "origem.sqlite", bancos.ORIGEM)
    replica = bancos.conectar(tmp_path / "replica.sqlite", bancos.REPLICA)
    destino = bancos.conectar(tmp_path / "destino.sqlite", bancos.DESTINO)
    origem.executemany("INSERT INTO grupo_item VALUES (?, ?)",
                       [(g, f"i{g}-{n}") for g in (1, 2, 3) for n in (1, 2)])
    with ApiFalsa(origem, destino) as api:
        yield origem, replica, destino, api
    for c in (origem, replica, destino):
        c.close()


def eventos(origem, replica, *pares):
    origem.executemany("INSERT INTO fila VALUES (?, ?)", pares)
    bancos.replicar(origem, replica)


def rodar(origem, replica, destino, api, **kw):
    return consumidor.processar(origem, replica, destino, api.url, espera=0, **kw)


def fila(origem):
    return sorted(origem.execute("SELECT grupo, atualizado_em FROM fila").fetchall())


def test_um_envio_por_grupo_com_o_evento_mais_recente(ambiente):
    origem, replica, destino, api = ambiente
    eventos(origem, replica, (1, "10:00"), (1, "10:05"), (1, "10:02"), (2, "10:01"))
    r = rodar(origem, replica, destino, api)
    assert r.situacoes[consumidor.SUCESSO] == 2 and dict(api.chamadas) == {1: 1, 2: 1}
    assert fila(origem) == []


def test_replica_atrasada_nao_causa_reenvio(ambiente):
    origem, replica, destino, api = ambiente
    eventos(origem, replica, (1, "10:00"))
    rodar(origem, replica, destino, api)
    assert replica.execute("SELECT count(*) FROM fila").fetchone()[0] == 1   # ainda não replicou a limpeza
    rodar(origem, replica, destino, api)
    assert api.chamadas[1] == 1


def test_evento_que_chega_durante_o_envio_fica_para_a_proxima(ambiente):
    origem, replica, destino, api = ambiente
    eventos(origem, replica, (1, "10:00"))
    api.ao_receber = lambda g: origem.execute("INSERT INTO fila VALUES (1, '10:30')") if api.chamadas[1] == 1 else None
    rodar(origem, replica, destino, api)
    assert fila(origem) == [(1, "10:30")]
    bancos.replicar(origem, replica)
    rodar(origem, replica, destino, api)
    assert fila(origem) == [] and api.chamadas[1] == 2


def test_500_tenta_de_novo_e_400_nao(ambiente):
    origem, replica, destino, api = ambiente
    eventos(origem, replica, (1, "10:00"), (2, "10:00"))
    api.programar(1, "500", "ok")
    api.programar(2, "400")
    r = rodar(origem, replica, destino, api)
    assert api.chamadas[1] == 2 and api.chamadas[2] == 1
    assert r.situacoes == {consumidor.SUCESSO: 1, consumidor.FALHA_API: 1}
    assert "400" in r.detalhes[2]
    assert fila(origem) == [(2, "10:00")]          # o que falhou continua na fila


def test_api_diz_200_sem_gravar_e_a_origem_nao_e_limpa(ambiente):
    origem, replica, destino, api = ambiente
    eventos(origem, replica, (3, "10:00"))
    api.programar(3, "sem_itens")
    r = rodar(origem, replica, destino, api)
    assert r.situacoes[consumidor.FALHA_MATERIALIZACAO] == 1 and fila(origem) == [(3, "10:00")]
    rodar(origem, replica, destino, api)
    assert fila(origem) == [] and consumidor.materializado(origem, destino, 3)


def test_falha_na_limpeza_nao_chama_a_api_de_novo(ambiente):
    origem, replica, destino, api = ambiente
    eventos(origem, replica, (1, "10:00"))
    r = rodar(origem, replica, destino, api, falhar_limpeza=frozenset({1}))
    assert r.situacoes[consumidor.FALHA_LIMPEZA] == 1 and fila(origem) == [(1, "10:00")]
    r = rodar(origem, replica, destino, api)
    assert r.so_limpeza == 1 and api.chamadas[1] == 1 and fila(origem) == []


def test_desiste_depois_do_maximo_de_tentativas(ambiente):
    origem, replica, destino, api = ambiente
    eventos(origem, replica, (1, "10:00"))
    api.programar(1, *["400"] * 10)
    for _ in range(7):
        rodar(origem, replica, destino, api)
    assert api.chamadas[1] == 5
    assert consumidor.pendentes(replica, "padrao") == []


def test_dry_run_nao_chama_nem_limpa(ambiente):
    origem, replica, destino, api = ambiente
    eventos(origem, replica, (1, "10:00"), (2, "10:00"))
    r = rodar(origem, replica, destino, api, dry_run=True)
    assert r.planejados == 2 and not api.chamadas and len(fila(origem)) == 2


def test_reparo_reenvia_sucesso_que_sumiu_do_destino(ambiente):
    origem, replica, destino, api = ambiente
    eventos(origem, replica, (1, "10:00"), (2, "10:00"))
    rodar(origem, replica, destino, api)
    destino.execute("DELETE FROM item WHERE grupo = 2")
    assert reparo.reparar(origem, replica, destino, api.url, espera=0) == {2: True}
    assert api.chamadas[2] == 2 and api.chamadas[1] == 1
