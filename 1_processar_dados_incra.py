"""
1_processar_dados_incra.py — separa SIGEF e SNCI (Brasil) em um arquivo por estado.

(Renomeado de separar_incra_por_uf.py — 1o passo do workflow numerado do
repositorio, sem mudanca de logica. Ver docs/metodologia.md.)

A partir de UMA camada do SIGEF (Brasil todo) e UMA do SNCI (Brasil todo), este
script gera, para cada UF, os arquivos:

    SIGEF_Privado_<UF>.shp
    SNCI_Privado_<UF>.shp

na pasta de destino. Arquivos que já existirem são PULADOS (não sobrescreve),
para você completar só o que falta.

COMO USAR
---------
1. Confira os caminhos em conformidade/caminhos.py (SIGEF, SNCI, pasta destino).
2. Com o ambiente 'geo' ativo, rode:

       python 1_processar_dados_incra.py

O script tenta descobrir sozinho a coluna que identifica o estado (sigla UF ou
código IBGE). Se não conseguir, ele lista as colunas disponíveis e para, para
você informar o nome na variável COLUNA_UF.
"""

from __future__ import annotations

import os
import sys
import geopandas as gpd

# =============================== CONFIG ===============================
# Caminhos das DUAS camadas de entrada (shapefile .shp ou geopackage .gpkg).
# Se for geopackage, informe o nome da camada em SIGEF_CAMADA / SNCI_CAMADA.
# Downloads do acervo fundiário do INCRA de 10/06/2026, camadas de natureza PRIVADA
# (no backup: Cadastro_INCRA\INCRA_SIGEF_SNCI_Brasil_20260610.zip). As pastas vêm de
# conformidade/caminhos.py (backup restaurado via PLANAVEG_RAIZ ou Dropbox).
from conformidade.caminhos import PASTA_INCRA, SIGEF_ARQ, SNCI_ARQ  # noqa: E402
SIGEF_CAMADA = None          # ex.: "sigef" se for .gpkg; None para shapefile
SNCI_CAMADA = None

# Pasta onde salvar os arquivos por estado (lida depois pelo script 4).
PASTA_SAIDA = PASTA_INCRA

# Coluna que identifica o estado, em CADA camada (elas podem ter nomes
# diferentes). Deixe None para o script tentar descobrir sozinho.
#  - Se a coluna guarda a SIGLA (ex.: "PA"), o script usa direto.
#  - Se guarda o código IBGE (7 dígitos do município, ex.: 1504752), o script
#    deriva a UF pelos 2 primeiros dígitos.
SIGEF_COLUNA_UF = "UF_ID"
SNCI_COLUNA_UF = "UF_municip"

# Sobrescrever arquivos que já existem? Padrão: NÃO (pula os existentes).
SOBRESCREVER = False
# =====================================================================


# Siglas das 27 UFs e o código IBGE (2 primeiros dígitos do geocódigo).
UF_POR_CODIGO = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP",
    "17": "TO", "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB",
    "26": "PE", "27": "AL", "28": "SE", "29": "BA", "31": "MG", "32": "ES",
    "33": "RJ", "35": "SP", "41": "PR", "42": "SC", "43": "RS", "50": "MS",
    "51": "MT", "52": "GO", "53": "DF",
}
SIGLAS = set(UF_POR_CODIGO.values())

# Nomes de coluna comuns para UF (sigla) e para código IBGE.
CAND_SIGLA = ["sigla_uf", "sigla", "uf", "nm_uf", "estado", "cod_estado", "uf_sigla"]
CAND_IBGE = ["geocodigo", "cd_mun", "cd_geocmu", "codigo_ibge", "cod_ibge",
             "geocodig", "cod_mun", "ibge", "cd_uf", "cod_uf"]


