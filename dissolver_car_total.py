"""
dissolver_car_total.py

HISTÓRICO: utilitário recuperado do histórico do repositório
cruzamento_vegetacao-secundaria (última versão: commit 5ea111e, 25/08/2026), de onde
foi removido em 7a1f420. Mantido aqui porque gerou os CARs dissolvidos por UF usados
pelo cômputo e pela visualização (backup: Analise_Territorial_CAR-INCRA_dissolvido).
Única mudança em relação àquela versão: a pasta por UF, os nomes dos arquivos de
entrada e a camada lida passaram para o CONFIG (PASTA_UF, ARQ_*, CAMADA_ENTRADA), com
os mesmos valores de antes. Os valores para a estrutura atual (dados_saída_<UF>) estão
comentados no CONFIG e ainda NÃO foram validados: rode primeiro com SOMENTE_ESTES=["AC"]
e compare com CAR_Brasil_Maio2026_Analisado_e_Nao_Analisado_dissolvido_UF do backup.

Gera uma camada com a AREA DISSOLVIDA de TODOS os imoveis cadastrados no
SICAR/CAR por estado, SEPARADA em duas categorias -- "analisado" e
"nao_analisado" -- para facilitar interseccoes posteriores. Cobre todo o
CAR (coerentes e incoerentes com o INCRA, ou seja, SEM nenhum filtro de
conformidade SICARxINCRA). Imoveis "Cancelado" ja ficam de fora porque nao
entram nos gpkg <UF>_analisados/<UF>_trabalho gerados pela etapa 'preparar'
do pacote conformidade (processar_lote.py).

Fontes por estado (pasta _saida_<UF> dentro de INCRA-CAR):
  <UF>_analisados.gpkg  -> camada AREA_IMOVEL -> categoria "analisado"
  <UF>_trabalho.gpkg    -> camada AREA_IMOVEL (Em Analise + Aguardando)
                            -> categoria "nao_analisado"

Dentro de cada categoria os imoveis sao dissolvidos entre si (a fronteira
entre imoveis da MESMA categoria desaparece, geometria simplificada).

PRIORIDADE entre categorias: "analisado" tem prioridade e fica intacto.
Depois de dissolvida, a geometria "nao_analisado" e RECORTADA (diferenca)
pela geometria "analisado" da mesma UF, para eliminar qualquer sobreposicao
remanescente entre as duas categorias -- mesmo principio ja usado no filtro
'sobrepoe_analisado' do pipeline de conformidade (Analisado nunca e
removido/alterado; quem sobrepoe cede).

Saida: UM unico GeoPackage com UMA camada 'CAR_total_UF' -- ate 54 feicoes
(2 por UF: analisado + nao_analisado), com campos:
  uf                sigla do estado
  categoria         "analisado" ou "nao_analisado"
  n_imoveis_origem  n. de registros de origem daquela categoria (antes do dissolve/recorte)
  area_ha           area geodesica (GRS80) do poligono final, em hectares
                     (para "nao_analisado", ja e a area POS-recorte)

Uso: ajustar CONFIG abaixo e rodar no ambiente conda 'geo' (mesmo ambiente
do pacote 'conformidade' / processar_lote.py).

    python dissolver_car_total.py

Resiliente: se uma UF falhar, o erro fica registrado e o lote segue para a
proxima (mesmo padrao do processar_lote.py). Rodar de novo so processa as
UFs que ainda nao tem as DUAS categorias completas na saida (ou as listadas
em REFAZER) -- nao refaz o que ja foi gravado. As duas categorias de uma UF
sao sempre (re)processadas juntas, porque o recorte do nao_analisado
depende do analisado da mesma UF.

Processa ate MAX_WORKERS UFs em paralelo (threads -- as operacoes do
shapely/GEOS liberam o GIL, entao isso usa nucleos de verdade); so a
gravacao no gpkg fica serializada. area_ha_geodesica() nunca retorna NaN
silenciosamente: se o pyproj devolver NaN (pode acontecer em geometrias
gigantes pos-recorte, artefato raro de coordenada degenerada), tenta
reparar e avisa no console.

Uniao (dissolve) e recorte (difference) sao ROBUSTOS: se o GEOS lancar
TopologyException (visto na pratica: PR quebrou com 'Ring edge missing' --
geometria complexa demais pro algoritmo de precisao flutuante padrao) ou
produzir coordenada invalida mesmo sem erro (visto no MA), a UF nao trava
o lote inteiro -- ela e marcada como falha (fica de fora da saida, entao
uma proxima execucao tenta de novo automaticamente) OU o script tenta de
novo com grid_size (arredondamento pra grade fixa, mais lento porem mais
robusto para geometria muito densa/complexa) antes de desistir.

HEARTBEAT: a cada HEARTBEAT_INTERVALO_S (default 5 min) o script escreve
ARQ_HEARTBEAT (heartbeat_dissolver.txt, na mesma pasta da saida) dizendo o
que cada UF esta fazendo agora e ha quanto tempo -- util porque o
dissolve/recorte de uma UF grande e uma UNICA chamada do GEOS que pode
ficar horas sem imprimir nada no console. Basta abrir esse arquivo (ou
checar a hora de modificacao dele) pra confirmar que o processo continua
vivo, sem precisar esperar a UF inteira terminar.
"""

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Geod, Transformer
from shapely.geometry import MultiPolygon
from shapely.ops import transform as _shapely_transform
from shapely.ops import unary_union

