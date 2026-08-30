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

import bd

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
        info["pontos_habilidade"] = info.get("pontos_habilidade", 0) + PONTOS_HABILIDADE_POR_NIVEL


def bonus_dano_nivel(info):
    return (info["nivel"] - 1) * DANO_BONUS_POR_NIVEL


# --- Arvore de habilidades (Ataque / Agilidade) --------------------------
# Cada no custa "pontos de habilidade" (ganhos ao subir de nivel) para
# desbloquear, e pode exigir que outro(s) no(s) ja estejam desbloqueados
# ("requisitos" = lista de ids, TODOS tem de estar desbloqueados). O
# "efeito" soma-se ao de todos os outros nos desbloqueados: "dano" e' um
# bonus fixo que se junta ao dano de ataque; "velocidade_pct" e' uma
# percentagem extra de velocidade de movimento (o servidor manda o total
# ao cliente, que e' quem de facto move o jogador no ecra).
#
# Esta e' a fonte da verdade: o servidor e' quem decide se um jogador pode
# desbloquear um no (pontos suficientes + requisitos cumpridos), tal como
# decide o resto do combate. O cliente so pede e desenha.
PONTOS_HABILIDADE_POR_NIVEL = 1

ARVORE_HABILIDADES = {
    "fundamentos": {
        "nome": "Fundamentos de Combate", "custo": 0, "requisitos": [],
        "ramo": "geral", "efeito": {},
    },
    "ataque_1": {
        "nome": "Golpe Firme", "custo": 1, "requisitos": ["fundamentos"],
        "ramo": "ataque", "efeito": {"dano": 3},
    },
    "agilidade_1": {
        "nome": "Passo Leve", "custo": 1, "requisitos": ["fundamentos"],
        "ramo": "agilidade", "efeito": {"velocidade_pct": 10},
    },
    "ataque_2": {
        "nome": "Golpe Pesado", "custo": 1, "requisitos": ["ataque_1"],
        "ramo": "ataque", "efeito": {"dano": 5},
    },
    "furia_selvagem": {
        "nome": "Furia Selvagem", "custo": 2, "requisitos": ["ataque_1", "agilidade_1"],
        "ramo": "geral", "efeito": {"dano": 6, "velocidade_pct": 6},
    },
    "agilidade_2": {
        "nome": "Corrida", "custo": 1, "requisitos": ["agilidade_1"],
        "ramo": "agilidade", "efeito": {"velocidade_pct": 12},
    },
    "combo_mortal": {
        "nome": "Combo Mortal", "custo": 2, "requisitos": ["ataque_2"],
        "ramo": "ataque", "efeito": {"dano": 10},
    },
    "vento_fugaz": {
        "nome": "Vento Fugaz", "custo": 2, "requisitos": ["agilidade_2"],
        "ramo": "agilidade", "efeito": {"velocidade_pct": 18},
    },
    "mestre_combate": {
        "nome": "Mestre do Combate", "custo": 3, "requisitos": ["combo_mortal", "vento_fugaz"],
        "ramo": "geral", "efeito": {"dano": 15, "velocidade_pct": 20},
    },
}


def bonus_dano_habilidades(info):
    total = 0
    for hab_id in info.get("habilidades", []):
        no = ARVORE_HABILIDADES.get(hab_id)
        if no:
            total += no["efeito"].get("dano", 0)
    return total


def bonus_velocidade_pct_habilidades(info):
    total = 0
    for hab_id in info.get("habilidades", []):
        no = ARVORE_HABILIDADES.get(hab_id)
        if no:
            total += no["efeito"].get("velocidade_pct", 0)
    return total


def info_habilidades(info):
    return {
        "tipo": "habilidades",
        "desbloqueadas": list(info.get("habilidades", [])),
        "pontos": info.get("pontos_habilidade", 0),
    }


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


# Guarda o estado de cada jogador ligado (chave = id da base de dados)
jogadores = {}
lock = threading.Lock()

