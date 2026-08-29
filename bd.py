"""
BASE DE DADOS DOS JOGADORES (SQLite)
-------------------------------------
Antes desta etapa, tudo o que um jogador tinha (moedas, nivel, itens,
habilidades...) vivia so na memoria do servidor: assim que o servidor
era reiniciado, perdia-se tudo e todos comecavam do zero outra vez.

Agora cada jogador tem uma CONTA (nome de utilizador + password), como em
qualquer jogo a serio:
    - Se e' a primeira vez que esse nome aparece, criamos a conta na hora
      com a password que a pessoa escreveu (o proximo id livre a comecar
      em 1).
    - Se o nome ja existe, a password tem de bater certo com a guardada
      para se poder entrar. So' ai carregamos o progresso guardado
      (moedas, nivel, inventario, habilidades, posicao, etc.).

A password NUNCA e' guardada em texto simples: guardamos so' um "hash"
(um resumo criado de forma a nao dar para voltar atras) feito com um
"sal" (salt) proprio de cada conta, usando PBKDF2 (funcao standard do
Python para isto, em hashlib). Mesmo que alguem abra o ficheiro
jogadores.db, nao consegue ver as passwords, so os hashes.

Guardamos periodicamente (e sempre que um jogador se desliga) o estado
atual de volta na base de dados, para nada se perder.

Ficheiro usado: jogadores.db (SQLite), criado ao lado deste ficheiro na
primeira vez que o servidor correr. Podes apagar esse ficheiro a qualquer
momento para comecar tudo do zero, ou correr:

    python bd.py --reset

para limpar a base de dados (apaga todas as contas guardadas e faz os
proximos ids voltarem a comecar no 1).
"""
import sqlite3
import json
import os
import hashlib
import threading

CAMINHO_BD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jogadores.db")

ITERACOES_HASH = 100_000   # quantas voltas o PBKDF2 da' - mais voltas, mais lento para alguem tentar adivinhar passwords

_lock = threading.Lock()
_ligacao = None


def obter_ligacao():
    """Devolve a ligacao sqlite3 partilhada (uma so, reutilizada; o sqlite3
    aceita ser usado a partir de varias threads desde que se avise com
    check_same_thread=False e se proteja o acesso com um lock, como
    fazemos aqui)."""
    global _ligacao
    if _ligacao is None:
        _ligacao = sqlite3.connect(CAMINHO_BD, check_same_thread=False)
        _ligacao.row_factory = sqlite3.Row
    return _ligacao


def inicializar():
    """Cria a tabela de jogadores se ainda nao existir. Chamar uma vez
    quando o servidor arranca, antes de aceitar ligacoes."""
    with _lock:
        con = obter_ligacao()
        con.execute("""
            CREATE TABLE IF NOT EXISTS jogadores (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                nome                TEXT NOT NULL UNIQUE,
                senha_hash          TEXT NOT NULL DEFAULT '',
                senha_salt          TEXT NOT NULL DEFAULT '',
                moedas              INTEGER NOT NULL DEFAULT 0,
                vida                INTEGER NOT NULL DEFAULT 100,
                vida_max            INTEGER NOT NULL DEFAULT 100,
                nivel               INTEGER NOT NULL DEFAULT 1,
                xp                  INTEGER NOT NULL DEFAULT 0,
                inventario          TEXT NOT NULL DEFAULT '[]',
                arma_equipada       TEXT,
                armadura_equipada   TEXT,
                habilidades         TEXT NOT NULL DEFAULT '["fundamentos"]',
                pontos_habilidade   INTEGER NOT NULL DEFAULT 0,
                missao_ativa        INTEGER NOT NULL DEFAULT 0,
                missao_progresso    INTEGER NOT NULL DEFAULT 0,
                missao_completa     INTEGER NOT NULL DEFAULT 0,
                missao_entregas     INTEGER NOT NULL DEFAULT 0,
                mapa                TEXT NOT NULL DEFAULT 'arena',
                x                   REAL NOT NULL DEFAULT 100,
                y                   REAL NOT NULL DEFAULT 100,
                criado_em           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ultimo_login        TEXT
            )
        """)
        con.commit()


def resetar_tudo():
    """Apaga TODAS as contas guardadas e faz os proximos ids voltarem a
    comecar no 1. Usar com cuidado - e' o "reset" completo da base de
    dados (ver 'python bd.py --reset')."""
    with _lock:
        con = obter_ligacao()
        con.execute("DELETE FROM jogadores")
        con.execute("DELETE FROM sqlite_sequence WHERE name='jogadores'")
        con.commit()


