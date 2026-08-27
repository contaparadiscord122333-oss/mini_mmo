"""
SERVIDOR - versao 3 (monstros + combate basico + inventario)
--------------------------------------------------------------
Novidades desta etapa em relacao a etapa 2:
- Monstros: cada mapa pode ter uma lista de monstros, com vida propria e
  movimento de patrulha simples (andam de um lado para o outro). O servidor
  e' o "juiz": e' ele que sabe a posicao/vida real de cada monstro, e manda
  isso aos clientes para eles so desenharem.
- Combate: o cliente pede para atacar um monstro (tipo "atacar"). O servidor
  confirma que o jogador esta perto o suficiente e nao atacou ha pouco tempo
  (cooldown), e so ai aplica dano. Isto evita batota (um cliente modificado
  nao pode simplesmente dizer "matei o monstro").
- Contacto com monstros: se um jogador tocar num monstro, leva dano (com
  cooldown tambem). Se a vida chegar a 0, o jogador "renasce" no spawn do
  mapa com a vida cheia.
- Inventario: continua a viver no servidor (lista de ids de itens). Agora
  tambem guardamos qual a arma equipada, que define o dano de ataque.

Como correr:
    python server.py
"""

import socket
import threading
import json
import os
import time
import math

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8080))

MOEDAS_INICIAIS = 100
VIDA_INICIAL = 100

# --- Catalogo de armas --------------------------------------------------
# Cada arma vendida nas lojas tem aqui o dano que da' quando equipada.
# Sem nenhuma arma equipada, o jogador ataca aos "punhos" (dano baixo).
DANO_PUNHOS = 4
ARMAS = {
    "espada_curta": {"nome": "Espada Curta", "dano": 10},
    "espada_longa": {"nome": "Espada Longa", "dano": 16},
    "espada_de_aco": {"nome": "Espada de Aco", "dano": 24},
    "espada_flamejante": {"nome": "Espada Flamejante", "dano": 38},
}

# NPCs fixos, organizados por mapa.
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

# Ponto de "renascimento" de cada mapa, para quando um jogador morre.
MAPA_SPAWN = {
    "arena": (100, 100),
    "missao1": (60, 270),
}

# --- Monstros ------------------------------------------------------------
# "patrulha" = quantos pixeis para cada lado do ponto inicial o monstro
# anda (0 = fica parado no sitio). Isto e' apenas o "molde"; o estado real
# (vida atual, se esta vivo, posicao atual) vive em MONSTROS_ESTADO.
MONSTROS_POR_MAPA = {
    "arena": [],
    "missao1": [
        {"id": "slime_1", "tipo": "slime", "x": 250, "y": 150, "vida_max": 30, "dano": 6, "moedas": 5, "patrulha": 50},
        {"id": "slime_2", "tipo": "slime", "x": 420, "y": 380, "vida_max": 30, "dano": 6, "moedas": 5, "patrulha": 50},
        {"id": "morcego_1", "tipo": "morcego", "x": 550, "y": 220, "vida_max": 18, "dano": 4, "moedas": 3, "patrulha": 0},
        {"id": "orc_1", "tipo": "orc", "x": 700, "y": 300, "vida_max": 60, "dano": 12, "moedas": 15, "patrulha": 70},
    ],
}

VELOCIDADE_MONSTRO = 1.4        # pixeis por "tick" do loop de monstros
INTERVALO_TICK = 0.15           # segundos entre atualizacoes de monstros
RESPAWN_MONSTRO_SEG = 15.0      # quanto tempo ate um monstro morto reaparecer
ALCANCE_ATAQUE = 70             # distancia maxima para o ataque do jogador acertar
COOLDOWN_ATAQUE = 0.45          # segundos minimos entre ataques do jogador
RAIO_CONTATO = 26               # distancia para um monstro "tocar" no jogador
COOLDOWN_DANO_CONTATO = 0.9     # segundos minimos entre dois danos de contacto


def construir_monstros_estado():
    """Cria o dicionario de estado (runtime) de todos os monstros, a partir
    dos moldes acima. Cada monstro passa a ter: posicao atual, vida atual,
    se esta vivo, e dados para a patrulha (direcao e limites)."""
    estado = {}
    for mapa, lista in MONSTROS_POR_MAPA.items():
        estado[mapa] = {}
        for molde in lista:
            estado[mapa][molde["id"]] = {
                **molde,
                "vida": molde["vida_max"],
                "vivo": True,
                "spawn_x": molde["x"],
                "spawn_y": molde["y"],
                "dir": 1,
                "hora_morte": None,
            }
    return estado


MONSTROS_ESTADO = construir_monstros_estado()


