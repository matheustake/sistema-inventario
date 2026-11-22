"""Persistência SQLite do sistema de estoque."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from util import (
    EstoqueError,
    calcular_custo_medio,
    clean_email,
    clean_multiline,
    clean_phone,
    clean_text,
    dec_money,
    dec_qty,
    format_money,
    format_qty,
    like_pattern,
    now,
)
from permissoes import PERFIS

SENHA_MINIMA = 6


def _hash_senha(senha: str) -> str:
    """Valida e criptografa a senha; o banco nunca guarda a senha em texto."""
    senha = senha or ""
    if len(senha) < SENHA_MINIMA:
        raise EstoqueError(f"A senha precisa ter pelo menos {SENHA_MINIMA} caracteres.")
    if len(senha) > 128:
        raise EstoqueError("A senha pode ter no máximo 128 caracteres.")
    return generate_password_hash(senha)


def _sem_senha(row) -> dict | None:
    if row is None:
        return None
    dados = dict(row)
    dados["tem_senha"] = bool(dados.pop("senha_hash", None))
    return dados

CATEGORIAS_PADRAO = (
    "Mercearia",
    "Bebidas",
    "Limpeza",
    "Higiene",
    "Escritório",
    "Outros",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS categorias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL COLLATE NOCASE UNIQUE,
    criado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fornecedores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    contato TEXT,
    telefone TEXT,
    email TEXT,
    observacao TEXT,
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS produtos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo TEXT NOT NULL COLLATE NOCASE UNIQUE,
    nome TEXT NOT NULL,
    categoria_id INTEGER REFERENCES categorias(id),
    unidade TEXT NOT NULL DEFAULT 'UN',
    estoque_minimo REAL NOT NULL DEFAULT 0 CHECK (estoque_minimo >= 0),
    custo REAL NOT NULL DEFAULT 0 CHECK (custo >= 0),
    preco_venda REAL NOT NULL DEFAULT 0 CHECK (preco_venda >= 0),
    quantidade REAL NOT NULL DEFAULT 0 CHECK (quantidade >= 0),
    descricao TEXT,
    ativo INTEGER NOT NULL DEFAULT 1 CHECK (ativo IN (0, 1)),
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS movimentacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    produto_id INTEGER NOT NULL REFERENCES produtos(id),
    tipo TEXT NOT NULL CHECK (tipo IN ('entrada', 'saida', 'ajuste')),
    quantidade REAL NOT NULL CHECK (quantidade != 0),
    quantidade_anterior REAL NOT NULL,
    quantidade_posterior REAL NOT NULL,
    custo_unitario REAL,
    custo_anterior REAL NOT NULL,
    custo_posterior REAL NOT NULL,
    motivo TEXT,
    fornecedor_id INTEGER REFERENCES fornecedores(id) ON DELETE SET NULL,
    documento TEXT,
    destino TEXT,
    observacao TEXT,
    estorno_de INTEGER REFERENCES movimentacoes(id),
    estornado INTEGER NOT NULL DEFAULT 0 CHECK (estornado IN (0, 1)),
    criado_em TEXT NOT NULL,
    usuario_email TEXT
);

CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    nome TEXT,
    perfil TEXT NOT NULL CHECK (perfil IN ('superadmin', 'admin', 'operador', 'consulta')),
    senha_hash TEXT,
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS atividades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_email TEXT,
    acao TEXT NOT NULL,
    detalhe TEXT,
    criado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_produtos_nome ON produtos(nome);
CREATE INDEX IF NOT EXISTS idx_produtos_categoria ON produtos(categoria_id);
CREATE INDEX IF NOT EXISTS idx_mov_produto ON movimentacoes(produto_id);
CREATE INDEX IF NOT EXISTS idx_mov_data ON movimentacoes(criado_em);
"""

PRODUTO_FROM = """
SELECT p.*, c.nome AS categoria_nome
FROM produtos p
LEFT JOIN categorias c ON c.id = p.categoria_id
"""

MOV_FROM = """
SELECT m.*,
       p.codigo AS produto_codigo,
       p.nome AS produto_nome,
       p.unidade AS produto_unidade,
       f.nome AS fornecedor_nome
FROM movimentacoes m
JOIN produtos p ON p.id = m.produto_id
LEFT JOIN fornecedores f ON f.id = m.fornecedor_id
"""


