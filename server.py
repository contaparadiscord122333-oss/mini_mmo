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

HOST = "0.0.0.0"   # aceita ligacoes de qualquer IP na rede
# Servicos como Railway atribuem a porta automaticamente pela variavel PORT.
PORT = int(os.environ.get("PORT", 8080))

MOEDAS_INICIAIS = 100

# NPCs fixos, organizados por mapa. Cada jogador so recebe os NPCs do mapa
# onde esta - por isso reenviamos esta lista sempre que o jogador muda de
# mapa, e nao so uma vez ao ligar.
NPCS_POR_MAPA = {
    "arena": [
        {
            "id": "guarda_1",
            "nome": "Guarda da Arena",
            "x": 300, "y": 230,
            "falas": [
                "Alto la! Isto e a Arena Central.",
                "So os corajosos se atrevem a entrar.",
                "Ha uma missao a leste, se procuras aventura.",
            ],
        },
        {
            "id": "guarda_2",
            "nome": "Guarda da Arena",
            "x": 660, "y": 230,
            "falas": [
                "Esta arena ja viu muitas batalhas.",
                "Cuidado com as poças de agua, dizem que trazem azar.",
            ],
        },
    ],
    "missao1": [
        {
            "id": "ferreiro",
            "nome": "Ferreiro",
            "x": 150, "y": 200,
            "falas": [
                "Bem-vindo a minha forja, aventureiro.",
                "Ainda nao tenho armas para vender - isso vem na proxima etapa.",
            ],
        },
    ],
}

# Guarda o estado de cada jogador ligado:
# { id_jogador: {"x":.., "y":.., "mapa":.., "moedas":.., "conn": socket} }
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
    """
    Manda o estado dos jogadores para todos os jogadores, mas cada um so
    recebe os jogadores que estao no MESMO mapa que ele (senao verias gente
    "invisivel" ligada mas noutra sala).
    """
    with lock:
        # agrupa jogadores por mapa, uma vez, para nao repetir trabalho por cada envio
        por_mapa = {}
        for pid, info in jogadores.items():
            por_mapa.setdefault(info["mapa"], {})[str(pid)] = {"x": info["x"], "y": info["y"]}

        for info in jogadores.values():
            estado = {
                "tipo": "estado",
                "mapa": info["mapa"],
                "jogadores": por_mapa.get(info["mapa"], {}),
            }
            enviar(info["conn"], estado)


def tratar_cliente(conn, addr):
    global proximo_id

    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    with lock:
        meu_id = proximo_id
        proximo_id += 1
        jogadores[meu_id] = {
            "x": 100, "y": 100, "mapa": "arena",
            "moedas": MOEDAS_INICIAIS, "conn": conn,
        }

    print(f"[+] Jogador {meu_id} ligou-se de {addr}")

    # diz ao cliente qual e o id dele e quantas moedas tem
    enviar(conn, {"tipo": "bem_vindo", "id": meu_id, "moedas": MOEDAS_INICIAIS})
    # manda os NPCs do mapa onde comeca (arena)
    enviar(conn, {"tipo": "npcs", "npcs": NPCS_POR_MAPA.get("arena", [])})
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

                elif msg.get("tipo") == "mudar_mapa":
                    # o jogador atravessou um portal: muda de mapa e reaparece
                    # na posicao de spawn que o cliente indicou
                    novo_mapa = msg["mapa"]
                    with lock:
                        if meu_id in jogadores:
                            jogadores[meu_id]["mapa"] = novo_mapa
                            jogadores[meu_id]["x"] = msg["x"]
                            jogadores[meu_id]["y"] = msg["y"]
                    # manda os NPCs do mapa novo (cada mapa tem os seus)
                    enviar(conn, {"tipo": "npcs", "npcs": NPCS_POR_MAPA.get(novo_mapa, [])})
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