def encontrar_item_loja(npc_id, item_id):
    for lista_npcs in NPCS_POR_MAPA.values():
        for npc in lista_npcs:
            if npc["id"] == npc_id and npc.get("loja"):
                for item in npc["itens"]:
                    if item["id"] == item_id:
                        return item
    return None


def info_inventario(info):
    """Monta a lista de itens do inventario (com nome e dano) para mandar
    ao cliente, mais qual esta equipada neste momento."""
    itens = []
    for item_id in info["inventario"]:
        arma = ARMAS.get(item_id, {"nome": item_id, "dano": 0})
        itens.append({"id": item_id, "nome": arma["nome"], "dano": arma["dano"]})
    return {"tipo": "inventario", "itens": itens, "equipada": info["arma_equipada"]}


# Guarda o estado de cada jogador ligado
jogadores = {}
lock = threading.Lock()
proximo_id = 1


def enviar(conn, dados: dict):
    try:
        msg = (json.dumps(dados) + "\n").encode("utf-8")
        conn.sendall(msg)
    except (BrokenPipeError, ConnectionResetError):
        pass


def transmitir_estado():
    """
    Manda a cada jogador: os outros jogadores do mesmo mapa, os monstros
    vivos do mesmo mapa, e o seu proprio estado pessoal (vida/moedas), que
    so ele precisa de saber.
    """
    with lock:
        por_mapa = {}
        for pid, info in jogadores.items():
            por_mapa.setdefault(info["mapa"], {})[str(pid)] = {"x": info["x"], "y": info["y"]}

        monstros_por_mapa_vivos = {}
        for mapa, monstros in MONSTROS_ESTADO.items():
            monstros_por_mapa_vivos[mapa] = {
                mid: {"x": m["x"], "y": m["y"], "tipo": m["tipo"], "vida": m["vida"], "vida_max": m["vida_max"]}
                for mid, m in monstros.items() if m["vivo"]
            }

        for pid, info in jogadores.items():
            estado = {
                "tipo": "estado",
                "mapa": info["mapa"],
                "jogadores": por_mapa.get(info["mapa"], {}),
                "monstros": monstros_por_mapa_vivos.get(info["mapa"], {}),
                "eu": {"vida": info["vida"], "vida_max": info["vida_max"], "moedas": info["moedas"]},
            }
            enviar(info["conn"], estado)


def loop_monstros():
    """
    Corre para sempre numa thread separada:
    - move os monstros que tem patrulha (andam de um lado para o outro)
    - faz "respawn" dos que ja estao mortos ha tempo suficiente
    - aplica dano de contacto a jogadores que estejam em cima de um monstro
    - transmite o novo estado a todos
    """
    while True:
        time.sleep(INTERVALO_TICK)
        agora = time.time()
        houve_mudanca = False

        with lock:
            for mapa, monstros in MONSTROS_ESTADO.items():
                for m in monstros.values():
                    if not m["vivo"]:
                        if m["hora_morte"] is not None and (agora - m["hora_morte"]) >= RESPAWN_MONSTRO_SEG:
                            m["vivo"] = True
                            m["vida"] = m["vida_max"]
                            m["x"], m["y"] = m["spawn_x"], m["spawn_y"]
                            m["hora_morte"] = None
                            houve_mudanca = True
                        continue

                    # patrulha simples: anda para um lado ate ao limite, depois inverte
                    if m["patrulha"] > 0:
                        m["x"] += VELOCIDADE_MONSTRO * m["dir"]
                        if m["x"] >= m["spawn_x"] + m["patrulha"]:
                            m["dir"] = -1
                        elif m["x"] <= m["spawn_x"] - m["patrulha"]:
                            m["dir"] = 1
                        houve_mudanca = True

                    # dano de contacto a jogadores do mesmo mapa
                    for info in jogadores.values():
                        if info["mapa"] != mapa:
                            continue
                        dist = math.hypot(info["x"] - m["x"], info["y"] - m["y"])
                        if dist < RAIO_CONTATO and (agora - info.get("ultimo_dano", 0)) >= COOLDOWN_DANO_CONTATO:
                            info["ultimo_dano"] = agora
                            info["vida"] -= m["dano"]
                            houve_mudanca = True
                            if info["vida"] <= 0:
                                info["vida"] = info["vida_max"]
                                spawn = MAPA_SPAWN.get(info["mapa"], (100, 100))
                                info["x"], info["y"] = spawn
                                enviar(info["conn"], {
                                    "tipo": "respawn", "x": spawn[0], "y": spawn[1], "vida": info["vida"],
                                })

        if houve_mudanca:
            transmitir_estado()