class Database:
    """Acesso ao banco. `usuario_email` identifica quem faz as alterações desta conexão."""

    def __init__(self, path: str | Path, *, usuario_email: str | None = None, inicializar: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.usuario_email = usuario_email
        self._tx_depth = 0
        self.conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if inicializar:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.executescript(SCHEMA)
            self.conn.execute("PRAGMA foreign_keys = ON")
            self._migrar()
            self._semear_categorias()

    def close(self) -> None:
        self.conn.close()

    def _migrar(self) -> None:
        """Atualiza bancos criados pela versão desktop sem perder dados."""
        colunas = {row["name"] for row in self.conn.execute("PRAGMA table_info(movimentacoes)")}
        if "usuario_email" not in colunas:
            self.conn.execute("ALTER TABLE movimentacoes ADD COLUMN usuario_email TEXT")
        colunas = {row["name"] for row in self.conn.execute("PRAGMA table_info(usuarios)")}
        if "senha_hash" not in colunas:
            self.conn.execute("ALTER TABLE usuarios ADD COLUMN senha_hash TEXT")
        self.conn.execute("INSERT OR IGNORE INTO meta (chave, valor) VALUES ('versao', 0)")

    def versao(self) -> int:
        """Número que muda a cada gravação; as telas usam para saber se há novidade."""
        row = self.conn.execute("SELECT valor FROM meta WHERE chave = 'versao'").fetchone()
        return int(row["valor"]) if row else 0

    def _registrar_atividade(self, acao: str, detalhe: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO atividades (usuario_email, acao, detalhe, criado_em) VALUES (?, ?, ?, ?)",
            (self.usuario_email, acao, detalhe, now()),
        )

    @contextmanager
    def _tx(self):
        top = self._tx_depth == 0
        if top:
            self.conn.execute("BEGIN IMMEDIATE")
        self._tx_depth += 1
        try:
            yield
            if top:
                self.conn.execute("UPDATE meta SET valor = valor + 1 WHERE chave = 'versao'")
                self.conn.commit()
        except Exception:
            if top:
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            self._tx_depth -= 1

    def _semear_categorias(self) -> None:
        total = self.conn.execute("SELECT COUNT(*) AS n FROM categorias").fetchone()["n"]
        if total:
            return
        momento = now()
        for nome in CATEGORIAS_PADRAO:
            self.conn.execute(
                "INSERT INTO categorias (nome, criado_em) VALUES (?, ?)",
                (nome, momento),
            )

    def backup(self, destino: str | Path) -> None:
        destino = Path(destino)
        if destino.resolve() == self.path.resolve():
            raise EstoqueError("Escolha um arquivo diferente do banco em uso.")
        destino.parent.mkdir(parents=True, exist_ok=True)
        copia = sqlite3.connect(destino)
        try:
            self.conn.backup(copia)
        finally:
            copia.close()
        with self._tx():
            self._registrar_atividade("Fez backup do banco")

    def listar_categorias(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT c.*, COUNT(p.id) AS total_produtos
            FROM categorias c
            LEFT JOIN produtos p ON p.categoria_id = c.id
            GROUP BY c.id
            ORDER BY c.nome COLLATE NOCASE
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def salvar_categoria(self, nome: str, categoria_id: int | None = None) -> dict:
        nome = clean_text(nome, required=True, label="o nome da categoria", max_len=60)
        with self._tx():
            duplicada = self.conn.execute(
                "SELECT id FROM categorias WHERE nome = ? COLLATE NOCASE AND id != ?",
                (nome, categoria_id or -1),
            ).fetchone()
            if duplicada:
                raise EstoqueError("Já existe uma categoria com esse nome.")
            if categoria_id:
                atual = self.conn.execute(
                    "SELECT id FROM categorias WHERE id = ?",
                    (categoria_id,),
                ).fetchone()
                if atual is None:
                    raise EstoqueError("Categoria não encontrada.")
                anterior = self._categoria_row(categoria_id)["nome"]
                self.conn.execute(
                    "UPDATE categorias SET nome = ? WHERE id = ?",
                    (nome, categoria_id),
                )
                if anterior != nome:
                    self._registrar_atividade("Renomeou categoria", f"{anterior} → {nome}")
            else:
                cur = self.conn.execute(
                    "INSERT INTO categorias (nome, criado_em) VALUES (?, ?)",
                    (nome, now()),
                )
                categoria_id = cur.lastrowid
                self._registrar_atividade("Criou categoria", nome)
        return self._categoria(categoria_id)

    def excluir_categoria(self, categoria_id: int) -> None:
        with self._tx():
            categoria = self._categoria_row(categoria_id)
            total = self.conn.execute(
                "SELECT COUNT(*) AS n FROM produtos WHERE categoria_id = ?",
                (categoria_id,),
            ).fetchone()["n"]
            if total:
                raise EstoqueError(
                    f"A categoria {categoria['nome']} está em {total} produto(s). "
                    "Altere esses produtos antes de excluir."
                )
            self.conn.execute("DELETE FROM categorias WHERE id = ?", (categoria_id,))
            self._registrar_atividade("Excluiu categoria", categoria["nome"])

    def listar_fornecedores(self, busca: str = "") -> list[dict]:
        sql = "SELECT * FROM fornecedores"
        params: list = []
        termo = (busca or "").strip()
        if termo:
            padrao = like_pattern(termo)
            sql += (
                " WHERE nome LIKE ? ESCAPE '\\' OR IFNULL(contato, '') LIKE ? ESCAPE '\\'"
                " OR IFNULL(telefone, '') LIKE ? ESCAPE '\\' OR IFNULL(email, '') LIKE ? ESCAPE '\\'"
            )
            params.extend([padrao, padrao, padrao, padrao])
        sql += " ORDER BY nome COLLATE NOCASE"
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def salvar_fornecedor(self, dados: dict) -> dict:
        nome = clean_text(dados.get("nome"), required=True, label="o nome do fornecedor", max_len=120)
        contato = clean_text(dados.get("contato"), max_len=80)
        telefone = clean_phone(dados.get("telefone"))
        email = clean_email(dados.get("email"))
        observacao = clean_multiline(dados.get("observacao"), max_len=500)
        fornecedor_id = dados.get("id")
        momento = now()
        with self._tx():
            if fornecedor_id:
                if self._fornecedor_row(fornecedor_id) is None:
                    raise EstoqueError("Fornecedor não encontrado.")
                self.conn.execute(
                    """
                    UPDATE fornecedores
                    SET nome = ?, contato = ?, telefone = ?, email = ?, observacao = ?, atualizado_em = ?
                    WHERE id = ?
                    """,
                    (nome, contato, telefone, email, observacao, momento, fornecedor_id),
                )
                self._registrar_atividade("Editou fornecedor", nome)
            else:
                cur = self.conn.execute(
                    """
                    INSERT INTO fornecedores
                        (nome, contato, telefone, email, observacao, criado_em, atualizado_em)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (nome, contato, telefone, email, observacao, momento, momento),
                )
                fornecedor_id = cur.lastrowid
                self._registrar_atividade("Criou fornecedor", nome)
        return dict(self._fornecedor_row(fornecedor_id))

    def obter_fornecedor(self, fornecedor_id: int) -> dict | None:
        row = self._fornecedor_row(fornecedor_id)
        return dict(row) if row else None

    def excluir_fornecedor(self, fornecedor_id: int) -> None:
        with self._tx():
            fornecedor = self._fornecedor_row(fornecedor_id)
            if fornecedor is None:
                raise EstoqueError("Fornecedor não encontrado.")
            self.conn.execute("DELETE FROM fornecedores WHERE id = ?", (fornecedor_id,))
            self._registrar_atividade("Excluiu fornecedor", fornecedor["nome"])

    def sugerir_codigo(self) -> str:
        numero = 1
        while numero < 100000:
            codigo = f"PRD-{numero:04d}"
            existe = self.conn.execute(
                "SELECT 1 FROM produtos WHERE codigo = ? COLLATE NOCASE",
                (codigo,),
            ).fetchone()
            if not existe:
                return codigo
            numero += 1
        raise EstoqueError("Não foi possível sugerir um código.")

    def listar_produtos(
        self,
        busca: str = "",
        categoria_id: int | None = None,
        situacao: str = "ativos",
        somente_atencao: bool = False,
    ) -> list[dict]:
        if situacao not in {"ativos", "inativos", "todos"}:
            raise EstoqueError("Situação de produto inválida.")
        sql = PRODUTO_FROM + " WHERE 1 = 1"
        params: list = []
        if situacao == "ativos":
            sql += " AND p.ativo = 1"
        elif situacao == "inativos":
            sql += " AND p.ativo = 0"
        if categoria_id:
            sql += " AND p.categoria_id = ?"
            params.append(categoria_id)
        if somente_atencao:
            sql += " AND p.ativo = 1 AND p.quantidade <= p.estoque_minimo"
        termo = (busca or "").strip()
        if termo:
            padrao = like_pattern(termo)
            sql += (
                " AND (p.codigo LIKE ? ESCAPE '\\' OR p.nome LIKE ? ESCAPE '\\'"
                " OR IFNULL(p.descricao, '') LIKE ? ESCAPE '\\')"
            )
            params.extend([padrao, padrao, padrao])
        sql += " ORDER BY p.nome COLLATE NOCASE"
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def obter_produto(self, produto_id: int) -> dict | None:
        row = self.conn.execute(PRODUTO_FROM + " WHERE p.id = ?", (produto_id,)).fetchone()
        return dict(row) if row else None

    def contar_produtos(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM produtos").fetchone()["n"])

    def salvar_produto(self, dados: dict) -> dict:
        produto_id = dados.get("id")
        codigo = self._codigo(dados.get("codigo"))
        nome = clean_text(dados.get("nome"), required=True, label="o nome do produto", max_len=120)
        unidade = self._unidade(dados.get("unidade"))
        minimo = dec_qty(dados.get("estoque_minimo") or 0)
        custo = dec_money(dados.get("custo") or 0)
        preco = dec_money(dados.get("preco_venda") or 0)
        if minimo < 0 or custo < 0 or preco < 0:
            raise EstoqueError("Mínimo, custo e preço não podem ser negativos.")
        descricao = clean_multiline(dados.get("descricao"), max_len=1000)
        categoria_id = dados.get("categoria_id") or None
        ativo = 1 if dados.get("ativo", True) else 0
        inicial = dec_qty(dados.get("quantidade_inicial") or 0)
        if inicial < 0:
            raise EstoqueError("A quantidade inicial não pode ser negativa.")
        if not produto_id and inicial > 0 and not ativo:
            raise EstoqueError("Para informar saldo inicial, deixe o produto ativo.")
        momento = now()
        with self._tx():
            self._exigir_categoria(categoria_id)
            duplicado = self.conn.execute(
                "SELECT id FROM produtos WHERE codigo = ? COLLATE NOCASE AND id != ?",
                (codigo, produto_id or -1),
            ).fetchone()
            if duplicado:
                raise EstoqueError("Já existe um produto com esse código.")
            if produto_id:
                anterior = self._lock_produto(produto_id, exigir_ativo=False)
                self.conn.execute(
                    """
                    UPDATE produtos
                    SET codigo = ?, nome = ?, categoria_id = ?, unidade = ?, estoque_minimo = ?,
                        custo = ?, preco_venda = ?, descricao = ?, ativo = ?, atualizado_em = ?
                    WHERE id = ?
                    """,
                    (
                        codigo,
                        nome,
                        categoria_id,
                        unidade,
                        float(minimo),
                        float(custo),
                        float(preco),
                        descricao,
                        ativo,
                        momento,
                        produto_id,
                    ),
                )
                rotulo = f"{codigo} · {nome}"
                if bool(anterior["ativo"]) != bool(ativo):
                    self._registrar_atividade("Reativou produto" if ativo else "Desativou produto", rotulo)
                else:
                    detalhe = rotulo
                    if dec_money(anterior["custo"]) != custo:
                        detalhe += (
                            f" (custo médio {format_money(anterior['custo'])} → {format_money(custo)})"
                        )
                    self._registrar_atividade("Editou produto", detalhe)
            else:
                cur = self.conn.execute(
                    """
                    INSERT INTO produtos (
                        codigo, nome, categoria_id, unidade, estoque_minimo, custo, preco_venda,
                        quantidade, descricao, ativo, criado_em, atualizado_em
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        codigo,
                        nome,
                        categoria_id,
                        unidade,
                        float(minimo),
                        float(custo),
                        float(preco),
                        descricao,
                        ativo,
                        momento,
                        momento,
                    ),
                )
                produto_id = cur.lastrowid
                self._registrar_atividade("Criou produto", f"{codigo} · {nome}")
                if inicial > 0:
                    self._entrada(
                        produto_id=produto_id,
                        quantidade=inicial,
                        custo_unitario=custo,
                        motivo="Estoque inicial",
                        data_iso=dados.get("data_iso") or momento,
                        fornecedor_id=None,
                        documento=None,
                        observacao="Saldo informado no cadastro do produto.",
                    )
        return self.obter_produto(produto_id)

    def registrar_entrada(
        self,
        produto_id: int,
        quantidade: Decimal,
        custo_unitario: Decimal,
        motivo: str,
        data_iso: str,
        fornecedor_id: int | None = None,
        documento: str | None = None,
        observacao: str | None = None,
    ) -> dict:
        with self._tx():
            mov_id = self._entrada(
                produto_id=produto_id,
                quantidade=dec_qty(quantidade),
                custo_unitario=dec_money(custo_unitario),
                motivo=motivo,
                data_iso=data_iso,
                fornecedor_id=fornecedor_id,
                documento=documento,
                observacao=observacao,
            )
            return self._movimento(mov_id)

    def registrar_saida(
        self,
        produto_id: int,
        quantidade: Decimal,
        motivo: str,
        data_iso: str,
        destino: str | None = None,
        documento: str | None = None,
        observacao: str | None = None,
    ) -> dict:
        with self._tx():
            produto = self._lock_produto(produto_id, exigir_ativo=True)
            qtd = dec_qty(quantidade)
            if qtd <= 0:
                raise EstoqueError("A quantidade deve ser maior que zero.")
            motivo, documento, destino, observacao = self._dados_movimento(
                motivo, documento, observacao, destino=destino
            )
            self._validar_data(data_iso)
            disponivel = dec_qty(produto["quantidade"])
            if disponivel - qtd < 0:
                raise EstoqueError(
                    f"Estoque insuficiente de {produto['nome']}. "
                    f"Disponível: {format_qty(disponivel)} {produto['unidade']}."
                )
            mov_id = self._mover(
                produto=produto,
                tipo="saida",
                delta=-qtd,
                custo_posterior=dec_money(produto["custo"]),
                custo_unitario=None,
                motivo=motivo,
                fornecedor_id=None,
                documento=documento,
                destino=destino,
                observacao=observacao,
                data_iso=data_iso,
            )
            return self._movimento(mov_id)

    def registrar_ajuste(
        self,
        produto_id: int,
        quantidade_contada: Decimal,
        motivo: str,
        data_iso: str,
        documento: str | None = None,
        observacao: str | None = None,
    ) -> dict:
        with self._tx():
            produto = self._lock_produto(produto_id, exigir_ativo=True)
            contada = dec_qty(quantidade_contada)
            if contada < 0:
                raise EstoqueError("A quantidade contada não pode ser negativa.")
            motivo, documento, _, observacao = self._dados_movimento(
                motivo, documento, observacao, destino=None
            )
            self._validar_data(data_iso)
            delta = (contada - dec_qty(produto["quantidade"])).quantize(Decimal("0.001"))
            if delta == 0:
                raise EstoqueError("A quantidade contada é igual ao estoque atual.")
            mov_id = self._mover(
                produto=produto,
                tipo="ajuste",
                delta=delta,
                custo_posterior=dec_money(produto["custo"]),
                custo_unitario=None,
                motivo=motivo,
                fornecedor_id=None,
                documento=documento,
                destino=None,
                observacao=observacao,
                data_iso=data_iso,
            )
            return self._movimento(mov_id)

    def estornar(self, movimento_id: int) -> dict:
        with self._tx():
            mov = self.conn.execute(
                "SELECT * FROM movimentacoes WHERE id = ?",
                (movimento_id,),
            ).fetchone()
            if mov is None:
                raise EstoqueError("Lançamento não encontrado.")
            if mov["estornado"]:
                raise EstoqueError("Este lançamento já foi estornado.")
            if mov["estorno_de"]:
                raise EstoqueError(
                    "Não é possível estornar um estorno. Registre uma nova entrada, saída ou ajuste."
                )
            ultimo = self.conn.execute(
                "SELECT id FROM movimentacoes WHERE produto_id = ? ORDER BY id DESC LIMIT 1",
                (mov["produto_id"],),
            ).fetchone()
            if ultimo["id"] != mov["id"]:
                raise EstoqueError(
                    "Só é possível estornar o último lançamento deste produto. "
                    "Para corrigir um lançamento antigo, registre um ajuste ou o movimento inverso."
                )
            produto = self._lock_produto(mov["produto_id"], exigir_ativo=True)
            if dec_qty(produto["quantidade"]) != dec_qty(mov["quantidade_posterior"]):
                raise EstoqueError("O saldo atual não confere com este lançamento. Use um ajuste.")
            custo_atual = dec_money(produto["custo"])
            if custo_atual == dec_money(mov["custo_posterior"]):
                custo_novo = dec_money(mov["custo_anterior"])
                extra = ""
            else:
                custo_novo = custo_atual
                extra = " O custo cadastrado foi mantido porque tinha sido alterado depois do lançamento."
            nomes = {"entrada": "entrada", "saida": "saída", "ajuste": "ajuste"}
            tipos = {"entrada": "saida", "saida": "entrada", "ajuste": "ajuste"}
            observacao = f"Estorno do lançamento #{mov['id']} ({nomes[mov['tipo']]})."
            if mov["observacao"]:
                observacao += f" Original: {mov['observacao']}"
            observacao += extra
            if len(observacao) > 1000:
                observacao = observacao[:997] + "..."
            novo_id = self._mover(
                produto=produto,
                tipo=tipos[mov["tipo"]],
                delta=dec_qty(mov["quantidade_anterior"]) - dec_qty(produto["quantidade"]),
                custo_posterior=custo_novo,
                custo_unitario=dec_money(mov["custo_unitario"]) if mov["custo_unitario"] is not None else None,
                motivo="Estorno",
                fornecedor_id=mov["fornecedor_id"],
                documento=mov["documento"],
                destino=mov["destino"],
                observacao=observacao,
                data_iso=now(),
                estorno_de=mov["id"],
            )
            self.conn.execute(
                "UPDATE movimentacoes SET estornado = 1 WHERE id = ?",
                (mov["id"],),
            )
            return self._movimento(novo_id)

    def listar_movimentos(
        self,
        busca: str = "",
        tipo: str | None = None,
        data_ini: str | None = None,
        data_fim: str | None = None,
        limit: int | None = 1000,
    ) -> tuple[list[dict], bool]:
        sql = MOV_FROM + " WHERE 1 = 1"
        params: list = []
        if tipo:
            if tipo not in {"entrada", "saida", "ajuste"}:
                raise EstoqueError("Tipo de lançamento inválido.")
            sql += " AND m.tipo = ?"
            params.append(tipo)
        if data_ini:
            sql += " AND date(m.criado_em) >= date(?)"
            params.append(data_ini)
        if data_fim:
            sql += " AND date(m.criado_em) <= date(?)"
            params.append(data_fim)
        termo = (busca or "").strip()
        if termo:
            padrao = like_pattern(termo)
            sql += " AND (p.codigo LIKE ? ESCAPE '\\' OR p.nome LIKE ? ESCAPE '\\' OR IFNULL(m.documento, '') LIKE ? ESCAPE '\\')"
            params.extend([padrao, padrao, padrao])
        sql += " ORDER BY m.criado_em DESC, m.id DESC"
        truncado = False
        if limit is not None:
            rows = self.conn.execute(sql + " LIMIT ?", [*params, limit + 1]).fetchall()
            truncado = len(rows) > limit
            rows = rows[:limit]
        else:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows], truncado

    def total_atencao(self) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM produtos
            WHERE ativo = 1 AND quantidade <= estoque_minimo
            """
        ).fetchone()
        return int(row["n"])

    def resumo(self) -> dict:
        row = self.conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN ativo = 1 THEN 1 ELSE 0 END), 0) AS ativos,
                COALESCE(SUM(CASE WHEN ativo = 0 THEN 1 ELSE 0 END), 0) AS inativos,
                COALESCE(SUM(CASE WHEN ativo = 1 THEN quantidade ELSE 0 END), 0) AS unidades,
                COALESCE(SUM(CASE WHEN ativo = 1 AND quantidade <= estoque_minimo THEN 1 ELSE 0 END), 0) AS atencao,
                COALESCE(SUM(CASE WHEN ativo = 1 THEN quantidade * custo ELSE 0 END), 0) AS valor_custo,
                COALESCE(SUM(CASE WHEN ativo = 1 THEN quantidade * preco_venda ELSE 0 END), 0) AS valor_venda
            FROM produtos
            """
        ).fetchone()
        recentes, _ = self.listar_movimentos(limit=8)
        return {
            "ativos": int(row["ativos"]),
            "inativos": int(row["inativos"]),
            "unidades": row["unidades"],
            "atencao": int(row["atencao"]),
            "valor_custo": row["valor_custo"],
            "valor_venda": row["valor_venda"],
            "alertas": self.listar_produtos(situacao="ativos", somente_atencao=True),
            "recentes": recentes,
        }

    def carregar_exemplo(self) -> None:
        with self._tx():
            if self.contar_produtos():
                raise EstoqueError(
                    "O exemplo só pode ser carregado quando ainda não há produtos cadastrados."
                )
            from datetime import datetime, timedelta

            self._registrar_atividade("Carregou dados de exemplo")
            momento = now()
            ontem = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 09:00:00")
            categorias = {
                nome: self._garantir_categoria(nome)
                for nome in ("Mercearia", "Limpeza", "Escritório")
            }
            fornecedores = [
                self.salvar_fornecedor(
                    {
                        "nome": "Atacado Central",
                        "contato": "Ana Souza",
                        "telefone": "(11) 98888-1000",
                        "email": "pedidos@atacadocentral.example",
                    }
                ),
                self.salvar_fornecedor(
                    {
                        "nome": "Distribuidora Leste",
                        "contato": "Paulo Lima",
                        "telefone": "(11) 97777-2000",
                        "email": "contato@distribuidoraleste.example",
                    }
                ),
            ]
            catalogo = [
                ("ARR-5KG", "Arroz tipo 1 5kg", "Mercearia", "UN", "10", "18.90", "27.50", "40"),
                ("FEI-1KG", "Feijão carioca 1kg", "Mercearia", "UN", "15", "7.40", "11.90", "30"),
                ("OLE-900", "Óleo de soja 900ml", "Mercearia", "UN", "12", "6.10", "9.49", "12"),
                ("DET-500", "Detergente neutro 500ml", "Limpeza", "UN", "8", "1.85", "3.29", "20"),
                ("PAP-A4", "Papel sulfite A4 500 folhas", "Escritório", "PCT", "5", "22.00", "32.90", "4"),
            ]
            produtos = {}
            for codigo, nome, categoria, unidade, minimo, custo, venda, qtd in catalogo:
                produto = self.salvar_produto(
                    {
                        "codigo": codigo,
                        "nome": nome,
                        "categoria_id": categorias[categoria],
                        "unidade": unidade,
                        "estoque_minimo": Decimal(minimo),
                        "custo": Decimal(custo),
                        "preco_venda": Decimal(venda),
                        "quantidade_inicial": Decimal(qtd),
                        "data_iso": ontem,
                        "ativo": True,
                    }
                )
                produtos[codigo] = produto
            self._entrada(
                produto_id=produtos["FEI-1KG"]["id"],
                quantidade=Decimal("10"),
                custo_unitario=Decimal("7.10"),
                motivo="Compra",
                data_iso=momento,
                fornecedor_id=fornecedores[0]["id"],
                documento="NF 1520",
                observacao="Reposição semanal.",
            )
            self.registrar_saida(
                produtos["ARR-5KG"]["id"],
                Decimal("3"),
                "Venda",
                momento,
                destino="Balcão",
                documento="VENDA 88",
            )
            self.registrar_saida(
                produtos["DET-500"]["id"],
                Decimal("2"),
                "Consumo interno",
                momento,
                destino="Copa",
            )

    # ------------------------------------------------------------------ usuários

    def listar_usuarios(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, email, nome, perfil, criado_em, atualizado_em, senha_hash IS NOT NULL AS tem_senha
            FROM usuarios
            ORDER BY CASE perfil
                WHEN 'superadmin' THEN 0 WHEN 'admin' THEN 1 WHEN 'operador' THEN 2 ELSE 3 END,
                email COLLATE NOCASE
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def obter_usuario(self, usuario_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
        return _sem_senha(row)

    def obter_usuario_por_email(self, email: str | None) -> dict | None:
        if not email:
            return None
        row = self.conn.execute(
            "SELECT * FROM usuarios WHERE email = ? COLLATE NOCASE",
            (email.strip(),),
        ).fetchone()
        return _sem_senha(row)

    def verificar_login(self, email: str | None, senha: str | None) -> dict | None:
        """Devolve o usuário quando e-mail e senha conferem; senão None (sem dizer qual errou)."""
        if not email or not senha:
            return None
        row = self.conn.execute(
            "SELECT * FROM usuarios WHERE email = ? COLLATE NOCASE",
            (email.strip(),),
        ).fetchone()
        if row is None or not row["senha_hash"] or not check_password_hash(row["senha_hash"], senha):
            return None
        return _sem_senha(row)

    def precisa_primeiro_acesso(self) -> bool:
        """Verdadeiro enquanto nenhum superadmin tiver senha: o sistema ainda não foi configurado."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM usuarios WHERE perfil = 'superadmin' AND senha_hash IS NOT NULL"
        ).fetchone()
        return row["n"] == 0

    def superadmin_sem_senha(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM usuarios WHERE perfil = 'superadmin' AND senha_hash IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
        return _sem_senha(row)

    def configurar_primeiro_acesso(self, email: str, nome: str, senha: str) -> dict:
        """Cria o superadmin (ou dá senha ao que já existe). Só funciona uma vez."""
        email_limpo = clean_email(email)
        if email_limpo is None:
            raise EstoqueError("Informe o e-mail.")
        nome_limpo = clean_text(nome, label="o nome", max_len=120)
        hash_senha = _hash_senha(senha)
        momento = now()
        with self._tx():
            if not self.precisa_primeiro_acesso():
                raise EstoqueError("O sistema já foi configurado. Entre com seu e-mail e senha.")
            atual = self.obter_usuario_por_email(email_limpo)
            if atual:
                self.conn.execute(
                    """
                    UPDATE usuarios SET perfil = 'superadmin', nome = COALESCE(?, nome), senha_hash = ?,
                        atualizado_em = ? WHERE id = ?
                    """,
                    (nome_limpo, hash_senha, momento, atual["id"]),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO usuarios (email, nome, perfil, senha_hash, criado_em, atualizado_em)
                    VALUES (?, ?, 'superadmin', ?, ?, ?)
                    """,
                    (email_limpo, nome_limpo, hash_senha, momento, momento),
                )
            self.usuario_email = email_limpo
            self._registrar_atividade("Configurou o primeiro acesso", f"{email_limpo} como superadmin")
        return self.obter_usuario_por_email(email_limpo)

    def trocar_senha(self, usuario_id: int, senha_atual: str, nova: str) -> None:
        row = self.conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
        if row is None:
            raise EstoqueError("Usuário não encontrado.")
        if not row["senha_hash"] or not check_password_hash(row["senha_hash"], senha_atual or ""):
            raise EstoqueError("A senha atual não confere.")
        hash_senha = _hash_senha(nova)
        with self._tx():
            self.conn.execute(
                "UPDATE usuarios SET senha_hash = ?, atualizado_em = ? WHERE id = ?",
                (hash_senha, now(), usuario_id),
            )
            self._registrar_atividade("Trocou a própria senha", row["email"])

    def salvar_usuario(self, dados: dict) -> dict:
        """Cria (senha inicial obrigatória) ou altera um usuário; `senha` na edição redefine a senha."""
        email = clean_email(dados.get("email"))
        if email is None:
            raise EstoqueError("Informe o e-mail.")
        nome = clean_text(dados.get("nome"), label="o nome", max_len=120)
        perfil = str(dados.get("perfil") or "").strip().lower()
        if perfil not in PERFIS:
            raise EstoqueError("Perfil inválido.")
        usuario_id = dados.get("id")
        senha = dados.get("senha") or ""
        if not usuario_id and not senha:
            raise EstoqueError("Defina uma senha inicial para a pessoa.")
        hash_senha = _hash_senha(senha) if senha else None
        momento = now()
        with self._tx():
            duplicado = self.conn.execute(
                "SELECT id FROM usuarios WHERE email = ? COLLATE NOCASE AND id != ?",
                (email, usuario_id or -1),
            ).fetchone()
            if duplicado:
                raise EstoqueError("Esse e-mail já está cadastrado.")
            if usuario_id:
                atual = self.obter_usuario(usuario_id)
                if atual is None:
                    raise EstoqueError("Usuário não encontrado.")
                if atual["perfil"] == "superadmin" and perfil != "superadmin":
                    self._exigir_outro_superadmin(usuario_id)
                self.conn.execute(
                    "UPDATE usuarios SET email = ?, nome = ?, perfil = ?, atualizado_em = ? WHERE id = ?",
                    (email, nome, perfil, momento, usuario_id),
                )
                detalhe = email if atual["perfil"] == perfil else f"{email}: {atual['perfil']} → {perfil}"
                self._registrar_atividade("Alterou acesso", detalhe)
                if hash_senha:
                    self.conn.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?", (hash_senha, usuario_id))
                    self._registrar_atividade("Redefiniu senha", email)
            else:
                cur = self.conn.execute(
                    """
                    INSERT INTO usuarios (email, nome, perfil, senha_hash, criado_em, atualizado_em)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (email, nome, perfil, hash_senha, momento, momento),
                )
                usuario_id = cur.lastrowid
                self._registrar_atividade("Liberou acesso", f"{email} como {perfil}")
        return self.obter_usuario(usuario_id)

    def remover_usuario(self, usuario_id: int) -> None:
        with self._tx():
            atual = self.obter_usuario(usuario_id)
            if atual is None:
                raise EstoqueError("Usuário não encontrado.")
            if atual["perfil"] == "superadmin":
                self._exigir_outro_superadmin(usuario_id)
            self.conn.execute("DELETE FROM usuarios WHERE id = ?", (usuario_id,))
            self._registrar_atividade("Removeu acesso", atual["email"])

    def _exigir_outro_superadmin(self, usuario_id: int) -> None:
        outros = self.conn.execute(
            "SELECT COUNT(*) AS n FROM usuarios WHERE perfil = 'superadmin' AND id != ?",
            (usuario_id,),
        ).fetchone()["n"]
        if not outros:
            raise EstoqueError("O sistema precisa de pelo menos um superadmin.")

    def listar_atividades(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM atividades ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _entrada(
        self,
        *,
        produto_id: int,
        quantidade: Decimal,
        custo_unitario: Decimal,
        motivo: str,
        data_iso: str,
        fornecedor_id: int | None,
        documento: str | None,
        observacao: str | None,
    ) -> int:
        produto = self._lock_produto(produto_id, exigir_ativo=True)
        qtd = dec_qty(quantidade)
        custo = dec_money(custo_unitario)
        if qtd <= 0:
            raise EstoqueError("A quantidade deve ser maior que zero.")
        if custo < 0:
            raise EstoqueError("O custo unitário não pode ser negativo.")
        motivo, documento, _, observacao = self._dados_movimento(
            motivo, documento, observacao, destino=None
        )
        self._validar_data(data_iso)
        self._exigir_fornecedor(fornecedor_id)
        custo_medio = calcular_custo_medio(produto["quantidade"], produto["custo"], qtd, custo)
        return self._mover(
            produto=produto,
            tipo="entrada",
            delta=qtd,
            custo_posterior=custo_medio,
            custo_unitario=custo,
            motivo=motivo,
            fornecedor_id=fornecedor_id,
            documento=documento,
            destino=None,
            observacao=observacao,
            data_iso=data_iso,
        )

    def _mover(
        self,
        *,
        produto,
        tipo: str,
        delta: Decimal,
        custo_posterior: Decimal,
        custo_unitario: Decimal | None,
        motivo: str | None,
        fornecedor_id: int | None,
        documento: str | None,
        destino: str | None,
        observacao: str | None,
        data_iso: str,
        estorno_de: int | None = None,
    ) -> int:
        anterior_q = dec_qty(produto["quantidade"])
        anterior_c = dec_money(produto["custo"])
        delta = dec_qty(delta)
        if delta == 0:
            raise EstoqueError("A operação não altera o estoque.")
        nova_q = dec_qty(anterior_q + delta)
        if nova_q < 0:
            raise EstoqueError(
                f"Estoque insuficiente de {produto['nome']}. "
                f"Disponível: {format_qty(anterior_q)} {produto['unidade']}."
            )
        if nova_q > Decimal("1000000000"):
            raise EstoqueError("O saldo resultante é grande demais.")
        nova_c = dec_money(custo_posterior)
        if nova_c < 0:
            raise EstoqueError("O custo não pode ser negativo.")
        self.conn.execute(
            "UPDATE produtos SET quantidade = ?, custo = ?, atualizado_em = ? WHERE id = ?",
            (float(nova_q), float(nova_c), now(), produto["id"]),
        )
        cur = self.conn.execute(
            """
            INSERT INTO movimentacoes (
                produto_id, tipo, quantidade, quantidade_anterior, quantidade_posterior,
                custo_unitario, custo_anterior, custo_posterior, motivo, fornecedor_id,
                documento, destino, observacao, estorno_de, estornado, criado_em, usuario_email
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                produto["id"],
                tipo,
                float(delta),
                float(anterior_q),
                float(nova_q),
                None if custo_unitario is None else float(dec_money(custo_unitario)),
                float(anterior_c),
                float(nova_c),
                motivo,
                fornecedor_id,
                documento,
                destino,
                observacao,
                estorno_de,
                data_iso,
                self.usuario_email,
            ),
        )
        return int(cur.lastrowid)

    def _dados_movimento(self, motivo, documento, observacao, *, destino):
        motivo_limpo = clean_text(motivo, required=True, label="o motivo", max_len=80)
        documento_limpo = clean_text(documento, max_len=40)
        destino_limpo = clean_text(destino, label="o destino", max_len=120)
        observacao_limpa = clean_multiline(observacao, max_len=1000)
        if motivo_limpo.casefold() == "outro" and not observacao_limpa:
            raise EstoqueError("Descreva o motivo na observação quando escolher Outro.")
        return motivo_limpo, documento_limpo, destino_limpo, observacao_limpa

    def _validar_data(self, data_iso: str) -> None:
        from datetime import datetime

        try:
            datetime.strptime(data_iso, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError) as exc:
            raise EstoqueError("Data do lançamento inválida.") from exc

    def _codigo(self, value) -> str:
        import re

        codigo = clean_text(value, required=True, label="o código", max_len=30)
        codigo = codigo.upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]{0,29}", codigo):
            raise EstoqueError(
                "O código deve ter até 30 caracteres e usar letras, números, ponto, barra ou hífen."
            )
        return codigo

    def _unidade(self, value) -> str:
        import re

        unidade = clean_text(value or "UN", required=True, label="a unidade", max_len=10).upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9./]{0,9}", unidade):
            raise EstoqueError("A unidade deve ter até 10 letras ou números.")
        return unidade

    def _exigir_categoria(self, categoria_id: int | None) -> None:
        if categoria_id is None:
            return
        if self.conn.execute("SELECT 1 FROM categorias WHERE id = ?", (categoria_id,)).fetchone() is None:
            raise EstoqueError("Categoria não encontrada.")

    def _exigir_fornecedor(self, fornecedor_id: int | None) -> None:
        if fornecedor_id is None:
            return
        if self._fornecedor_row(fornecedor_id) is None:
            raise EstoqueError("Fornecedor não encontrado.")

    def _garantir_categoria(self, nome: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM categorias WHERE nome = ? COLLATE NOCASE",
            (nome,),
        ).fetchone()
        if row:
            return int(row["id"])
        return int(self.salvar_categoria(nome)["id"])

    def _lock_produto(self, produto_id: int, *, exigir_ativo: bool):
        row = self.conn.execute("SELECT * FROM produtos WHERE id = ?", (produto_id,)).fetchone()
        if row is None:
            raise EstoqueError("Produto não encontrado.")
        if exigir_ativo and not row["ativo"]:
            raise EstoqueError("Este produto está inativo. Reative-o para movimentar o estoque.")
        return row

    def _categoria_row(self, categoria_id: int):
        row = self.conn.execute("SELECT * FROM categorias WHERE id = ?", (categoria_id,)).fetchone()
        if row is None:
            raise EstoqueError("Categoria não encontrada.")
        return row

    def _categoria(self, categoria_id: int) -> dict:
        return dict(self._categoria_row(categoria_id))

    def _fornecedor_row(self, fornecedor_id: int):
        return self.conn.execute(
            "SELECT * FROM fornecedores WHERE id = ?",
            (fornecedor_id,),
        ).fetchone()

    def obter_movimento(self, movimento_id: int) -> dict | None:
        row = self.conn.execute(MOV_FROM + " WHERE m.id = ?", (movimento_id,)).fetchone()
        return dict(row) if row else None

    def _movimento(self, movimento_id: int) -> dict:
        row = self.conn.execute(MOV_FROM + " WHERE m.id = ?", (movimento_id,)).fetchone()
        if row is None:
            raise EstoqueError("Lançamento não encontrado.")
        return dict(row)
