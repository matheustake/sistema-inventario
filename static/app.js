// Atualização automática, confirmações e prévia das operações.
(function () {
  "use strict";

  var body = document.body;
  var versao = body.dataset.versao;
  var mexeu = false;
  var CHAVE_SCROLL = "estoque-scroll:" + location.pathname + location.search;

  // ------------------------------------------------------------ rolagem após recarregar
  try {
    var salvo = sessionStorage.getItem(CHAVE_SCROLL);
    if (salvo !== null) {
      sessionStorage.removeItem(CHAVE_SCROLL);
      window.scrollTo(0, parseInt(salvo, 10) || 0);
    }
  } catch (e) { /* armazenamento indisponível */ }

  function recarregar() {
    try { sessionStorage.setItem(CHAVE_SCROLL, String(window.scrollY)); } catch (e) { /* ignora */ }
    location.reload();
  }

  // ------------------------------------------------------------ novidades de outras pessoas
  document.addEventListener("input", function (evento) {
    if (evento.target.closest("form")) { mexeu = true; }
  });
  document.addEventListener("submit", function () { mexeu = false; });

  var aviso = document.getElementById("novidades");
  if (aviso) {
    aviso.querySelector("button").addEventListener("click", recarregar);
  }

  function checar() {
    if (!versao || document.hidden) { return; }
    fetch("/api/versao", { cache: "no-store", credentials: "same-origin" })
      .then(function (resposta) {
        if (resposta.status === 401) { recarregar(); return null; }
        return resposta.ok ? resposta.json() : null;
      })
      .then(function (dados) {
        if (!dados || String(dados.versao) === versao) { return; }
        if (mexeu) {
          if (aviso) { aviso.classList.add("visivel"); }
        } else {
          recarregar();
        }
      })
      .catch(function () { /* servidor fora do ar: tenta de novo depois */ });
  }

  if (versao) {
    setInterval(checar, 5000);
    document.addEventListener("visibilitychange", checar);
  }

  // ------------------------------------------------------------ confirmações
  document.addEventListener("submit", function (evento) {
    var texto = evento.target.getAttribute("data-confirma");
    if (texto && !window.confirm(texto)) {
      evento.preventDefault();
    }
  }, true);

  // ------------------------------------------------------------ prévia das operações
  function numero(texto) {
    var bruto = String(texto || "").replace(/R\$/g, "").replace(/\s/g, "");
    if (!bruto) { return NaN; }
    if (bruto.indexOf(",") >= 0) {
      bruto = bruto.replace(/\./g, "").replace(",", ".");
    } else if (/^\d{1,3}(\.\d{3})+$/.test(bruto)) {
      bruto = bruto.replace(/\./g, "");
    }
    return /^-?\d+(\.\d+)?$/.test(bruto) ? parseFloat(bruto) : NaN;
  }

  function qtd(valor) {
    return valor.toLocaleString("pt-BR", { maximumFractionDigits: 3 });
  }

  function dinheiro(valor) {
    return valor.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
  }

  var formOperacao = document.getElementById("form-operacao");
  if (formOperacao) {
    var modo = formOperacao.dataset.modo;
    var seletor = document.getElementById("produto_id");
    var busca = document.getElementById("busca-produto");
    var campoQtd = document.getElementById("quantidade");
    var campoCusto = document.getElementById("custo");
    var previa = document.getElementById("previa");
    var info = document.getElementById("info-produto");

    var atualizar = function () {
      var opcao = seletor.options[seletor.selectedIndex];
      if (!opcao || !opcao.value) {
        info.textContent = "Selecione um produto.";
        previa.className = "previa";
        previa.textContent = "Escolha o produto e informe a quantidade.";
        return;
      }
      var saldo = parseFloat(opcao.dataset.saldo);
      var custo = parseFloat(opcao.dataset.custo);
      var un = opcao.dataset.unidade;
      info.textContent = "Estoque: " + qtd(saldo) + " " + un +
        "   ·   Mínimo: " + qtd(parseFloat(opcao.dataset.minimo)) +
        "   ·   Custo médio: " + dinheiro(custo);
      var q = numero(campoQtd.value);
      if (isNaN(q)) {
        previa.className = "previa";
        previa.textContent = modo === "ajuste" ? "Informe a quantidade contada." : "Informe a quantidade.";
        return;
      }
      var linhas = [];
      var classe = "previa ok";
      if (modo === "entrada") {
        var unit = numero(campoCusto.value);
        if (isNaN(unit)) { unit = 0; }
        var medio = saldo <= 0 ? unit : (saldo * custo + q * unit) / (saldo + q);
        linhas.push("Novo saldo: " + qtd(saldo + q) + " " + un);
        linhas.push("Novo custo médio: " + dinheiro(medio));
      } else if (modo === "saida") {
        if (q > saldo) {
          classe = "previa erro";
          linhas.push("Estoque insuficiente. Disponível: " + qtd(saldo) + " " + un + ".");
        } else {
          linhas.push("Novo saldo: " + qtd(saldo - q) + " " + un);
          linhas.push("O custo médio permanece o mesmo.");
        }
      } else {
        var diferenca = q - saldo;
        if (Math.abs(diferenca) < 0.0005) {
          classe = "previa";
          linhas.push("A quantidade contada está igual ao saldo atual.");
        } else {
          linhas.push("Diferença: " + (diferenca > 0 ? "+" : "") + qtd(diferenca) + " " + un);
        }
      }
      previa.className = classe;
      previa.textContent = linhas.join("\n");
    };

    if (busca) {
      busca.addEventListener("input", function () {
        var termo = busca.value.trim().toLowerCase();
        Array.prototype.forEach.call(seletor.options, function (opcao) {
          if (!opcao.value) { return; }
          opcao.hidden = termo !== "" && opcao.textContent.toLowerCase().indexOf(termo) < 0;
        });
      });
    }

    var preencher = function (trocou) {
      var opcao = seletor.options[seletor.selectedIndex];
      if (!opcao || !opcao.value) { return; }
      if (modo === "entrada" && campoCusto && (trocou || !campoCusto.value)) {
        campoCusto.value = opcao.dataset.custoTexto;
      }
      if (modo === "ajuste" && (trocou || !campoQtd.value)) {
        campoQtd.value = opcao.dataset.saldoTexto;
      }
    };

    seletor.addEventListener("change", function () {
      preencher(true);
      atualizar();
    });
    formOperacao.addEventListener("input", atualizar);
    preencher(false);
    atualizar();
  }
})();
