"""Testes do sistema web: login com senha, perfis de acesso, autoria e atualização."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app import create_app
from database import SCHEMA, Database
from permissoes import PERFIS, pode

CSRF = "token-de-teste"
SUPER = "dono@exemplo.com"
SENHA = "senha-forte-1"


def email_do(perfil: str) -> str:
    return SUPER if perfil == "superadmin" else f"{perfil}@exemplo.com"


class Ambiente:
    """Banco temporário com dados e clientes HTTP logados em cada perfil."""

    def __init__(self, configurar: bool = True):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "estoque.db"
        self.app = create_app({"DB_PATH": self.db_path, "SECRET_KEY": "teste", "TESTING": True})
        if configurar:
            self._popular()

    def _popular(self):
        db = self.abrir()
        try:
            db.configurar_primeiro_acesso(SUPER, "Dono", SENHA)
            for perfil in PERFIS:
                if perfil != "superadmin":
                    db.salvar_usuario({"email": email_do(perfil), "perfil": perfil, "senha": SENHA})
            db.salvar_usuario({"email": "alvo@exemplo.com", "perfil": "consulta", "senha": SENHA})
            self.categoria_livre = db.salvar_categoria("Sem uso")["id"]
            self.fornecedor = db.salvar_fornecedor({"nome": "Atacado"})["id"]
            self.produto = db.salvar_produto({
                "codigo": "ARR-1", "nome": "Arroz", "unidade": "UN", "estoque_minimo": Decimal("1"),
                "custo": Decimal("10"), "preco_venda": Decimal("15"), "quantidade_inicial": Decimal("50"),
            })["id"]
            db.salvar_produto({
                "codigo": "FEI-1", "nome": "Feijão", "unidade": "UN", "custo": Decimal("5"),
                "quantidade_inicial": Decimal("8"),
            })
            self.mov_estornavel = db.listar_movimentos(busca="FEI-1")[0][0]["id"]
            self.alvo = db.obter_usuario_por_email("alvo@exemplo.com")["id"]
        finally:
            db.close()

    def abrir(self) -> Database:
        return Database(self.db_path, inicializar=False)

    def cliente(self, email: str | None):
        cliente = self.app.test_client()
        with cliente.session_transaction() as sessao:
            sessao["csrf"] = CSRF
            if email:
                sessao["email"] = email
        return cliente

    def fechar(self):
        self.tmp.cleanup()


def rotas(amb: Ambiente) -> list[tuple[str, str, dict | None, str]]:
    """(método, url, dados do POST, ação exigida) de todas as rotas protegidas."""
    p, f, c = amb.produto, amb.fornecedor, amb.categoria_livre
    return [
        ("GET", "/", None, "ver"),
        ("GET", "/produtos", None, "ver"),
        ("GET", "/movimentos", None, "ver"),
        ("GET", f"/movimentos/{amb.mov_estornavel}", None, "ver"),
        ("GET", "/fornecedores", None, "ver"),
        ("GET", "/categorias", None, "ver"),
        ("GET", "/conta", None, "ver"),
        ("GET", "/produtos/novo", None, "editar_cadastro"),
        ("GET", f"/produtos/{p}/editar", None, "editar_cadastro"),
        ("GET", "/entrada", None, "movimentar"),
        ("GET", "/saida", None, "movimentar"),
        ("GET", "/ajuste", None, "movimentar"),
        ("GET", "/exportar/produtos", None, "exportar"),
        ("GET", "/exportar/movimentos", None, "exportar"),
        ("GET", "/atividades", None, "ver_atividades"),
        ("GET", "/usuarios", None, "gerenciar_usuarios"),
        ("POST", "/entrada", {"produto_id": p, "quantidade": "5", "custo": "12,00", "motivo": "Compra",
                              "data": "2026-11-01"}, "movimentar"),
        ("POST", "/saida", {"produto_id": p, "quantidade": "2", "motivo": "Venda", "data": "2026-11-01"},
         "movimentar"),
        ("POST", "/ajuste", {"produto_id": p, "quantidade": "33", "motivo": "Contagem de inventário",
                             "data": "2026-11-01"}, "movimentar"),
        ("POST", "/produtos/novo", {"codigo": "NOVO-1", "nome": "Novo", "unidade": "UN"}, "editar_cadastro"),
        ("POST", f"/produtos/{p}/editar", {"codigo": "ARR-1", "nome": "Arroz 5kg", "unidade": "UN",
                                           "custo": "11,00", "ativo": "1"}, "editar_cadastro"),
        ("POST", "/fornecedores/salvar", {"nome": "Distribuidora"}, "editar_cadastro"),
        ("POST", "/categorias/salvar", {"nome": "Bebidas quentes"}, "editar_cadastro"),
        ("POST", f"/produtos/{p}/situacao", {"ativo": "0"}, "excluir_cadastro"),
        ("POST", f"/fornecedores/{f}/excluir", {}, "excluir_cadastro"),
        ("POST", f"/categorias/{c}/excluir", {}, "excluir_cadastro"),
        ("POST", f"/movimentos/{amb.mov_estornavel}/estornar", {}, "estornar"),
        ("POST", "/usuarios/salvar", {"email": "novo@exemplo.com", "perfil": "operador", "senha": SENHA},
         "gerenciar_usuarios"),
        ("POST", f"/usuarios/{amb.alvo}/remover", {}, "gerenciar_usuarios"),
        ("POST", "/backup", {}, "sistema"),
        ("POST", "/exemplo", {}, "sistema"),
    ]


class TestePermissoes(unittest.TestCase):
    def test_cada_perfil_so_executa_o_que_a_tabela_permite(self):
        for perfil in PERFIS:
            amb = Ambiente()
            try:
                cliente = amb.cliente(email_do(perfil))
                for metodo, url, dados, acao in rotas(amb):
                    with self.subTest(perfil=perfil, metodo=metodo, url=url):
                        if metodo == "GET":
                            resposta = cliente.get(url)
                        else:
                            resposta = cliente.post(url, data={**dados, "csrf": CSRF})
                        if pode(perfil, acao):
                            if resposta.status_code not in (200, 302):
                                self.fail(f"{resposta.status_code}: {resposta.get_data()[:400]!r}")
                        else:
                            self.assertEqual(resposta.status_code, 403)
            finally:
                amb.fechar()

    def test_tabela_de_permissoes_combinada_com_o_usuario(self):
        esperado = {
            "superadmin": {"ver", "movimentar", "editar_cadastro", "exportar", "excluir_cadastro",
                           "ver_atividades", "estornar", "gerenciar_usuarios", "sistema"},
            "admin": {"ver", "movimentar", "editar_cadastro", "exportar", "excluir_cadastro", "ver_atividades"},
            "operador": {"ver", "movimentar", "editar_cadastro", "exportar"},
            "consulta": {"ver"},
        }
        from permissoes import PERMISSOES

        for perfil, acoes in esperado.items():
            self.assertEqual({acao for acao in PERMISSOES if pode(perfil, acao)}, acoes, perfil)

    def test_operador_nao_desativa_produto_pelo_formulario_de_edicao(self):
        amb = Ambiente()
        try:
            cliente = amb.cliente(email_do("operador"))
            resposta = cliente.post(f"/produtos/{amb.produto}/editar", data={
                "codigo": "ARR-1", "nome": "Arroz", "unidade": "UN", "csrf": CSRF,
            })  # sem o campo "ativo", que desativaria o produto
            self.assertEqual(resposta.status_code, 302)
            db = amb.abrir()
            try:
                self.assertTrue(db.obter_produto(amb.produto)["ativo"])
            finally:
                db.close()
        finally:
            amb.fechar()

    def test_menu_e_indicador_mostram_o_perfil(self):
        amb = Ambiente()
        try:
            html_visualizador = amb.cliente(email_do("consulta")).get("/").get_data(as_text=True)
            html_super = amb.cliente(SUPER).get("/").get_data(as_text=True)
            self.assertNotIn('href="/usuarios"', html_visualizador)
            self.assertNotIn('href="/entrada"', html_visualizador)
            self.assertIn("Visualizador", html_visualizador)
            self.assertIn("Conectado como", html_visualizador)
            self.assertIn('href="/usuarios"', html_super)
            detalhe = f"/movimentos/{amb.mov_estornavel}"
            self.assertNotIn("Estornar lançamento",
                             amb.cliente(email_do("admin")).get(detalhe).get_data(as_text=True))
            self.assertIn("Estornar lançamento", amb.cliente(SUPER).get(detalhe).get_data(as_text=True))
        finally:
            amb.fechar()


class TesteLogin(unittest.TestCase):
    def setUp(self):
        self.amb = Ambiente()

    def tearDown(self):
        self.amb.fechar()

    def _entrar(self, email, senha):
        cliente = self.amb.cliente(None)
        resposta = cliente.post("/login", data={"email": email, "senha": senha, "csrf": CSRF})
        return cliente, resposta

    def test_sem_login_vai_para_a_tela_de_login(self):
        cliente = self.amb.cliente(None)
        self.assertEqual(cliente.get("/").headers["Location"], "/login")
        self.assertEqual(cliente.get("/api/versao").status_code, 401)

    def test_email_e_senha_corretos_entram(self):
        cliente, resposta = self._entrar("Operador@Exemplo.com", SENHA)
        self.assertEqual(resposta.headers["Location"], "/")
        self.assertEqual(cliente.get("/").status_code, 200)

    def test_senha_errada_ou_email_desconhecido_nao_entram(self):
        for email, senha in ((email_do("operador"), "errada123"), ("intruso@exemplo.com", SENHA), (SUPER, "")):
            with self.subTest(email=email):
                cliente, resposta = self._entrar(email, senha)
                self.assertEqual(resposta.status_code, 401)
                self.assertIn("E-mail ou senha incorretos.", resposta.get_data(as_text=True))
                self.assertEqual(cliente.get("/").headers["Location"], "/login")

    def test_senha_fica_criptografada_no_banco(self):
        conn = sqlite3.connect(self.amb.db_path)
        try:
            guardadas = [linha[0] for linha in conn.execute("SELECT senha_hash FROM usuarios")]
        finally:
            conn.close()
        self.assertTrue(guardadas)
        for guardada in guardadas:
            self.assertNotIn(SENHA, guardada)
            self.assertNotEqual(guardada, SENHA)

    def test_trocar_a_propria_senha_exige_a_atual(self):
        cliente = self.amb.cliente(email_do("operador"))
        errada = cliente.post("/conta", data={"atual": "nao-e-essa", "nova": "nova-senha-2",
                                              "confirmacao": "nova-senha-2", "csrf": CSRF})
        self.assertEqual(errada.status_code, 400)
        certa = cliente.post("/conta", data={"atual": SENHA, "nova": "nova-senha-2",
                                             "confirmacao": "nova-senha-2", "csrf": CSRF})
        self.assertEqual(certa.status_code, 302)
        self.assertEqual(self._entrar(email_do("operador"), SENHA)[1].status_code, 401)
        self.assertEqual(self._entrar(email_do("operador"), "nova-senha-2")[1].status_code, 302)

    def test_superadmin_redefine_a_senha_de_outra_pessoa(self):
        db = self.amb.abrir()
        try:
            operador = db.obter_usuario_por_email(email_do("operador"))
        finally:
            db.close()
        self.amb.cliente(SUPER).post("/usuarios/salvar", data={
            "id": operador["id"], "email": operador["email"], "perfil": "operador",
            "senha": "redefinida-3", "csrf": CSRF,
        })
        self.assertEqual(self._entrar(email_do("operador"), "redefinida-3")[1].status_code, 302)

    def test_cadastro_sem_senha_ou_com_senha_curta_e_recusado(self):
        cliente = self.amb.cliente(SUPER)
        for senha in ("", "123"):
            with self.subTest(senha=senha):
                resposta = cliente.post("/usuarios/salvar", data={
                    "email": "sem.senha@exemplo.com", "perfil": "operador", "senha": senha, "csrf": CSRF,
                })
                self.assertEqual(resposta.status_code, 400)

    def test_acesso_removido_encerra_a_sessao_na_hora(self):
        cliente = self.amb.cliente(email_do("operador"))
        self.assertEqual(cliente.get("/").status_code, 200)
        db = self.amb.abrir()
        try:
            db.remover_usuario(db.obter_usuario_por_email(email_do("operador"))["id"])
        finally:
            db.close()
        self.assertEqual(cliente.get("/").headers["Location"], "/login")
        with cliente.session_transaction() as sessao:
            self.assertNotIn("email", sessao)

    def test_post_sem_token_csrf_e_recusado(self):
        cliente = self.amb.cliente(SUPER)
        resposta = cliente.post("/categorias/salvar", data={"nome": "Sem token", "csrf": "outro"})
        self.assertEqual(resposta.status_code, 400)

    def test_redirecionamento_de_volta_nao_sai_do_sistema(self):
        cliente = self.amb.cliente(SUPER)
        resposta = cliente.post(f"/produtos/{self.amb.produto}/situacao",
                                data={"ativo": "0", "voltar": "//site-malicioso.com", "csrf": CSRF})
        self.assertEqual(resposta.headers["Location"], "/produtos")


class TestePrimeiroAcesso(unittest.TestCase):
    def setUp(self):
        self.amb = Ambiente(configurar=False)

    def tearDown(self):
        self.amb.fechar()

    def _configurar(self, cliente, email, senha="primeira-1", confirmacao=None):
        return cliente.post("/primeiro-acesso", data={
            "email": email, "nome": "Dono", "senha": senha,
            "confirmacao": senha if confirmacao is None else confirmacao, "csrf": CSRF,
        })

    def test_sistema_novo_leva_ao_primeiro_acesso(self):
        cliente = self.amb.cliente(None)
        self.assertEqual(cliente.get("/").headers["Location"], "/primeiro-acesso")
        self.assertEqual(cliente.get("/login").headers["Location"], "/primeiro-acesso")

    def test_primeiro_acesso_cria_superadmin_e_so_funciona_uma_vez(self):
        cliente = self.amb.cliente(None)
        resposta = self._configurar(cliente, "chefe@exemplo.com")
        self.assertEqual(resposta.headers["Location"], "/")
        self.assertIn("Superadmin", cliente.get("/").get_data(as_text=True))
        intruso = self.amb.cliente(None)
        self.assertEqual(intruso.get("/primeiro-acesso").headers["Location"], "/login")
        self._configurar(intruso, "intruso@exemplo.com")
        db = self.amb.abrir()
        try:
            self.assertIsNone(db.obter_usuario_por_email("intruso@exemplo.com"))
            with self.assertRaises(Exception):
                db.configurar_primeiro_acesso("intruso@exemplo.com", "Intruso", "qualquer-1")
            self.assertIsNone(db.obter_usuario_por_email("intruso@exemplo.com"))
        finally:
            db.close()

    def test_superadmin_cadastrado_sem_senha_recebe_a_senha(self):
        db = self.amb.abrir()
        try:
            db.salvar_usuario({"email": "chefe@exemplo.com", "perfil": "superadmin", "senha": "provisoria-1"})
            db.conn.execute("UPDATE usuarios SET senha_hash = NULL")
        finally:
            db.close()
        cliente = self.amb.cliente(None)
        self.assertIn("chefe@exemplo.com", cliente.get("/primeiro-acesso").get_data(as_text=True))
        self._configurar(cliente, "chefe@exemplo.com")
        db = self.amb.abrir()
        try:
            self.assertEqual(len(db.listar_usuarios()), 1)
            self.assertIsNotNone(db.verificar_login("chefe@exemplo.com", "primeira-1"))
        finally:
            db.close()

    def test_confirmacao_diferente_nao_configura(self):
        cliente = self.amb.cliente(None)
        resposta = self._configurar(cliente, "chefe@exemplo.com", confirmacao="outra-senha")
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(cliente.get("/").headers["Location"], "/primeiro-acesso")


class TesteAutoriaEVersao(unittest.TestCase):
    def setUp(self):
        self.amb = Ambiente()

    def tearDown(self):
        self.amb.fechar()

    def test_lancamento_registra_quem_fez(self):
        cliente = self.amb.cliente(email_do("operador"))
        cliente.post("/saida", data={"produto_id": self.amb.produto, "quantidade": "3", "motivo": "Venda",
                                     "data": "2026-11-01", "csrf": CSRF})
        db = self.amb.abrir()
        try:
            ultimo = db.obter_movimento(
                db.conn.execute("SELECT MAX(id) FROM movimentacoes").fetchone()[0]
            )
            self.assertEqual(ultimo["tipo"], "saida")
            self.assertEqual(ultimo["usuario_email"], email_do("operador"))
        finally:
            db.close()
        self.assertIn(email_do("operador"), cliente.get("/movimentos").get_data(as_text=True))

    def test_exclusao_fica_no_registro_de_atividades(self):
        cliente = self.amb.cliente(email_do("admin"))
        cliente.post(f"/fornecedores/{self.amb.fornecedor}/excluir", data={"csrf": CSRF})
        db = self.amb.abrir()
        try:
            atividade = db.listar_atividades()[0]
        finally:
            db.close()
        self.assertEqual((atividade["acao"], atividade["detalhe"]), ("Excluiu fornecedor", "Atacado"))
        self.assertEqual(atividade["usuario_email"], email_do("admin"))

    def test_versao_muda_quando_alguem_grava(self):
        cliente = self.amb.cliente(email_do("consulta"))
        antes = cliente.get("/api/versao").get_json()["versao"]
        self.amb.cliente(email_do("operador")).post("/categorias/salvar", data={"nome": "Nova", "csrf": CSRF})
        depois = cliente.get("/api/versao").get_json()["versao"]
        self.assertGreater(depois, antes)
        self.assertEqual(cliente.get("/api/versao").get_json()["versao"], depois)

    def test_sistema_precisa_de_um_superadmin(self):
        db = self.amb.abrir()
        try:
            super_id = db.obter_usuario_por_email(SUPER)["id"]
            with self.assertRaises(Exception):
                db.remover_usuario(super_id)
            with self.assertRaises(Exception):
                db.salvar_usuario({"id": super_id, "email": SUPER, "perfil": "admin"})
            db.salvar_usuario({"email": "outro@exemplo.com", "perfil": "superadmin", "senha": SENHA})
            db.salvar_usuario({"id": super_id, "email": SUPER, "perfil": "admin"})
            self.assertEqual(db.obter_usuario(super_id)["perfil"], "admin")
        finally:
            db.close()


class TesteMigracao(unittest.TestCase):
    def test_banco_de_versoes_anteriores_ganha_as_colunas_novas_sem_perder_dados(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "antigo.db"
            # Esquema da versão desktop: sem usuario_email e sem as tabelas novas.
            antigo = SCHEMA.split("CREATE TABLE IF NOT EXISTS usuarios")[0]
            antigo = antigo.replace(",\n    usuario_email TEXT\n", "\n")
            self.assertNotIn("usuario_email", antigo)
            conn = sqlite3.connect(caminho)
            conn.executescript(antigo)
            conn.executescript(
                """
                INSERT INTO produtos (id, codigo, nome, quantidade, criado_em, atualizado_em)
                    VALUES (1, 'A', 'Antigo', 3, '2026-01-01 10:00:00', '2026-01-01 10:00:00');
                INSERT INTO movimentacoes (produto_id, tipo, quantidade, quantidade_anterior,
                    quantidade_posterior, custo_anterior, custo_posterior, criado_em)
                    VALUES (1, 'entrada', 3, 0, 3, 0, 0, '2026-01-01 10:00:00');
                -- Tabela de usuários da versão com Google: sem senha.
                CREATE TABLE usuarios (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, nome TEXT,
                    perfil TEXT NOT NULL, criado_em TEXT NOT NULL, atualizado_em TEXT NOT NULL
                );
                INSERT INTO usuarios (email, perfil, criado_em, atualizado_em)
                    VALUES ('chefe@exemplo.com', 'superadmin', '2026-01-01', '2026-01-01');
                """
            )
            conn.close()
            db = Database(caminho)
            try:
                colunas = {r["name"] for r in db.conn.execute("PRAGMA table_info(movimentacoes)")}
                self.assertIn("usuario_email", colunas)
                colunas = {r["name"] for r in db.conn.execute("PRAGMA table_info(usuarios)")}
                self.assertIn("senha_hash", colunas)
                self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM movimentacoes").fetchone()[0], 1)
                self.assertEqual(db.conn.execute("SELECT nome FROM produtos").fetchone()[0], "Antigo")
                self.assertTrue(db.precisa_primeiro_acesso())
                self.assertEqual(db.superadmin_sem_senha()["email"], "chefe@exemplo.com")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
