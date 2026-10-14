# -*- coding: utf-8 -*-
r"""
5_extracao_APP_RL_AUR.py — extrai as camadas temáticas APP / RESERVA_LEGAL /
USO_RESTRITO (AUR) do SICAR bruto, para os imóveis já selecionados:

  Habilitados      : TODOS os imóveis de <UF>_CAR_Imoveis_Selecionados_Habilitados.gpkg
                      (não passam pela análise de conformidade do script 4 —
                      por definição já não têm pendência).
  Analisados       : só os "Representante (manter)" da análise de
                      conformidade do script 4, em
                      <UF>_Conformidade_Imoveis_Analisados.gpkg.
  Não Analisados   : idem, em <UF>_Conformidade_Imoveis_Nao_Analisados.gpkg.

(Generalização de recorte_notificacao.py — 5o passo do workflow numerado do
repositório, inserido entre a análise de conformidade [script 4] e a
validação final [script 6].)

Como os geopackages de imóveis guardam só o AREA_IMOVEL (sem os planos
temáticos), as APP/RL/AUR são lidas da FONTE BRUTA do SICAR
(PASTA_SICAR\<ESTADO>\), descompactando os .zip e juntando os pedaços
fatiados quando necessário.

Para cada estado x categoria:
  1. lê o geopackage de origem da categoria e define o conjunto de
     cod_imovel a extrair (todos, para Habilitados; só "Representante
     (manter)", para Analisados/Não Analisados);
  2. descompacta APPS / RESERVA_LEGAL / USO_RESTRITO do bruto e concatena;
  3. filtra as feições temáticas por esses cod_imovel (descartando
     canceladas, por segurança);
  4. grava as camadas CAR_<UF>_APP_Selecionados_<Categoria>,
     CAR_<UF>_RL_Selecionados_<Categoria> e CAR_<UF>_AUR_Selecionados_<Categoria>
     no MESMO geopackage de origem da categoria.

COMO USAR
---------
Ambiente 'geo' ativo:
    python 5_extracao_APP_RL_AUR.py
Valide com SOMENTE_ESTES = ["AC"]; depois esvazie para todos.
"""

from __future__ import annotations

import os
import glob
import zipfile
import unicodedata
from datetime import datetime

import geopandas as gpd
import pandas as pd

from conformidade.io_dados import ler_camada, escrever_camada
from conformidade.fases import classificar_fase, CANCELADO

# =============================== CONFIG ===============================
# Pastas de dados: ver conformidade/caminhos.py (backup restaurado via PLANAVEG_RAIZ ou Dropbox).
from conformidade.caminhos import PASTA_ANALISE, PASTA_SICAR  # noqa: E402

# Deixe vazio para TODOS; ou liste siglas (ex.: ["AC"]) para validar.
SOMENTE_ESTES: list[str] = []

# Apagar os .shp descompactados ao terminar cada estado (poupa espaço)?
APAGAR_SHP_AO_FIM = True

COL_COD = "cod_imovel"
PLANOS_TEMATICOS = [("APPS", "APP"), ("RESERVA_LEGAL", "RL"), ("USO_RESTRITO", "AUR")]
SEL_MANTER = "Representante (manter)"
CATEGORIAS = ["Habilitados", "Analisados", "Nao_Analisados"]
# =====================================================================

NOME_PARA_UF = {
    "ACRE": "AC", "ALAGOAS": "AL", "AMAPA": "AP", "AMAZONAS": "AM",
    "BAHIA": "BA", "CEARA": "CE", "DISTRITO FEDERAL": "DF",
    "ESPIRITO SANTO": "ES", "GOIAS": "GO", "MARANHAO": "MA",
    "MATO GROSSO": "MT", "MATO GROSSO DO SUL": "MS", "MINAS GERAIS": "MG",
    "PARA": "PA", "PARAIBA": "PB", "PARANA": "PR", "PERNAMBUCO": "PE",
    "PIAUI": "PI", "RIO DE JANEIRO": "RJ", "RIO GRANDE DO NORTE": "RN",
    "RIO GRANDE DO SUL": "RS", "RONDONIA": "RO", "RORAIMA": "RR",
    "SANTA CATARINA": "SC", "SAO PAULO": "SP", "SERGIPE": "SE",
    "TOCANTINS": "TO",
}
UF_PARA_NOME = {uf: nome for nome, uf in NOME_PARA_UF.items()}
UFS = sorted(NOME_PARA_UF.values())


def _norm(txt: str) -> str:
    t = unicodedata.normalize("NFKD", txt)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.upper().split())


def achar_pasta_estado(uf: str) -> str | None:
    """Acha a pasta do estado no bruto (nome por extenso, tolerante a acento)."""
    alvo = UF_PARA_NOME.get(uf)
    if not alvo:
        return None
    if not os.path.isdir(PASTA_SICAR):
        return None
    for d in os.listdir(PASTA_SICAR):
        if os.path.isdir(os.path.join(PASTA_SICAR, d)) and _norm(d) == alvo:
            return os.path.join(PASTA_SICAR, d)
    return None