try:
    from shapely import make_valid as _make_valid
except ImportError:  # shapely < 2.0
    from shapely.validation import make_valid as _make_valid

_SHAPELY2 = tuple(int(p) for p in shapely.__version__.split(".")[:2]) >= (2, 0)

# ----------------------------- CONFIG -----------------------------------
PASTA_BASE = Path(r"C:\Users\User\Dropbox\#CONSULTANCY\PLANAVEG\GEODATABASE\INCRA-CAR")
SAIDA = PASTA_BASE / "CAR_total_dissolvido_UF.gpkg"
CAMADA_SAIDA = "CAR_total_UF"

# Pasta de cada UF dentro de PASTA_BASE e arquivos/camada de entrada.
# Estrutura usada em ago/2026 (pacote conformidade / processar_lote.py):
PASTA_UF = "_saida_{uf}"
ARQ_ANALISADO = "{uf}_analisados.gpkg"
ARQ_NAO_ANALISADO = "{uf}_trabalho.gpkg"
CAMADA_ENTRADA = "AREA_IMOVEL"      # None = primeira camada do arquivo
# Estrutura atual (scripts 2 a 6 deste repositório) - NÃO VALIDADO, ver cabeçalho:
# from conformidade.caminhos import PASTA_ANALISE
# PASTA_BASE = Path(PASTA_ANALISE)
# SAIDA = PASTA_BASE.parent / "CAR_total_dissolvido_UF.gpkg"
# PASTA_UF = r"dados_saída_{uf}\{uf}_geopackage"
# ARQ_ANALISADO = "{uf}_CAR_Imoveis_Selecionados_Analisados.gpkg"
# ARQ_NAO_ANALISADO = "{uf}_CAR_Imoveis_Selecionados_Nao_Analisados.gpkg"
# CAMADA_ENTRADA = None

SOMENTE_ESTES = []   # vazio = todos os estados encontrados. A retomada e automatica: so
                     # processa UFs que ainda nao tem as categorias esperadas na saida (um
                     # travamento no meio de uma UF perde o trabalho parcial DELA).
PULAR = []
REFAZER = []         # UFs a reprocessar mesmo ja estando na saida

# quantas UFs processar AO MESMO TEMPO (threads). As operacoes pesadas do
# shapely/GEOS liberam o GIL do Python, entao isso realmente roda em
# paralelo nos nucleos disponiveis (nao e so IO) -- testado: 2 dissolves
# simultaneos em 2 nucleos levaram ~metade do tempo de rodar em sequencia.
# Ajuste conforme os nucleos/RAM da sua maquina; UFs grandes (MG/MT/PA/SP)
# consomem bastante memoria cada uma, entao nao exagere.
MAX_WORKERS = 4