COMPRIMENTO_MAX_NOME = 16
COMPRIMENTO_MIN_SENHA = 4
COMPRIMENTO_MAX_CHAT = 140
COOLDOWN_CHAT = 0.5         # segundos minimos entre mensagens de chat da mesma pessoa
INTERVALO_AUTOSAVE = 20.0   # segundos entre gravacoes automaticas na base de dados

# --- Staff / dono do jogo -------------------------------------------------
# O jogador com este nome (login) e' reconhecido automaticamente como "Owner"
# e ganha acesso aos comandos de staff (escritos no chat, a comecar por "/").
# A comparacao ignora maiusculas/minusculas, para nao depender de escreveres
# o nome sempre exatamente igual.
NOME_OWNER = "EuGhoooost"

# Lista de comandos disponiveis, so' para referencia rapida no /ajuda.
COMANDOS_STAFF = [
    ("/ajuda", "Mostra esta lista de comandos."),
    ("/anunciar <texto>", "Manda uma mensagem a todos os jogadores ligados."),
    ("/curar [jogador]", "Cura por completo (a ti ou a alguem)."),
    ("/vida <valor> [jogador]", "Define a vida atual (a ti ou a alguem)."),
    ("/moedas <valor> [jogador]", "Da (ou tira, com valor negativo) moedas."),
    ("/nivel <valor> [jogador]", "Define o nivel (recalcula vida maxima)."),
    ("/tp <mapa>", "Teleporta-te para o spawn desse mapa (arena/missao1)."),
    ("/ir <jogador>", "Teleporta-te para onde esse jogador esta."),
    ("/trazer <jogador>", "Teleporta esse jogador para onde tu estas."),
    ("/god", "Liga/desliga a tua invencibilidade a monstros."),
    ("/kick <jogador>", "Desliga esse jogador do servidor."),
    ("/jogadores", "Lista quem esta ligado agora e em que mapa."),
]


def eh_staff(nome: str) -> bool:
    """Verdade se este nome de conta e' reconhecido como Owner/staff."""
    return (nome or "").strip().lower() == NOME_OWNER.lower()


def encontrar_jogador_por_nome(nome: str):
    """Devolve o dicionario do jogador ligado com este nome (sem
    depender de maiusculas/minusculas), ou None se ninguem estiver ligado
    com esse nome. Chamar sempre com o `lock` ja adquirido."""
    alvo = (nome or "").strip().lower()
    for info in jogadores.values():
        if info["nome"].strip().lower() == alvo:
            return info
    return None


def mensagem_sistema(conn, texto: str):
    """Manda uma mensagem 'de sistema' (visivel so' para quem recebe),
    reaproveitando a caixa de chat do cliente."""
    enviar(conn, {
        "tipo": "chat_mensagem", "id": -1, "nome": "Sistema", "texto": texto,
    })


def enviar_teleporte(info):
    """Avisa o cliente deste jogador que a sua posicao/mapa mudaram por
    causa de um comando de staff (/tp, /ir, /trazer). Ao contrario do
    movimento normal (onde e' o cliente que manda x/y), aqui e' o servidor
    a empurrar a mudanca — por isso o cliente precisa de uma mensagem
    propria ('teleporte') para saber que tem de trocar de mapa/fundo e
    actualizar a sua posicao local, e nao so' esperar que ele proprio
    detete um portal."""
    enviar(info["conn"], {
        "tipo": "teleporte", "mapa": info["mapa"], "x": info["x"], "y": info["y"],
    })
    enviar(info["conn"], {"tipo": "npcs", "npcs": NPCS_POR_MAPA.get(info["mapa"], [])})


