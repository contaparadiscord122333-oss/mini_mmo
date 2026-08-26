"""
SERVIDOR - versao 1 (rede local)
--------------------------------
O que faz:
- Aceita varios jogadores a ligarem-se por TCP
- Cada jogador manda a sua posicao (x, y)
- O servidor reenvia a posicao de TODOS os jogadores para TODOS os jogadores
- Deteta quando um jogador desliga e remove-o

Como correr:
    python server.py

Isto corre na tua maquina. Para os amigos testarem na MESMA rede (wifi de
casa), eles ligam-se ao teu IP local (ex: 192.168.1.50). Para testarem pela
INTERNET, mais tarde vamos publicar isto num servidor na nuvem (Railway,
Render, etc) - por agora o objetivo e teres a logica de rede a funcionar.
"""

import socket
import threading
import json
import os

HOST = "0.0.0.0"   # aceita ligacoes de qualquer IP (necessario para a cloud)
# Servicos como Railway atribuem a porta automaticamente pela variavel PORT.
# Localmente, se essa variavel nao existir, usa-se 5555 por omissao.
PORT = int(os.environ.get("PORT", 5555))

# Guarda o estado de cada jogador ligado: { id_jogador: {"x":.., "y":.., "conn": socket} }
jogadores = {}
lock = threading.Lock()   # protege o dicionario 'jogadores' contra acessos em simultaneo
proximo_id = 1


def enviar(conn, dados: dict):
    """Envia um dicionario como JSON, terminado por \\n (para saber onde acaba a mensagem)."""
    try:
        msg = (json.dumps(dados) + "\n").encode("utf-8")
        conn.sendall(msg)
    except (BrokenPipeError, ConnectionResetError):
        pass


def transmitir_estado():
    """Manda o estado de todos os jogadores para todos os jogadores."""
    with lock:
        estado = {
            "tipo": "estado",
            "jogadores": {
                str(pid): {"x": info["x"], "y": info["y"]}
                for pid, info in jogadores.items()
            },
        }
        for info in jogadores.values():
            enviar(info["conn"], estado)


def tratar_cliente(conn, addr):
    global proximo_id

    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    with lock:
        meu_id = proximo_id
        proximo_id += 1
        jogadores[meu_id] = {"x": 100, "y": 100, "conn": conn}

    print(f"[+] Jogador {meu_id} ligou-se de {addr}")

    # diz ao cliente qual e o id dele
    enviar(conn, {"tipo": "bem_vindo", "id": meu_id})
    transmitir_estado()

    buffer = ""
    try:
        while True:
            dados = conn.recv(1024)
            if not dados:
                break  # cliente desligou

            buffer += dados.decode("utf-8")
            # pode chegar mais que uma mensagem de cada vez, ou uma mensagem partida
            while "\n" in buffer:
                linha, buffer = buffer.split("\n", 1)
                if not linha.strip():
                    continue
                msg = json.loads(linha)

                if msg.get("tipo") == "mover":
                    with lock:
                        if meu_id in jogadores:
                            jogadores[meu_id]["x"] = msg["x"]
                            jogadores[meu_id]["y"] = msg["y"]
                    transmitir_estado()

    except (ConnectionResetError, json.JSONDecodeError):
        pass
    finally:
        with lock:
            jogadores.pop(meu_id, None)
        conn.close()
        print(f"[-] Jogador {meu_id} desligou-se")
        transmitir_estado()


def main():
    servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    servidor.bind((HOST, PORT))
    servidor.listen()
    print(f"Servidor a correr em {HOST}:{PORT} (ctrl+C para parar)")

    try:
        while True:
            conn, addr = servidor.accept()
            threading.Thread(target=tratar_cliente, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\nServidor a fechar...")
    finally:
        servidor.close()


if __name__ == "__main__":
    main()
