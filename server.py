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

# --- Catalogo de armaduras ------------------------------------------------
# "defesa" e' quanto reduz do dano de contacto que os monstros dao (dano
# minimo continua a ser 1, para uma armadura nunca tornar o jogador
# totalmente imune). Vendidas pelo Armeiro, guardadas no mesmo inventario
# das armas (o inventario agora tem os dois "slots": arma e armadura).
ARMADURAS = {
    "armadura_couro": {"nome": "Armadura de Couro", "defesa": 4},
    "armadura_ferro": {"nome": "Armadura de Ferro", "defesa": 9},
    "armadura_placas": {"nome": "Armadura de Placas", "defesa": 15},
}

# --- Sistema de niveis ------------------------------------------------
# Cada monstro morto da experiencia (xp). Ao juntar xp suficiente, o
# jogador sobe de nivel: fica com mais vida maxima (e cura tudo) e ganha
# um pequeno bonus de dano fixo, que soma ao dano da arma equipada.
XP_BASE_POR_NIVEL = 25      # nivel 1->2 precisa de 25xp, 2->3 de 50xp, etc.
VIDA_BONUS_POR_NIVEL = 15
DANO_BONUS_POR_NIVEL = 1    # por nivel acima do 1


def xp_necessario(nivel):
    return XP_BASE_POR_NIVEL * nivel


def ganhar_xp(info, quantidade):
    """Soma xp ao jogador e sobe de nivel quantas vezes for preciso.
    Sobe de nivel = mais vida maxima + cura completa + bonus de dano."""
    if quantidade <= 0:
        return
    info["xp"] += quantidade
    while info["xp"] >= xp_necessario(info["nivel"]):
        info["xp"] -= xp_necessario(info["nivel"])
        info["nivel"] += 1
        info["vida_max"] += VIDA_BONUS_POR_NIVEL
        info["vida"] = info["vida_max"]


def bonus_dano_nivel(info):
    return (info["nivel"] - 1) * DANO_BONUS_POR_NIVEL


# --- Missao de cacada ---------------------------------------------------
# Missao simples e repetivel: derrotar N monstros de um certo tipo. Os
# "bichos redondos" da missao sao os slimes.
MISSAO_TIPO_ALVO = "slime"
MISSAO_QTD_ALVO = 5
MISSAO_RECOMPENSA_MOEDAS = 40
MISSAO_RECOMPENSA_XP = 30

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
        {
            "id": "armeiro",
            "nome": "Armeiro",
            "x": 480, "y": 130,
            "loja": True,
            "falas": [],
            "itens": [
                {"id": "armadura_couro", "nome": "Armadura de Couro", "preco": 20},
                {"id": "armadura_ferro", "nome": "Armadura de Ferro", "preco": 55},
                {"id": "armadura_placas", "nome": "Armadura de Placas", "preco": 100},
            ],
        },
    ],
    "missao1": [
        {
            "id": "cacador",
            "nome": "Cacador",
            "x": 60, "y": 150,
            "missao": True,
            "falas": [],
        },
    ],
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
        {"id": "slime_1", "tipo": "slime", "x": 250, "y": 150, "vida_max": 30, "dano": 6, "moedas": 5, "xp": 8, "patrulha": 50},
        {"id": "slime_2", "tipo": "slime", "x": 420, "y": 380, "vida_max": 30, "dano": 6, "moedas": 5, "xp": 8, "patrulha": 50},
        {"id": "slime_3", "tipo": "slime", "x": 130, "y": 380, "vida_max": 30, "dano": 6, "moedas": 5, "xp": 8, "patrulha": 40},
        {"id": "morcego_1", "tipo": "morcego", "x": 550, "y": 220, "vida_max": 18, "dano": 4, "moedas": 3, "xp": 5, "patrulha": 0},
        {"id": "orc_1", "tipo": "orc", "x": 700, "y": 300, "vida_max": 60, "dano": 12, "moedas": 15, "xp": 25, "patrulha": 70},
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
    """Monta a lista de itens do inventario (armas E armaduras) para
    mandar ao cliente, cada um com o seu "tipo" (arma/armadura) e um
    "valor" (dano ou defesa, conforme o tipo), mais qual arma e qual
    armadura estao equipadas neste momento."""
    itens = []
    for item_id in info["inventario"]:
        if item_id in ARMADURAS:
            dados = ARMADURAS[item_id]
            itens.append({"id": item_id, "nome": dados["nome"], "tipo": "armadura", "valor": dados["defesa"]})
        else:
            dados = ARMAS.get(item_id, {"nome": item_id, "dano": 0})
            itens.append({"id": item_id, "nome": dados["nome"], "tipo": "arma", "valor": dados["dano"]})
    return {
        "tipo": "inventario",
        "itens": itens,
        "equipada": info["arma_equipada"],
        "armadura_equipada": info.get("armadura_equipada"),
    }


def defesa_atual(info):
    armadura = ARMADURAS.get(info.get("armadura_equipada"))
    return armadura["defesa"] if armadura else 0