# a cada quantos segundos o script escreve um "sinal de vida" (heartbeat) --
# um arquivo de texto dizendo o que cada UF esta fazendo agora e ha quanto
# tempo. Existe porque o dissolve/recorte de uma UF grande e UMA UNICA
# chamada do GEOS que pode ficar HORAS sem imprimir nada -- sem isso, nao
# da pra distinguir "ainda processando, so que é lento" de "travou de
# verdade" sem esperar a UF inteira terminar (foi exatamente o que
# aconteceu com o MG). O heartbeat roda numa thread separada e nao atrapalha
# o processamento (GEOS libera o GIL).
HEARTBEAT_INTERVALO_S = 300  # 5 minutos
ARQ_HEARTBEAT = "heartbeat_dissolver.txt"
# --------------------------------------------------------------------------

GEOD = Geod(ellps="GRS80")
UFS_BRASIL = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA",
              "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"]

# --------------------------- HEARTBEAT -----------------------------------
_estagio_lock = threading.Lock()
_estagio_atual = {}  # uf -> (texto da etapa em andamento, timestamp de quando comecou)


def _marcar_estagio(uf, texto):
    with _estagio_lock:
        _estagio_atual[uf] = (texto, time.time())


def _limpar_estagio(uf):
    with _estagio_lock:
        _estagio_atual.pop(uf, None)


def _heartbeat_loop(parar_evento, intervalo_s, caminho):
    """Roda em thread daemon separada; a cada intervalo_s escreve (e
    imprime) um resumo do que cada UF esta fazendo e ha quanto tempo --
    assim da pra checar de fora (abrindo o .txt, sem mexer no processo)
    que o script continua vivo mesmo numa etapa silenciosa de horas."""
    while not parar_evento.wait(intervalo_s):
        agora = time.time()
        with _estagio_lock:
            snapshot = dict(_estagio_atual)
        linhas = [f"heartbeat {time.strftime('%Y-%m-%d %H:%M:%S')} -- processo ainda ativo"]
        if not snapshot:
            linhas.append("  (nenhuma UF em etapa longa no momento -- entre etapas rapidas, "
                           "ou o lote esta terminando)")
        else:
            for uf, (texto, t0) in sorted(snapshot.items()):
                dt = agora - t0
                linhas.append(f"  [{uf}] {texto} -- em andamento ha {dt:.0f}s ({dt/60:.1f} min)")
        conteudo = "\n".join(linhas) + "\n"
        print("\n" + conteudo)
        try:
            caminho.write_text(conteudo, encoding="utf-8")
        except Exception as e:
            print(f"  (heartbeat: nao consegui escrever {caminho.name}: {e})")

# fallback pro calculo de area quando o pyproj/GEOD teima em devolver NaN
# mesmo depois de todo reparo (visto no MA -- geometria sem coordenada
# NaN/Inf detectavel, mas o algoritmo geodesico do pyproj ainda assim
# produz NaN, provavelmente por alguma degenerescencia numerica sutil
# numa geometria com centenas de milhares de vertices). Reprojetar pra uma
# CRS equal-area e calcular a area planar la e um caminho de calculo
# COMPLETAMENTE DIFERENTE (GEOS puro, sem a formula geodesica do pyproj) --
# validado a mao contra o metodo geodesico no AC: diferenca de 0,003%.
_TRANSFORMER_EQAREA = Transformer.from_crs("EPSG:4674", "EPSG:6933", always_xy=True)


def _area_ha_equal_area(geom):
    if geom is None or geom.is_empty:
        return None
    try:
        geom_proj = _shapely_transform(_TRANSFORMER_EQAREA.transform, geom)
        area_m2 = geom_proj.area
    except Exception:
        return None
    if area_m2 != area_m2 or area_m2 <= 0:
        return None
    return area_m2 / 10_000.0


