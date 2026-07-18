"""
Limpiador de RIPS - EPS SURA
============================

Reglas:
1. FEC00 + número -> FEC + número.
2. Elimina el archivo AD y su referencia en CT.
3. Normaliza autorizaciones que comienzan por 139610:
   inserta un guion después de los primeros 10 dígitos.
4. En AF, cambia 806009230-2 por 806009230.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional


PREFIJO_FEC_VIEJO = "FEC00"
PREFIJO_FEC_NUEVO = "FEC"
NIT_CON_DV = "806009230-2"
NIT_SIN_DV = "806009230"
NOMBRE_ARCHIVO_AD = "AD"

# El guion va inmediatamente después del prefijo 139610.
# Ejemplo: 13961015071100 -> 139610-15071100.
# Si el guion ya existe, no se vuelve a insertar.
PATRON_AUTORIZACION_SURA = re.compile(
    r"(?<!\d)(139610)(?!-)(\d+)"
)


@dataclass
class ResultadoArchivoSura:
    nombre: str
    total_lineas: int = 0
    lineas_modificadas: int = 0
    contenido: Optional[str] = None
    eliminado: bool = False
    cambios: List[str] = field(default_factory=list)

    @property
    def fue_modificado(self) -> bool:
        return self.eliminado or self.lineas_modificadas > 0


@dataclass
class ResultadoLimpiezaSura:
    nombre_zip: str
    archivos: List[ResultadoArchivoSura] = field(default_factory=list)
    zip_bytes: Optional[bytes] = None
    errores: List[str] = field(default_factory=list)

    @property
    def resumen(self) -> str:
        modificados = [a for a in self.archivos if a.fue_modificado]
        return (
            f"{self.nombre_zip} | archivos afectados: "
            f"{len(modificados)}/{len(self.archivos)}"
        )


def _normalizar_autorizaciones(linea: str) -> tuple[str, int]:
    nueva, total = PATRON_AUTORIZACION_SURA.subn(r"\1-\2", linea)
    return nueva, total


def _transformar_linea_sura(
    linea: str,
    es_archivo_af: bool,
) -> tuple[str, List[str]]:
    nueva = linea
    cambios: List[str] = []

    if PREFIJO_FEC_VIEJO in nueva:
        total = nueva.count(PREFIJO_FEC_VIEJO)
        nueva = nueva.replace(PREFIJO_FEC_VIEJO, PREFIJO_FEC_NUEVO)
        cambios.append(f"FEC00→FEC (x{total})")

    nueva_aut, total_aut = _normalizar_autorizaciones(nueva)
    if total_aut:
        nueva = nueva_aut
        cambios.append(
            f"Autorización SURA: guion después de 139610 (x{total_aut})"
        )

    if es_archivo_af and NIT_CON_DV in nueva:
        total = nueva.count(NIT_CON_DV)
        nueva = nueva.replace(NIT_CON_DV, NIT_SIN_DV)
        cambios.append(f"NIT AF sin DV (x{total})")

    return nueva, cambios


def _linea_ct_referencia_ad(
    linea: str,
    archivos_ad: List[str],
) -> bool:
    if not archivos_ad:
        return False

    partes = linea.split(",")
    if len(partes) < 3:
        return False

    referencia = partes[2].strip().upper()
    for nombre_ad in archivos_ad:
        base_ad = os.path.splitext(os.path.basename(nombre_ad))[0].upper()
        if (
            referencia == base_ad
            or referencia.startswith(base_ad)
            or base_ad.startswith(referencia)
        ):
            return True

    return referencia.startswith("AD")


def limpiar_zip_sura(
    zip_bytes: bytes,
    nombre_zip: str,
) -> ResultadoLimpiezaSura:
    resultado = ResultadoLimpiezaSura(nombre_zip=nombre_zip)

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as entrada:
            nombres_txt = [
                n
                for n in entrada.namelist()
                if n.lower().endswith(".txt") and not n.endswith("/")
            ]

            if not nombres_txt:
                resultado.errores.append(
                    "El ZIP no contiene archivos .txt de RIPS."
                )
                return resultado

            contenidos: dict[str, str] = {}
            for nombre in nombres_txt:
                datos = entrada.read(nombre)
                try:
                    texto = datos.decode("utf-8-sig")
                except UnicodeDecodeError:
                    texto = datos.decode("latin-1")
                contenidos[nombre] = texto

            archivos_ad = [
                n
                for n in nombres_txt
                if os.path.splitext(os.path.basename(n))[0]
                .upper()
                .startswith(NOMBRE_ARCHIVO_AD)
            ]

            archivos_resultado: List[ResultadoArchivoSura] = []

            for nombre in nombres_txt:
                base_archivo = os.path.splitext(
                    os.path.basename(nombre)
                )[0].upper()
                texto = contenidos[nombre]

                if base_archivo.startswith(NOMBRE_ARCHIVO_AD):
                    archivos_resultado.append(
                        ResultadoArchivoSura(
                            nombre=nombre,
                            eliminado=True,
                            cambios=["Archivo AD eliminado"],
                        )
                    )
                    continue

                es_af = base_archivo.startswith("AF")
                es_ct = base_archivo.startswith("CT")
                nuevas_lineas: List[str] = []
                cambios_archivo: List[str] = []
                lineas_modificadas = 0
                lineas = texto.splitlines()

                for linea in lineas:
                    if es_ct and _linea_ct_referencia_ad(
                        linea,
                        archivos_ad,
                    ):
                        cambios_archivo.append(
                            f"Referencia AD eliminada del CT: {linea}"
                        )
                        lineas_modificadas += 1
                        continue

                    nueva, cambios = _transformar_linea_sura(
                        linea,
                        es_archivo_af=es_af,
                    )
                    if nueva != linea:
                        lineas_modificadas += 1
                        cambios_archivo.extend(cambios)

                    nuevas_lineas.append(nueva)

                texto_nuevo = "\n".join(nuevas_lineas)
                if texto.endswith("\n") or texto.endswith("\r"):
                    texto_nuevo += "\n"

                archivos_resultado.append(
                    ResultadoArchivoSura(
                        nombre=nombre,
                        total_lineas=len(lineas),
                        lineas_modificadas=lineas_modificadas,
                        contenido=texto_nuevo,
                        cambios=cambios_archivo,
                    )
                )

            salida = io.BytesIO()
            with zipfile.ZipFile(
                salida,
                "w",
                zipfile.ZIP_DEFLATED,
            ) as zip_salida:
                for archivo in archivos_resultado:
                    if archivo.eliminado or archivo.contenido is None:
                        continue
                    zip_salida.writestr(
                        archivo.nombre,
                        archivo.contenido,
                    )

            resultado.archivos = archivos_resultado
            resultado.zip_bytes = salida.getvalue()

    except zipfile.BadZipFile:
        resultado.errores.append(
            f"Archivo inválido o no es ZIP: {nombre_zip}"
        )
    except Exception as exc:
        resultado.errores.append(
            f"Error procesando {nombre_zip}: {exc}"
        )

    return resultado


def limpiar_multiples_sura(
    entradas: List[tuple[bytes, str]],
) -> List[ResultadoLimpiezaSura]:
    return [
        limpiar_zip_sura(datos, nombre)
        for datos, nombre in entradas
    ]