def falas_missao(info):
    """Constroi a conversa do Cacador consoante o estado da missao deste
    jogador: ainda nao aceite, em curso, pronta a entregar (da' a
    recompensa nesse momento), ou recem-entregue."""
    if info.get("missao_completa"):
        info["missao_completa"] = False
        info["missao_ativa"] = False
        info["missao_progresso"] = 0
        info["missao_entregas"] = info.get("missao_entregas", 0) + 1
        info["moedas"] += MISSAO_RECOMPENSA_MOEDAS
        ganhar_xp(info, MISSAO_RECOMPENSA_XP)
        return [
            "Boa! Trouxeste provas de que limpaste aqueles bichos redondos.",
            f"Toma a tua recompensa: {MISSAO_RECOMPENSA_MOEDAS} moedas e {MISSAO_RECOMPENSA_XP} de experiencia.",
            "Se quiseres, ha sempre mais slimes onde foste buscar esses.",
        ]

    if not info.get("missao_ativa"):
        info["missao_ativa"] = True
        info["missao_progresso"] = 0
        return [
            "Preciso de ajuda com uma praga de bichos redondos por aqui.",
            f"Derrota {MISSAO_QTD_ALVO} slimes e traz-me a noticia.",
            f"Pago bem: {MISSAO_RECOMPENSA_MOEDAS} moedas e {MISSAO_RECOMPENSA_XP} de experiencia.",
        ]

    faltam = max(0, MISSAO_QTD_ALVO - info.get("missao_progresso", 0))
    if faltam > 0:
        return [
            f"Ja derrotaste {info.get('missao_progresso', 0)} de {MISSAO_QTD_ALVO} slimes.",
            f"Faltam {faltam}. Continua a cacar e volta quando acabares.",
        ]
    return ["Ja acabaste? Fala comigo outra vez para receberes a recompensa."]


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
                "eu": {
                    "vida": info["vida"], "vida_max": info["vida_max"], "moedas": info["moedas"],
                    "nivel": info["nivel"], "xp": info["xp"], "xp_prox": xp_necessario(info["nivel"]),
                },
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
                            dano_sofrido = max(1, m["dano"] - defesa_atual(info))
                            info["vida"] -= dano_sofrido
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
            "armadura_equipada": None,
            "vida": VIDA_INICIAL, "vida_max": VIDA_INICIAL,
            "ultimo_ataque": 0.0,
            "ultimo_dano": 0.0,
            "nivel": 1,
            "xp": 0,
            "missao_ativa": False,
            "missao_progresso": 0,
            "missao_completa": False,
            "missao_entregas": 0,
        }
        info_local = jogadores[meu_id]

    print(f"[+] Jogador {meu_id} ligou-se de {addr}")

    enviar(conn, {
        "tipo": "bem_vindo", "id": meu_id, "moedas": MOEDAS_INICIAIS, "vida": VIDA_INICIAL,
        "nivel": 1, "xp": 0, "xp_prox": xp_necessario(1),
    })
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
                    slot = msg.get("slot", "arma")
                    with lock:
                        info = jogadores.get(meu_id)
                        if info is None:
                            continue
                        # None = "desequipar" esse slot
                        if item_id is None or item_id in info["inventario"]:
                            if slot == "armadura":
                                info["armadura_equipada"] = item_id
                            else:
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
                            dano_base = arma["dano"] if arma else DANO_PUNHOS
                            dano = dano_base + bonus_dano_nivel(info)
                            monstro["vida"] -= dano

                            morreu = monstro["vida"] <= 0
                            subiu_nivel = False
                            if morreu:
                                monstro["vivo"] = False
                                monstro["vida"] = 0
                                monstro["hora_morte"] = agora
                                info["moedas"] += monstro["moedas"]

                                nivel_antes = info["nivel"]
                                ganhar_xp(info, monstro.get("xp", 0))
                                subiu_nivel = info["nivel"] > nivel_antes

                                # progresso da missao de cacada (so conta
                                # se a missao estiver ativa e ainda nao
                                # tiver sido concluida)
                                if (
                                    monstro["tipo"] == MISSAO_TIPO_ALVO
                                    and info.get("missao_ativa")
                                    and not info.get("missao_completa")
                                ):
                                    info["missao_progresso"] = info.get("missao_progresso", 0) + 1
                                    if info["missao_progresso"] >= MISSAO_QTD_ALVO:
                                        info["missao_progresso"] = MISSAO_QTD_ALVO
                                        info["missao_completa"] = True

                            enviar(conn, {
                                "tipo": "acerto",
                                "monstro_id": monstro_id,
                                "dano": dano,
                                "morreu": morreu,
                                "moedas": info["moedas"],
                                "subiu_nivel": subiu_nivel,
                                "nivel": info["nivel"],
                            })
                    transmitir_estado()

                elif tipo == "consultar_missao":
                    npc_id = msg.get("npc_id")
                    with lock:
                        info = jogadores.get(meu_id)
                        if info is None:
                            continue
                        falas = falas_missao(info)
                        enviar(conn, {
                            "tipo": "missao_dialogo",
                            "npc_id": npc_id,
                            "nome": "Cacador",
                            "falas": falas,
                            "moedas": info["moedas"],
                        })
                        enviar(conn, info_inventario(info))
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
