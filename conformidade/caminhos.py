r"""
caminhos.py - pastas de dados usadas pelos scripts numerados (1 a 6) e pelo
dissolver_car_total.py.

Duas organizações são reconhecidas, nesta ordem:

1. Backup restaurado (repositório backup_planaveg_2026): tudo dentro de
   <PLANAVEG_RAIZ>\Analise_Conformidade_CAR-INCRA\
       Resultados_Analise_Conformidade\      (dados_saída_<UF>)
       SICAR_BASE_CAR_ESTADOS_05_2026\       (<ESTADO>\<plano>.zip)
       INCRA\                                (SIGEF_Privado_<UF>, SNCI_Privado_<UF>, SIGEF\, SNCI\)
   Usada quando a variável de ambiente PLANAVEG_RAIZ aponta para uma pasta que tem
   Analise_Conformidade_CAR-INCRA.

2. Máquina original (padrão): pastas no Dropbox, listadas em ORIGINAL abaixo.

Cada pasta também pode ser fixada por uma variável de ambiente própria
(CONFORMIDADE_ANALISE, CONFORMIDADE_SICAR, CONFORMIDADE_INCRA), que tem prioridade.
"""
import os

_DROPBOX = r"C:\Users\User\Dropbox"
ORIGINAL = {
    "analise": _DROPBOX + r"\#CONSULTANCY\PLANAVEG\GEODATABASE\Analise_Conformidade_CAR-INCRA\Resultados_Analise_Conformidade",
    "sicar": _DROPBOX + r"\Geoinformation\GEOINFO BRASIL\SICAR\BASE_CAR_ESTADOS_05_2026",
    "incra": _DROPBOX + r"\Geoinformation\GEOINFO BRASIL\INCRA",
}
RESTAURADO = {   # relativo a <PLANAVEG_RAIZ>\Analise_Conformidade_CAR-INCRA
    "analise": "Resultados_Analise_Conformidade",
    "sicar": "SICAR_BASE_CAR_ESTADOS_05_2026",
    "incra": "INCRA",
}
VARIAVEL = {"analise": "CONFORMIDADE_ANALISE", "sicar": "CONFORMIDADE_SICAR", "incra": "CONFORMIDADE_INCRA"}


def pasta(chave):
    """Caminho da pasta 'analise', 'sicar' ou 'incra' (ver o cabeçalho)."""
    if os.environ.get(VARIAVEL[chave]):
        return os.environ[VARIAVEL[chave]]
    raiz = os.environ.get("PLANAVEG_RAIZ")
    if raiz:
        base = os.path.join(raiz, "Analise_Conformidade_CAR-INCRA")
        if os.path.isdir(base):
            return os.path.join(base, RESTAURADO[chave])
    return ORIGINAL[chave]


PASTA_ANALISE = pasta("analise")
PASTA_SICAR = pasta("sicar")
PASTA_INCRA = pasta("incra")
# Downloads nacionais do INCRA (10/06/2026), camadas de natureza privada - entrada do script 1
SIGEF_ARQ = os.path.join(PASTA_INCRA, "SIGEF", "Sigef Privado.shp")
SNCI_ARQ = os.path.join(PASTA_INCRA, "SNCI", "Imóvel certificado SNCI Privado.shp")
