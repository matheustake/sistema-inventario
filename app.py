"""Sistema de estoque no navegador: login com e-mail e senha, perfis de acesso e telas.

Tudo roda neste computador: sem internet, sem chaves e sem contas externas.
"""

from __future__ import annotations

import hmac
import io
import os
import secrets
import tempfile
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from database import SENHA_MINIMA, Database
from exportacao import csv_movimentos, csv_produtos, rotulo_tipo
from permissoes import DESCRICAO_PERFIS, NOMES_PERFIS, PERFIS, pode
from util import (
    EstoqueError,
    dec_qty,
    format_datetime,
    format_delta,
    format_input_money,
    format_input_qty,
    format_int,
    format_money,
    format_qty,
    parse_data,
    parse_data_filtro,
    parse_money,
    parse_qty,
    valor_estoque,
)

ROOT = Path(__file__).resolve().parent
VERSAO = "2.1"

UNIDADES = ("UN", "CX", "KG", "G", "L", "ML", "M", "PCT", "PAR", "FD")
MOTIVOS = {
    "entrada": ["Compra", "Devolução de cliente", "Estoque inicial", "Transferência recebida", "Outro"],
    "saida": ["Venda", "Consumo interno", "Perda ou avaria", "Devolução ao fornecedor", "Transferência enviada", "Outro"],
    "ajuste": ["Contagem de inventário", "Correção de lançamento", "Outro"],
}
TITULOS = {
    "entrada": ("Entrada de mercadoria", "Registra compras, devoluções e outros recebimentos. O custo médio é recalculado."),
    "saida": ("Saída de mercadoria", "Registra vendas, consumo, perdas e devoluções. O saldo não fica negativo."),
    "ajuste": ("Ajuste de estoque", "Iguala o saldo à quantidade contada e preserva o histórico."),
}

# Rotas que não exigem login.
PUBLICAS = {"login", "primeiro_acesso", "static"}