def descompactar_plano(pasta_estado: str, plano: str) -> list[str]:
    """Descompacta o(s) .zip do plano (se preciso) e retorna os .shp achados."""
    pasta_plano = os.path.join(pasta_estado, plano)
    if os.path.isdir(pasta_plano):
        shps = sorted(glob.glob(os.path.join(pasta_plano, "*.shp")))
        if shps:
            return shps
        for z in glob.glob(os.path.join(pasta_plano, "*.zip")):
            with zipfile.ZipFile(z) as zf:
                zf.extractall(pasta_plano)
        shps = sorted(glob.glob(os.path.join(pasta_plano, "*.shp")))
        if shps:
            return shps
    z = os.path.join(pasta_estado, plano + ".zip")
    if os.path.exists(z):
        os.makedirs(pasta_plano, exist_ok=True)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(pasta_plano)
        return sorted(glob.glob(os.path.join(pasta_plano, "*.shp")))
    return []


def _apagar_shapefiles(pasta: str) -> None:
    if not os.path.isdir(pasta):
        return
    for shp in glob.glob(os.path.join(pasta, "*.shp")):
        base = os.path.splitext(shp)[0]
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qmd", ".fix"):
            f = base + ext
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass


def cods_alvo(pasta_gpkg: str, uf: str, categoria: str) -> tuple[set[str], str] | None:
    """Determina o gpkg de origem/destino e o conjunto de cod_imovel a extrair.

    Retorna (cods, caminho_gpkg) ou None se a fonte não existir/estiver vazia.
    """
    if categoria == "Habilitados":
        caminho = os.path.join(pasta_gpkg, f"{uf}_CAR_Imoveis_Selecionados_Habilitados.gpkg")
        layer = f"CAR_{uf}_Imoveis_Habilitados"
        if not os.path.exists(caminho):
            return None
        gdf = ler_camada(caminho, layer)
        cods = set(gdf[COL_COD].astype(str))
        return (cods, caminho) if cods else None

    caminho = os.path.join(pasta_gpkg, f"{uf}_Conformidade_Imoveis_{categoria}.gpkg")
    layer = f"CAR_{uf}_Imoveis_{categoria}_coerentes"
    if not os.path.exists(caminho):
        return None
    coer = ler_camada(caminho, layer)
    if "selecao_final" not in coer.columns:
        raise KeyError(f"{caminho}::{layer} sem 'selecao_final'")
    manter = coer[coer["selecao_final"] == SEL_MANTER]
    cods = set(manter[COL_COD].astype(str))
    return (cods, caminho) if cods else None


def processar_uf_categoria(uf: str, categoria: str) -> dict:
    """Extrai as temáticas de um bucket de um estado. Lança exceção em erro."""
    pasta_gpkg = os.path.join(PASTA_ANALISE, f"dados_saída_{uf}", f"{uf}_geopackage")

    alvo = cods_alvo(pasta_gpkg, uf, categoria)
    if alvo is None:
        return {"uf": uf, "categoria": categoria, "aviso": "fonte vazia ou inexistente"}
    cods, gpkg_destino = alvo

    pasta_estado = achar_pasta_estado(uf)
    if not pasta_estado:
        raise FileNotFoundError(f"pasta bruta do estado {uf} não encontrada em {PASTA_SICAR}")

    resumo = {"uf": uf, "categoria": categoria, "selecionados": len(cods)}
    for plano, sigla in PLANOS_TEMATICOS:
        shps = descompactar_plano(pasta_estado, plano)
        if not shps:
            resumo[sigla] = 0
            continue
        partes = []
        for shp in shps:
            g = ler_camada(shp)
            if COL_COD not in g.columns:
                continue
            g = g[g[COL_COD].astype(str).isin(cods)]
            # salvaguarda: descarta feições canceladas
            if "des_condic" in g.columns and len(g) > 0:
                fase = g["des_condic"].map(classificar_fase)
                g = g[fase != CANCELADO]
            if len(g) > 0:
                partes.append(g)
        if partes:
            sel = gpd.GeoDataFrame(pd.concat(partes, ignore_index=True),
                                   crs=partes[0].crs)
            escrever_camada(sel, gpkg_destino, f"CAR_{uf}_{sigla}_Selecionados_{categoria}")
            resumo[sigla] = len(sel)
        else:
            resumo[sigla] = 0

    if APAGAR_SHP_AO_FIM:
        for plano, _ in PLANOS_TEMATICOS:
            _apagar_shapefiles(os.path.join(pasta_estado, plano))

    return resumo


def main() -> int:
    alvos = SOMENTE_ESTES if SOMENTE_ESTES else UFS
    print(f"Estados a processar ({len(alvos)}): {', '.join(alvos)}")
    print(f"Início: {datetime.now():%H:%M:%S}\n")

    sucesso, falhas = [], []
    for uf in alvos:
        for categoria in CATEGORIAS:
            print(f"  [{uf}] {categoria}: extraindo temáticas...", flush=True)
            try:
                r = processar_uf_categoria(uf, categoria)
                if r.get("aviso"):
                    print(f"       (aviso: {r['aviso']})", flush=True)
                else:
                    print(f"       selecionados={r['selecionados']} | APP={r.get('APP', 0)} "
                          f"| RL={r.get('RL', 0)} | AUR={r.get('AUR', 0)}", flush=True)
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