def processar_comando(info, conn, texto_comando):
    """Interpreta um comando de staff (ex: '/curar Fulano') e executa-o.
    So' e' chamado depois de confirmar que quem escreveu e' staff."""
    partes = texto_comando.strip().split()
    if not partes:
        return
    nome_cmd = partes[0].lower()
    args = partes[1:]

    if nome_cmd == "/ajuda":
        mensagem_sistema(conn, "== Comandos de staff ==")
        for cmd, descricao in COMANDOS_STAFF:
            mensagem_sistema(conn, f"{cmd} — {descricao}")
        return

    if nome_cmd == "/anunciar":
        texto_aviso = " ".join(args).strip()
        if not texto_aviso:
            mensagem_sistema(conn, "Usa: /anunciar <texto>")
            return
        aviso = {
            "tipo": "chat_mensagem", "id": -1,
            "nome": "📢 Aviso do Owner", "texto": texto_aviso,
        }
        for j in jogadores.values():
            enviar(j["conn"], aviso)
        return

    if nome_cmd == "/jogadores":
        if not jogadores:
            mensagem_sistema(conn, "Nao ha ninguem ligado.")
            return
        mensagem_sistema(conn, f"Ligados ({len(jogadores)}):")
        for j in jogadores.values():
            mensagem_sistema(conn, f"- {j['nome']} (mapa: {j['mapa']}, nivel {j['nivel']})")
        return

    if nome_cmd == "/curar":
        alvo = encontrar_jogador_por_nome(args[0]) if args else info
        if alvo is None:
            mensagem_sistema(conn, f"Nao encontrei o jogador '{args[0]}'.")
            return
        alvo["vida"] = alvo["vida_max"]
        mensagem_sistema(conn, f"{alvo['nome']} foi curado por completo.")
        if alvo is not info:
            mensagem_sistema(alvo["conn"], f"{info['nome']} curou-te por completo.")
        bd.guardar_jogador(alvo)
        return

    if nome_cmd == "/vida":
        if not args or not args[0].lstrip("-").isdigit():
            mensagem_sistema(conn, "Usa: /vida <valor> [jogador]")
            return
        valor = int(args[0])
        alvo = encontrar_jogador_por_nome(args[1]) if len(args) > 1 else info
        if alvo is None:
            mensagem_sistema(conn, f"Nao encontrei o jogador '{args[1]}'.")
            return
        alvo["vida"] = max(1, min(valor, alvo["vida_max"]))
        mensagem_sistema(conn, f"Vida de {alvo['nome']} definida para {alvo['vida']}.")
        if alvo is not info:
            mensagem_sistema(alvo["conn"], f"{info['nome']} ajustou a tua vida para {alvo['vida']}.")
        bd.guardar_jogador(alvo)
        return

    if nome_cmd == "/moedas":
        if not args or not args[0].lstrip("-").isdigit():
            mensagem_sistema(conn, "Usa: /moedas <valor> [jogador]")
            return
        valor = int(args[0])
        alvo = encontrar_jogador_por_nome(args[1]) if len(args) > 1 else info
        if alvo is None:
            mensagem_sistema(conn, f"Nao encontrei o jogador '{args[1]}'.")
            return
        alvo["moedas"] = max(0, alvo["moedas"] + valor)
        mensagem_sistema(conn, f"{alvo['nome']} tem agora {alvo['moedas']} moedas.")
        if alvo is not info:
            mensagem_sistema(alvo["conn"], f"{info['nome']} deu-te {valor} moedas! Tens {alvo['moedas']}.")
        bd.guardar_jogador(alvo)
        return

    if nome_cmd == "/nivel":
        if not args or not args[0].isdigit() or int(args[0]) < 1:
            mensagem_sistema(conn, "Usa: /nivel <valor> [jogador]")
            return
        valor = int(args[0])
        alvo = encontrar_jogador_por_nome(args[1]) if len(args) > 1 else info
        if alvo is None:
            mensagem_sistema(conn, f"Nao encontrei o jogador '{args[1]}'.")
            return
        alvo["nivel"] = valor
        alvo["xp"] = 0
        alvo["vida_max"] = VIDA_INICIAL + VIDA_BONUS_POR_NIVEL * (valor - 1)
        alvo["vida"] = alvo["vida_max"]
        mensagem_sistema(conn, f"{alvo['nome']} passou a nivel {valor}.")
        if alvo is not info:
            mensagem_sistema(alvo["conn"], f"{info['nome']} pos-te no nivel {valor}!")
        bd.guardar_jogador(alvo)
        return

    if nome_cmd == "/tp":
        if not args or args[0] not in MAPA_SPAWN:
            mapas_validos = ", ".join(MAPA_SPAWN.keys())
            mensagem_sistema(conn, f"Usa: /tp <mapa> (mapas: {mapas_validos})")
            return
        mapa_destino = args[0]
        info["mapa"] = mapa_destino
        info["x"], info["y"] = MAPA_SPAWN[mapa_destino]
        enviar_teleporte(info)
        mensagem_sistema(conn, f"Teleportado para '{mapa_destino}'.")
        bd.guardar_jogador(info)
        return

    if nome_cmd == "/ir":
        if not args:
            mensagem_sistema(conn, "Usa: /ir <jogador>")
            return
        alvo = encontrar_jogador_por_nome(args[0])
        if alvo is None:
            mensagem_sistema(conn, f"Nao encontrei o jogador '{args[0]}'.")
            return
        info["mapa"] = alvo["mapa"]
        info["x"], info["y"] = alvo["x"], alvo["y"]
        enviar_teleporte(info)
        mensagem_sistema(conn, f"Teleportado para junto de {alvo['nome']}.")
        return

    if nome_cmd == "/trazer":
        if not args:
            mensagem_sistema(conn, "Usa: /trazer <jogador>")
            return
        alvo = encontrar_jogador_por_nome(args[0])
        if alvo is None:
            mensagem_sistema(conn, f"Nao encontrei o jogador '{args[0]}'.")
            return
        alvo["mapa"] = info["mapa"]
        alvo["x"], alvo["y"] = info["x"], info["y"]
        enviar_teleporte(alvo)
        mensagem_sistema(conn, f"{alvo['nome']} foi teleportado ate ti.")
        mensagem_sistema(alvo["conn"], f"{info['nome']} teleportou-te ate ele/ela.")
        return

    if nome_cmd == "/god":
        info["god"] = not info.get("god", False)
        estado = "ATIVADA" if info["god"] else "DESATIVADA"
        mensagem_sistema(conn, f"Invencibilidade {estado}.")
        return

    if nome_cmd == "/kick":
        if not args:
            mensagem_sistema(conn, "Usa: /kick <jogador>")
            return
        alvo = encontrar_jogador_por_nome(args[0])
        if alvo is None:
            mensagem_sistema(conn, f"Nao encontrei o jogador '{args[0]}'.")
            return
        if eh_staff(alvo["nome"]):
            mensagem_sistema(conn, "Nao podes expulsar outro membro da staff.")
            return
        mensagem_sistema(alvo["conn"], "Foste expulso do servidor por um membro da staff.")
        try:
            alvo["conn"].close()
        except OSError:
            pass
        mensagem_sistema(conn, f"{alvo['nome']} foi expulso.")
        return

    mensagem_sistema(conn, f"Comando desconhecido: {nome_cmd}. Escreve /ajuda para veres a lista.")


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
            por_mapa.setdefault(info["mapa"], {})[str(pid)] = {
                "x": info["x"], "y": info["y"], "nome": info["nome"],
            }

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
                    "pontos_habilidade": info.get("pontos_habilidade", 0),
                    "velocidade_pct": bonus_velocidade_pct_habilidades(info),
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
                        if info.get("god"):
                            continue  # staff com invencibilidade ligada: ignora dano de contacto
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


