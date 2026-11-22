"""Inicia o sistema de estoque.

Execute com:
    python main.py
e abra http://localhost:5000 no navegador. Não precisa de internet nem de chaves.
"""

from __future__ import annotations

from app import create_app

PORTA = 5000

app = create_app()


def main() -> None:
    print(f"Sistema de estoque em http://localhost:{PORTA}  (feche esta janela ou Ctrl+C para parar)")
    app.run(host="127.0.0.1", port=PORTA, debug=False, threaded=True)


if __name__ == "__main__":
    main()
