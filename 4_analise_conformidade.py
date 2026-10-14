# -*- coding: utf-8 -*-
r"""
4_analise_conformidade.py — roda a conformidade SICAR × INCRA sobre os
imóveis já categorizados pelo script 3 (3_categorizar_dados_sicar.py),
estado por estado, para os buckets "Analisados" e "Não Analisados".

(Generalização de conformidade_notificacao.py — 4o passo do workflow
numerado do repositório. O bucket "Habilitados" NÃO passa por este script:
por definição já são imóveis analisados sem pendência, então vão direto
do script 3 para o script 5_extracao_APP_RL_AUR.py.)

Para cada bucket (universo a classificar), a referência/prioridade do
filtro de sobreposição (quem "vence" e nunca é removido) é:
  - Analisados     -> Habilitados (os demais analisados, sem a pendência
                       de notificação — mesma mecânica já validada em
                       conformidade_notificacao.py).
  - Não Analisados -> Habilitados + Analisados juntos (replica o
                       comportamento já validado do antigo
                       processar_lote.py/``conformidade analisar``, que
                       usava TODO o bucket "Analisado" pré-categorização
                       como referência).

Só classifica coerência + sobreposição interna + filtro final; NÃO faz
recorte de APP/RESERVA_LEGAL/USO_RESTRITO (isso é o script 5).

Para cada estado (pasta dados_saída_<UF>\<UF>_geopackage em PASTA_ANALISE):
  1. lê <UF>_Imoveis_Privados_<Categoria>.gpkg (camada
     CAR_<UF>_Imoveis_<Categoria>) como universo;
  2. monta a referência de prioridade a partir do(s) bucket(s) acima;
  3. classifica o universo contra o INCRA privado (SIGEF+SNCI);
  4. aplica sobreposição interna + filtro contra a referência de prioridade;
  5. grava <UF>_Conformidade_Imoveis_<Categoria>.gpkg, com as camadas
     CAR_<UF>_Imoveis_<Categoria>_coerentes e
     CAR_<UF>_Imoveis_<Categoria>_incoerentes;
  6. grava, num arquivo à parte, <UF>_CAR_Imoveis_Selecionados_<Categoria>.gpkg
     (camada CAR_<UF>_Imoveis_Selecionados_<Categoria>) só com o limite dos
     imóveis "Representante (manter)" — o mesmo conjunto que o script 5 usa
     para extrair APP/RL/AUR, aqui persistido como camada de imóveis.

COMO USAR
---------
Ambiente 'geo' ativo (mesmo do repositório de conformidade):
    python 4_analise_conformidade.py
Para validar, deixe SOMENTE_ESTES = ["AC"]; depois esvazie para todos.
"""

from __future__ import annotations

import os
import glob
from datetime import datetime

import geopandas as gpd
import pandas as pd

from conformidade.io_dados import ler_camada, escrever_camada
from conformidade.pipeline import (
    ConfigPipeline, montar_referencia, classificar_camada_sicar,
    aplicar_sobreposicao, aplicar_filtro_analisados,
)

# =============================== CONFIG ===============================
# Pastas de dados: ver conformidade/caminhos.py (backup restaurado via PLANAVEG_RAIZ ou Dropbox).
from conformidade.caminhos import PASTA_ANALISE, PASTA_INCRA  # noqa: E402

# Deixe vazio para TODOS os estados; ou liste siglas (ex.: ["AC"]) para validar.
SOMENTE_ESTES: list[str] = []

NATUREZA = "Privado"
CATEGORIAS = ["Analisados", "Nao_Analisados"]  # buckets classificados aqui
SEL_MANTER = "Representante (manter)"
# =====================================================================

UFS = [
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
    "SP", "SE", "TO",
]


def achar_incra(uf: str) -> list[gpd.GeoDataFrame]:
    """Lê SIGEF_Privado_<UF>.shp e SNCI_Privado_<UF>.shp (os que existirem)."""
    camadas = []
    for prefixo in ("SIGEF_Privado", "SNCI_Privado"):
        caminho = os.path.join(PASTA_INCRA, f"{prefixo}_{uf}.shp")
        if os.path.exists(caminho):
            camadas.append(ler_camada(caminho))
    return camadas


def _ler_bucket(pasta_gpkg: str, uf: str, categoria: str) -> gpd.GeoDataFrame | None:
    # Habilitados foi renomeado de _Imoveis_Privados_ para _Imoveis_Selecionados_
    # (10/09/2026) -- os demais buckets (Analisados/Nao_Analisados/Outros_Analisado)
    # continuam com o nome antigo _Imoveis_Privados_<categoria>.gpkg.
    if categoria == "Habilitados":
        nome_arquivo = f"{uf}_CAR_Imoveis_Selecionados_Habilitados.gpkg"
    else:
        nome_arquivo = f"{uf}_Imoveis_Privados_{categoria}.gpkg"
    caminho = os.path.join(pasta_gpkg, nome_arquivo)
    if not os.path.exists(caminho):
        return None
    return ler_camada(caminho, f"CAR_{uf}_Imoveis_{categoria}")