def _detectar_coluna(gdf: gpd.GeoDataFrame, coluna_informada: str | None) -> tuple[str, str]:
    """Descobre a coluna de UF e o modo ('sigla' ou 'ibge').

    Se ``coluna_informada`` for dada, usa-a; senão tenta detectar.
    Retorna (nome_coluna, modo). Levanta erro claro se não achar.
    """
    if coluna_informada:
        # busca tolerante a maiúsculas/minúsculas (shapefile rebaixa nomes)
        col = coluna_informada
        if col not in gdf.columns:
            mapa = {c.lower(): c for c in gdf.columns}
            if col.lower() in mapa:
                col = mapa[col.lower()]
            else:
                raise KeyError(
                    f"Coluna '{coluna_informada}' não existe. "
                    f"Colunas: {list(gdf.columns)}"
                )
        # decide o modo pela amostra
        amostra = gdf[col].dropna().astype(str).head(50)
        if amostra.str.strip().str.upper().isin(SIGLAS).mean() > 0.5:
            return col, "sigla"
        return col, "ibge"

    cols_lower = {c.lower(): c for c in gdf.columns}
    # 1) tenta coluna de sigla
    for cand in CAND_SIGLA:
        if cand in cols_lower:
            col = cols_lower[cand]
            amostra = gdf[col].dropna().astype(str).head(50).str.strip().str.upper()
            if amostra.isin(SIGLAS).mean() > 0.5:
                return col, "sigla"
    # 2) tenta coluna de código IBGE
    for cand in CAND_IBGE:
        if cand in cols_lower:
            return cols_lower[cand], "ibge"
    # 3) varredura: qualquer coluna cujos valores sejam siglas de UF
    for col in gdf.columns:
        if col == gdf.geometry.name:
            continue
        try:
            amostra = gdf[col].dropna().astype(str).head(50).str.strip().str.upper()
            if len(amostra) and amostra.isin(SIGLAS).mean() > 0.7:
                return col, "sigla"
        except Exception:
            continue
    raise KeyError(
        "Não consegui identificar a coluna de UF automaticamente.\n"
        f"Colunas disponíveis: {list(gdf.columns)}\n"
        "Edite COLUNA_UF no topo do script com o nome correto."
    )


def _uf_series(gdf: gpd.GeoDataFrame, col: str, modo: str):
    """Devolve uma série de siglas de UF (2 letras) para cada feição."""
    if modo == "sigla":
        return gdf[col].astype(str).str.strip().str.upper()
    # modo ibge: usa os 2 primeiros dígitos do código
    cod2 = gdf[col].astype(str).str.replace(r"\D", "", regex=True).str[:2]
    return cod2.map(UF_POR_CODIGO)


def separar(nome: str, arq: str, camada, prefixo: str, coluna_uf: str | None) -> None:
    """Lê uma base nacional e grava um arquivo por UF."""
    print(f"\n=== {nome} ===", file=sys.stderr)
    if not os.path.exists(arq):
        print(f"  ! arquivo não encontrado: {arq}", file=sys.stderr)
        print(f"    (preencha {nome}_ARQ no topo do script)", file=sys.stderr)
        return
    print(f"  lendo {arq} ...", file=sys.stderr)
    gdf = gpd.read_file(arq, layer=camada) if camada else gpd.read_file(arq)
    col, modo = _detectar_coluna(gdf, coluna_uf)
    print(f"  coluna de estado: '{col}' (modo: {modo}) | {len(gdf)} feições",
          file=sys.stderr)

    ufs = _uf_series(gdf, col, modo)
    gdf = gdf.assign(_uf=ufs)
    presentes = sorted(u for u in gdf["_uf"].dropna().unique() if u in SIGLAS)
    print(f"  estados encontrados ({len(presentes)}): {', '.join(presentes)}",
          file=sys.stderr)

    os.makedirs(PASTA_SAIDA, exist_ok=True)
    criados, pulados = 0, 0
    for uf in presentes:
        destino = os.path.join(PASTA_SAIDA, f"{prefixo}_{uf}.shp")
        if os.path.exists(destino) and not SOBRESCREVER:
            pulados += 1
            continue
        sub = gdf[gdf["_uf"] == uf].drop(columns=["_uf"])
        sub.to_file(destino)  # shapefile
        criados += 1
        print(f"    ok {prefixo}_{uf}.shp ({len(sub)} feições)", file=sys.stderr)
    print(f"  >> {criados} criados, {pulados} já existiam (pulados)",
          file=sys.stderr)


def main() -> int:
    print("Separando INCRA por estado...", file=sys.stderr)
    separar("SIGEF", SIGEF_ARQ, SIGEF_CAMADA, "SIGEF_Privado", SIGEF_COLUNA_UF)
    separar("SNCI", SNCI_ARQ, SNCI_CAMADA, "SNCI_Privado", SNCI_COLUNA_UF)
    print("\nConcluído.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
