# fila-para-api

[![testes](https://github.com/BrunoMaia23/fila-para-api/actions/workflows/testes.yml/badge.svg)](https://github.com/BrunoMaia23/fila-para-api/actions/workflows/testes.yml)

Uma trigger no banco grava numa tabela de fila cada grupo que mudou, e um processo precisa avisar uma API
para ela refazer aquele grupo. Ler, chamar e apagar parece pouco código, e é, até a primeira vez que a
API responde 200 sem gravar nada, ou que a réplica ainda mostra o evento que você acabou de apagar na
origem. Fiz esse consumidor no trabalho; este repositório tem o mesmo desenho, escrito do zero, com
SQLite fazendo o papel dos bancos e uma API falsa em HTTP local.

*In English: a queue-table-to-API relay that never resends and never loses a newer event. Reads from a
lagging replica, deduplicates per group, retries only what can succeed, checks that the API actually
materialized the data, cleans the source only up to the event it sent, and keeps a per-step status so the
next run resumes at the right point. Synthetic data, local fake API.*

## O que pode dar errado, e o que o consumidor faz

| Situação | O que acontece |
|---|---|
| O mesmo grupo tem vários eventos na fila | Vai um envio só, com o evento mais recente |
| A réplica ainda mostra o evento já enviado e apagado na origem | O log de envio barra o reenvio |
| Um evento novo do grupo chega enquanto o envio está no ar | A limpeza apaga só até o evento enviado; o novo fica para a próxima |
| A API devolve 500 ou não responde | Tenta de novo com espera crescente; se não der, fica `falha_api` |
| A API devolve 400 | Não adianta repetir na hora: `falha_api` direto |
| A API responde 200 mas não grava | O consumidor confere o destino; fica `falha_materializacao` e a origem não é limpa |
| A limpeza da origem falha | Fica `falha_limpeza`; a próxima execução só refaz a limpeza, sem chamar a API de novo |
| Um grupo falha sempre | Depois de 5 tentativas sai da fila de trabalho, para não travar o resto |
| Um sucesso antigo some do destino | O reparo reenvia os sucessos recentes que não batem mais com o destino |

O log de envio (`envio_log`) tem uma linha por grupo e evento, com a situação e o número de tentativas.
É ele que diz de onde a próxima execução continua.

## A demo

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m fila demo
```

A demo sobe a API falsa numa porta local e programa as falhas: o grupo 3 fica fora do ar nas três
tentativas, o grupo 5 recebe 200 sem gravar, a origem recusa a limpeza do grupo 8, e alguém altera o
grupo 10 bem no meio do envio dele.

```
[origem]      12 grupos e 20 eventos na fila; a réplica vê 20
[envio 1]     12 grupos: 9 concluídos | falha na API 1 | sem materializar 1 | falha na limpeza 1
                grupo 3: HTTP 500 em 3 tentativas
                grupo 5: API respondeu 200 e o destino não bate
                grupo 8: database is locked
                grupo 10: chegou um evento novo durante o envio; a limpeza apagou só até o enviado
[envio 2]     3 grupos: 3 concluídos | 1 só refez a limpeza
                sem replicar: a réplica ainda mostra os eventos já enviados, e reenvios indevidos = 0
[replicação]  a réplica agora vê 1 evento (o novo do grupo 10)
[envio 3]     1 grupo: 1 concluído
[final]       fila da origem com 0 eventos | 12 de 12 grupos certos no destino | grupos chamados mais de uma vez: 3:4, 5:2, 10:2
[reparo]      o grupo 2 perdeu os itens no destino; reenviado e conferido
```

O grupo 3 aparece com 4 chamadas (3 na primeira execução, 1 na segunda). O 8 não aparece porque foi
chamado uma vez só: na segunda execução ele só refez a limpeza.

## No projeto real

A fila nasce no Oracle, e o consumidor lê de uma réplica em PostgreSQL. A API é a de um sistema interno,
e o processo roda como uma DAG do Airflow a cada poucos minutos, com um modo de simulação ligado por
variável de ambiente. A conferência do destino compara os itens que a API gravou com os que o grupo
deveria ter, que é a mesma ideia do `materializado()` daqui.

## Testes

`pytest` sobe a API falsa para cada teste e cobre: um envio por grupo com o evento mais recente, réplica
atrasada sem reenvio, evento novo preservado, 500 com nova tentativa e 400 sem, 200 sem gravar, falha na
limpeza sem nova chamada, desistência depois do máximo de tentativas, simulação sem efeito e o reparo.
