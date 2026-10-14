# -*- coding: utf-8 -*-
r"""
3_categorizar_dados_sicar.py — separa os imóveis de cada estado (UF), já
preparados pelo script 2 (2_processar_dados_sicar.py), em três categorias
conforme o campo ``des_condic``:

  Habilitados     : des_condic é um dos 5 textos abaixo (Analisado, sem
                    pendência de notificação — qualquer das derivações de
                    "em conformidade"/"em regularização"/"sem pendências").
  Analisados      : des_condic = "Analisado, aguardando atendimento a
                    notificação" (analisado COM pendência).
  Não Analisados  : des_condic em fase "Em Análise" ou "Aguardando Análise"
                    (qualquer derivação — já é exatamente o conteúdo de
                    <UF>_trabalho.gpkg, produzido pelo script 2).

Fonte: Analise_Conformidade\dados_saída_<UF>\<UF>_geopackage\<UF>_analisados.gpkg
(camada AREA_IMOVEL, fase Analisado) e ...\<UF>_trabalho.gpkg (camada
AREA_IMOVEL, Em Análise + Aguardando) — saída do script 2.

Saída (mesma pasta <UF>_geopackage, mesmo padrão de nomes já usado no
projeto para as camadas CAR_<UF>_...):
    <UF>_CAR_Imoveis_Selecionados_Habilitados.gpkg layer CAR_<UF>_Imoveis_Habilitados
    <UF>_Imoveis_Privados_Analisados.gpkg      layer CAR_<UF>_Imoveis_Analisados
    <UF>_Imoveis_Privados_Nao_Analisados.gpkg  layer CAR_<UF>_Imoveis_Nao_Analisados

Os des_condic da fase Analisado que NÃO batem com nenhum dos 8 textos
conhecidos (5 de Habilitados + 3 de Analisados) NÃO são descartados
silenciosamente: entram numa quarta saída de auditoria,
<UF>_Imoveis_Privados_Outros_Analisado.gpkg (layer
CAR_<UF>_Imoveis_Outros_Analisado), e o script imprime um aviso com a
lista de textos não reconhecidos e a contagem — para você decidir (por
exemplo, incluir o texto na lista de Habilitados) em vez do script
"adivinhar" uma classificação em dado de conformidade legal.

COMO USAR
---------
Ambiente 'geo' ativo (mesmo do repositório de conformidade):
    python 3_categorizar_dados_sicar.py
Deixe SOMENTE_ESTES = [] para todas as 27 UFs; ou liste siglas para validar
antes do lote completo.
"""

from __future__ import annotations

import os
import sys
import glob
import unicodedata
from datetime import datetime

import geopandas as gpd
import pandas as pd

from conformidade.io_dados import ler_camada, escrever_camada

# =============================== CONFIG ===============================
# Pastas de dados: ver conformidade/caminhos.py (backup restaurado via PLANAVEG_RAIZ ou Dropbox).
from conformidade.caminhos import PASTA_ANALISE  # noqa: E402

SOMENTE_ESTES: list[str] = []   # ex.: ["AC"] para validar; [] = todas as 27 UFs
COL_FASE = "des_condic"

# --- textos EXATOS (após normalização: sem acento, minúsculas, espaços
# colapsados) que definem cada subcategoria dos "Analisado" ---
HABILITADOS_TEXTOS = [
    "Analisado, em regularização ambiental (Lei n 12.651/2012)",
    "Analisado, em conformidade com a Lei n 12.651/2012",
    "Analisado, em conformidade com a Lei n 12.651/2012, com ativos ambientais",
    "Analisado, aguardando regularização ambiental (Lei n 12.651/2012)",
    "Analisado sem pendências",
]
ANALISADOS_TEXTOS = [
    "Analisado, aguardando atendimento a notificação",
    # variantes descobertas em auditoria (*_Outros_Analisado.gpkg) e
    # classificadas manualmente como Analisados (com pendência), não
    # Habilitados — decisão do usuário, não uma inferência automática:
    "Analisado, em regularização ambiental (Lei n 12.651/2012), com ativos ambientais",
    "Analisado pelo Filtro Automático",
]
# =====================================================================

UFS = [
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
    "SP", "SE", "TO",
]


def _norm(txt) -> str:
    if txt is None:
        return ""
    t = unicodedata.normalize("NFKD", str(txt))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.strip().lower().split())


HABILITADOS_NORM = {_norm(t) for t in HABILITADOS_TEXTOS}
ANALISADOS_NORM = {_norm(t) for t in ANALISADOS_TEXTOS}


def classificar_analisado(des_condic) -> str:
    """'habilitado' | 'analisado' | 'outros' para uma linha já em fase Analisado."""
    t = _norm(des_condic)
    if t in HABILITADOS_NORM:
        return "habilitado"
    if t in ANALISADOS_NORM:
        return "analisado"
    return "outros"


