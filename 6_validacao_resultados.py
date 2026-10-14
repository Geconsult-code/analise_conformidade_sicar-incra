# -*- coding: utf-8 -*-
"""
6_validacao_resultados.py

Fusão de relatorio_consistencia_conformidade.py + correcao_geometrias.py —
6o (e último) passo do workflow numerado do repositório. Roda em duas
etapas sequenciais, ambas resilientes/retomáveis:

ETAPA 1 — RELATÓRIO: para cada UF e cada arquivo .gpkg de FILES_TPL
(saída dos scripts 3, 4 e 5, em Analise_Conformidade\\dados_saída_<UF>\\
<UF>_geopackage\\), confere projeção, valida geometrias (conta inválidas,
sem alterar o .gpkg original — é só leitura), conta polígonos/sem-geometria
por camada, calcula área total em hectares (reprojeção equal-area
EPSG:6933) e, para as camadas *_coerentes/*_incoerentes, área por
categoria (des_condic × selecao_final). Grava um JSON por .gpkg (mesmo
nome, extensão .json) na mesma pasta. Pula arquivos que já têm JSON,
a menos que a UF esteja em REFAZER.

ETAPA 2 — CORREÇÃO: usa os JSONs recém-gerados (ou já existentes) da
etapa 1 para montar a lista de camadas com geometrias inválidas, e
regrava (in-place) SÓ a geometria dessas feições com shapely.make_valid()
e fallback buffer(0) — mesmo padrão já validado no projeto (inclusive no
caso patológico do Amazonas). Não mexe em geometrias válidas nem em
contagem de feições. Roda em blocos de tempo (BUDGET_S) via
rodada_correcao(), retomável entre chamadas.

ETAPA 3 — VERIFICAÇÃO: reescaneia as camadas corrigidas e confirma que
zero geometrias seguem inválidas.

Rodar no ambiente conda 'geo' do usuário:
    conda activate geo
    python 6_validacao_resultados.py

RECOMENDADO: para validar antes do lote completo, rode uma vez com
SOMENTE_ESTES = ["AC"] (ou outra UF pequena). O padrão de produção abaixo
(SOMENTE_ESTES = [], REFAZER = []) processa todas as 27 UFs.
"""

import os
import sys
import json
import time
import glob
import threading

import numpy as np
import geopandas as gpd
import shapely

from osgeo import ogr, gdal
gdal.UseExceptions()

# --------------------------------------------------------------------------
# CONFIGURACAO
# --------------------------------------------------------------------------

# Pastas de dados: ver conformidade/caminhos.py (backup restaurado via PLANAVEG_RAIZ ou Dropbox).
from conformidade.caminhos import PASTA_ANALISE as BASE_ROOT  # noqa: E402

EQUAL_AREA_CRS = "EPSG:6933"  # World Cylindrical Equal Area - validado no projeto

UFS = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT",
       "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"]

# Saída dos scripts 3 (categorização), 4 (conformidade) e 5 (extração APP/RL/AUR).
FILES_TPL = [
    "{uf}_CAR_Imoveis_Selecionados_Habilitados.gpkg",
    "{uf}_Imoveis_Privados_Analisados.gpkg",
    "{uf}_Imoveis_Privados_Nao_Analisados.gpkg",
    "{uf}_Imoveis_Privados_Outros_Analisado.gpkg",   # auditoria do script 3 (pode não existir)
    "{uf}_Conformidade_Imoveis_Analisados.gpkg",
    "{uf}_Conformidade_Imoveis_Nao_Analisados.gpkg",
    "{uf}_CAR_Imoveis_Selecionados_Analisados.gpkg",
    "{uf}_CAR_Imoveis_Selecionados_Nao_Analisados.gpkg",
]

# --- ajuste aqui conforme a rodada ---
SOMENTE_ESTES = []       # lista de UFs para restringir a rodada (ex.: ["AC"]); vazio = todas as 27 UFs (padrao de producao)
PULAR = []
REFAZER = []              # lista de UFs para forcar regravar mesmo com JSON existente; vazio = nenhuma (padrao de producao)
# --------------------------------------

