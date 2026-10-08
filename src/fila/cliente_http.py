"""Chamada à API com nova tentativa só para o que pode dar certo na segunda vez."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


def enviar(url: str, grupo: int, atualizado_em: str, tentativas: int = 3, espera: float = 0.5) -> tuple[bool, str]:
    """(deu certo, detalhe). Erro 4xx não se repete; 5xx e falta de conexão esperam e tentam de novo."""
    corpo = json.dumps({"atualizado_em": atualizado_em}).encode("utf-8")
    ultimo = ""
    for tentativa in range(1, tentativas + 1):
        pedido = urllib.request.Request(f"{url}/grupos/{grupo}/carga", data=corpo, method="POST",
                                        headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(pedido, timeout=10) as resposta:
                return True, f"HTTP {resposta.status}"
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                return False, f"HTTP {exc.code}, sem nova tentativa"
            ultimo = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            ultimo = f"sem conexão ({exc.reason})"
        if tentativa < tentativas:
            time.sleep(espera * 2 ** (tentativa - 1))
    return False, f"{ultimo} em {tentativas} tentativas"