def loop_autosave():
    """Corre para sempre numa thread separada: de tempos a tempos, grava
    na base de dados o estado atual de todos os jogadores ligados, para
    nao se perder nada se o servidor cair ou for desligado sem aviso."""
    while True:
        time.sleep(INTERVALO_AUTOSAVE)
        with lock:
            copia = list(jogadores.values())
        for info in copia:
            bd.guardar_jogador(info)


def tratar_cliente(conn, addr):
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    meu_id = None    # so fica definido depois de recebermos o "login" com o nome
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

                # --- login: tem de ser a primeira mensagem do cliente. Ate
                # isto acontecer, ignoramos qualquer outra coisa que chegue
                # (nao ha ainda nenhum jogador para aplicar essas acoes). ---
                if tipo == "login":
                    if meu_id is not None:
                        continue  # ja fez login, ignora tentativas repetidas

                    nome_pedido = (msg.get("nome") or "").strip()[:COMPRIMENTO_MAX_NOME]
                    senha_pedida = msg.get("senha") or ""

                    if not nome_pedido:
                        enviar(conn, {
                            "tipo": "login_erro", "campo": "nome",
                            "mensagem": "Escreve um nome de utilizador.",
                        })
                        continue
                    if len(senha_pedida) < COMPRIMENTO_MIN_SENHA:
                        enviar(conn, {
                            "tipo": "login_erro", "campo": "senha",
                            "mensagem": f"A password tem de ter pelo menos {COMPRIMENTO_MIN_SENHA} caracteres.",
                        })
                        continue

                    with lock:
                        ja_esta_a_jogar = any(
                            info["nome"].lower() == nome_pedido.lower() for info in jogadores.values()
                        )
                        if ja_esta_a_jogar:
                            enviar(conn, {
                                "tipo": "login_erro", "campo": "nome",
                                "mensagem": f'Ja ha alguem a jogar como "{nome_pedido}" agora mesmo.',
                            })
                            continue

                        status, registo = bd.autenticar(
                            nome_pedido, senha_pedida, MOEDAS_INICIAIS, VIDA_INICIAL,
                            ["espada_curta"], "espada_curta",
                        )

                        if status == "senha_errada":
                            enviar(conn, {
                                "tipo": "login_erro", "campo": "senha",
                                "mensagem": "Password incorreta para esse utilizador.",
                            })
                            continue

                        conta_nova = (status == "novo")
                        if conta_nova:
                            print(f"[+] Nova conta registada: {registo['nome']} (id {registo['id']})")
                        else:
                            print(f"[+] {registo['nome']} entrou (id {registo['id']})")

                        meu_id = registo["id"]
                        jogadores[meu_id] = {
                            "id": meu_id,
                            "nome": registo["nome"],
                            "x": registo["x"], "y": registo["y"], "mapa": registo["mapa"],
                            "moedas": registo["moedas"], "conn": conn,
                            "inventario": registo["inventario"],
                            "arma_equipada": registo["arma_equipada"],
                            "armadura_equipada": registo["armadura_equipada"],
                            "vida": registo["vida"], "vida_max": registo["vida_max"],
                            "ultimo_ataque": 0.0,
                            "ultimo_dano": 0.0,
                            "ultimo_chat": 0.0,
                            "god": False,
                            "nivel": registo["nivel"],
                            "xp": registo["xp"],
                            "habilidades": registo["habilidades"],
                            "pontos_habilidade": registo["pontos_habilidade"],
                            "missao_ativa": registo["missao_ativa"],
                            "missao_progresso": registo["missao_progresso"],
                            "missao_completa": registo["missao_completa"],
                            "missao_entregas": registo["missao_entregas"],
                        }
                        info_local = jogadores[meu_id]

                    print(f"[+] Jogador {meu_id} ({info_local['nome']}) ligou-se de {addr}")

                    enviar(conn, {
                        "tipo": "bem_vindo", "id": meu_id, "nome": info_local["nome"], "novo": conta_nova,
                        "moedas": info_local["moedas"], "vida": info_local["vida"], "vida_max": info_local["vida_max"],
                        "nivel": info_local["nivel"], "xp": info_local["xp"],
                        "xp_prox": xp_necessario(info_local["nivel"]),
                    })
                    enviar(conn, {"tipo": "npcs", "npcs": NPCS_POR_MAPA.get(info_local["mapa"], [])})
                    enviar(conn, info_inventario(info_local))
                    enviar(conn, info_habilidades(info_local))
                    transmitir_estado()
                    continue

                if meu_id is None:
                    continue  # ainda sem login feito: ignora tudo o resto

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
                            bd.guardar_jogador(jogadores[meu_id])
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
                                bd.guardar_jogador(info)

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
                            bd.guardar_jogador(info)

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
                            dano = dano_base + bonus_dano_nivel(info) + bonus_dano_habilidades(info)
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
                            if subiu_nivel:
                                enviar(conn, info_habilidades(info))
                            if morreu:
                                # gravar aqui (e nao so no autosave/desligar)
                                # para moedas/xp/missao nao se perderem
                                # facilmente se o servidor cair.
                                bd.guardar_jogador(info)
                    transmitir_estado()

                elif tipo == "desbloquear_habilidade":
                    hab_id = msg.get("habilidade_id")
                    with lock:
                        info = jogadores.get(meu_id)
                        if info is None:
                            continue
                        no = ARVORE_HABILIDADES.get(hab_id)
                        resposta = {"tipo": "habilidade_resultado", "habilidade_id": hab_id}

                        if no is None:
                            resposta["sucesso"] = False
                            resposta["mensagem"] = "Essa habilidade nao existe."
                        elif hab_id in info["habilidades"]:
                            resposta["sucesso"] = False
                            resposta["mensagem"] = "Ja tens essa habilidade."
                        elif any(req not in info["habilidades"] for req in no["requisitos"]):
                            resposta["sucesso"] = False
                            resposta["mensagem"] = "Falta desbloquear um requisito antes."
                        elif info.get("pontos_habilidade", 0) < no["custo"]:
                            resposta["sucesso"] = False
                            resposta["mensagem"] = "Pontos de habilidade insuficientes."
                        else:
                            info["pontos_habilidade"] -= no["custo"]
                            info["habilidades"].append(hab_id)
                            resposta["sucesso"] = True
                            resposta["mensagem"] = f"Desbloqueaste: {no['nome']}!"

                        enviar(conn, resposta)
                        if resposta["sucesso"]:
                            enviar(conn, info_habilidades(info))
                            bd.guardar_jogador(info)
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
                        bd.guardar_jogador(info)
                    transmitir_estado()

                elif tipo == "chat":
                    texto_chat = (msg.get("texto") or "").strip()[:COMPRIMENTO_MAX_CHAT]
                    agora_chat = time.time()
                    with lock:
                        info = jogadores.get(meu_id)
                        if info is None or not texto_chat:
                            continue

                        # Comandos de staff (ex: "/curar", "/tp arena"): so'
                        # funcionam para o Owner e nunca sao vistos por mais
                        # ninguem no chat. Nao levam o cooldown normal do
                        # chat, para a staff poder encadear varios comandos.
                        eh_comando = texto_chat.startswith("/")
                        if eh_comando:
                            if eh_staff(info["nome"]):
                                processar_comando(info, conn, texto_chat)
                            else:
                                mensagem_sistema(conn, "Nao tens permissao para usar comandos.")
                            destinatarios = None
                        else:
                            if (agora_chat - info.get("ultimo_chat", 0.0)) < COOLDOWN_CHAT:
                                continue
                            info["ultimo_chat"] = agora_chat
                            # chat "local": so' quem esta no mesmo mapa ve a mensagem
                            destinatarios = [j["conn"] for j in jogadores.values() if j["mapa"] == info["mapa"]]
                            mensagem_chat = {
                                "tipo": "chat_mensagem",
                                "id": meu_id,
                                "nome": info["nome"],
                                "texto": texto_chat,
                            }
                    if destinatarios is None:
                        if eh_comando:
                            # Um comando pode ter mudado posicao/vida/moedas etc.
                            # (ex: /tp, /curar, /vida) — atualiza todos os clientes.
                            transmitir_estado()
                        continue
                    for c in destinatarios:
                        enviar(c, mensagem_chat)

    except (ConnectionResetError, json.JSONDecodeError):
        pass
    finally:
        with lock:
            info_final = jogadores.pop(meu_id, None)
        if info_final is not None:
            bd.guardar_jogador(info_final)
            print(f"[-] Jogador {meu_id} ({info_final['nome']}) desligou-se (progresso guardado)")
        conn.close()
        transmitir_estado()


def main():
    bd.inicializar()

    servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    servidor.bind((HOST, PORT))
    servidor.listen()
    print(f"Servidor a correr em {HOST}:{PORT} (ctrl+C para parar)")
    print(f"Base de dados dos jogadores: {bd.CAMINHO_BD}")

    threading.Thread(target=loop_monstros, daemon=True).start()
    threading.Thread(target=loop_autosave, daemon=True).start()

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