def tratar_cliente(conn, addr):
    global proximo_id

    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    with lock:
        meu_id = proximo_id
        proximo_id += 1
        jogadores[meu_id] = {
            "x": 100, "y": 100, "mapa": "arena",
            "moedas": MOEDAS_INICIAIS, "conn": conn,
            # Todo o jogador comeca ja com a Espada Curta (a espada que
            # "apanha" na tela de introducao do cliente), em vez de comecar
            # aos punhos. Continua a poder trocar de arma na loja/inventario
            # como antes.
            "inventario": ["espada_curta"],
            "arma_equipada": "espada_curta",
            "vida": VIDA_INICIAL, "vida_max": VIDA_INICIAL,
            "ultimo_ataque": 0.0,
            "ultimo_dano": 0.0,
        }
        info_local = jogadores[meu_id]

    print(f"[+] Jogador {meu_id} ligou-se de {addr}")

    enviar(conn, {"tipo": "bem_vindo", "id": meu_id, "moedas": MOEDAS_INICIAIS, "vida": VIDA_INICIAL})
    enviar(conn, {"tipo": "npcs", "npcs": NPCS_POR_MAPA.get("arena", [])})
    enviar(conn, info_inventario(info_local))
    transmitir_estado()

    buffer = ""
    try:
        while True:
            dados = conn.recv(1024)
            if not dados:
                break

            buffer += dados.decode("utf-8")
            while "\n" in buffer:
                linha, buffer = buffer.split("\n", 1)
                if not linha.strip():
                    continue
                msg = json.loads(linha)
                tipo = msg.get("tipo")

                if tipo == "mover":
                    with lock:
                        if meu_id in jogadores:
                            jogadores[meu_id]["x"] = msg["x"]
                            jogadores[meu_id]["y"] = msg["y"]
                    transmitir_estado()

                elif tipo == "mudar_mapa":
                    novo_mapa = msg["mapa"]
                    with lock:
                        if meu_id in jogadores:
                            jogadores[meu_id]["mapa"] = novo_mapa
                            jogadores[meu_id]["x"] = msg["x"]
                            jogadores[meu_id]["y"] = msg["y"]
                    enviar(conn, {"tipo": "npcs", "npcs": NPCS_POR_MAPA.get(novo_mapa, [])})
                    transmitir_estado()

                elif tipo == "comprar":
                    npc_id = msg.get("npc_id")
                    item_id = msg.get("item_id")
                    item = encontrar_item_loja(npc_id, item_id)

                    resposta = {"tipo": "compra_resultado", "item_id": item_id}
                    if item is None:
                        resposta["sucesso"] = False
                        resposta["mensagem"] = "Esse item ja nao existe."
                        enviar(conn, resposta)
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
                            if resposta["sucesso"]:
                                enviar(conn, info_inventario(info))

                elif tipo == "equipar":
                    item_id = msg.get("item_id")
                    with lock:
                        info = jogadores.get(meu_id)
                        if info is None:
                            continue
                        # None = "desequipar" (voltar a lutar aos punhos)
                        if item_id is None or item_id in info["inventario"]:
                            info["arma_equipada"] = item_id
                            enviar(conn, info_inventario(info))

                elif tipo == "atacar":
                    monstro_id = msg.get("monstro_id")
                    agora = time.time()
                    with lock:
                        info = jogadores.get(meu_id)
                        if info is None:
                            continue
                        monstro = MONSTROS_ESTADO.get(info["mapa"], {}).get(monstro_id)

                        pode_atacar = (
                            monstro is not None and monstro["vivo"]
                            and (agora - info["ultimo_ataque"]) >= COOLDOWN_ATAQUE
                        )
                        if pode_atacar:
                            dist = math.hypot(info["x"] - monstro["x"], info["y"] - monstro["y"])
                            pode_atacar = dist <= ALCANCE_ATAQUE

                        if pode_atacar:
                            info["ultimo_ataque"] = agora
                            arma = ARMAS.get(info["arma_equipada"])
                            dano = arma["dano"] if arma else DANO_PUNHOS
                            monstro["vida"] -= dano

                            morreu = monstro["vida"] <= 0
                            if morreu:
                                monstro["vivo"] = False
                                monstro["vida"] = 0
                                monstro["hora_morte"] = agora
                                info["moedas"] += monstro["moedas"]

                            enviar(conn, {
                                "tipo": "acerto",
                                "monstro_id": monstro_id,
                                "dano": dano,
                                "morreu": morreu,
                                "moedas": info["moedas"],
                            })
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

    threading.Thread(target=loop_monstros, daemon=True).start()

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
