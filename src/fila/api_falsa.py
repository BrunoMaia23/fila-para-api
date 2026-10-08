"""Uma API de verdade, em HTTP local, para o consumidor conversar.

POST /grupos/<n>/carga materializa no destino os itens do grupo, lidos da origem. Dá para programar
respostas por grupo: "500" (erro do servidor), "400" (pedido recusado), "sem_itens" (responde 200 mas
não materializa nada) e "ok". Cada chamada consome a próxima resposta programada.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ApiFalsa:
    def __init__(self, origem: sqlite3.Connection, destino: sqlite3.Connection):
        self.origem, self.destino = origem, destino
        self.roteiro: dict[int, deque] = defaultdict(deque)
        self.chamadas: dict[int, int] = defaultdict(int)
        self.ao_receber = None  # gancho: roda antes de responder (ex.: chega um evento novo no meio)
        self.trava = threading.Lock()
        api = self

        class Tratador(BaseHTTPRequestHandler):
            def do_POST(self):
                partes = self.path.strip("/").split("/")
                if len(partes) != 3 or partes[0] != "grupos" or partes[2] != "carga" or not partes[1].isdigit():
                    return self._responder(404, {"erro": "rota desconhecida"})
                corpo = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                status, resposta = api.atender(int(partes[1]), corpo)
                self._responder(status, resposta)

            def _responder(self, status, corpo):
                dados = json.dumps(corpo).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(dados)))
                self.end_headers()
                self.wfile.write(dados)

            def log_message(self, *args):
                pass

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Tratador)
        self.url = f"http://127.0.0.1:{self.servidor.server_address[1]}"
        self._thread = threading.Thread(target=self.servidor.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.servidor.shutdown()
        self.servidor.server_close()

    def programar(self, grupo: int, *respostas: str) -> None:
        self.roteiro[grupo].extend(respostas)

    def atender(self, grupo: int, corpo: dict) -> tuple[int, dict]:
        with self.trava:
            self.chamadas[grupo] += 1
            if self.ao_receber:
                self.ao_receber(grupo)
            acao = self.roteiro[grupo].popleft() if self.roteiro[grupo] else "ok"
            if acao == "500":
                return 500, {"erro": "falha interna"}
            if acao == "400":
                return 400, {"erro": "grupo inválido"}
            if acao == "ok":
                itens = [r[0] for r in self.origem.execute("SELECT item FROM grupo_item WHERE grupo = ?", [grupo])]
                self.destino.execute("BEGIN")
                self.destino.execute("DELETE FROM item WHERE grupo = ?", [grupo])
                self.destino.executemany("INSERT INTO item VALUES (?, ?)", [(grupo, i) for i in itens])
                self.destino.execute("COMMIT")
            return 200, {"grupo": grupo, "recebido_em": corpo.get("atualizado_em")}