def processar_uf(uf: str) -> dict:
    """Categoriza um estado. Lança exceção em erro."""
    pasta_gpkg = os.path.join(PASTA_ANALISE, f"dados_saída_{uf}", f"{uf}_geopackage")
    gpkg_ana = os.path.join(pasta_gpkg, f"{uf}_analisados.gpkg")
    gpkg_tra = os.path.join(pasta_gpkg, f"{uf}_trabalho.gpkg")

    resumo = {"uf": uf, "habilitados": 0, "analisados": 0, "outros_analisado": 0,
              "nao_analisados": 0, "outros_textos": {}}

    # ---- fase Analisado: separa Habilitados / Analisados / Outros ----
    if os.path.exists(gpkg_ana):
        analisado = ler_camada(gpkg_ana, "AREA_IMOVEL")
        if COL_FASE not in analisado.columns:
            raise KeyError(f"coluna '{COL_FASE}' ausente em {uf}_analisados.gpkg")

        classe = analisado[COL_FASE].map(classificar_analisado)

        habilitados = analisado[classe == "habilitado"].copy()
        analisados = analisado[classe == "analisado"].copy()
        outros = analisado[classe == "outros"].copy()

        if len(habilitados) > 0:
            out = os.path.join(pasta_gpkg, f"{uf}_CAR_Imoveis_Selecionados_Habilitados.gpkg")
            escrever_camada(habilitados, out, f"CAR_{uf}_Imoveis_Habilitados")
            resumo["habilitados"] = len(habilitados)

        if len(analisados) > 0:
            out = os.path.join(pasta_gpkg, f"{uf}_Imoveis_Privados_Analisados.gpkg")
            escrever_camada(analisados, out, f"CAR_{uf}_Imoveis_Analisados")
            resumo["analisados"] = len(analisados)

        if len(outros) > 0:
            out = os.path.join(pasta_gpkg, f"{uf}_Imoveis_Privados_Outros_Analisado.gpkg")
            escrever_camada(outros, out, f"CAR_{uf}_Imoveis_Outros_Analisado")
            resumo["outros_analisado"] = len(outros)
            contagem = outros[COL_FASE].value_counts()
            resumo["outros_textos"] = {str(k): int(v) for k, v in contagem.items()}

    # ---- fase trabalho (Em Análise + Aguardando) = Não Analisados direto ----
    if os.path.exists(gpkg_tra):
        trabalho = ler_camada(gpkg_tra, "AREA_IMOVEL")
        if len(trabalho) > 0:
            out = os.path.join(pasta_gpkg, f"{uf}_Imoveis_Privados_Nao_Analisados.gpkg")
            escrever_camada(trabalho, out, f"CAR_{uf}_Imoveis_Nao_Analisados")
            resumo["nao_analisados"] = len(trabalho)

    return resumo


def main() -> int:
    alvos = SOMENTE_ESTES if SOMENTE_ESTES else UFS
    print(f"Estados a processar ({len(alvos)}): {', '.join(alvos)}")
    print(f"Início: {datetime.now():%H:%M:%S}\n")

    sucesso, falhas = [], []
    avisos_outros = []
    for uf in alvos:
        print(f"  [{uf}] categorizando...", flush=True)
        try:
            r = processar_uf(uf)
            print(f"       habilitados={r['habilitados']} | analisados={r['analisados']} "
                  f"| nao_analisados={r['nao_analisados']} | outros={r['outros_analisado']}",
                  flush=True)
            if r["outros_textos"]:
                avisos_outros.append((uf, r["outros_textos"]))
            sucesso.append(uf)
        except Exception as e:
            print(f"       !! ERRO: {e}", flush=True)
            falhas.append((uf, str(e)))

    print(f"\n{'#'*60}\n  RELATÓRIO ({datetime.now():%H:%M:%S})\n{'#'*60}")
    print(f"Sucesso ({len(sucesso)}): {', '.join(sucesso) or '—'}")
    if falhas:
        print(f"Falhas ({len(falhas)}):")
        for uf, msg in falhas:
            print(f"  {uf}: {msg}")
    if avisos_outros:
        print(f"\n!! ATENÇÃO: des_condic da fase Analisado não reconhecidos em "
              f"{len(avisos_outros)} UF(s) — gravados em *_Outros_Analisado.gpkg, "
              f"NÃO entraram em Habilitados nem Analisados:")
        for uf, textos in avisos_outros:
            for txt, n in textos.items():
                print(f"  [{uf}] ({n}x) {txt!r}")
        print("Revise HABILITADOS_TEXTOS/ANALISADOS_TEXTOS no topo do script se "
              "algum desses textos for uma categoria válida, e rode de novo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
