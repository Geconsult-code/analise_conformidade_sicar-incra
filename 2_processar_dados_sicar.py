r"""
2_processar_dados_sicar.py — processa TODOS os estados do Brasil em sequência
(etapa "preparar": separa cada imóvel do SICAR bruto por fase de análise).

(Adaptado de processar_lote.py — 2o passo do workflow numerado do
repositório. MUDANÇA DE ESCOPO em relação ao processar_lote.py original:
este script agora faz SÓ a preparação (descompacta AREA_IMOVEL, junta os
pedaços fatiados, roda ``conformidade preparar``). A classificação de
coerência SICAR × INCRA (antigo ``conformidade analisar``) saiu daqui —
agora roda de forma genérica para os buckets Analisados/Não Analisados no
script 4_analise_conformidade.py, depois da categorização do script
3_categorizar_dados_sicar.py. Por isso este script também NÃO descompacta
mais os planos temáticos (APPS/RESERVA_LEGAL/USO_RESTRITO) — isso passou
para o script 5_extracao_APP_RL_AUR.py, que já os busca direto do SICAR
bruto quando precisa.

Também muda o DESTINO da saída: antes ia para uma subpasta ``_saida_<UF>``
dentro da própria pasta do estado no SICAR bruto; agora vai para
Analise_Conformidade\dados_saída_<UF>\<UF>_geopackage (mesma estrutura que
o script 6_validacao_resultados.py já espera), com os arquivos temporários
descompactados em Analise_Conformidade\dados_saída_<UF>\<UF>_shapefile.

Ferramenta de atualização: sempre que novos dados do SICAR forem
disponibilizados, basta atualizar PASTA_SICAR e rodar este script para
reprocessar o país inteiro (ou os estados que você escolher).

Para cada estado dentro de ``PASTA_SICAR``, o script:
  1. descompacta o .zip do plano AREA_IMOVEL, pulando se já estiver
     descompactado;
  2. junta os pedaços fatiados (AREA_IMOVEL_1..N);
  3. roda ``conformidade preparar``, gravando <UF>_analisados.gpkg (fase
     Analisado, todas as derivações) e <UF>_trabalho.gpkg (Em Análise +
     Aguardando) na pasta <UF>_geopackage;
  4. opcionalmente apaga o .shp descompactado para poupar espaço.

Se um estado falhar, o script anota o erro e SEGUE para o próximo; ao final
lista os que deram certo e os que falharam.

COMO USAR
---------
1. Coloque o SICAR bruto de cada estado em PASTA_SICAR\<ESTADO>\ (com os
   .zip dos planos).
2. Confira os caminhos na seção CONFIG.
3. Com o ambiente 'geo' ativo:
       python 2_processar_dados_sicar.py

Por padrão processa TODOS os estados encontrados. Para refazer só alguns,
preencha SOMENTE_ESTES (ex.: ["SP", "TO"]). Para pular alguns, use PULAR.
Reprocessar sobrescreve os resultados anteriores em cada <UF>_geopackage.

NOTA: zips muito grandes do SICAR podem, ocasionalmente, não abrir pelo Python
(formato ZIP64 ou download corrompido). Nesse caso, extraia o .zip do plano à
mão na respectiva subpasta — o script detecta os .shp já descompactados e segue.
"""

from __future__ import annotations

import os
import sys
import glob
import zipfile
import subprocess
import unicodedata
from datetime import datetime

import geopandas as gpd

# =============================== CONFIG ===============================
# Pastas de dados: ver conformidade/caminhos.py (backup restaurado via PLANAVEG_RAIZ ou Dropbox).
from conformidade.caminhos import PASTA_ANALISE, PASTA_SICAR  # noqa: E402

# Estados a PULAR nesta execução (opcional). Deixe vazio para processar TODOS.
PULAR: list[str] = []
# Para rodar SÓ alguns estados, liste as siglas aqui (senão deixe vazio = todos).
SOMENTE_ESTES: list[str] = []

# Apagar o .shp descompactado ao terminar cada estado (poupa espaço)?
APAGAR_SHP_AO_FIM = True

# Plano usado nesta etapa. Os planos temáticos entram só no script 5.
PLANO_AREA = "AREA_IMOVEL"
# =====================================================================


# Mapa nome-do-estado (por extenso, normalizado) -> sigla UF.
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


def _norm(txt: str) -> str:
    """Maiúsculas, sem acento, espaços colapsados — para casar nomes de pasta."""
    t = unicodedata.normalize("NFKD", txt)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.upper().split())


def uf_da_pasta(nome_pasta: str) -> str | None:
    """Descobre a sigla UF a partir do nome da pasta do estado."""
    return NOME_PARA_UF.get(_norm(nome_pasta))


def descompactar_plano(pasta_estado: str, plano: str) -> list[str]:
    """Descompacta o(s) .zip do plano (se preciso) e retorna os .shp achados.

    Procura tanto uma subpasta com o nome do plano quanto um <plano>.zip solto.
    Se os .shp já existirem, não descompacta de novo.
    """
    pasta_plano = os.path.join(pasta_estado, plano)
    # 1) já existe subpasta com .shp dentro?
    if os.path.isdir(pasta_plano):
        shps = sorted(glob.glob(os.path.join(pasta_plano, "*.shp")))
        if shps:
            return shps
        # subpasta existe mas sem shp: procurar zip dentro dela
        for z in glob.glob(os.path.join(pasta_plano, "*.zip")):
            with zipfile.ZipFile(z) as zf:
                zf.extractall(pasta_plano)
        shps = sorted(glob.glob(os.path.join(pasta_plano, "*.shp")))
        if shps:
            return shps
    # 2) zip solto na pasta do estado (ex.: AREA_IMOVEL.zip)
    z = os.path.join(pasta_estado, plano + ".zip")
    if os.path.exists(z):
        os.makedirs(pasta_plano, exist_ok=True)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(pasta_plano)
        return sorted(glob.glob(os.path.join(pasta_plano, "*.shp")))
    return []


