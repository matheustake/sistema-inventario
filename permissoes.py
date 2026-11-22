"""Perfis de acesso e o que cada um pode fazer.

Esta tabela é a única fonte da regra: as telas usam para esconder botões e o
servidor usa para recusar a ação.
"""

from __future__ import annotations

# "consulta" é a chave interna do perfil Visualizador (só leitura).
PERFIS = ("superadmin", "admin", "operador", "consulta")

NOMES_PERFIS = {
    "superadmin": "Superadmin",
    "admin": "Admin",
    "operador": "Operador",
    "consulta": "Visualizador",
}

DESCRICAO_PERFIS = {
    "superadmin": "Tudo, inclusive estorno, backup, cadastrar pessoas e redefinir senhas.",
    "admin": "Operação e cadastros, inclusive excluir e desativar. Sem estorno nem usuários.",
    "operador": "Entrada, saída, ajuste, editar cadastros e exportar. Não exclui nem desativa.",
    "consulta": "Só visualiza.",
}

# Ação -> perfis que podem executá-la.
PERMISSOES: dict[str, frozenset[str]] = {
    # Ver painel, produtos, movimentações, fornecedores e categorias.
    "ver": frozenset(PERFIS),
    # Entrada, saída e ajuste de inventário.
    "movimentar": frozenset({"superadmin", "admin", "operador"}),
    # Criar e editar produtos (inclusive custo médio), fornecedores e categorias.
    "editar_cadastro": frozenset({"superadmin", "admin", "operador"}),
    # Baixar planilhas CSV.
    "exportar": frozenset({"superadmin", "admin", "operador"}),
    # Excluir fornecedor ou categoria; desativar ou reativar produto.
    "excluir_cadastro": frozenset({"superadmin", "admin"}),
    # Ver o registro de atividades (quem fez o quê).
    "ver_atividades": frozenset({"superadmin", "admin"}),
    # Estornar lançamento.
    "estornar": frozenset({"superadmin"}),
    # Cadastrar, alterar e remover pessoas; redefinir senhas.
    "gerenciar_usuarios": frozenset({"superadmin"}),
    # Backup do banco e carregar dados de exemplo.
    "sistema": frozenset({"superadmin"}),
}


def pode(perfil: str | None, acao: str) -> bool:
    if acao not in PERMISSOES:
        raise KeyError(f"Ação desconhecida: {acao}")
    return perfil in PERMISSOES[acao]
