# Sistema de Estoque

Controle de estoque que abre no navegador e roda inteiro neste computador: entrada, saída, ajuste,
estorno, cadastros e histórico. Não precisa de internet, contas externas nem chaves.

Cada pessoa entra com e-mail e senha e tem um perfil, como numa empresa de verdade. Isso evita que todo
mundo possa apagar ou alterar tudo, um problema comum em quem trabalha com estoque de alimentos.

## Perfis

| Ação | Superadmin | Admin | Operador | Visualizador |
|---|:-:|:-:|:-:|:-:|
| Ver painel, produtos e movimentações | ✅ | ✅ | ✅ | ✅ |
| Entrada, saída e ajuste | ✅ | ✅ | ✅ | ❌ |
| Criar e editar produtos (inclusive custo médio), fornecedores e categorias | ✅ | ✅ | ✅ | ❌ |
| Exportar planilhas | ✅ | ✅ | ✅ | ❌ |
| Excluir fornecedor ou categoria, desativar produto | ✅ | ✅ | ❌ | ❌ |
| Ver o registro de atividades | ✅ | ✅ | ❌ | ❌ |
| Estornar lançamento | ✅ | ❌ | ❌ | ❌ |
| Backup, cadastrar pessoas e redefinir senhas | ✅ | ❌ | ❌ | ❌ |

A regra fica em `permissoes.py`. As telas escondem os botões e o servidor recusa a ação, mesmo que alguém
tente chamá-la direto. O topo de toda tela mostra quem está conectado e com qual perfil.

## Como executar

**Pré-requisito:** Python 3.10 ou mais novo (python.org). Na instalação, marque "Add Python to PATH".

Dê dois cliques em `executar.bat`. Na primeira vez ele prepara o ambiente e instala o Flask; depois abre
<http://localhost:5000> sozinho. Deixe a janela preta aberta enquanto usa o sistema.

Pelo terminal, o equivalente é:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

## Primeiro uso

1. Na primeira vez, aparece a tela **Primeiro acesso**: informe o e-mail e crie a senha do Superadmin.
2. Em **Usuários**, cadastre cada pessoa com e-mail, perfil e uma senha inicial. Passe a senha para ela.
3. Cada pessoa pode trocar a própria senha em **Minha conta** (link no topo da tela).
4. Esqueceu a senha? O Superadmin digita uma nova na linha da pessoa, em **Usuários**, e clica em Salvar.

As senhas ficam criptografadas no banco; nem quem abre o arquivo consegue lê-las.

## Dados

Ficam em `data/estoque.db`. O Superadmin baixa uma cópia em **Painel → Baixar backup**. Um banco de uma
versão anterior do sistema é atualizado sozinho na primeira execução, sem perder nada.

Para apresentar ou explorar, o Superadmin pode carregar dados de exemplo no Painel enquanto não houver
produtos cadastrados.

## Testes

```powershell
.\.venv\Scripts\python.exe -m unittest test_estoque test_web -v
```

## Limite

A segurança é a de um sistema local: protege contra uso indevido pelas telas, mas quem tem acesso ao
computador pode copiar o arquivo `data/estoque.db`. Para guardar dados sensíveis de verdade, o banco
precisaria ficar num servidor protegido.
