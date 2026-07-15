"""
Limpiador de RIPS - EPS Mutual (Colombia)
=========================================

Módulo de limpieza de archivos RIPS (formato plano .txt) contenidos en
archivos .zip, aplicando las reglas de negocio definidas para la EPS Mutual.

Reglas aplicadas:
  1. Todo código de factura que comience con "FEC00" se cambia a "FEC"
     (ej. FEC0039627 -> FEC39627). Se aplica en todos los archivos donde
     aparezca el código FEC (AD, AF, AH, AT, ...).
  2. El archivo AD (datos del afiliado) y todas sus referencias se eliminan:
     se borra el archivo AD*.txt y se quita su línea del CT (control).
  3. El NIT "806009230-2" se deja sin dígito de verificación: "806009230".
  4. Si el RIPS es CONTRIBUTIVO se aplican cambios adicionales:
        - En AF y US: "ESS207" -> "ESSC07"
        - En US: el régimen "2" -> "1"

Autor: ingeniería de sistemas - sector salud (CEMIC)
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional

# ---------------------------------------------------------------------------
# Constantes de negocio
# ---------------------------------------------------------------------------

NIT_ORIGEN = "806009230-2"
NIT_DESTINO = "806009230"

PREFIJO_FEC_VIEJO = "FEC00"
PREFIJO_FEC_NUEVO = "FEC"

ENTIDAD_VIEJA = "ESS207"
ENTIDAD_NUEVA = "ESSC07"

# Régimen en el archivo US: 2 = subsidiado, 1 = contributivo
REGIMEN_SUBSIDIADO = "2"
REGIMEN_CONTRIBUTIVO = "1"

NOMBRE_ARCHIVO_AFILIADO = "AD"  # prefijo del archivo de afiliado a eliminar


# ---------------------------------------------------------------------------
# Resultado de procesamiento
# ---------------------------------------------------------------------------

@dataclass
class ResultadoArchivo:
    """Resumen de lo que se hizo en un archivo de texto."""
    nombre: str
    total_lineas: int = 0
    lineas_modificadas: int = 0
    contenido: Optional[str] = None          # None si fue eliminado
    eliminado: bool = False
    cambios: List[str] = field(default_factory=list)

    @property
    def fue_modificado(self) -> bool:
        return self.lineas_modificadas > 0 or self.eliminado


@dataclass
class ResultadoLimpieza:
    nombre_zip: str
    regimen: str  # "contributivo" | "subsidiado"
    archivos: List[ResultadoArchivo] = field(default_factory=list)
    zip_bytes: Optional[bytes] = None
    errores: List[str] = field(default_factory=list)

    @property
    def resumen(self) -> str:
        mod = [a for a in self.archivos if a.fue_modificado]
        return (
            f"{self.nombre_zip} | régimen: {self.regimen} | "
            f"archivos afectados: {len(mod)}/{len(self.archivos)}"
        )


# ---------------------------------------------------------------------------
# Funciones de transformación de una línea
# ---------------------------------------------------------------------------

def _transformar_linea(linea: str, es_contributivo: bool) -> tuple[str, List[str]]:
    """Aplica las reglas de limpieza a una sola línea.

    Devuelve (linea_nueva, lista_de_cambios_descriptivos).
    """
    original = linea
    cambios: List[str] = []
    nueva = linea

    # Regla 1: FEC00 -> FEC  (replace global, es idempotente)
    if PREFIJO_FEC_VIEJO in nueva:
        oc = original.count(PREFIJO_FEC_VIEJO)
        nueva = nueva.replace(PREFIJO_FEC_VIEJO, PREFIJO_FEC_NUEVO)
        cambios.append(f"FEC00->FEC (x{oc})")

    # Regla 3: NIT 806009230-2 -> 806009230
    if NIT_ORIGEN in nueva:
        nueva = nueva.replace(NIT_ORIGEN, NIT_DESTINO)
        cambios.append("NIT sin DV")

    # Regla 4 (contributivo): ESS207 -> ESSC07 (en cualquier archivo, p.ej. AF/US)
    if es_contributivo and ENTIDAD_VIEJA in nueva:
        nueva = nueva.replace(ENTIDAD_VIEJA, ENTIDAD_NUEVA)
        cambios.append("ESS207->ESSC07")

    return nueva, cambios


def contar_fec00(original: str) -> int:
    return original.count(PREFIJO_FEC_VIEJO)


def _transformar_linea_us(linea: str, es_contributivo: bool) -> tuple[str, List[str]]:
    """Línea del archivo US tiene tratamiento especial en el régimen.

    Formato US (por posición de campo separado por comas):
      tipoId,numId,entidad,regimen,ape1,ape2,nom1,nom2,edad,sexo,...

    - Régimen es el campo 4 (índice 3). Solo cambiamos "2"->"1" si es
      contributivo y el campo es exactamente "2".
    - ESS207->ESSC07 también aplica aquí (campo 3, índice 2).
    """
    cambios: List[str] = []
    nueva = linea

    # Regla 4 contributivo: ESS207 -> ESSC07
    if es_contributivo and ENTIDAD_VIEJA in nueva:
        nueva = nueva.replace(ENTIDAD_VIEJA, ENTIDAD_NUEVA)
        cambios.append("ESS207->ESSC07")

    if es_contributivo:
        partes = nueva.split(",")
        # campo régimen = índice 3
        if len(partes) > 3 and partes[3] == REGIMEN_SUBSIDIADO:
            partes[3] = REGIMEN_CONTRIBUTIVO
            cambios.append("régimen 2->1")
            nueva = ",".join(partes)

    return nueva, cambios


# ---------------------------------------------------------------------------
# Procesamiento de un ZIP
# ---------------------------------------------------------------------------

def limpiar_zip_bytes(zip_bytes: bytes, nombre_zip: str, es_contributivo: bool) -> ResultadoLimpieza:
    """Lee un ZIP de RIPS en memoria, aplica las reglas y devuelve el ZIP limpio."""
    resultado = ResultadoLimpieza(
        nombre_zip=nombre_zip,
        regimen="contributivo" if es_contributivo else "subsidiado",
    )

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zin:
            nombres = zin.namelist()
            # filtrar solo archivos (no directorios) y txt
            txts = [n for n in nombres if n.lower().endswith(".txt") and not n.endswith("/")]

            if not txts:
                resultado.errores.append("El ZIP no contiene archivos .txt de RIPS.")
                return resultado

            contenidos: dict[str, str] = {}
            for n in txts:
                data = zin.read(n)
                # decodificar tolerante a BOM y latin-1
                try:
                    texto = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    texto = data.decode("latin-1")
                contenidos[n] = texto

            # Determinar prefijos (ej. "AD005436") para eliminar referencias
            prefijos = {os.path.splitext(n)[0].upper(): n for n in txts}

            # --- Identificar y eliminar AD ---
            ad_a_eliminar: List[str] = [n for n in txts
                                        if os.path.splitext(n)[0].upper().startswith(NOMBRE_ARCHIVO_AFILIADO)]

            # --- Procesar cada archivo ---
            archivos_resultado: List[ResultadoArchivo] = []

            for n in txts:
                base = os.path.splitext(n)[0].upper()
                texto = contenidos[n]

                # Regla 2: eliminar AD y sus referencias
                if base.startswith(NOMBRE_ARCHIVO_AFILIADO):
                    ra = ResultadoArchivo(nombre=n, eliminado=True)
                    ra.cambios.append("archivo AD eliminado")
                    archivos_resultado.append(ra)
                    continue

                es_us = base.startswith("US")
                lineas = texto.splitlines()
                nuevas_lineas: List[str] = []
                lineas_mod = 0
                cambios_arch: List[str] = []

                for ln in lineas:
                    if es_us:
                        nl, camb = _transformar_linea_us(ln, es_contributivo)
                    else:
                        nl, camb = _transformar_linea(ln, es_contributivo)

                    # Regla 2: si esta línea del CT referencia un AD, se omite
                    if base.startswith("CT") and _linea_referencia_ad(ln, ad_a_eliminar, prefijos):
                        cambios_arch.append(f"referencia AD eliminada: {ln}")
                        continue  # no se incluye la línea

                    if nl != ln:
                        lineas_mod += 1
                        cambios_arch.extend(camb)
                    nuevas_lineas.append(nl)

                texto_nuevo = "\n".join(nuevas_lineas)
                # preservar salto de línea final si lo tenía
                if texto.endswith("\n") or texto.endswith("\r"):
                    texto_nuevo += "\n"

                ra = ResultadoArchivo(
                    nombre=n,
                    total_lineas=len(lineas),
                    lineas_modificadas=lineas_mod,
                    contenido=texto_nuevo,
                    cambios=cambios_arch,
                )
                archivos_resultado.append(ra)

            # --- Empaquetar ZIP de salida ---
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zout:
                for ra in archivos_resultado:
                    if ra.eliminado:
                        continue
                    if ra.contenido is None:
                        continue
                    zout.writestr(ra.nombre, ra.contenido)

            resultado.archivos = archivos_resultado
            resultado.zip_bytes = buffer.getvalue()

    except zipfile.BadZipFile:
        resultado.errores.append(f"Archivo inválido / no es un ZIP: {nombre_zip}")
    except Exception as e:  # noqa: BLE001
        resultado.errores.append(f"Error procesando {nombre_zip}: {e}")

    return resultado


def _linea_referencia_ad(linea: str, ad_a_eliminar: List[str], prefijos: dict) -> bool:
    """Detecta si una línea del CT referencia un archivo AD que será eliminado.

    Formato CT típico:
      codigosuministro,fecha,nombrearchivo,cantidad
    El nombre del archivo (sin .txt) aparece en el 3er campo.
    """
    if not ad_a_eliminar:
        return False
    partes = linea.split(",")
    if len(partes) < 3:
        return False
    nombre_ref = partes[2].strip().upper()
    # El nombre del archivo referenciado en el CT (sin .txt) suele coincidir
    # con el nombre del AD a eliminar (ej. "AD005436"). Para ser robusto ante
    # discrepancias de nombre, se considera referencia AD cualquier campo cuyo
    # valor coincida con el nombre base del AD o que comience por el prefijo "AD".
    for ad in ad_a_eliminar:
        ad_base = os.path.splitext(ad)[0].upper()
        if nombre_ref == ad_base or nombre_ref.startswith(ad_base) or ad_base.startswith(nombre_ref):
            return True
    return False


# ---------------------------------------------------------------------------
# Cruce con CSV de autorizaciones (plataforma Mutual)
# ---------------------------------------------------------------------------

@dataclass
class MapaAutorizaciones:
    """CSV de autorizaciones de la plataforma Mutual cargado en memoria.

    Permite resolver el régimen de un RIPS cruzando por documento de afiliado.
    La columna C_ADMINISTRADORA indica la entidad:
        ESS207 -> subsidiado ; ESSC07 -> contributivo
    """
    doc_a_regimen: dict = field(default_factory=dict)   # doc -> "contributivo"/"subsidiado"
    doc_a_administradora: dict = field(default_factory=dict)
    doc_a_autorizacion: dict = field(default_factory=dict)  # doc -> nro autorización
    doc_a_estado: dict = field(default_factory=dict)        # doc -> estado solicitud
    total_filas: int = 0

    def regimen_para_doc(self, doc: str) -> Optional[str]:
        return self.doc_a_regimen.get(str(doc).strip().upper())

    def info_para_doc(self, doc: str) -> dict:
        """Devuelve {regimen, administradora, autorizacion, estado} o {}."""
        d = str(doc).strip().upper()
        if d not in self.doc_a_regimen:
            return {}
        return {
            "regimen": self.doc_a_regimen.get(d),
            "administradora": self.doc_a_administradora.get(d),
            "autorizacion": self.doc_a_autorizacion.get(d),
            "estado": self.doc_a_estado.get(d),
        }


def cargar_csv_autorizaciones(csv_bytes: bytes) -> MapaAutorizaciones:
    """Carga el CSV de autorizaciones de Mutual y construye el mapa doc->régimen.

    Tolerante a BOM, separador ';' o ',', y codificaciones utf-8/latin-1.
    """
    import csv as _csv

    try:
        texto = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = csv_bytes.decode("latin-1")

    # Detectar separador
    primera = texto.splitlines()[0] if texto else ""
    delim = ";" if primera.count(";") > primera.count(",") else ","

    reader = _csv.reader(io.StringIO(texto), delimiter=delim)
    filas = list(reader)
    if not filas:
        return MapaAutorizaciones()

    header = [h.strip().upper() for h in filas[0]]
    try:
        idx_doc = header.index("C_DOCUMENTO_AFILIADO")
        idx_adm = header.index("C_ADMINISTRADORA")
    except ValueError:
        # columnas no encontradas: devolver mapa vacío (la app reportará el error)
        m = MapaAutorizaciones()
        m.total_filas = len(filas) - 1
        return m

    # Columnas opcionales de autorización/estado
    idx_aut = header.index("N_NUMERO_AUTORIZACION") if "N_NUMERO_AUTORIZACION" in header else None
    idx_est = header.index("C_ESTADO_SOLICITUD") if "C_ESTADO_SOLICITUD" in header else None

    mapa = MapaAutorizaciones()
    for fila in filas[1:]:
        if len(fila) <= max(idx_doc, idx_adm):
            continue
        doc = fila[idx_doc].strip().upper()
        adm = fila[idx_adm].strip().upper()
        if not doc:
            continue
        regimen = "contributivo" if adm == ENTIDAD_NUEVA else "subsidiado"
        aut = fila[idx_aut].strip() if (idx_aut is not None and len(fila) > idx_aut) else ""
        est = fila[idx_est].strip() if (idx_est is not None and len(fila) > idx_est) else ""
        # Si hay dos regímenes distintos para el mismo doc, prevalece contributivo
        prev = mapa.doc_a_regimen.get(doc)
        if prev is None or regimen == "contributivo":
            mapa.doc_a_regimen[doc] = regimen
            mapa.doc_a_administradora[doc] = adm
            mapa.doc_a_autorizacion[doc] = aut
            mapa.doc_a_estado[doc] = est

    mapa.total_filas = len(filas) - 1
    return mapa


def extraer_documentos_zip(zip_bytes: bytes) -> List[str]:
    """Extrae los documentos de afiliado presentes en un RIPS (.zip).

    Se buscan en los archivos US (campo 2, índice 1), AH y AT (campo 4, índice 3)
    y también AF (puede no traer doc). Devuelve lista de documentos únicos.
    """
    docs: List[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zin:
            for n in zin.namelist():
                if not n.lower().endswith(".txt") or n.endswith("/"):
                    continue
                base = os.path.splitext(os.path.basename(n))[0].upper()
                try:
                    data = zin.read(n)
                    try:
                        texto = data.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        texto = data.decode("latin-1")
                except Exception:  # noqa: BLE001
                    continue
                if base.startswith("US"):
                    idx = 1
                elif base.startswith("AH") or base.startswith("AT"):
                    idx = 3
                else:
                    continue
                for ln in texto.splitlines():
                    partes = ln.split(",")
                    if len(partes) > idx and partes[idx].strip():
                        docs.append(partes[idx].strip().upper())
    except Exception:  # noqa: BLE001
        return docs
    # únicos preservando orden
    vistos = set()
    unicos = []
    for d in docs:
        if d not in vistos:
            vistos.add(d)
            unicos.append(d)
    return unicos


def inferir_regimen_zip(zip_bytes: bytes, mapa: MapaAutorizaciones) -> tuple[Optional[str], List[str], List[dict]]:
    """Cruza los documentos del ZIP contra el CSV y deduce el régimen.

    Devuelve (regimen, documentos_no_encontrados, detalles_por_doc):
      - regimen: "subsidiado" / "contributivo" / None (mezcla o sin match)
      - documentos_no_encontrados: docs del ZIP que no aparecen en el CSV
      - detalles_por_doc: lista de dicts con la info de autorización cruzada
        ({doc, regimen, administradora, autorizacion, estado, encontrado})

    Nota: los RIPS no traen el nro de autorización, así que el cruce se hace por
    documento del afiliado; la autorización/estado se reportan desde el CSV para
    que el usuario confirme contra la factura.
    """
    docs = extraer_documentos_zip(zip_bytes)
    if not docs:
        return None, [], []
    encontrados = []
    no_encontrados = []
    detalles = []
    for d in docs:
        info = mapa.info_para_doc(d)
        if not info:
            no_encontrados.append(d)
            detalles.append({"doc": d, "encontrado": False})
        else:
            encontrados.append(info["regimen"])
            detalles.append({
                "doc": d,
                "encontrado": True,
                "regimen": info["regimen"],
                "administradora": info["administradora"],
                "autorizacion": info.get("autorizacion"),
                "estado": info.get("estado"),
            })
    if not encontrados:
        return None, no_encontrados, detalles
    if all(r == "contributivo" for r in encontrados):
        return "contributivo", no_encontrados, detalles
    if all(r == "subsidiado" for r in encontrados):
        return "subsidiado", no_encontrados, detalles
    # mezcla -> indeterminado
    return None, no_encontrados, detalles


# ---------------------------------------------------------------------------
# API de alto nivel
# ---------------------------------------------------------------------------

def limpiar_multiples(entradas: List[tuple[bytes, str, bool]]) -> List[ResultadoLimpieza]:
    """Procesa varios ZIP. entradas = [(bytes_zip, nombre, es_contributivo), ...]"""
    return [limpiar_zip_bytes(b, n, c) for (b, n, c) in entradas]
