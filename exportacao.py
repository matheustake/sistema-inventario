"""Planilhas CSV (separadas por ponto e vírgula, abrem direto no Excel)."""

from __future__ import annotations

import csv
import io

from util import csv_money, csv_qty, format_datetime, valor_estoque

NOMES_TIPO = {"entrada": "Entrada", "saida": "Saída", "ajuste": "Ajuste"}


def rotulo_tipo(mov) -> str:
    if mov["estorno_de"]:
        return "Estorno"
    base = NOMES_TIPO[mov["tipo"]]
    if mov["estornado"]:
        sufixo = "estornado" if mov["tipo"] == "ajuste" else "estornada"
        return f"{base} · {sufixo}"
    return base


def _csv(headers: list[str], rows: list[list]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def csv_produtos(produtos: list[dict]) -> bytes:
    rows = [
        [
            produto["codigo"],
            produto["nome"],
            produto.get("categoria_nome") or "",
            produto["unidade"],
            csv_qty(produto["quantidade"]),
            csv_qty(produto["estoque_minimo"]),
            csv_money(produto["custo"]),
            csv_money(produto["preco_venda"]),
            csv_money(valor_estoque(produto["quantidade"], produto["custo"])),
            "Ativo" if produto["ativo"] else "Inativo",
            produto.get("descricao") or "",
        ]
        for produto in produtos
    ]
    return _csv(
        [
            "Código",
            "Produto",
            "Categoria",
            "Unidade",
            "Estoque",
            "Mínimo",
            "Custo",
            "Preço de venda",
            "Valor em custo",
            "Situação",
            "Descrição",
        ],
        rows,
    )


def csv_movimentos(movimentos: list[dict]) -> bytes:
    rows = [
        [
            format_datetime(mov["criado_em"]),
            rotulo_tipo(mov),
            mov["produto_codigo"],
            mov["produto_nome"],
            csv_qty(mov["quantidade"]),
            mov["produto_unidade"],
            csv_qty(mov["quantidade_anterior"]),
            csv_qty(mov["quantidade_posterior"]),
            "" if mov["custo_unitario"] is None else csv_money(mov["custo_unitario"]),
            csv_money(mov["custo_posterior"]),
            mov.get("documento") or "",
            mov.get("motivo") or "",
            mov.get("fornecedor_nome") or "",
            mov.get("destino") or "",
            mov.get("observacao") or "",
            "Sim" if mov["estornado"] else "Não",
            mov.get("estorno_de") or "",
            mov.get("usuario_email") or "",
        ]
        for mov in movimentos
    ]
    return _csv(
        [
            "Data",
            "Tipo",
            "Código",
            "Produto",
            "Quantidade",
            "Unidade",
            "Saldo anterior",
            "Saldo posterior",
            "Custo unitário",
            "Custo médio posterior",
            "Documento",
            "Motivo",
            "Fornecedor",
            "Destino",
            "Observação",
            "Estornado",
            "Estorno de",
            "Usuário",
        ],
        rows,
    )
