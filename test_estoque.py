"""Testes das regras de estoque, sem abrir a interface."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from database import Database
from util import dec_money, dec_qty, parse_money, parse_number, parse_qty


def produto_base(**extra):
    dados = {
        "codigo": "ARR-1",
        "nome": "Arroz",
        "unidade": "UN",
        "estoque_minimo": Decimal("5"),
        "custo": Decimal("10"),
        "preco_venda": Decimal("15"),
        "ativo": True,
    }
    dados.update(extra)
    return dados


class TesteNumeros(unittest.TestCase):
    def test_formatos_aceitos(self):
        self.assertEqual(parse_number("1.234,56"), Decimal("1234.56"))
        self.assertEqual(parse_number("10,5"), Decimal("10.5"))
        self.assertEqual(parse_number("10.5"), Decimal("10.5"))
        self.assertEqual(parse_number("1.500"), Decimal("1500"))
        self.assertEqual(parse_number("R$ 2.000,10"), Decimal("2000.10"))
        self.assertEqual(parse_number("1,234.56"), Decimal("1234.56"))

    def test_rejeita_texto_e_negativo(self):
        with self.assertRaises(ValueError):
            parse_number("abc")
        with self.assertRaises(ValueError):
            parse_number("")
        with self.assertRaises(ValueError):
            parse_qty("-1")
        self.assertEqual(parse_money("10,005"), Decimal("10.01"))


class TesteEstoque(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "estoque.db")
        self.hoje = "2026-10-30 10:00:00"

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_entrada_atualiza_media_e_saida_preserva_custo(self):
        produto = self.db.salvar_produto(produto_base(custo=Decimal("18.90"), quantidade_inicial=Decimal("40")))
        self.assertEqual(dec_qty(produto["quantidade"]), Decimal("40.000"))
        entrada = self.db.registrar_entrada(
            produto["id"], Decimal("10"), Decimal("20"), "Compra", self.hoje, documento="NF 1"
        )
        produto = self.db.obter_produto(produto["id"])
        self.assertEqual(dec_qty(produto["quantidade"]), Decimal("50.000"))
        self.assertEqual(dec_money(produto["custo"]), Decimal("19.12"))
        self.assertEqual(dec_qty(entrada["quantidade"]), Decimal("10.000"))
        self.db.registrar_saida(produto["id"], Decimal("5"), "Venda", self.hoje, destino="Balcão")
        produto = self.db.obter_produto(produto["id"])
        self.assertEqual(dec_qty(produto["quantidade"]), Decimal("45.000"))
        self.assertEqual(dec_money(produto["custo"]), Decimal("19.12"))

    def test_estoque_zerado_nao_mistura_custo_antigo(self):
        produto = self.db.salvar_produto(produto_base(custo=Decimal("99")))
        self.db.registrar_entrada(produto["id"], Decimal("5"), Decimal("4"), "Compra", self.hoje)
        produto = self.db.obter_produto(produto["id"])
        self.assertEqual(dec_money(produto["custo"]), Decimal("4.00"))
        self.db.estornar(self.db.listar_movimentos()[0][0]["id"])
        produto = self.db.obter_produto(produto["id"])
        self.assertEqual(dec_qty(produto["quantidade"]), Decimal("0.000"))
        self.assertEqual(dec_money(produto["custo"]), Decimal("99.00"))

    def test_saida_acima_do_saldo_nao_grava(self):
        produto = self.db.salvar_produto(produto_base(quantidade_inicial=Decimal("4")))
        antes = self.db.listar_movimentos()[0]
        with self.assertRaises(Exception) as ctx:
            self.db.registrar_saida(produto["id"], Decimal("9"), "Venda", self.hoje)
        self.assertIn("insuficiente", str(ctx.exception).lower())
        produto = self.db.obter_produto(produto["id"])
        self.assertEqual(dec_qty(produto["quantidade"]), Decimal("4.000"))
        self.assertEqual(len(self.db.listar_movimentos()[0]), len(antes))

    def test_ajuste_e_estorno_do_ultimo_lancamento(self):
        produto = self.db.salvar_produto(produto_base(quantidade_inicial=Decimal("10"), custo=Decimal("8")))
        self.db.registrar_saida(produto["id"], Decimal("2"), "Venda", self.hoje)
        movimentos, _ = self.db.listar_movimentos()
        entrada = next(item for item in movimentos if item["motivo"] == "Estoque inicial")
        saida = next(item for item in movimentos if item["tipo"] == "saida")
        with self.assertRaises(Exception):
            self.db.estornar(entrada["id"])
        self.db.estornar(saida["id"])
        produto = self.db.obter_produto(produto["id"])
        self.assertEqual(dec_qty(produto["quantidade"]), Decimal("10.000"))
        estorno, _ = self.db.listar_movimentos()
        estorno = estorno[0]
        with self.assertRaises(Exception):
            self.db.estornar(estorno["id"])
        self.db.registrar_ajuste(produto["id"], Decimal("7"), "Contagem de inventário", self.hoje)
        produto = self.db.obter_produto(produto["id"])
        self.assertEqual(dec_qty(produto["quantidade"]), Decimal("7.000"))
        self.assertEqual(dec_money(produto["custo"]), Decimal("8.00"))
        with self.assertRaises(Exception):
            self.db.registrar_ajuste(produto["id"], Decimal("7"), "Contagem de inventário", self.hoje)

    def test_produto_inativo_nao_movimenta_e_edicao_preserva_saldo(self):
        produto = self.db.salvar_produto(produto_base(quantidade_inicial=Decimal("6")))
        editado = self.db.salvar_produto(
            produto_base(id=produto["id"], codigo="ARR-1", custo=Decimal("5"), ativo=False)
        )
        self.assertEqual(dec_qty(editado["quantidade"]), Decimal("6.000"))
        self.assertEqual(dec_money(editado["custo"]), Decimal("5.00"))
        self.assertFalse(editado["ativo"])
        with self.assertRaises(Exception):
            self.db.registrar_saida(produto["id"], Decimal("1"), "Venda", self.hoje)

    def test_categoria_em_uso_e_codigo_duplicado(self):
        categoria = self.db.listar_categorias()[0]
        produto = self.db.salvar_produto(produto_base(categoria_id=categoria["id"]))
        with self.assertRaises(Exception):
            self.db.excluir_categoria(categoria["id"])
        self.db.salvar_produto(produto_base(id=produto["id"], categoria_id=None))
        self.db.excluir_categoria(categoria["id"])
        with self.assertRaises(Exception):
            self.db.salvar_produto(produto_base())

    def test_backup_e_exemplo(self):
        produto = self.db.salvar_produto(produto_base())
        destino = Path(self.tmp.name) / "copia.db"
        self.db.backup(destino)
        copia = Database(destino)
        try:
            self.assertIsNotNone(copia.obter_produto(produto["id"]))
        finally:
            copia.close()
        vazio = Database(Path(self.tmp.name) / "vazio.db")
        try:
            vazio.carregar_exemplo()
            self.assertGreaterEqual(vazio.contar_produtos(), 5)
            self.assertGreaterEqual(vazio.resumo()["atencao"], 2)
            with self.assertRaises(Exception):
                vazio.carregar_exemplo()
            self.assertGreaterEqual(vazio.contar_produtos(), 5)
        finally:
            vazio.close()

    def test_exclusao_de_fornecedor_preserva_historico(self):
        fornecedor = self.db.salvar_fornecedor({"nome": "Atacado", "telefone": "(11) 98888-1000"})
        produto = self.db.salvar_produto(produto_base())
        self.db.registrar_entrada(
            produto["id"], Decimal("2"), Decimal("3"), "Compra", self.hoje, fornecedor_id=fornecedor["id"]
        )
        self.db.excluir_fornecedor(fornecedor["id"])
        movimento = self.db.listar_movimentos()[0][0]
        self.assertIsNone(movimento["fornecedor_id"])
        self.assertEqual(dec_qty(self.db.obter_produto(produto["id"])["quantidade"]), Decimal("2.000"))

    def test_sugestao_de_codigo(self):
        self.assertEqual(self.db.sugerir_codigo(), "PRD-0001")
        self.db.salvar_produto(produto_base(codigo="PRD-0001"))
        self.assertEqual(self.db.sugerir_codigo(), "PRD-0002")


if __name__ == "__main__":
    unittest.main()