def _gerar_hash(senha, salt_bytes):
    """PBKDF2-HMAC-SHA256: forma standard e lenta-de-propositio de
    transformar uma password num hash, para dificultar quem tentar
    adivinhar passwords a partir de uma copia da base de dados."""
    return hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt_bytes, ITERACOES_HASH)


def _linha_para_dict(linha):
    return {
        "id": linha["id"],
        "nome": linha["nome"],
        "moedas": linha["moedas"],
        "vida": linha["vida"],
        "vida_max": linha["vida_max"],
        "nivel": linha["nivel"],
        "xp": linha["xp"],
        "inventario": json.loads(linha["inventario"]),
        "arma_equipada": linha["arma_equipada"],
        "armadura_equipada": linha["armadura_equipada"],
        "habilidades": json.loads(linha["habilidades"]),
        "pontos_habilidade": linha["pontos_habilidade"],
        "missao_ativa": bool(linha["missao_ativa"]),
        "missao_progresso": linha["missao_progresso"],
        "missao_completa": bool(linha["missao_completa"]),
        "missao_entregas": linha["missao_entregas"],
        "mapa": linha["mapa"],
        "x": linha["x"],
        "y": linha["y"],
    }


def autenticar(nome, senha, moedas_iniciais, vida_inicial, inventario_inicial, arma_inicial):
    """Tenta entrar com este nome + password.

    Devolve sempre um par (status, dados):
        ("novo", dados)          -> nome nao existia, conta criada agora
        ("ok", dados)            -> nome existia e a password bateu certo
        ("senha_errada", None)   -> nome existe mas a password nao bate certo

    'dados' e' o dicionario do jogador (ver _linha_para_dict), pronto a
    usar para preencher o estado inicial dele no servidor.
    """
    with _lock:
        con = obter_ligacao()
        linha = con.execute(
            "SELECT * FROM jogadores WHERE nome = ? COLLATE NOCASE", (nome,)
        ).fetchone()

        if linha is None:
            salt = os.urandom(16)
            hash_ = _gerar_hash(senha, salt)
            cursor = con.execute(
                """INSERT INTO jogadores
                       (nome, senha_hash, senha_salt, moedas, vida, vida_max, inventario, arma_equipada, ultimo_login)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    nome, hash_.hex(), salt.hex(),
                    moedas_iniciais, vida_inicial, vida_inicial,
                    json.dumps(inventario_inicial), arma_inicial,
                ),
            )
            con.commit()
            nova_linha = con.execute("SELECT * FROM jogadores WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return "novo", _linha_para_dict(nova_linha)

        salt = bytes.fromhex(linha["senha_salt"])
        hash_dado = _gerar_hash(senha, salt).hex()
        if hash_dado != linha["senha_hash"]:
            return "senha_errada", None

        con.execute("UPDATE jogadores SET ultimo_login = CURRENT_TIMESTAMP WHERE id = ?", (linha["id"],))
        con.commit()
        return "ok", _linha_para_dict(linha)


def guardar_jogador(info):
    """Grava de volta na base de dados o estado atual (em memoria) de um
    jogador. 'info' e' o dicionario que o servidor usa durante o jogo
    (jogadores[id]) e tem de incluir a chave 'id'."""
    with _lock:
        con = obter_ligacao()
        con.execute(
            """UPDATE jogadores SET
                moedas = ?, vida = ?, vida_max = ?, nivel = ?, xp = ?,
                inventario = ?, arma_equipada = ?, armadura_equipada = ?,
                habilidades = ?, pontos_habilidade = ?,
                missao_ativa = ?, missao_progresso = ?, missao_completa = ?, missao_entregas = ?,
                mapa = ?, x = ?, y = ?, ultimo_login = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                info["moedas"], info["vida"], info["vida_max"], info["nivel"], info["xp"],
                json.dumps(info["inventario"]), info["arma_equipada"], info["armadura_equipada"],
                json.dumps(info["habilidades"]), info["pontos_habilidade"],
                int(info.get("missao_ativa", False)), info.get("missao_progresso", 0),
                int(info.get("missao_completa", False)), info.get("missao_entregas", 0),
                info["mapa"], info["x"], info["y"],
                info["id"],
            ),
        )
        con.commit()


if __name__ == "__main__":
    import sys
    inicializar()
    if "--reset" in sys.argv:
        resposta = input(
            "Isto apaga TODAS as contas guardadas em jogadores.db. Escreve 'sim' para confirmar: "
        )
        if resposta.strip().lower() == "sim":
            resetar_tudo()
            print("Base de dados limpa. As proximas contas a entrar comecam do id 1.")
        else:
            print("Cancelado, nada foi apagado.")
    else:
        print(f"Base de dados em: {CAMINHO_BD}")
        print("Usa 'python bd.py --reset' para limpar todas as contas guardadas.")