def _chave_secreta(pasta_dados: Path) -> str:
    """Chave que assina o cookie de sessão: gerada uma vez e guardada em data/secret_key."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    arquivo = pasta_dados / "secret_key"
    if arquivo.exists():
        return arquivo.read_text(encoding="utf-8").strip()
    pasta_dados.mkdir(parents=True, exist_ok=True)
    chave = secrets.token_hex(32)
    arquivo.write_text(chave, encoding="utf-8")
    return chave


def requer(acao: str):
    """Recusa a requisição (403) quando o perfil logado não pode executar a ação."""

    def decorador(view):
        @wraps(view)
        def envolvida(*args, **kwargs):
            if not g.usuario or not pode(g.usuario["perfil"], acao):
                abort(403)
            return view(*args, **kwargs)

        return envolvida

    return decorador


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    db_path = Path((config or {}).get("DB_PATH") or ROOT / "data" / "estoque.db")
    app.config.update(
        DB_PATH=db_path,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    if config:
        app.config.update(config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _chave_secreta(db_path.parent)

    # Cria tabelas e atualiza bancos de versões anteriores sem perder dados.
    Database(app.config["DB_PATH"]).close()

    _registrar_filtros(app)
    _registrar_ciclo(app)
    _registrar_rotas(app)
    return app


# ---------------------------------------------------------------------------- ciclo


def _registrar_filtros(app: Flask) -> None:
    app.jinja_env.filters.update(
        money=format_money,
        qty=format_qty,
        delta=format_delta,
        dt=format_datetime,
        inteiro=format_int,
        tipo=rotulo_tipo,
        input_qty=format_input_qty,
        input_money=format_input_money,
        perfil=lambda valor: NOMES_PERFIS.get(valor, valor),
    )

    @app.context_processor
    def contexto():
        usuario = getattr(g, "usuario", None)
        return {
            "usuario": usuario,
            "pode": lambda acao: bool(usuario) and pode(usuario["perfil"], acao),
            "csrf_token": session.get("csrf", ""),
            "versao_dados": g.db.versao() if getattr(g, "db", None) and usuario else "",
            "atencao_total": g.db.total_atencao() if getattr(g, "db", None) and usuario else 0,
            "versao_app": VERSAO,
            "pagina": request.endpoint,
            "senha_minima": SENHA_MINIMA,
            "descricao_perfil": DESCRICAO_PERFIS.get(usuario["perfil"], "") if usuario else "",
        }


def _registrar_ciclo(app: Flask) -> None:
    @app.before_request
    def carregar_usuario():
        g.usuario = None
        g.db = None
        if request.endpoint == "static":
            return None
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        g.db = Database(app.config["DB_PATH"], inicializar=False)
        email = session.get("email")
        if email:
            usuario = g.db.obter_usuario_por_email(email)
            if usuario is None or not usuario["tem_senha"]:
                # Acesso removido pelo superadmin: encerra a sessão na hora.
                session.clear()
                session["csrf"] = secrets.token_urlsafe(32)
            else:
                g.usuario = usuario
                g.db.usuario_email = usuario["email"]
        if request.method == "POST":
            enviado = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
            esperado = session.get("csrf") or ""
            if not esperado or not hmac.compare_digest(enviado, esperado):
                abort(400)
        if g.usuario is None and request.endpoint != "primeiro_acesso" and g.db.precisa_primeiro_acesso():
            return redirect(url_for("primeiro_acesso"))
        if request.endpoint not in PUBLICAS and g.usuario is None:
            if request.path.startswith("/api/"):
                return jsonify({"erro": "login"}), 401
            return redirect(url_for("login"))
        return None

    @app.teardown_request
    def fechar_db(_exc):
        db = getattr(g, "db", None)
        if db is not None:
            db.close()

    @app.errorhandler(403)
    def sem_permissao(_exc):
        return render_template("erro.html", titulo="Sem permissão", mensagem=(
            "Seu perfil não pode fazer essa ação. Se precisar, fale com o superadmin."
        )), 403

    @app.errorhandler(400)
    def requisicao_invalida(_exc):
        return render_template("erro.html", titulo="Requisição inválida", mensagem=(
            "A página expirou ou o formulário veio de outro lugar. Volte e tente de novo."
        )), 400

    @app.errorhandler(404)
    def nao_encontrado(_exc):
        return render_template("erro.html", titulo="Não encontrado", mensagem="Essa página não existe."), 404


def _entrar(usuario: dict) -> Response:
    """Abre a sessão com um cookie novo (evita reaproveitar uma sessão anterior)."""
    session.clear()
    session.permanent = True
    session["email"] = usuario["email"]
    session["csrf"] = secrets.token_urlsafe(32)
    return redirect(url_for("painel"))


# ---------------------------------------------------------------------------- rotas


def _erro(exc: Exception) -> None:
    flash(str(exc), "erro")


def _int(valor) -> int | None:
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return None
    return numero if numero > 0 else None


def _registrar_rotas(app: Flask) -> None:  # noqa: C901 - lista de rotas
    # ------------------------------------------------------------ login

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.usuario:
            return redirect(url_for("painel"))
        email = ""
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            usuario = g.db.verificar_login(email, request.form.get("senha", ""))
            if usuario:
                return _entrar(usuario)
            flash("E-mail ou senha incorretos.", "erro")
            return render_template("login.html", email=email), 401
        return render_template("login.html", email=email)

    @app.route("/primeiro-acesso", methods=["GET", "POST"])
    def primeiro_acesso():
        if not g.db.precisa_primeiro_acesso():
            return redirect(url_for("login"))
        existente = g.db.superadmin_sem_senha()
        form = {"email": existente["email"] if existente else "", "nome": (existente or {}).get("nome") or ""}
        if request.method == "POST":
            form = {"email": request.form.get("email", ""), "nome": request.form.get("nome", "")}
            senha = request.form.get("senha", "")
            try:
                if senha != request.form.get("confirmacao", ""):
                    raise EstoqueError("A confirmação não é igual à senha.")
                usuario = g.db.configurar_primeiro_acesso(form["email"], form["nome"], senha)
            except (EstoqueError, ValueError) as exc:
                _erro(exc)
                return render_template("primeiro_acesso.html", form=form), 400
            flash("Superadmin configurado. Bem-vindo!", "ok")
            return _entrar(usuario)
        return render_template("primeiro_acesso.html", form=form)

    @app.route("/conta", methods=["GET", "POST"])
    def conta():
        if request.method == "POST":
            nova = request.form.get("nova", "")
            try:
                if nova != request.form.get("confirmacao", ""):
                    raise EstoqueError("A confirmação não é igual à nova senha.")
                g.db.trocar_senha(g.usuario["id"], request.form.get("atual", ""), nova)
            except (EstoqueError, ValueError) as exc:
                _erro(exc)
                return render_template("conta.html"), 400
            flash("Senha alterada.", "ok")
            return redirect(url_for("conta"))
        return render_template("conta.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/api/versao")
    def api_versao():
        return jsonify({"versao": g.db.versao()})

    # ------------------------------------------------------------ painel

    @app.get("/")
    def painel():
        return render_template("painel.html", resumo=g.db.resumo())

    @app.post("/exemplo")
    @requer("sistema")
    def carregar_exemplo():
        try:
            g.db.carregar_exemplo()
        except (EstoqueError, ValueError) as exc:
            _erro(exc)
        else:
            flash("Dados de exemplo carregados.", "ok")
        return redirect(url_for("painel"))

    @app.post("/backup")
    @requer("sistema")
    def backup():
        with tempfile.TemporaryDirectory() as pasta:
            destino = Path(pasta) / "copia.db"
            g.db.backup(destino)
            conteudo = destino.read_bytes()
        nome = f"estoque-backup-{datetime.now():%Y%m%d-%H%M}.db"
        return send_file(io.BytesIO(conteudo), as_attachment=True, download_name=nome,
                         mimetype="application/octet-stream")

    # ------------------------------------------------------------ produtos

    def _filtros_produtos():
        situacao = request.args.get("situacao", "ativos")
        if situacao not in {"ativos", "inativos", "todos"}:
            situacao = "ativos"
        return {
            "busca": request.args.get("busca", ""),
            "categoria": _int(request.args.get("categoria")),
            "situacao": situacao,
            "atencao": request.args.get("atencao") == "1",
        }

    def _listar_produtos(filtros):
        return g.db.listar_produtos(
            busca=filtros["busca"],
            categoria_id=filtros["categoria"],
            situacao="ativos" if filtros["atencao"] else filtros["situacao"],
            somente_atencao=filtros["atencao"],
        )

    @app.get("/produtos")
    def produtos():
        filtros = _filtros_produtos()
        lista = _listar_produtos(filtros)
        for produto in lista:
            produto["valor"] = valor_estoque(produto["quantidade"], produto["custo"])
            produto["baixo"] = bool(produto["ativo"]) and dec_qty(produto["quantidade"]) <= dec_qty(
                produto["estoque_minimo"]
            )
        return render_template(
            "produtos.html",
            produtos=lista,
            filtros=filtros,
            categorias=g.db.listar_categorias(),
            em_atencao=sum(1 for p in lista if p["baixo"]),
        )

    def _form_produto(produto=None, sugestao=""):
        if produto is None:
            return {
                "codigo": sugestao, "nome": "", "categoria_id": "", "unidade": "UN",
                "estoque_minimo": "0", "custo": "0,00", "preco_venda": "0,00",
                "descricao": "", "ativo": True, "quantidade_inicial": "",
            }
        return {
            "codigo": produto["codigo"],
            "nome": produto["nome"],
            "categoria_id": produto["categoria_id"] or "",
            "unidade": produto["unidade"],
            "estoque_minimo": format_input_qty(produto["estoque_minimo"]),
            "custo": format_input_money(produto["custo"]),
            "preco_venda": format_input_money(produto["preco_venda"]),
            "descricao": produto.get("descricao") or "",
            "ativo": bool(produto["ativo"]),
        }

    def _salvar_produto(produto):
        form = request.form
        pode_ativo = pode(g.usuario["perfil"], "excluir_cadastro")
        if pode_ativo:
            ativo = form.get("ativo") == "1"
        else:
            # Sem permissão de desativar: mantém a situação atual (novo produto nasce ativo).
            ativo = bool(produto["ativo"]) if produto else True
        dados = {
            "codigo": form.get("codigo", ""),
            "nome": form.get("nome", ""),
            "categoria_id": _int(form.get("categoria_id")),
            "unidade": form.get("unidade", ""),
            "estoque_minimo": parse_qty(form.get("estoque_minimo") or "0", allow_zero=True),
            "custo": parse_money(form.get("custo") or "0"),
            "preco_venda": parse_money(form.get("preco_venda") or "0"),
            "descricao": form.get("descricao", ""),
            "ativo": ativo,
        }
        if produto is None:
            texto = (form.get("quantidade_inicial") or "").strip()
            dados["quantidade_inicial"] = parse_qty(texto, allow_zero=True) if texto else 0
            dados["data_iso"] = parse_data(date.today().strftime("%d/%m/%Y"))
        else:
            dados["id"] = produto["id"]
        return g.db.salvar_produto(dados)

    @app.route("/produtos/novo", methods=["GET", "POST"])
    @requer("editar_cadastro")
    def produto_novo():
        if request.method == "POST":
            try:
                salvo = _salvar_produto(None)
            except (EstoqueError, ValueError) as exc:
                _erro(exc)
                form = request.form.to_dict()
                form["ativo"] = request.form.get("ativo") == "1"
                return render_template("produto_form.html", produto=None, form=form,
                                       categorias=g.db.listar_categorias(), unidades=UNIDADES), 400
            flash(f"Produto {salvo['codigo']} cadastrado.", "ok")
            return redirect(url_for("produtos"))
        return render_template("produto_form.html", produto=None, form=_form_produto(sugestao=g.db.sugerir_codigo()),
                               categorias=g.db.listar_categorias(), unidades=UNIDADES)

    @app.route("/produtos/<int:produto_id>/editar", methods=["GET", "POST"])
    @requer("editar_cadastro")
    def produto_editar(produto_id):
        produto = g.db.obter_produto(produto_id)
        if produto is None:
            abort(404)
        if request.method == "POST":
            try:
                salvo = _salvar_produto(produto)
            except (EstoqueError, ValueError) as exc:
                _erro(exc)
                form = request.form.to_dict()
                form["ativo"] = request.form.get("ativo") == "1" if pode(g.usuario["perfil"], "excluir_cadastro") \
                    else bool(produto["ativo"])
                return render_template("produto_form.html", produto=produto, form=form,
                                       categorias=g.db.listar_categorias(), unidades=UNIDADES), 400
            flash(f"Produto {salvo['codigo']} salvo.", "ok")
            return redirect(url_for("produtos"))
        return render_template("produto_form.html", produto=produto, form=_form_produto(produto),
                               categorias=g.db.listar_categorias(), unidades=UNIDADES)

    @app.post("/produtos/<int:produto_id>/situacao")
    @requer("excluir_cadastro")
    def produto_situacao(produto_id):
        produto = g.db.obter_produto(produto_id)
        if produto is None:
            abort(404)
        ativar = request.form.get("ativo") == "1"
        try:
            g.db.salvar_produto({
                "id": produto["id"], "codigo": produto["codigo"], "nome": produto["nome"],
                "categoria_id": produto["categoria_id"], "unidade": produto["unidade"],
                "estoque_minimo": produto["estoque_minimo"], "custo": produto["custo"],
                "preco_venda": produto["preco_venda"], "descricao": produto["descricao"],
                "ativo": ativar,
            })
        except (EstoqueError, ValueError) as exc:
            _erro(exc)
        else:
            flash("Produto reativado." if ativar else "Produto desativado.", "ok")
        voltar = request.form.get("voltar") or ""
        if not voltar.startswith("/") or voltar.startswith("//"):
            voltar = url_for("produtos")
        return redirect(voltar)

    @app.get("/exportar/produtos")
    @requer("exportar")
    def exportar_produtos():
        filtros = _filtros_produtos()
        if request.args.get("tudo") == "1":
            lista = g.db.listar_produtos(situacao="todos")
        else:
            lista = _listar_produtos(filtros)
        nome = f"produtos-{datetime.now():%Y%m%d-%H%M}.csv"
        return Response(csv_produtos(lista), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={nome}"})

    # ------------------------------------------------------------ operações

    @app.route("/<any(entrada, saida, ajuste):modo>", methods=["GET", "POST"])
    @requer("movimentar")
    def operacao(modo):
        db = g.db
        form = {
            "produto_id": request.args.get("produto", ""),
            "quantidade": "",
            "custo": "",
            "motivo": MOTIVOS[modo][0],
            "fornecedor_id": "",
            "destino": "",
            "documento": "",
            "data": date.today().isoformat(),
            "observacao": "",
        }
        status = 200
        if request.method == "POST":
            form.update(request.form.to_dict())
            try:
                produto_id = _int(form.get("produto_id"))
                if produto_id is None:
                    raise ValueError("Selecione um produto.")
                data_iso = parse_data(form.get("data", ""))
                comum = {
                    "documento": form.get("documento"),
                    "observacao": form.get("observacao"),
                }
                if modo == "entrada":
                    mov = db.registrar_entrada(
                        produto_id,
                        parse_qty(form.get("quantidade", "")),
                        parse_money(form.get("custo") or "0"),
                        form.get("motivo", ""),
                        data_iso,
                        fornecedor_id=_int(form.get("fornecedor_id")),
                        **comum,
                    )
                elif modo == "saida":
                    mov = db.registrar_saida(
                        produto_id,
                        parse_qty(form.get("quantidade", "")),
                        form.get("motivo", ""),
                        data_iso,
                        destino=form.get("destino"),
                        **comum,
                    )
                else:
                    mov = db.registrar_ajuste(
                        produto_id,
                        parse_qty(form.get("quantidade", ""), allow_zero=True),
                        form.get("motivo", ""),
                        data_iso,
                        **comum,
                    )
            except (EstoqueError, ValueError) as exc:
                _erro(exc)
                status = 400
            else:
                flash(
                    f"Lançamento registrado. {mov['produto_nome']} está com "
                    f"{format_qty(mov['quantidade_posterior'])} {mov['produto_unidade']}.",
                    "ok",
                )
                return redirect(url_for("operacao", modo=modo, produto=mov["produto_id"]))
        titulo, subtitulo = TITULOS[modo]
        return render_template(
            "operacao.html",
            modo=modo,
            titulo=titulo,
            subtitulo=subtitulo,
            form=form,
            produtos=db.listar_produtos(situacao="ativos"),
            fornecedores=db.listar_fornecedores() if modo == "entrada" else [],
            motivos=MOTIVOS[modo],
            hoje=date.today().isoformat(),
        ), status

    # ------------------------------------------------------------ movimentações

    def _filtros_movimentos():
        tipos = {"entrada", "saida", "ajuste"}
        tipo = request.args.get("tipo", "")
        return {
            "busca": request.args.get("busca", ""),
            "tipo": tipo if tipo in tipos else "",
            "de": request.args.get("de", ""),
            "ate": request.args.get("ate", ""),
        }

    def _listar_movimentos(filtros, limit):
        try:
            inicio = parse_data_filtro(filtros["de"])
            fim = parse_data_filtro(filtros["ate"])
        except ValueError as exc:
            _erro(exc)
            inicio = fim = None
        return g.db.listar_movimentos(
            busca=filtros["busca"], tipo=filtros["tipo"] or None, data_ini=inicio, data_fim=fim, limit=limit
        )

    @app.get("/movimentos")
    def movimentos():
        filtros = _filtros_movimentos()
        lista, truncado = _listar_movimentos(filtros, 1000)
        hoje = date.today()
        periodos = {
            "Hoje": (hoje, hoje),
            "7 dias": (hoje - timedelta(days=6), hoje),
            "30 dias": (hoje - timedelta(days=29), hoje),
        }
        return render_template("movimentos.html", movimentos=lista, truncado=truncado, filtros=filtros,
                               periodos={k: (a.isoformat(), b.isoformat()) for k, (a, b) in periodos.items()})

    @app.get("/movimentos/<int:movimento_id>")
    def movimento(movimento_id):
        mov = g.db.obter_movimento(movimento_id)
        if mov is None:
            abort(404)
        return render_template("movimento.html", mov=mov)

    @app.post("/movimentos/<int:movimento_id>/estornar")
    @requer("estornar")
    def estornar(movimento_id):
        try:
            novo = g.db.estornar(movimento_id)
        except (EstoqueError, ValueError) as exc:
            _erro(exc)
            return redirect(url_for("movimento", movimento_id=movimento_id))
        flash(
            f"Lançamento #{movimento_id} estornado. Saldo de {novo['produto_nome']}: "
            f"{format_qty(novo['quantidade_posterior'])} {novo['produto_unidade']}.",
            "ok",
        )
        return redirect(url_for("movimentos"))

    @app.get("/exportar/movimentos")
    @requer("exportar")
    def exportar_movimentos():
        lista, _ = _listar_movimentos(_filtros_movimentos(), 100000)
        nome = f"movimentacoes-{datetime.now():%Y%m%d-%H%M}.csv"
        return Response(csv_movimentos(lista), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={nome}"})

    # ------------------------------------------------------------ fornecedores

    @app.get("/fornecedores")
    def fornecedores():
        atual = None
        fornecedor_id = _int(request.args.get("id"))
        if fornecedor_id:
            atual = g.db.obter_fornecedor(fornecedor_id)
        return render_template("fornecedores.html", fornecedores=g.db.listar_fornecedores(request.args.get("busca", "")),
                               atual=atual, form=atual or {}, busca=request.args.get("busca", ""))

    @app.post("/fornecedores/salvar")
    @requer("editar_cadastro")
    def fornecedor_salvar():
        dados = {chave: request.form.get(chave, "") for chave in ("nome", "contato", "telefone", "email", "observacao")}
        fornecedor_id = _int(request.form.get("id"))
        if fornecedor_id:
            dados["id"] = fornecedor_id
        try:
            salvo = g.db.salvar_fornecedor(dados)
        except (EstoqueError, ValueError) as exc:
            _erro(exc)
            atual = g.db.obter_fornecedor(fornecedor_id) if fornecedor_id else None
            return render_template("fornecedores.html", fornecedores=g.db.listar_fornecedores(),
                                   atual=atual, form={**dados, "id": fornecedor_id}, busca=""), 400
        flash(f"Fornecedor {salvo['nome']} salvo.", "ok")
        return redirect(url_for("fornecedores", id=salvo["id"]))

    @app.post("/fornecedores/<int:fornecedor_id>/excluir")
    @requer("excluir_cadastro")
    def fornecedor_excluir(fornecedor_id):
        try:
            g.db.excluir_fornecedor(fornecedor_id)
        except (EstoqueError, ValueError) as exc:
            _erro(exc)
        else:
            flash("Fornecedor excluído.", "ok")
        return redirect(url_for("fornecedores"))

    # ------------------------------------------------------------ categorias

    @app.get("/categorias")
    def categorias():
        return render_template("categorias.html", categorias=g.db.listar_categorias())

    @app.post("/categorias/salvar")
    @requer("editar_cadastro")
    def categoria_salvar():
        categoria_id = _int(request.form.get("id"))
        try:
            g.db.salvar_categoria(request.form.get("nome", ""), categoria_id)
        except (EstoqueError, ValueError) as exc:
            _erro(exc)
        else:
            flash("Categoria atualizada." if categoria_id else "Categoria adicionada.", "ok")
        return redirect(url_for("categorias"))

    @app.post("/categorias/<int:categoria_id>/excluir")
    @requer("excluir_cadastro")
    def categoria_excluir(categoria_id):
        try:
            g.db.excluir_categoria(categoria_id)
        except (EstoqueError, ValueError) as exc:
            _erro(exc)
        else:
            flash("Categoria excluída.", "ok")
        return redirect(url_for("categorias"))

    # ------------------------------------------------------------ atividades e usuários

    @app.get("/atividades")
    @requer("ver_atividades")
    def atividades():
        return render_template("atividades.html", atividades=g.db.listar_atividades())

    @app.get("/usuarios")
    @requer("gerenciar_usuarios")
    def usuarios():
        return render_template("usuarios.html", usuarios=g.db.listar_usuarios(), perfis=PERFIS,
                               descricoes=DESCRICAO_PERFIS, form={})

    @app.post("/usuarios/salvar")
    @requer("gerenciar_usuarios")
    def usuario_salvar():
        dados = {
            "email": request.form.get("email", ""),
            "nome": request.form.get("nome", ""),
            "perfil": request.form.get("perfil", ""),
            "senha": request.form.get("senha", ""),
        }
        usuario_id = _int(request.form.get("id"))
        if usuario_id:
            dados["id"] = usuario_id
        try:
            salvo = g.db.salvar_usuario(dados)
        except (EstoqueError, ValueError) as exc:
            _erro(exc)
            form = {} if usuario_id else {**dados, "senha": ""}
            return render_template("usuarios.html", usuarios=g.db.listar_usuarios(), perfis=PERFIS,
                                   descricoes=DESCRICAO_PERFIS, form=form), 400
        aviso = " Senha redefinida." if usuario_id and dados["senha"] else ""
        flash(f"{salvo['email']} salvo como {NOMES_PERFIS[salvo['perfil']]}.{aviso}", "ok")
        return redirect(url_for("usuarios"))

    @app.post("/usuarios/<int:usuario_id>/remover")
    @requer("gerenciar_usuarios")
    def usuario_remover(usuario_id):
        try:
            g.db.remover_usuario(usuario_id)
        except (EstoqueError, ValueError) as exc:
            _erro(exc)
        else:
            flash("Acesso removido.", "ok")
        return redirect(url_for("usuarios"))