def _so_poligonos(geom):
    """Extrai so a parte poligonal de uma geometria (descarta linha/ponto
    remanescentes de reparo de geometria invalida, dissolve ou recorte)."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        partes = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        if not partes:
            return None
        return unary_union(partes)
    return None


def _reparar(geom):
    """Repara geometria invalida de forma resiliente: tenta make_valid, cai
    para buffer(0) se make_valid falhar/lancar excecao, descarta se nada
    funcionar. Mesma logica usada em cruzar_vegsec.py para o caso do AM
    (make_valid as vezes lanca IllegalArgumentException em geometrias muito
    patologicas)."""
    if geom is None or geom.is_empty:
        return None
    if geom.is_valid:
        return _so_poligonos(geom)

    reparada = None
    try:
        reparada = _make_valid(geom)
    except Exception:
        reparada = None

    if reparada is None or reparada.is_empty:
        try:
            reparada = geom.buffer(0)
        except Exception:
            reparada = None

    if reparada is None or reparada.is_empty:
        return None
    return _so_poligonos(reparada)


def _tem_coordenada_invalida(geom):
    """Detecta NaN/Inf em qualquer vertice -- overlays (difference/union)
    envolvendo geometrias com centenas de milhares de vertices raramente
    podem produzir um vertice degenerado (NaN) sem lancar excecao nenhuma;
    o pyproj tambem NAO lanca excecao nesse caso, so retorna area=NaN
    silenciosamente. Foi assim que a area de MA/nao_analisado saiu 'nan'."""
    if geom is None or geom.is_empty:
        return False
    try:
        coords = shapely.get_coordinates(geom)
        return bool(coords.size) and not np.isfinite(coords).all()
    except Exception:
        return False


# bbox bem generoso do Brasil (graus, EPSG:4674) -- so pra pegar vertice
# GRAVEMENTE degenerado: finito (passa no isfinite acima), mas geografico
# absurdo. Visto no MA numa segunda rodada: a diferenca (recorte) produziu
# um resultado sem NaN/Inf detectavel (_tem_coordenada_invalida = False),
# porem mesmo assim TANTO o calculo geodesico (pyproj) QUANTO o fallback
# equal-area (reprojecao + shapely puro) devolveram NaN -- sinal de que
# sobrou algum vertice finito porem fora de qualquer lugar real (artefato
# raro de arredondamento/grid_size numa geometria com centenas de milhares
# de vertices). Esse bbox nunca deve descartar dado bom: e ~3x maior que o
# territorio brasileiro em cada direcao.
_BRASIL_BBOX = (-76.0, -36.0, -28.0, 8.0)  # lon_min, lat_min, lon_max, lat_max


def _tem_coordenada_absurda(geom):
    """Vertice finito (passa isfinite) mas fora do _BRASIL_BBOX -- pega o
    caso que _tem_coordenada_invalida (NaN/Inf) nao pega, mas que ainda
    assim quebra o calculo de area geodesico E o equal-area."""
    if geom is None or geom.is_empty:
        return False
    try:
        coords = shapely.get_coordinates(geom)
        if not coords.size:
            return False
        lon_min, lat_min, lon_max, lat_max = _BRASIL_BBOX
        fora = (
            (coords[:, 0] < lon_min) | (coords[:, 0] > lon_max) |
            (coords[:, 1] < lat_min) | (coords[:, 1] > lat_max)
        )
        return bool(fora.any())
    except Exception:
        return False


def _geometria_suspeita(geom):
    """NaN/Inf OU coordenada absurda -- usado onde antes so se checava
    _tem_coordenada_invalida, pra tambem pegar o caso do MA."""
    return _tem_coordenada_invalida(geom) or _tem_coordenada_absurda(geom)


def _sanitizar_para_area(geom, contexto):
    """Remove/repara coordenadas invalidas antes do calculo de area. Tenta
    buffer(0) na geometria inteira primeiro (corrige a maioria dos casos);
    se ainda sobrar problema, descarta so as partes (poligonos) irreparaveis
    de um MultiPolygon em vez de perder a area inteira."""
    if geom is None or geom.is_empty:
        return geom

    try:
        reparada = geom.buffer(0)
    except Exception:
        reparada = None
    if reparada is not None and not reparada.is_empty and not _geometria_suspeita(reparada):
        return reparada

    partes = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    boas = []
    descartadas = 0
    for parte in partes:
        try:
            p = parte.buffer(0)
        except Exception:
            p = None
        if p is None or p.is_empty or _geometria_suspeita(p):
            descartadas += 1
            continue
        boas.append(p)
    if descartadas:
        print(f"  [{contexto}] aviso: {descartadas} parte(s) com coordenada invalida "
              f"descartada(s) do calculo de area (geometria irreparavel)")
    if not boas:
        return None
    return unary_union(boas)


def _uniao_robusta(geoms, contexto):
    """unary_union normal primeiro (rapido); se o GEOS explodir com
    TopologyException/GEOSException (visto na pratica no PR: 'Ring edge
    missing' -- geometria muito complexa/densa pro algoritmo de precisao
    flutuante padrao), tenta de novo com grid_size (arredondamento pra uma
    grade fixa -- mais lento, porem o jeito recomendado do GEOS pra evitar
    exatamente esse tipo de excecao)."""
    try:
        return unary_union(geoms)
    except Exception as e:
        print(f"  [{contexto}] uniao falhou ({e}); tentando de novo com grid_size fixo "
              f"(mais lento, porem mais robusto p/ geometria complexa)...")
        arr = np.asarray(geoms, dtype=object)
        for grid in (1e-9, 1e-7, 1e-5):
            try:
                r = shapely.union_all(arr, grid_size=grid)
                print(f"  [{contexto}] uniao com grid_size={grid} funcionou")
                return r
            except Exception as e2:
                print(f"  [{contexto}] grid_size={grid} tambem falhou: {e2}")
        print(f"  [{contexto}] TODAS as tentativas de uniao falharam -- propagando o erro")
        raise


def _diferenca_robusta(a, b, contexto):
    """Mesma ideia de _uniao_robusta, mas pro recorte (difference). Alem de
    tentar grid_size quando a chamada normal lanca excecao, tambem checa se
    o resultado (mesmo sem excecao) ficou com coordenada invalida/NaN --
    caso do MA: a diferenca 'funcionou' sem erro nenhum, mas produziu um
    vertice NaN que o pyproj engoliu silenciosamente."""
    try:
        d = a.difference(b)
    except Exception as e:
        d = None
        print(f"  [{contexto}] diferenca falhou ({e}); tentando de novo com grid_size...")
        for grid in (1e-9, 1e-7, 1e-5):
            try:
                d = a.difference(b, grid_size=grid)
                print(f"  [{contexto}] diferenca com grid_size={grid} funcionou")
                break
            except Exception as e2:
                print(f"  [{contexto}] grid_size={grid} tambem falhou: {e2}")
        if d is None:
            print(f"  [{contexto}] TODAS as tentativas de diferenca falharam -- usando a "
                  f"geometria SEM recorte como ultimo recurso (pode manter alguma "
                  f"sobreposicao residual entre analisado/nao_analisado nessa UF)")
            return a

    if _geometria_suspeita(d):
        print(f"  [{contexto}] diferenca produziu coordenada invalida/absurda (NaN/Inf ou "
              f"fora do bbox esperado); tentando de novo com grid_size...")
        for grid in (1e-9, 1e-7, 1e-5):
            try:
                d2 = a.difference(b, grid_size=grid)
                if not _geometria_suspeita(d2):
                    print(f"  [{contexto}] grid_size={grid} eliminou a coordenada invalida/absurda")
                    return d2
            except Exception as e2:
                print(f"  [{contexto}] grid_size={grid} tambem falhou: {e2}")
        print(f"  [{contexto}] nao foi possivel eliminar via grid_size; "
              f"vai passar pelo reparo de partes mais adiante")
    return d


def area_ha_geodesica(geom, contexto="?"):
    if geom is None or geom.is_empty:
        return 0.0
    area, _ = GEOD.geometry_area_perimeter(geom)
    if area != area:  # NaN (nan != nan e sempre True)
        print(f"  [{contexto}] aviso: area saiu NaN, geometria tem coordenada invalida -- reparando...")
        geom_ok = _sanitizar_para_area(geom, contexto)
        if geom_ok is None or geom_ok.is_empty:
            print(f"  [{contexto}] geometria ficou vazia apos reparo, area = 0.0")
            return 0.0
        area, _ = GEOD.geometry_area_perimeter(geom_ok)
        if area != area:
            print(f"  [{contexto}] aviso: area geodesica (pyproj) continua NaN mesmo apos reparo; "
                  f"tentando via reprojecao equal-area (EPSG:6933) como ultimo recurso...")
            area_alt = _area_ha_equal_area(geom_ok)
            if area_alt is None:
                area_alt = _area_ha_equal_area(geom)  # tenta tambem com a geometria original
            if area_alt is not None:
                print(f"  [{contexto}] area via equal-area funcionou: {area_alt:,.1f} ha "
                      f"(pode ter diferenca de ate ~0,01% vs o metodo geodesico das outras UFs)")
                return area_alt
            print(f"  [{contexto}] aviso: TAMBEM falhou via equal-area, retornando 0.0 "
                  f"-- ESSA UF PRECISA DE INSPECAO MANUAL")
            return 0.0
    return abs(area) / 10_000.0


def _multipolygon(geom):
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    return geom


def _reparar_lote_vetorizado(uf, categoria, arr):
    """Repara geometrias invalidas de um array inteiro de uma vez (funcoes
    vetorizadas do shapely 2, muito mais rapido que chamar make_valid geom a
    geom em Python puro -- essencial para estados grandes tipo BA/MG/MT
    com centenas de milhares a milhoes de feicoes). Cai para o reparo
    geom-a-geom (mais lento) so no subconjunto invalido, e so se o lote
    vetorizado falhar (mesmo caso do AM: make_valid pode lancar excecao em
    geometria muito patologica)."""
    arr = np.asarray(arr, dtype=object)
    validas = shapely.is_valid(arr)
    n_invalidas = int((~validas).sum())
    if n_invalidas == 0:
        return arr

    print(f"  [{uf}/{categoria}] reparando {n_invalidas} geometrias invalidas de {len(arr)}...")
    try:
        arr[~validas] = shapely.make_valid(arr[~validas])
    except Exception:
        print(f"  [{uf}/{categoria}] make_valid em lote falhou, reparando uma a uma "
              f"(mais lento, so as {n_invalidas} invalidas)...")
        idx_invalidas = np.where(~validas)[0]
        for i in idx_invalidas:
            arr[i] = _reparar(arr[i])
    return arr


def dissolver_arquivo(uf, categoria, caminho):
    """Le um gpkg (camada CAMADA_ENTRADA), repara e dissolve TODOS os imoveis
    dele entre si (uma unica categoria -> uma unica geometria)."""
    if not caminho.exists():
        print(f"  [{uf}/{categoria}] aviso: {caminho.name} nao encontrado, pulando")
        return None, 0

    t0 = time.time()
    gdf = gpd.read_file(caminho, layer=CAMADA_ENTRADA) if CAMADA_ENTRADA else gpd.read_file(caminho)
    n_base = len(gdf)
    print(f"  [{uf}/{categoria}] lido {n_base} imoveis em {time.time()-t0:.1f}s")
    if n_base == 0:
        print(f"  [{uf}/{categoria}] sem feicoes, pulando")
        return None, 0

    if _SHAPELY2:
        t1 = time.time()
        arr = _reparar_lote_vetorizado(uf, categoria, gdf.geometry.values)

        # filtro vetorizado: mantem so Polygon/MultiPolygon nao-vazios;
        # GeometryCollection (raro, sobra de reparo) tratado a parte
        type_ids = shapely.get_type_id(arr)
        vazias = shapely.is_empty(arr)
        mask_ok = np.isin(type_ids, [3, 6]) & ~vazias  # 3=Polygon, 6=MultiPolygon
        geoms_ok = list(arr[mask_ok])

        mask_gc = (type_ids == 7) & ~vazias  # 7=GeometryCollection
        for g in arr[mask_gc]:
            gp = _so_poligonos(g)
            if gp is not None and not gp.is_empty:
                geoms_ok.append(gp)

        descartadas = n_base - len(geoms_ok)
        print(f"  [{uf}/{categoria}] reparo+filtro vetorizado em {time.time()-t1:.1f}s "
              f"({descartadas} descartadas)")
    else:
        geoms_ok = []
        descartadas = 0
        for geom in gdf.geometry:
            g = _reparar(geom)
            if g is None or g.is_empty:
                descartadas += 1
                continue
            geoms_ok.append(g)
        if descartadas:
            print(f"  [{uf}/{categoria}] {descartadas} geometrias descartadas na leitura")

    if not geoms_ok:
        print(f"  [{uf}/{categoria}] todas as geometrias descartadas, pulando")
        return None, n_base

    t2 = time.time()
    _marcar_estagio(uf, f"{categoria}: dissolve (uniao geometrica) de {len(geoms_ok)} partes")
    dissolvido = _uniao_robusta(geoms_ok, f"{uf}/{categoria}")
    dissolvido = _so_poligonos(dissolvido)
    dissolvido = _multipolygon(dissolvido)
    print(f"  [{uf}/{categoria}] dissolve (uniao geometrica) em {time.time()-t2:.1f}s")
    return dissolvido, n_base


def processar_uf(uf, pasta):
    t0 = time.time()
    resultados = []

    geom_analisado, n_analisado = dissolver_arquivo(
        uf, "analisado", pasta / ARQ_ANALISADO.format(uf=uf)
    )
    geom_nao_analisado, n_nao_analisado = dissolver_arquivo(
        uf, "nao_analisado", pasta / ARQ_NAO_ANALISADO.format(uf=uf)
    )

    if geom_analisado is not None:
        area_ha = area_ha_geodesica(geom_analisado, f"{uf}/analisado")
        resultados.append({
            "uf": uf, "categoria": "analisado",
            "n_imoveis_origem": n_analisado, "area_ha": area_ha,
            "geometry": geom_analisado,
        })
        print(f"  [{uf}/analisado] {n_analisado} imoveis base -> {area_ha:,.1f} ha")

    if geom_nao_analisado is not None:
        if geom_analisado is not None:
            # PRIORIDADE: analisado fica intacto; nao_analisado e recortado
            # para eliminar sobreposicao remanescente entre as categorias.
            t_recorte = time.time()
            _marcar_estagio(uf, "nao_analisado: recorte (diferenca) contra analisado")
            recortado = _diferenca_robusta(geom_nao_analisado, geom_analisado, f"{uf}/nao_analisado")
            recortado = _so_poligonos(recortado)
            recortado = _multipolygon(recortado)
            # garante que a geometria GRAVADA no gpkg tambem fica sem
            # coordenada invalida (antes so a area era saneada, a geometria
            # bruta ainda ia pro arquivo -- foi o que aconteceu com o MA)
            if recortado is not None and _geometria_suspeita(recortado):
                recortado = _sanitizar_para_area(recortado, f"{uf}/nao_analisado (geometria)")
                recortado = _multipolygon(recortado)
            print(f"  [{uf}/nao_analisado] recorte (diferenca) contra analisado em "
                  f"{time.time()-t_recorte:.1f}s")
        else:
            recortado = geom_nao_analisado

        area_ha = area_ha_geodesica(recortado, f"{uf}/nao_analisado") if recortado is not None else 0.0
        if recortado is None:
            # todo o nao_analisado estava contido no analisado
            recortado = MultiPolygon([])
        resultados.append({
            "uf": uf, "categoria": "nao_analisado",
            "n_imoveis_origem": n_nao_analisado, "area_ha": area_ha,
            "geometry": recortado,
        })
        print(f"  [{uf}/nao_analisado] {n_nao_analisado} imoveis base -> {area_ha:,.1f} ha "
              f"(pos-recorte contra analisado)")

    _limpar_estagio(uf)
    dt = time.time() - t0
    print(f"  [{uf}] concluido em {dt:.1f}s")
    return resultados


def ler_saida_existente():
    if not SAIDA.exists():
        return None
    try:
        return gpd.read_file(SAIDA, layer=CAMADA_SAIDA)
    except Exception:
        return None


def main():
    ufs_disponiveis = sorted(
        uf for uf in UFS_BRASIL if (PASTA_BASE / PASTA_UF.format(uf=uf)).is_dir()
    )
    ufs = SOMENTE_ESTES or ufs_disponiveis
    ufs = [u for u in ufs if u not in PULAR]

    existente = ler_saida_existente()
    if existente is not None:
        categorias_por_uf = existente.groupby("uf")["categoria"].apply(set).to_dict()
    else:
        categorias_por_uf = {}

    def categorias_esperadas(uf):
        # nem toda UF tem as duas categorias na origem (ex.: BA tem 0
        # imoveis "Analisado" -> nunca existe BA_analisados.gpkg; isso e
        # dado real, nao falha). So exige na saida as categorias cujo
        # arquivo de origem realmente existe.
        pasta = PASTA_BASE / PASTA_UF.format(uf=uf)
        esp = set()
        if (pasta / ARQ_ANALISADO.format(uf=uf)).exists():
            esp.add("analisado")
        if (pasta / ARQ_NAO_ANALISADO.format(uf=uf)).exists():
            esp.add("nao_analisado")
        return esp

    ufs_completas = set()
    for uf in ufs:
        if uf in REFAZER:
            continue
        cats_presentes = categorias_por_uf.get(uf, set())
        if categorias_esperadas(uf).issubset(cats_presentes):
            ufs_completas.add(uf)
    pendentes = [uf for uf in ufs if uf not in ufs_completas]

    n_workers = max(1, min(MAX_WORKERS, len(pendentes))) if pendentes else 1
    print(f"UFs disponiveis: {len(ufs_disponiveis)} | a processar: {len(pendentes)} "
          f"| ja completas (puladas): {len(set(ufs) & ufs_completas)} | "
          f"paralelismo: {n_workers} UF(s) por vez")

    falhas = []
    atual = existente  # GeoDataFrame acumulado, gravado no disco a cada UF concluida
    lock_gravacao = threading.Lock()  # so uma thread escreve no gpkg por vez

    def gravar(atual_gdf):
        atual_gdf = atual_gdf.sort_values(["uf", "categoria"]).reset_index(drop=True)
        atual_gdf.to_file(SAIDA, layer=CAMADA_SAIDA, driver="GPKG")
        return atual_gdf

    def processar_e_gravar(uf):
        # roda em uma thread do pool: processa a UF inteira (leitura, reparo,
        # dissolve, recorte) e so entao pede o lock pra gravar -- assim o
        # trabalho pesado (que libera o GIL) roda de fato em paralelo, e so
        # a gravacao no gpkg (rapida) fica serializada.
        nonlocal atual
        pasta = PASTA_BASE / PASTA_UF.format(uf=uf)
        if not pasta.exists():
            print(f"  [{uf}] pasta nao encontrada: {pasta}")
            return uf, False
        r = processar_uf(uf, pasta)
        if not r:
            print(f"  [{uf}] nada a gravar (sem resultados)")
            return uf, True
        novo_gdf = gpd.GeoDataFrame(r, geometry="geometry", crs="EPSG:4674")
        with lock_gravacao:
            if atual is not None:
                atual = atual[atual["uf"] != uf]
                atual = pd.concat([atual, novo_gdf], ignore_index=True)
                atual = gpd.GeoDataFrame(atual, geometry="geometry", crs="EPSG:4674")
            else:
                atual = novo_gdf
            atual = gravar(atual)
            print(f"  [{uf}] gravado em disco ({len(atual)} feicoes no total ate agora)")
        return uf, True

    parar_heartbeat = threading.Event()
    heartbeat_thread = None
    if pendentes:
        caminho_heartbeat = PASTA_BASE / ARQ_HEARTBEAT
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(parar_heartbeat, HEARTBEAT_INTERVALO_S, caminho_heartbeat),
            daemon=True,
        )
        heartbeat_thread.start()
        print(f"Heartbeat ativado: a cada {HEARTBEAT_INTERVALO_S//60} min, escreve "
              f"{caminho_heartbeat} com o que cada UF esta fazendo -- da pra checar de "
              f"fora (ou pedir pra eu checar) sem esperar a UF inteira terminar.")

    try:
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futuros = {executor.submit(processar_e_gravar, uf): uf for uf in pendentes}
            for futuro in as_completed(futuros):
                uf = futuros[futuro]
                try:
                    _, ok = futuro.result()
                    if not ok:
                        falhas.append(uf)
                except Exception as e:
                    print(f"  [{uf}] ERRO: {e}")
                    traceback.print_exc()
                    falhas.append(uf)
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuario (Ctrl+C). O que ja tinha sido processado "
              "ate a ultima UF concluida ja esta salvo em disco. Rode o script de novo "
              "para continuar de onde parou. (UFs ainda em andamento nas outras threads "
              "podem levar alguns segundos pra realmente parar.)")
        raise
    finally:
        parar_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)

    if atual is None:
        print("\nNada foi gravado (nenhuma UF pendente teve resultado).")
    else:
        print(f"\nSaida final: {SAIDA} (camada {CAMADA_SAIDA}), {len(atual)} feicoes")
        print(atual[["uf", "categoria", "n_imoveis_origem", "area_ha"]].to_string(index=False))

    if falhas:
        print(f"\nUFs com falha: {falhas}")


if __name__ == "__main__":
    main()