HEARTBEAT_INTERVALO_S = 300
CHUNK_CORRECAO = 25000     # tamanho do bloco de FIDs por leitura/transacao (etapa 2)
BUDGET_S_CORRECAO = 35     # orcamento de tempo por chamada de rodada_correcao()

PROGRESSO_CORRECAO = os.path.join(BASE_ROOT, "_progresso_correcao.json")

_heartbeat_status = {"uf": None, "arquivo": None, "layer": None, "inicio": None}
_heartbeat_stop = False


def _heartbeat_loop(path_hb):
    while not _heartbeat_stop:
        try:
            snap = dict(_heartbeat_status)
            if snap.get("inicio"):
                snap["decorrido_s"] = round(time.time() - snap["inicio"], 1)
            with open(path_hb, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        time.sleep(HEARTBEAT_INTERVALO_S)


# ============================================================================
# ETAPA 1 — RELATORIO
# ============================================================================

def listar_layers(path):
    try:
        import pyogrio
        return [row[0] for row in pyogrio.list_layers(path)]
    except Exception:
        import fiona
        return list(fiona.listlayers(path))


def area_ha_equal_area(gdf):
    """Area aproximada (equal-area EPSG:6933), em hectares. Vetorizado."""
    proj = gdf.to_crs(EQUAL_AREA_CRS)
    return proj.geometry.area / 10000.0


def categoria_da_layer(nome_layer):
    if nome_layer.endswith("_incoerentes"):
        return "incoerente"
    if nome_layer.endswith("_coerentes"):
        return "coerente"
    return None


def analisar_layer(gdf, categorizar=None):
    epsg = None
    try:
        epsg = gdf.crs.to_epsg()
    except Exception:
        pass

    n_total = len(gdf)
    arr = np.asarray(gdf.geometry.values, dtype=object)

    missing_mask = shapely.is_missing(arr)
    empty_mask = np.zeros(len(arr), dtype=bool)
    idx_presentes = np.where(~missing_mask)[0]
    if len(idx_presentes) > 0:
        empty_mask[idx_presentes] = shapely.is_empty(arr[idx_presentes])
    vazio_mask = missing_mask | empty_mask
    n_vazio = int(vazio_mask.sum())

    sub = gdf.loc[~vazio_mask].copy()
    n_invalid = 0
    n_descartadas_reparo = 0
    if len(sub) > 0:
        sub_arr = np.asarray(sub.geometry.values, dtype=object)
        invalid_mask = ~shapely.is_valid(sub_arr)
        n_invalid = int(invalid_mask.sum())
        if n_invalid > 0:
            fixed = sub_arr.copy()
            try:
                fixed[invalid_mask] = shapely.make_valid(sub_arr[invalid_mask])
            except Exception:
                idx_invalid = np.where(invalid_mask)[0]
                for i in idx_invalid:
                    g = sub_arr[i]
                    novo = None
                    try:
                        novo = shapely.make_valid(g)
                    except Exception:
                        try:
                            novo = g.buffer(0)
                        except Exception:
                            novo = None
                    if novo is None or novo.is_empty:
                        n_descartadas_reparo += 1
                        fixed[i] = None
                    else:
                        fixed[i] = novo
            sub["geometry"] = fixed
            if n_descartadas_reparo > 0:
                descarte_mask = np.array([g is None for g in fixed])
                sub = sub.loc[~descarte_mask].copy()

        if len(sub) > 0:
            sub["_area_ha"] = area_ha_equal_area(sub)
            area_total = float(sub["_area_ha"].sum())
        else:
            area_total = 0.0
    else:
        area_total = 0.0

    result = {
        "epsg": epsg,
        "num_poligonos": int(n_total),
        "num_geometrias_invalidas": n_invalid,
        "num_geometrias_descartadas_no_reparo": n_descartadas_reparo,
        "num_sem_geometria": n_vazio,
        "area_total_ha": round(area_total, 4),
    }

    if categorizar == "incoerente" and len(sub) > 0 and "des_condic" in sub.columns:
        tmp = sub.copy()
        tmp["des_condic"] = tmp["des_condic"].fillna("(vazio)")
        grp = tmp.groupby("des_condic")["_area_ha"].agg(["count", "sum"])
        result["area_por_categoria"] = {
            "campo": "des_condic",
            "categorias": {
                str(k): {"num_poligonos": int(v["count"]), "area_ha": round(float(v["sum"]), 4)}
                for k, v in grp.iterrows()
            },
        }
    elif categorizar == "coerente" and len(sub) > 0 and "des_condic" in sub.columns and "selecao_final" in sub.columns:
        tmp = sub.copy()
        tmp["des_condic"] = tmp["des_condic"].fillna("(vazio)")
        tmp["selecao_final"] = tmp["selecao_final"].fillna("(vazio)")
        grp = tmp.groupby(["des_condic", "selecao_final"])["_area_ha"].agg(["count", "sum"])
        result["area_por_categoria"] = {
            "campos": ["des_condic", "selecao_final"],
            "categorias": {
                f"{k[0]} | {k[1]}": {"num_poligonos": int(v["count"]), "area_ha": round(float(v["sum"]), 4)}
                for k, v in grp.iterrows()
            },
        }

    return result


def processar_arquivo(uf, folder_gpkg, fn):
    path = os.path.join(folder_gpkg, fn)
    if not os.path.isfile(path):
        return None

    layers = listar_layers(path)
    out = {
        "arquivo_geopackage": fn,
        "uf": uf,
        "gerado_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "layers": {},
    }
    for lname in layers:
        _heartbeat_status.update({"uf": uf, "arquivo": fn, "layer": lname, "inicio": time.time()})
        t0 = time.time()
        try:
            gdf = gpd.read_file(path, layer=lname)
            cat = categoria_da_layer(lname)
            info = analisar_layer(gdf, categorizar=cat)
        except Exception as e:
            info = {"erro": str(e)}
        out["layers"][lname] = info
        dt = time.time() - t0
        if "erro" in info:
            print(f"  [{uf}] {fn}::{lname} -> ERRO: {info['erro']}")
        else:
            print(f"  [{uf}] {fn}::{lname} -> {info['num_poligonos']} poligonos, "
                  f"{info['num_geometrias_invalidas']} invalidas, "
                  f"{info['num_sem_geometria']} sem geometria, "
                  f"{info['area_total_ha']:.1f} ha ({dt:.1f}s)")
    return out


def rodar_relatorio():
    """Etapa 1: gera/atualiza os JSONs de relatório para as UFs/arquivos alvo."""
    global _heartbeat_stop

    hb_path = os.path.join(BASE_ROOT, "heartbeat_relatorio.txt")
    hb_thread = threading.Thread(target=_heartbeat_loop, args=(hb_path,), daemon=True)
    hb_thread.start()

    ufs = SOMENTE_ESTES if SOMENTE_ESTES else UFS
    resumo = {}

    for uf in ufs:
        if uf in PULAR:
            continue
        folder_saida = os.path.join(BASE_ROOT, f"dados_saída_{uf}")
        folder_gpkg = os.path.join(folder_saida, f"{uf}_geopackage")
        if not os.path.isdir(folder_gpkg):
            print(f"[{uf}] pasta nao encontrada, pulando")
            continue

        t0 = time.time()
        arquivos_ok = []
        for ftpl in FILES_TPL:
            fn = ftpl.format(uf=uf)
            out_json = os.path.join(folder_gpkg, fn.replace(".gpkg", ".json"))
            if os.path.isfile(out_json) and uf not in REFAZER:
                arquivos_ok.append(fn)
                print(f"[{uf}] {fn} ja tem JSON, pulando (use REFAZER pra forcar)")
                continue
            try:
                res = processar_arquivo(uf, folder_gpkg, fn)
                if res is None:
                    continue
                with open(out_json, "w", encoding="utf-8") as f:
                    json.dump(res, f, ensure_ascii=False, indent=1)
                arquivos_ok.append(fn)
                print(f"[{uf}] gravado {out_json}")
            except Exception as e:
                print(f"[{uf}] ERRO em {fn}: {e}")

        resumo[uf] = {"arquivos": arquivos_ok, "tempo_s": round(time.time() - t0, 1)}
        print(f"[{uf}] concluido em {resumo[uf]['tempo_s']}s")

    _heartbeat_stop = True

    resumo_path = os.path.join(BASE_ROOT, "resumo_relatorio_consistencia.json")
    with open(resumo_path, "w", encoding="utf-8") as f:
        json.dump(resumo, f, ensure_ascii=False, indent=1)
    print("\nRESUMO ETAPA 1 (relatório):")
    print(json.dumps(resumo, ensure_ascii=False, indent=1))
    return resumo


# ============================================================================
# ETAPA 2 — CORRECAO (usa os JSONs gerados/lidos na etapa 1)
# ============================================================================

def carregar_worklist_correcao():
    """Varre os JSONs de relatório (um por .gpkg, na pasta <UF>_geopackage) e
    monta a lista de camadas com num_geometrias_invalidas > 0."""
    ufs = SOMENTE_ESTES if SOMENTE_ESTES else UFS
    itens = []
    for uf in ufs:
        if uf in PULAR:
            continue
        folder_gpkg = os.path.join(BASE_ROOT, f"dados_saída_{uf}", f"{uf}_geopackage")
        for ftpl in FILES_TPL:
            fn = ftpl.format(uf=uf)
            json_path = os.path.join(folder_gpkg, fn.replace(".gpkg", ".json"))
            if not os.path.isfile(json_path):
                continue
            with open(json_path, "r", encoding="utf-8") as f:
                rel = json.load(f)
            for lname, info in rel.get("layers", {}).items():
                n_inval = info.get("num_geometrias_invalidas") or 0
                if n_inval > 0:
                    itens.append({
                        "uf": uf, "arquivo": fn, "layer": lname,
                        "path": os.path.join(folder_gpkg, fn),
                        "num_poligonos": info["num_poligonos"],
                        "num_invalidas_relatorio": n_inval,
                    })
    itens.sort(key=lambda x: (x["uf"], x["arquivo"], x["layer"]))
    return itens


def carregar_progresso_correcao():
    if os.path.isfile(PROGRESSO_CORRECAO):
        with open(PROGRESSO_CORRECAO, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_progresso_correcao(prog):
    with open(PROGRESSO_CORRECAO, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=1)


def reparar_geom(ogr_geom):
    wkb = bytes(ogr_geom.ExportToIsoWkb())
    g = shapely.from_wkb(wkb)
    try:
        novo = shapely.make_valid(g)
    except Exception:
        try:
            novo = g.buffer(0)
        except Exception:
            return None
    if novo is None or novo.is_empty:
        return None
    return ogr.CreateGeometryFromWkb(shapely.to_wkb(novo))


def processar_chunk_correcao(item, fid_lo, fid_hi):
    ds = ogr.Open(item["path"], update=1)
    lyr = ds.GetLayerByName(item["layer"])
    fidcol = lyr.GetFIDColumn() or "fid"
    lyr.SetAttributeFilter(f'"{fidcol}" >= {fid_lo} AND "{fidcol}" < {fid_hi}')
    lyr.ResetReading()

    n_scan = n_inval = n_fix = n_desc = 0
    lyr.StartTransaction()
    feat = lyr.GetNextFeature()
    while feat is not None:
        n_scan += 1
        geom = feat.GetGeometryRef()
        if geom is not None and not geom.IsValid():
            n_inval += 1
            novo = reparar_geom(geom)
            if novo is not None:
                if novo.GetGeometryType() == ogr.wkbPolygon:
                    novo = ogr.ForceToMultiPolygon(novo)
                feat.SetGeometry(novo)
                lyr.SetFeature(feat)
                n_fix += 1
            else:
                n_desc += 1
        feat = lyr.GetNextFeature()
    lyr.CommitTransaction()
    lyr.SetAttributeFilter(None)
    ds = None
    return n_scan, n_inval, n_fix, n_desc


def rodada_correcao():
    """Etapa 2, um bloco de tempo (BUDGET_S_CORRECAO). Retorna True quando
    TODAS as camadas do worklist estiverem concluídas."""
    t0 = time.time()
    worklist = carregar_worklist_correcao()
    prog = carregar_progresso_correcao()

    if not worklist:
        print("Nenhuma camada com geometrias invalidas segundo os relatórios. Nada a corrigir.")
        return True

    log = []
    for item in worklist:
        chave = f"{item['uf']}::{item['arquivo']}::{item['layer']}"
        st = prog.get(chave, {
            "fid_proximo": 0, "concluido": False,
            "total_invalidas": 0, "total_corrigidas": 0, "total_descartadas": 0,
            "total_escaneadas": 0,
        })
        if st["concluido"]:
            continue

        total = item["num_poligonos"]
        while st["fid_proximo"] <= total:
            if time.time() - t0 > BUDGET_S_CORRECAO:
                prog[chave] = st
                salvar_progresso_correcao(prog)
                log.append(f"[PAUSA orcamento] {chave} em fid={st['fid_proximo']}/{total}")
                print("\n".join(log))
                print(f"\nRODADA parcial: {round(time.time()-t0,1)}s. Rode de novo pra continuar.")
                return False

            lo = st["fid_proximo"]
            hi = lo + CHUNK_CORRECAO
            n_scan, n_inval, n_fix, n_desc = processar_chunk_correcao(item, lo, hi)
            st["fid_proximo"] = hi
            st["total_escaneadas"] += n_scan
            st["total_invalidas"] += n_inval
            st["total_corrigidas"] += n_fix
            st["total_descartadas"] += n_desc

            if n_scan == 0 and lo > total:
                break

        st["concluido"] = True
        prog[chave] = st
        salvar_progresso_correcao(prog)
        log.append(
            f"[OK] {chave}: escaneadas={st['total_escaneadas']} invalidas={st['total_invalidas']} "
            f"corrigidas={st['total_corrigidas']} descartadas={st['total_descartadas']}"
        )

    salvar_progresso_correcao(prog)
    print("\n".join(log))
    print(f"\nRODADA completa: TODAS as {len(worklist)} camadas processadas. Tempo: {round(time.time()-t0,1)}s")
    return True


# ============================================================================
# ETAPA 3 — VERIFICACAO
# ============================================================================

def verificar():
    """Reescaneia todas as camadas do worklist e confere que zero geometrias
    seguem invalidas."""
    worklist = carregar_worklist_correcao()
    problemas = []
    for item in worklist:
        ds = ogr.Open(item["path"])
        lyr = ds.GetLayerByName(item["layer"])
        n_inval = 0
        lyr.ResetReading()
        feat = lyr.GetNextFeature()
        while feat is not None:
            geom = feat.GetGeometryRef()
            if geom is not None and not geom.IsValid():
                n_inval += 1
            feat = lyr.GetNextFeature()
        ds = None
        if n_inval > 0:
            problemas.append({"uf": item["uf"], "arquivo": item["arquivo"],
                              "layer": item["layer"], "restantes": n_inval})
    print(json.dumps(problemas, ensure_ascii=False, indent=1))
    print(f"\nCamadas ainda com invalidas: {len(problemas)} de {len(worklist)}")
    return problemas


if __name__ == "__main__":
    print("="*70 + "\nETAPA 1/3 — RELATÓRIO\n" + "="*70)
    rodar_relatorio()

    print("\n" + "="*70 + "\nETAPA 2/3 — CORREÇÃO DE GEOMETRIAS\n" + "="*70)
    while not rodada_correcao():
        pass

    print("\n" + "="*70 + "\nETAPA 3/3 — VERIFICAÇÃO\n" + "="*70)
    verificar()

    print("\nLOTE COMPLETO.")