def juntar_pedacos(shps: list[str], destino: str) -> str:
    """Se houver vários .shp (plano fatiado), concatena num único destino.

    Retorna o caminho do arquivo único (o próprio, se já era um só).
    """
    if len(shps) == 1:
        return shps[0]
    partes = [gpd.read_file(s) for s in shps]
    junto = gpd.GeoDataFrame(
        gpd.pd.concat(partes, ignore_index=True), crs=partes[0].crs)
    junto.to_file(destino)
    return destino


def processar_estado(nome_pasta: str) -> None:
    """Roda a preparação para um estado. Lança exceção em caso de erro."""
    pasta_estado = os.path.join(PASTA_SICAR, nome_pasta)
    uf = uf_da_pasta(nome_pasta)
    if not uf:
        raise ValueError(f"não reconheci a UF da pasta '{nome_pasta}'")

    print(f"\n{'='*60}\n  {nome_pasta}  (UF={uf})\n{'='*60}", file=sys.stderr)
    pasta_saida_uf = os.path.join(PASTA_ANALISE, f"dados_saída_{uf}")
    pasta_shp = os.path.join(pasta_saida_uf, f"{uf}_shapefile")
    pasta_gpkg = os.path.join(pasta_saida_uf, f"{uf}_geopackage")
    os.makedirs(pasta_shp, exist_ok=True)
    os.makedirs(pasta_gpkg, exist_ok=True)

    criados_tmp: list[str] = []  # arquivos que podemos apagar no fim

    # ---- AREA_IMOVEL (obrigatório) ----
    print("  [descompactar] AREA_IMOVEL...", file=sys.stderr)
    shps_area = descompactar_plano(pasta_estado, PLANO_AREA)
    if not shps_area:
        raise FileNotFoundError(f"AREA_IMOVEL não encontrado em {pasta_estado}")
    area_unico = juntar_pedacos(
        shps_area, os.path.join(pasta_shp, f"_area_imovel_{uf}.shp"))
    if area_unico != shps_area[0]:
        criados_tmp.append(area_unico)

    # ---- PREPARAR ----
    print("  [preparar] separando por fase...", file=sys.stderr)
    cmd_prep = [sys.executable, "-m", "conformidade.cli", "preparar",
                "--area-imovel", area_unico, "--uf", uf, "--saida", pasta_gpkg]
    _run(cmd_prep, f"preparar {uf}")

    # ---- limpeza dos .shp descompactados ----
    if APAGAR_SHP_AO_FIM:
        print("  [limpeza] apagando .shp descompactados...", file=sys.stderr)
        _apagar_shapefiles(os.path.join(pasta_estado, PLANO_AREA))
        for tmp in criados_tmp:
            _apagar_shapefile_unico(tmp)

    print(f"  OK: resultado em {pasta_gpkg}", file=sys.stderr)


def _run(cmd: list[str], rotulo: str) -> None:
    """Executa um subcomando e mostra o resumo; lança erro se falhar."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    saida = (r.stderr or "") + (r.stdout or "")
    for linha in saida.strip().splitlines()[-12:]:
        print("    " + linha, file=sys.stderr)
    if r.returncode != 0:
        raise RuntimeError(f"{rotulo} falhou (código {r.returncode})")


def _apagar_shapefiles(pasta: str) -> None:
    """Apaga todos os shapefiles (e arquivos irmãos) de uma pasta."""
    if not os.path.isdir(pasta):
        return
    for shp in glob.glob(os.path.join(pasta, "*.shp")):
        _apagar_shapefile_unico(shp)


def _apagar_shapefile_unico(shp: str) -> None:
    base = os.path.splitext(shp)[0]
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qmd", ".fix"):
        f = base + ext
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


def main() -> int:
    if not os.path.isdir(PASTA_SICAR):
        print(f"Pasta do SICAR não encontrada: {PASTA_SICAR}", file=sys.stderr)
        return 1

    # Descobre os estados (subpastas da pasta do SICAR).
    pastas = [d for d in sorted(os.listdir(PASTA_SICAR))
              if os.path.isdir(os.path.join(PASTA_SICAR, d))]

    alvos = []
    for nome in pastas:
        uf = uf_da_pasta(nome)
        if not uf:
            continue  # pasta que não é estado
        if uf in PULAR:
            continue
        if SOMENTE_ESTES and uf not in SOMENTE_ESTES:
            continue
        alvos.append(nome)

    print(f"Estados a processar ({len(alvos)}): "
          f"{', '.join(uf_da_pasta(n) for n in alvos)}", file=sys.stderr)
    print(f"Início: {datetime.now():%H:%M:%S}\n", file=sys.stderr)

    sucesso, falhas = [], []
    for nome in alvos:
        uf = uf_da_pasta(nome)
        try:
            processar_estado(nome)
            sucesso.append(uf)
        except Exception as e:
            print(f"  !! ERRO em {uf}: {e}", file=sys.stderr)
            falhas.append((uf, str(e)))

    # ---- Relatório final ----
    print(f"\n{'#'*60}\n  RELATÓRIO FINAL  ({datetime.now():%H:%M:%S})\n{'#'*60}",
          file=sys.stderr)
    print(f"Sucesso ({len(sucesso)}): {', '.join(sucesso) or '—'}",
          file=sys.stderr)
    if falhas:
        print(f"\nFalharam ({len(falhas)}):", file=sys.stderr)
        for uf, msg in falhas:
            print(f"  {uf}: {msg}", file=sys.stderr)
    else:
        print("\nNenhuma falha.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