def montar_prioridade(pasta_gpkg: str, uf: str, categoria: str) -> gpd.GeoDataFrame | None:
    """Referência de prioridade do filtro final, conforme o bucket a classificar."""
    if categoria == "Analisados":
        partes = ["Habilitados"]
    elif categoria == "Nao_Analisados":
        partes = ["Habilitados", "Analisados"]
    else:
        raise ValueError(f"categoria desconhecida: {categoria}")

    gdfs = [g for g in (_ler_bucket(pasta_gpkg, uf, p) for p in partes) if g is not None and len(g) > 0]
    if not gdfs:
        return None
    if len(gdfs) == 1:
        return gdfs[0]
    return gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs=gdfs[0].crs)


def processar_uf_categoria(uf: str, categoria: str) -> dict:
    """Roda a conformidade de um bucket para um estado. Lança exceção em erro."""
    pasta_gpkg = os.path.join(PASTA_ANALISE, f"dados_saída_{uf}", f"{uf}_geopackage")

    universo = _ler_bucket(pasta_gpkg, uf, categoria)
    if universo is None or len(universo) == 0:
        return {"uf": uf, "categoria": categoria, "universo": 0,
                "aviso": "bucket vazio ou inexistente"}

    incra_gdfs = achar_incra(uf)
    if not incra_gdfs:
        raise FileNotFoundError(f"INCRA privado de {uf} não encontrado")
    referencia = montar_referencia(incra_gdfs)

    prioridade = montar_prioridade(pasta_gpkg, uf, categoria)

    cfg = ConfigPipeline()  # limiares padrão (limiar_vs_analisado=0.30, etc.)

    # 1) coerência (+ subdivisão)
    res = classificar_camada_sicar(universo, referencia, cfg, natureza=NATUREZA)
    # 2) sobreposição interna (entre os próprios imóveis do bucket)
    coer = aplicar_sobreposicao(res.coerentes, cfg)
    # 3) filtro contra a referência de prioridade
    coer = aplicar_filtro_analisados(coer, prioridade, cfg)

    saida = os.path.join(pasta_gpkg, f"{uf}_Conformidade_Imoveis_{categoria}.gpkg")
    if os.path.exists(saida):
        os.remove(saida)
    escrever_camada(coer, saida, f"CAR_{uf}_Imoveis_{categoria}_coerentes")
    escrever_camada(res.incoerentes, saida, f"CAR_{uf}_Imoveis_{categoria}_incoerentes")

    # limite (boundary) dos imóveis selecionados como "Representante (manter)",
    # num arquivo próprio — é o mesmo conjunto de cod_imovel que o script 5
    # usa para extrair APP/RL/AUR, mas aqui persistido como camada de imóveis.
    manter_gdf = coer[coer["selecao_final"] == SEL_MANTER]
    saida_selecionados = os.path.join(pasta_gpkg, f"{uf}_CAR_Imoveis_Selecionados_{categoria}.gpkg")
    if os.path.exists(saida_selecionados):
        os.remove(saida_selecionados)
    if len(manter_gdf) > 0:
        escrever_camada(manter_gdf, saida_selecionados,
                        f"CAR_{uf}_Imoveis_Selecionados_{categoria}")

    manter = int(len(manter_gdf))
    return {"uf": uf, "categoria": categoria, "universo": len(universo),
            "coerentes": len(res.coerentes), "incoerentes": len(res.incoerentes),
            "manter": manter, "saida": saida, "saida_selecionados": saida_selecionados}


def main() -> int:
    alvos = SOMENTE_ESTES if SOMENTE_ESTES else UFS
    print(f"Estados a processar ({len(alvos)}): {', '.join(alvos)}")
    print(f"Início: {datetime.now():%H:%M:%S}\n")

    sucesso, falhas = [], []
    for uf in alvos:
        for categoria in CATEGORIAS:
            print(f"  [{uf}] {categoria}: classificando conformidade...", flush=True)
            try:
                r = processar_uf_categoria(uf, categoria)
                if r.get("aviso"):
                    print(f"       (aviso: {r['aviso']})", flush=True)
                else:
                    print(f"       universo={r['universo']} | coerentes={r['coerentes']} "
                          f"| incoerentes={r['incoerentes']} | manter={r['manter']}",
                          flush=True)
                sucesso.append(f"{uf}::{categoria}")
            except Exception as e:
                print(f"       !! ERRO: {e}", flush=True)
                falhas.append((f"{uf}::{categoria}", str(e)))

    print(f"\n{'#'*60}\n  RELATÓRIO ({datetime.now():%H:%M:%S})\n{'#'*60}")
    print(f"Sucesso ({len(sucesso)}): {', '.join(sucesso) or '—'}")
    if falhas:
        print(f"Falhas ({len(falhas)}):")
        for chave, msg in falhas:
            print(f"  {chave}: {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
