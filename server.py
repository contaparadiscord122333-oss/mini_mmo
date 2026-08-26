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
        {
            "id": "ferreiro",
            "nome": "Ferreiro",
            "x": 110, "y": 430,
            # NPCs com "loja" nao usam dialogo normal: ao falares com eles
            # o cliente abre diretamente a interface de compra (ver "itens").
            "loja": True,
            "falas": [],
            "itens": [
                {"id": "espada_curta", "nome": "Espada Curta", "preco": 15},
                {"id": "espada_longa", "nome": "Espada Longa", "preco": 35},
                {"id": "espada_de_aco", "nome": "Espada de Aco", "preco": 60},
                {"id": "espada_flamejante", "nome": "Espada Flamejante", "preco": 120},
            ],
        },
    ],
    "missao1": [],
}


def encontrar_item_loja(npc_id, item_id):
    """Procura, em qualquer mapa, um NPC-loja com este id e devolve o item
    pedido (dict) ou None se o NPC/item nao existir ou o NPC nao for loja."""
    for lista_npcs in NPCS_POR_MAPA.values():
        for npc in lista_npcs:
            if npc["id"] == npc_id and npc.get("loja"):
                for item in npc["itens"]:
                    if item["id"] == item_id:
                        return item
    return None

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
            "inventario": [],
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

                elif msg.get("tipo") == "comprar":
                    # o jogador tentou comprar um item na loja de um NPC
                    npc_id = msg.get("npc_id")
                    item_id = msg.get("item_id")
                    item = encontrar_item_loja(npc_id, item_id)

                    resposta = {"tipo": "compra_resultado", "item_id": item_id}
                    if item is None:
                        resposta["sucesso"] = False
                        resposta["mensagem"] = "Esse item ja nao existe."
                    else:
                        with lock:
                            info = jogadores.get(meu_id)
                            if info is None:
                                continue
                            if info["moedas"] >= item["preco"]:
                                info["moedas"] -= item["preco"]
                                info["inventario"].append(item_id)
                                resposta["sucesso"] = True
                                resposta["mensagem"] = f"Compraste: {item['nome']}!"
                                resposta["moedas"] = info["moedas"]
                            else:
                                resposta["sucesso"] = False
                                resposta["mensagem"] = "Moedas insuficientes."
                    enviar(conn, resposta)

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
