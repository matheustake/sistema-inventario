"""Funções de número, data e texto usadas pelo estoque."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class EstoqueError(Exception):
    """Erro de regra de negócio que pode ser mostrado ao usuário."""


def dec_qty(value) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    else:
        number = Decimal(str(value))
    return number.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def dec_money(value) -> Decimal:
    if isinstance(value, Decimal):
        number = value
    else:
        number = Decimal(str(value))
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_number(text: str) -> Decimal:
    raw = str(text).strip().replace("R$", "").replace(" ", "").replace("\u00a0", "")
    if not raw:
        raise ValueError("Informe um valor numérico.")
    neg = raw.startswith("-")
    if raw[0] in "+-":
        raw = raw[1:]
    if not raw or not re.fullmatch(r"\d[\d.,]*", raw):
        raise ValueError("Valor numérico inválido. Use vírgula nos decimais, por exemplo 10,5.")
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    elif raw.count(".") > 1 or re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
        raw = raw.replace(".", "")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Valor numérico inválido.") from exc
    return -value if neg else value


def parse_qty(text: str, *, allow_zero: bool = False) -> Decimal:
    number = dec_qty(parse_number(text))
    if number < 0:
        raise ValueError("A quantidade não pode ser negativa.")
    if number == 0 and not allow_zero:
        raise ValueError("A quantidade deve ser maior que zero.")
    if number > Decimal("1000000000"):
        raise ValueError("A quantidade informada é grande demais.")
    return number


def parse_money(text: str, *, allow_zero: bool = True) -> Decimal:
    number = dec_money(parse_number(text))
    if number < 0:
        raise ValueError("O valor não pode ser negativo.")
    if number == 0 and not allow_zero:
        raise ValueError("O valor deve ser maior que zero.")
    if number > Decimal("1000000000"):
        raise ValueError("O valor informado é grande demais.")
    return number


def format_qty(value) -> str:
    number = dec_qty(value)
    return _format_decimal(number, 3, trim=True)


def format_delta(value) -> str:
    text = format_qty(value)
    if text.startswith("-"):
        return text
    return f"+{text}"


def format_money(value) -> str:
    number = dec_money(value)
    sign = "-" if number < 0 else ""
    return f"{sign}R$ {_format_decimal(abs(number), 2, trim=False)}"


def format_input_qty(value) -> str:
    number = dec_qty(value)
    text = f"{number:.3f}".replace(".", ",")
    if "," in text:
        text = text.rstrip("0").rstrip(",")
    return text


def format_input_money(value) -> str:
    return f"{dec_money(value):.2f}".replace(".", ",")


def format_int(value) -> str:
    return f"{int(value):,}".replace(",", ".")


def csv_qty(value) -> str:
    return f"{dec_qty(value):.3f}".replace(".", ",")


def csv_money(value) -> str:
    return f"{dec_money(value):.2f}".replace(".", ",")


def hoje() -> str:
    return date.today().strftime("%d/%m/%Y")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_data(text: str) -> str:
    """Converte dd/mm/aaaa em data e hora local gravável."""
    parsed = _parse_date(text)
    moment = datetime.now()
    if parsed.date() == moment.date():
        parsed = parsed.replace(hour=moment.hour, minute=moment.minute, second=moment.second)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def parse_data_filtro(text: str) -> str | None:
    if not str(text).strip():
        return None
    return _parse_date(text).strftime("%Y-%m-%d")


def format_datetime(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return parsed.strftime("%d/%m/%Y %H:%M")


def like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def clean_text(value, *, required: bool = False, label: str = "o campo", max_len: int = 200) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        if required:
            raise ValueError(f"Informe {label}.")
        return None
    if len(text) > max_len:
        raise ValueError(f"O texto de {label} pode ter no máximo {max_len} caracteres.")
    return text


def clean_multiline(value, *, max_len: int = 1000) -> str | None:
    text = str(value or "").replace("\r\n", "\n").strip()
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        return None
    if len(text) > max_len:
        raise ValueError(f"O texto pode ter no máximo {max_len} caracteres.")
    return text


def clean_email(value) -> str | None:
    text = clean_text(value, max_len=120)
    if text is None:
        return None
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        raise ValueError("E-mail inválido.")
    return text.lower()


def clean_phone(value) -> str | None:
    text = clean_text(value, max_len=30)
    if text is None:
        return None
    if not re.fullmatch(r"[0-9+()\-.\s]{8,30}", text):
        raise ValueError("Telefone inválido. Use apenas números e os símbolos + ( ) - .")
    return text


def valor_estoque(qtd, custo) -> Decimal:
    return (dec_qty(qtd) * dec_money(custo)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calcular_custo_medio(qtd_atual, custo_atual, qtd_entrada, custo_entrada) -> Decimal:
    atual = dec_qty(qtd_atual)
    custo = dec_money(custo_atual)
    entrada = dec_qty(qtd_entrada)
    custo_novo = dec_money(custo_entrada)
    if entrada <= 0:
        raise ValueError("A quantidade da entrada deve ser maior que zero.")
    if atual <= 0:
        return custo_novo
    total = atual * custo + entrada * custo_novo
    return (total / (atual + entrada)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _format_decimal(number: Decimal, places: int, *, trim: bool) -> str:
    negative = number < 0
    quantized = abs(number)
    text = f"{quantized:.{places}f}"
    whole, frac = text.split(".")
    if trim:
        frac = frac.rstrip("0")
    grouped = f"{int(whole):,}".replace(",", ".")
    body = grouped if not frac else f"{grouped},{frac}"
    return f"-{body}" if negative else body


def _parse_date(text: str) -> datetime:
    raw = str(text).strip()
    parsed = None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("Data inválida. Use o formato dd/mm/aaaa.")
    if parsed.year < 2000 or parsed.date() > date.today() + timedelta(days=1):
        raise ValueError("Informe uma data entre 01/01/2000 e amanhã.")
    return parsed
