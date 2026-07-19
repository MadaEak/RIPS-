"""
Generador de JSON RIPS para EPS SURA
====================================

Extiende el generador general de RIPS y agrega:

- Detección automática del régimen desde uno o varios XML corregidos.
- Normalización de autorizaciones SURA 139610-...
- Estructura de medicamentos conforme al Documento Técnico 1 de la Resolución 948 de 2026.
- Consulta local de TablaReferenciaIUM.xlsx.
- Conservación de IUM válidos.
- Extracción del CUM cuando el código antiguo trae el ATC concatenado.
- Selección automática del IUM más semejante cuando existen varias presentaciones.
- Reporte de auditoría de medicamentos.

La tabla TablaReferenciaIUM.xlsx debe estar en la misma carpeta que este archivo.
"""

from __future__ import annotations

import copy
import io
import math
import os
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from lxml import etree

from generador_json import CreadorJsonRips, normalizar_factura


VERSION_GENERADOR_SURA = "2026.07.19-v11-diagnosticos-relacionados-unicos"

PATRON_AUTORIZACION_SURA = re.compile(r"(?<!\d)(139610)(?!-)(\d+)")
PATRON_CUM_INICIAL = re.compile(r"^\s*(\d{4,8}-\d{1,3})(?:\s*-\s*[A-Z0-9]+)?", re.I)
PATRON_FORTALEZA = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(MCG|UG|MG|ML|UI|G|U)",
    re.I,
)



MAPA_IUM_DIRECTO_SURA: Dict[str, Dict[str, str]] = {
    # Soluciones y medicamentos cuyos nombres llegan compactados en el AM.
    # Se usan únicamente cuando la presentación puede identificarse con
    # seguridad por nombre, concentración, vía y contenido.
    "CLORURODESODIO09500MLS": {
        "codigo": "1S1016191010103",
        "descripcion": (
            "SODIO CLORURO 9MG/1ML, SOLUCIÓN INTRAVENOSA, "
            "BOLSA 500 ML"
        ),
    },
    "LEVOMEPROMAZINA40MG20ML4S": {
        "codigo": "1L1011811000100",
        "descripcion": (
            "LEVOMEPROMAZINA 40MG/1ML, SOLUCIÓN ORAL, "
            "FRASCO 20 ML"
        ),
    },
    "MIDAZOLAM5MG5MLAMPOLLA": {
        "codigo": "1M1006621000103",
        "descripcion": (
            "MIDAZOLAM 1MG/1ML, SOLUCIÓN INTRAVENOSA, "
            "AMPOLLA 5 ML"
        ),
    },
    "LACTATODERINGER500MLSOLUCI": {
        "codigo": "2C1025481002100",
        "descripcion": (
            "SOLUCIÓN LACTATO DE RINGER, INTRAVENOSA, "
            "BOLSA 500 ML, PRESENTACIÓN GENÉRICA"
        ),
    },
}


MAPA_CUPS_EQUIVALENTES_VIGENTES: Dict[str, Dict[str, str]] = {
    # Código histórico de creatinina en suero. En la tabla CUPS vigente
    # el procedimiento equivalente se encuentra como 903895.
    "903825": {
        "codigo": "903895",
        "descripcion": "CREATININA EN SUERO U OTROS FLUIDOS",
    },
}




MAPA_DIAGNOSTICOS_SURA: Dict[str, str] = {
    # Zeus/SURA entrega 6031 sin la letra y con un quinto carácter que
    # no corresponde al formato RIPS. El diagnóstico CIE-10 base es
    # F60.3 y debe informarse sin punto como F603.
    "6031": "F603",
    # Repara también JSON generados por la versión V9.
    "F6031": "F603",
}


def _normalizar_diagnostico_sura(valor: Any) -> Optional[str]:
    """Normaliza códigos CIE-10 provenientes de los RIPS planos SURA.

    Se retiran puntos y espacios. Los códigos exclusivamente numéricos no se
    aceptan salvo equivalencias verificadas expresamente en el proyecto.
    """
    if valor is None:
        return None

    texto = str(valor).strip().upper()
    if not texto or texto in {"NULL", "NONE", "NAN"}:
        return None

    compacto = re.sub(r"[^A-Z0-9]", "", texto)
    if compacto in MAPA_DIAGNOSTICOS_SURA:
        return MAPA_DIAGNOSTICOS_SURA[compacto]

    if re.fullmatch(r"\d+", compacto):
        raise ValueError(
            "Código de diagnóstico sin prefijo alfabético: "
            f"{texto}. Agregue una equivalencia CIE-10 confirmada."
        )

    # Los campos CIE-10 del RIPS deben quedar en formato compacto:
    # una letra seguida de tres caracteres, por ejemplo F603 o F99X.
    if not re.fullmatch(r"[A-Z][0-9]{2}[0-9A-Z]", compacto):
        raise ValueError(
            "Código de diagnóstico con formato o longitud inválida: "
            f"{texto} -> {compacto}. Se esperaban 4 caracteres, "
            "por ejemplo F603."
        )

    return compacto


def _extraer_recaudos_af_sura(
    zip_bytes: bytes,
) -> Dict[str, Dict[str, int]]:
    """Lee copago y cuota moderadora agregados del archivo AF."""
    resultados: Dict[str, Dict[str, int]] = {}

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archivo_zip:
        nombres = [
            nombre
            for nombre in archivo_zip.namelist()
            if Path(nombre).name.upper().startswith("AF")
            and nombre.lower().endswith(".txt")
        ]

        if not nombres:
            return resultados

        contenido = archivo_zip.read(nombres[0]).decode(
            "utf-8-sig",
            errors="ignore",
        )

    for linea in contenido.splitlines():
        if not linea.strip():
            continue

        partes = [parte.strip() for parte in linea.split(",")]
        if len(partes) < 15:
            continue

        factura = normalizar_factura(partes[4])

        def entero_moneda(indice: int) -> int:
            try:
                return max(
                    0,
                    int(Decimal(partes[indice].replace(",", "."))),
                )
            except (InvalidOperation, ValueError, IndexError):
                return 0

        resultados[factura] = {
            "01": entero_moneda(13),
            "02": entero_moneda(14),
        }

    return resultados


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return etree.QName(tag).localname


def _id_directo(elemento: etree._Element) -> Optional[str]:
    for hijo in elemento:
        if _local_name(hijo.tag) == "ID":
            valor = (hijo.text or "").strip()
            if valor:
                return valor
    return None


def _extraer_factura_embebida_xml(
    root: etree._Element,
) -> Optional[etree._Element]:
    for elemento in root.iter():
        if _local_name(elemento.tag) != "Description":
            continue

        contenido = (elemento.text or "").strip()
        if "<Invoice" not in contenido:
            continue

        inicio_invoice = contenido.find("<Invoice")
        inicio_declaracion = contenido.rfind(
            "<?xml",
            0,
            inicio_invoice,
        )

        if inicio_declaracion >= 0:
            contenido = contenido[inicio_declaracion:]
        else:
            contenido = contenido[inicio_invoice:]

        try:
            return etree.fromstring(contenido.encode("utf-8"))
        except etree.XMLSyntaxError as exc:
            raise ValueError(
                "No fue posible leer la factura Invoice embebida "
                "en el XML."
            ) from exc

    return None


def extraer_factura_regimen_xml_sura(
    xml_bytes: bytes,
    nombre_archivo: str = "factura.xml",
) -> Tuple[str, str]:
    """Extrae número de factura y régimen desde un XML ya corregido.

    COBERTURA_PLAN_BENEFICIOS:
    - schemeID 16 -> contributivo
    - schemeID 17 -> subsidiado
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise ValueError(
            f"{nombre_archivo}: el archivo no es un XML válido."
        ) from exc

    invoice = _extraer_factura_embebida_xml(root)
    fuente = invoice if invoice is not None else root

    numero_factura = (
        _id_directo(fuente)
        or _id_directo(root)
        or Path(nombre_archivo).stem
    )
    numero_factura = normalizar_factura(numero_factura)

    cobertura_id = ""
    cobertura_texto = ""

    for informacion in fuente.iter():
        if _local_name(informacion.tag) != "AdditionalInformation":
            continue

        nombre = None
        valor = None

        for hijo in informacion:
            local = _local_name(hijo.tag)
            if local == "Name":
                nombre = (hijo.text or "").strip().upper()
            elif local == "Value":
                valor = hijo

        if nombre != "COBERTURA_PLAN_BENEFICIOS" or valor is None:
            continue

        cobertura_id = (valor.get("schemeID") or "").strip()
        cobertura_texto = (valor.text or "").strip().upper()
        break

    if cobertura_id == "16" or "CONTRIBUTIVO" in cobertura_texto:
        regimen = "contributivo"
    elif cobertura_id == "17" or "SUBSIDIADO" in cobertura_texto:
        regimen = "subsidiado"
    else:
        raise ValueError(
            f"{nombre_archivo}: no se encontró una cobertura válida "
            "en COBERTURA_PLAN_BENEFICIOS. Se esperaba schemeID 16 o 17."
        )

    if not numero_factura:
        raise ValueError(
            f"{nombre_archivo}: no se pudo identificar el número de factura."
        )

    return numero_factura, regimen


def construir_mapa_regimen_xml_sura(
    xmls: List[Tuple[str, bytes]],
) -> Dict[str, str]:
    """Construye factura -> régimen a partir de varios XML."""
    mapa: Dict[str, str] = {}

    for nombre, contenido in xmls:
        factura, regimen = extraer_factura_regimen_xml_sura(
            contenido,
            nombre,
        )

        anterior = mapa.get(factura)
        if anterior and anterior != regimen:
            raise ValueError(
                f"La factura {factura} aparece en varios XML con "
                f"regímenes diferentes: {anterior} y {regimen}."
            )

        mapa[factura] = regimen

    return mapa


def _normalizar_texto(valor: Any) -> str:
    texto = "" if valor is None else str(valor)
    texto = "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", texto)
        if unicodedata.category(caracter) != "Mn"
    )
    texto = texto.upper()
    texto = re.sub(r"[^A-Z0-9]+", " ", texto)
    return " ".join(texto.split())


def _compactar(valor: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _normalizar_texto(valor))


def _numero_fortaleza_canonico(valor: Any) -> str:
    texto = str(valor or "").strip().replace(",", ".")
    try:
        numero = Decimal(texto)
    except (InvalidOperation, ValueError):
        return texto

    normalizado = format(numero.normalize(), "f")
    if "." in normalizado:
        normalizado = normalizado.rstrip("0").rstrip(".")
    return normalizado or "0"


def _nombre_busqueda_ium(nombre: Any) -> str:
    """Amplía nombres compactados del AM para buscar un IUM exacto."""
    original = "" if nombre is None else str(nombre)
    compacto = _compactar(original)

    if compacto.startswith("LEVOMEPROMAZINA40MG20ML"):
        return (
            "LEVOMEPROMAZINA 40MG/1ML OTRAS SOLUCIONES ORAL "
            "FRASCO 20ML"
        )

    if compacto.startswith("MIDAZOLAM5MG5MLAMPOLLA"):
        return (
            "MIDAZOLAM 1MG/1ML OTRAS SOLUCIONES INTRAVENOSA "
            "AMPOLLA 5ML"
        )

    if compacto.startswith("CLORURODESODIO09500ML"):
        return (
            "SODIO CLORURO 9MG/1ML OTRAS SOLUCIONES "
            "INTRAVENOSA BOLSA 500ML"
        )

    if compacto.startswith("CARBONATODELITIOTABLETA"):
        # Carbonato de litio se trata como LITIO. Como la tabla aportada
        # no contiene 300 mg, se selecciona la tableta oral más cercana,
        # priorizando el empaque informado en el AM.
        return "LITIO TABLETA ORAL"

    if compacto.startswith("DIVALPROATODESODIO"):
        # Divalproato se trata como ácido valproico, conservando la
        # concentración 250/500 mg y la forma de tableta.
        coincidencia = re.search(r"(\d+(?:\.\d+)?)MG", compacto)
        concentracion = (
            f"{coincidencia.group(1)}MG "
            if coincidencia
            else ""
        )
        return (
            f"ACIDO VALPROICO {concentracion}"
            "TABLETA ORAL"
        )

    if compacto.startswith("CLONAZEPAM05MGTABLETA"):
        # No hay tableta de 0,5 mg en la tabla. Se usa la tableta de
        # clonazepam más cercana según el empaque del AM.
        return "CLONAZEPAM TABLETA ORAL"

    if compacto.startswith("MELATONINA3MGTABLETA"):
        # La tabla contiene melatonina 3 mg en cápsula. Se usa como
        # presentación oral más cercana.
        return "MELATONINA 3MG CAPSULA ORAL"

    return original




def _ignorar_concentracion_en_equivalencia_ium(
    nombre: Any,
) -> bool:
    """Indica cuándo la concentración puede aproximarse.

    Esta excepción es deliberada y limitada a las equivalencias autorizadas:
    - carbonato de litio 300 mg -> litio oral más cercano;
    - clonazepam 0,5 mg -> tableta de clonazepam más cercana.

    Para divalproato y melatonina se sigue exigiendo la concentración exacta.
    """
    compacto = _compactar(nombre)

    return (
        compacto.startswith("CARBONATODELITIOTABLETA")
        or compacto.startswith("CLONAZEPAM05MGTABLETA")
    )


def _nota_equivalencia_aproximada_ium(
    nombre: Any,
) -> Optional[str]:
    compacto = _compactar(nombre)

    if compacto.startswith("CARBONATODELITIOTABLETA"):
        return (
            "Equivalencia aproximada autorizada: carbonato de litio "
            "se trató como LITIO; se permitió aproximar la concentración "
            "de 300 mg a la tableta oral más cercana disponible."
        )

    if compacto.startswith("DIVALPROATODESODIO"):
        return (
            "Equivalencia aproximada autorizada: divalproato de sodio "
            "se trató como ÁCIDO VALPROICO conservando concentración "
            "y forma de tableta."
        )

    if compacto.startswith("CLONAZEPAM05MGTABLETA"):
        return (
            "Equivalencia aproximada autorizada: se permitió "
            "aproximar clonazepam 0,5 mg a la tableta de clonazepam "
            "más cercana disponible."
        )

    if compacto.startswith("MELATONINA3MGTABLETA"):
        return (
            "Equivalencia aproximada autorizada: melatonina 3 mg "
            "tableta se relacionó con la presentación oral de 3 mg "
            "más cercana disponible."
        )

    return None


def _principio_objetivo_ium(nombre: Any) -> Optional[str]:
    compacto = _compactar(nombre)

    if compacto.startswith("SODIOCLORURO"):
        return "SODIOCLORURO"
    if compacto.startswith("ACIDOVALPROICO"):
        return "ACIDOVALPROICO"
    if compacto.startswith("LITIO"):
        return "LITIO"
    if compacto.startswith("CLONAZEPAM"):
        return "CLONAZEPAM"
    if compacto.startswith("MELATONINA"):
        return "MELATONINA"
    if compacto.startswith("VALPROATOSEMISODICO"):
        return "VALPROATOSEMISODICO"
    return None


def _normalizar_autorizacion(valor: Any) -> Optional[str]:
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() in ("null", "none", "nan"):
        return None
    return PATRON_AUTORIZACION_SURA.sub(r"\1-\2", texto)


def _extraer_fortalezas(*valores: Any) -> set[str]:
    resultado: set[str] = set()
    for valor in valores:
        texto = "" if valor is None else str(valor)
        texto = "".join(
            caracter
            for caracter in unicodedata.normalize("NFD", texto)
            if unicodedata.category(caracter) != "Mn"
        ).upper().replace(",", ".")

        for numero, unidad in PATRON_FORTALEZA.findall(texto):
            numero = _numero_fortaleza_canonico(numero)
            unidad = unidad.upper()
            if unidad == "UG":
                unidad = "MCG"
            resultado.add(f"{numero}{unidad}")
    return resultado


def _forma_general(*valores: Any) -> Optional[str]:
    texto = " ".join(_normalizar_texto(v) for v in valores if v is not None)

    equivalencias = (
        ("TABLETA", ("TABLETA", "COMPRIMIDO")),
        ("CAPSULA", ("CAPSULA",)),
        ("SOLUCION", ("SOLUCION",)),
        ("SUSPENSION", ("SUSPENSION",)),
        ("JARABE", ("JARABE",)),
        ("CREMA", ("CREMA",)),
        ("UNGUENTO", ("UNGUENTO", "POMADA")),
        ("GEL", ("GEL",)),
        ("POLVO", ("POLVO",)),
        ("AEROSOL", ("AEROSOL", "INHALADOR")),
        ("PARCHE", ("PARCHE",)),
        ("SUPOSITORIO", ("SUPOSITORIO",)),
        ("AMPOLLA", ("AMPOLLA",)),
        ("VIAL", ("VIAL",)),
        ("FRASCO", ("FRASCO",)),
    )

    for forma, terminos in equivalencias:
        if any(termino in texto for termino in terminos):
            return forma
    return None


def _extraer_empaque(nombre: Any) -> Optional[int]:
    texto = _normalizar_texto(nombre)
    coincidencias = re.findall(r"\bX\s+(\d{1,6})\b", texto)
    if not coincidencias:
        return None
    try:
        return int(coincidencias[-1])
    except (TypeError, ValueError):
        return None


def _extraer_marca(nombre: Any) -> Optional[str]:
    """Obtiene una marca comercial evitando etiquetas como (NA)."""
    texto = "" if nombre is None else str(nombre)
    marcas = re.findall(r"\(([^()]+)\)", texto)

    for valor in reversed(marcas):
        marca = _normalizar_texto(valor)
        if marca in ("", "NA", "N A", "NO APLICA"):
            continue
        if len(_compactar(marca)) < 4:
            continue
        return marca

    return None


def _extraer_via(*valores: Any) -> Optional[str]:
    texto = " ".join(
        _normalizar_texto(valor)
        for valor in valores
        if valor is not None
    )

    for via in ("ORAL", "INTRAVENOSA", "INTRAMUSCULAR", "TOPICA",
                "SUBCUTANEA", "OFTALMICA", "NASAL", "RECTAL",
                "VAGINAL", "INHALATORIA"):
        if via in texto:
            return via

    return None


def _similitud_texto(origen: Any, candidato: Any) -> float:
    origen_norm = _normalizar_texto(origen)
    candidato_norm = _normalizar_texto(candidato)

    if not origen_norm or not candidato_norm:
        return 0.0

    return SequenceMatcher(
        None,
        _compactar(origen_norm),
        _compactar(candidato_norm),
    ).ratio()



def _indice_columna_excel(referencia: str) -> int:
    """Convierte A, B, AA... en índices 0, 1, 26..."""
    coincidencia = re.match(r"([A-Z]+)", referencia or "")
    if not coincidencia:
        return 0

    indice = 0
    for caracter in coincidencia.group(1):
        indice = indice * 26 + (ord(caracter) - ord("A") + 1)
    return indice - 1


def _texto_nodo_excel(elemento: ET.Element) -> str:
    """Concatena los nodos de texto, incluidos rich text runs."""
    partes = []
    for nodo in elemento.iter():
        if nodo.tag.endswith("}t") and nodo.text is not None:
            partes.append(nodo.text)
    return "".join(partes)


def _ruta_primera_hoja_xlsx(archivo: zipfile.ZipFile) -> str:
    """Obtiene la ruta interna de la primera hoja del libro."""
    ns_main = {
        "m": "http://schemas.openxmlformats.org/"
             "spreadsheetml/2006/main"
    }
    ns_rel = {
        "r": "http://schemas.openxmlformats.org/"
             "package/2006/relationships"
    }
    id_rel_ns = (
        "{http://schemas.openxmlformats.org/"
        "officeDocument/2006/relationships}id"
    )

    workbook = ET.fromstring(archivo.read("xl/workbook.xml"))
    primera_hoja = workbook.find("m:sheets/m:sheet", ns_main)
    if primera_hoja is None:
        raise ValueError("El archivo XLSX no contiene hojas.")

    relacion_id = primera_hoja.attrib.get(id_rel_ns)
    if not relacion_id:
        return "xl/worksheets/sheet1.xml"

    relaciones = ET.fromstring(
        archivo.read("xl/_rels/workbook.xml.rels")
    )
    for relacion in relaciones.findall("r:Relationship", ns_rel):
        if relacion.attrib.get("Id") != relacion_id:
            continue

        destino = relacion.attrib.get("Target", "")
        destino = destino.replace("\\", "/").lstrip("/")

        if destino.startswith("xl/"):
            return destino
        return f"xl/{destino}"

    return "xl/worksheets/sheet1.xml"


def _leer_xlsx_sin_openpyxl(ruta: str) -> pd.DataFrame:
    """Lee la primera hoja de un XLSX usando solo la biblioteca estándar.

    Evita la dependencia opcional ``openpyxl`` de pandas, lo que permite
    ejecutar el generador en Streamlit Cloud o entornos donde esa librería
    no esté instalada.
    """
    filas: List[Dict[int, str]] = []

    with zipfile.ZipFile(ruta, "r") as archivo:
        cadenas_compartidas: List[str] = []

        if "xl/sharedStrings.xml" in archivo.namelist():
            with archivo.open("xl/sharedStrings.xml") as flujo:
                for evento, elemento in ET.iterparse(
                    flujo,
                    events=("end",),
                ):
                    if elemento.tag.endswith("}si"):
                        cadenas_compartidas.append(
                            _texto_nodo_excel(elemento)
                        )
                        elemento.clear()

        ruta_hoja = _ruta_primera_hoja_xlsx(archivo)

        with archivo.open(ruta_hoja) as flujo:
            for evento, elemento in ET.iterparse(
                flujo,
                events=("end",),
            ):
                if not elemento.tag.endswith("}row"):
                    continue

                fila: Dict[int, str] = {}

                for celda in list(elemento):
                    if not celda.tag.endswith("}c"):
                        continue

                    referencia = celda.attrib.get("r", "A1")
                    indice = _indice_columna_excel(referencia)
                    tipo = celda.attrib.get("t", "")

                    valor_nodo = next(
                        (
                            hijo
                            for hijo in celda
                            if hijo.tag.endswith("}v")
                        ),
                        None,
                    )

                    if tipo == "inlineStr":
                        valor = _texto_nodo_excel(celda)
                    elif valor_nodo is None or valor_nodo.text is None:
                        valor = ""
                    elif tipo == "s":
                        try:
                            valor = cadenas_compartidas[
                                int(valor_nodo.text)
                            ]
                        except (ValueError, IndexError):
                            valor = ""
                    elif tipo == "b":
                        valor = (
                            "TRUE"
                            if valor_nodo.text == "1"
                            else "FALSE"
                        )
                    else:
                        valor = valor_nodo.text

                    fila[indice] = valor

                if fila:
                    filas.append(fila)

                elemento.clear()

    if not filas:
        return pd.DataFrame()

    encabezados_fila = filas[0]
    max_columna = max(encabezados_fila)
    encabezados = [
        str(encabezados_fila.get(indice, "")).strip()
        for indice in range(max_columna + 1)
    ]

    registros = []
    for fila in filas[1:]:
        registro = {
            encabezados[indice]: fila.get(indice, "")
            for indice in range(len(encabezados))
            if encabezados[indice]
        }
        registros.append(registro)

    return pd.DataFrame(registros, columns=[
        encabezado
        for encabezado in encabezados
        if encabezado
    ])



def _extraer_concentracion_am(valor: Any) -> Any:
    """Convierte concentraciones como 25MG o 250 MG en números JSON."""
    if valor is None:
        return 0

    if isinstance(valor, (int, float)):
        numero = float(valor)
    else:
        texto = str(valor).strip().replace(",", ".")
        coincidencia = re.search(r"[-+]?\d+(?:\.\d+)?", texto)
        if not coincidencia:
            return 0
        try:
            numero = float(coincidencia.group(0))
        except ValueError:
            return 0

    if numero.is_integer():
        return int(numero)
    return numero


def _codigo_unidad_medida_am(valor: Any) -> int:
    """Convierte la unidad textual del AM al código UMM numérico.

    La fuente SURA puede traer textos concatenados como MILIGRAMOSZI.
    """
    if valor is None:
        return 0

    if isinstance(valor, (int, float)):
        try:
            return max(0, int(valor))
        except (TypeError, ValueError):
            return 0

    texto = _normalizar_texto(valor)
    compacto = re.sub(r"[^A-Z0-9]", "", texto)

    # Códigos de la tabla Unidad de Medida de Medicamentos (UMM).
    equivalencias = (
        (("MILIGRAMO", "MILIGRAMOS", "MG"), 168),
        (("MICROGRAMO", "MICROGRAMOS", "MCG", "UG"), 137),
        (("GRAMO", "GRAMOS", "GR"), 62),
        (("MILILITRO", "MILILITROS", "ML"), 176),
        (("LITRO", "LITROS", "LT"), 100),
        (("UNIDADINTERNACIONAL", "UNIDADESINTERNACIONALES", "UI"), 72),
    )

    for nombres, codigo in equivalencias:
        for nombre in nombres:
            if compacto == nombre or compacto.startswith(nombre):
                return codigo

    # Cuando el AM ya trae directamente un código numérico.
    coincidencia = re.fullmatch(r"\d{1,4}", compacto)
    if coincidencia:
        return int(compacto)

    return 0



def _normalizar_concentracion_unidad_am(
    valor_concentracion: Any,
    valor_unidad: Any,
) -> tuple[int, int]:
    """Convierte concentración y unidad a valores enteros válidos.

    Ejemplos:
    - 0.5 mg = 500 microgramos, UMM 137
    - 0.5 g  = 500 miligramos, UMM 168
    - 0.5 ml = 500 microlitros, UMM 146
    - 0.5 l  = 500 mililitros, UMM 176
    """
    unidad = _codigo_unidad_medida_am(valor_unidad)

    if valor_concentracion is None:
        return 0, unidad

    if isinstance(valor_concentracion, (int, float)):
        texto_numero = str(valor_concentracion)
    else:
        texto = str(valor_concentracion).strip().replace(",", ".")
        coincidencia = re.search(r"[-+]?\d+(?:\.\d+)?", texto)
        if not coincidencia:
            return 0, unidad
        texto_numero = coincidencia.group(0)

    try:
        numero = Decimal(texto_numero)
    except (InvalidOperation, ValueError):
        return 0, unidad

    if numero == numero.to_integral_value():
        return int(numero), unidad

    conversiones = {
        168: (Decimal("1000"), 137),
        62: (Decimal("1000"), 168),
        176: (Decimal("1000"), 146),
        100: (Decimal("1000"), 176),
    }

    conversion = conversiones.get(unidad)
    if conversion:
        factor, unidad_destino = conversion
        convertido = numero * factor
        if convertido == convertido.to_integral_value():
            return int(convertido), unidad_destino

    # El esquema rechaza decimales; no se genera un float.
    return 0, unidad


def _forma_farmaceutica_am(valor: Any) -> Optional[str]:
    """Conserva la forma únicamente cuando la fuente trae un código útil."""
    if valor is None:
        return None

    texto = str(valor).strip()
    normalizado = _normalizar_texto(texto)

    if normalizado in (
        "",
        "NULL",
        "NINGUNA",
        "FORMASINDEFINIR",
        "FORMA SIN DEFINIR",
        "SIN DEFINIR",
    ):
        return None

    return texto


@dataclass
class DecisionIUM:
    factura: str
    usuario: str
    medicamento: str
    codigo_original: str
    codigo_final: str
    estado: str
    detalle: str
    candidatos: str = ""
    numero_documento_profesional: Optional[str] = None
    factura_generada: str = "PENDIENTE"
    error_factura: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CatalogoIUM:
    _cache: Dict[str, Tuple[pd.DataFrame, set[str]]] = {}

    def __init__(self, ruta_excel: str | os.PathLike[str]):
        self.ruta_excel = str(Path(ruta_excel).resolve())
        self.df, self.codigos = self._cargar(self.ruta_excel)

    @classmethod
    def _cargar(cls, ruta: str) -> Tuple[pd.DataFrame, set[str]]:
        if ruta in cls._cache:
            return cls._cache[ruta]

        if not os.path.exists(ruta):
            raise FileNotFoundError(
                "No se encontró TablaReferenciaIUM.xlsx en la carpeta "
                "Generador Json."
            )

        df = _leer_xlsx_sin_openpyxl(ruta)
        df.columns = [str(c).strip() for c in df.columns]

        requeridas = {
            "Codigo",
            "Nombre",
            "Habilitado",
            "Extra_II:PrincipioActivo",
            "Extra_IV:FormaFarmaceutica",
        }
        faltantes = requeridas.difference(df.columns)
        if faltantes:
            raise ValueError(
                "La tabla IUM no contiene las columnas requeridas: "
                + ", ".join(sorted(faltantes))
            )

        df = df.copy()
        df["Codigo"] = df["Codigo"].fillna("").astype(str).str.strip()
        df["Nombre"] = df["Nombre"].fillna("").astype(str).str.strip()
        df["Habilitado"] = (
            df["Habilitado"].fillna("").astype(str).str.strip().str.upper()
        )
        df = df[(df["Codigo"] != "") & (df["Habilitado"] == "SI")].copy()

        df["_nombre_norm"] = df["Nombre"].map(_normalizar_texto)
        df["_nombre_compacto"] = df["Nombre"].map(_compactar)
        df["_principio_norm"] = (
            df["Extra_II:PrincipioActivo"]
            .fillna("")
            .astype(str)
            .map(_normalizar_texto)
        )
        df["_forma_norm"] = (
            df["Extra_IV:FormaFarmaceutica"]
            .fillna("")
            .astype(str)
            .map(_normalizar_texto)
        )
        df["_fortalezas"] = df["Nombre"].map(_extraer_fortalezas)
        df["_forma_general"] = df.apply(
            lambda fila: _forma_general(
                fila.get("Extra_IV:FormaFarmaceutica"),
                fila.get("Nombre"),
            ),
            axis=1,
        )
        df["_empaque"] = df["Nombre"].map(_extraer_empaque)
        df["_marca"] = df["Nombre"].map(_extraer_marca)
        df["_via"] = df["Nombre"].map(_extraer_via)
        df["_condicion"] = (
            df.get(
                "Extra_IX:CondicionRegistroMuestraMedica",
                "",
            )
            .fillna("")
            .astype(str)
            .map(_normalizar_texto)
        )
        df["_es_generico"] = df["_condicion"].str.contains(
            "GENERICO",
            regex=False,
        )
        df["_es_muestra"] = df["_condicion"].str.contains(
            "SI ES MUESTRA",
            regex=False,
        )
        df["_comercializable"] = ~df["_condicion"].str.contains(
            "NO SE COMERCIALIZA",
            regex=False,
        )

        codigos = set(df["Codigo"].tolist())
        cls._cache[ruta] = (df, codigos)
        return df, codigos

    def _candidatos(
        self,
        nombre: str,
        concentracion: Any,
        forma: Any,
        cantidad: Any,
    ) -> List[Tuple[float, Dict[str, Any]]]:
        """Ordena los IUM equivalentes de mayor a menor semejanza.

        Los criterios principales son:
        1. principio activo;
        2. concentración;
        3. forma farmacéutica;
        4. vía de administración;
        5. marca, cuando el RIPS la informa;
        6. condición comercializable/no muestra;
        7. presentación más cercana a la cantidad reportada.

        Cuando el RIPS no informa una marca, se prefieren registros clasificados
        como genéricos en la tabla oficial.
        """
        nombre_busqueda = _nombre_busqueda_ium(nombre)
        fuente_norm = _normalizar_texto(nombre_busqueda)
        fuente_compacta = _compactar(nombre_busqueda)
        ignorar_concentracion = (
            _ignorar_concentracion_en_equivalencia_ium(nombre)
        )

        if ignorar_concentracion:
            fortalezas = set()
        else:
            fortalezas = _extraer_fortalezas(
                nombre_busqueda,
                concentracion,
            )
        forma_fuente = _forma_general(nombre_busqueda, forma)
        via_fuente = _extraer_via(nombre_busqueda, forma)
        marca_fuente = _extraer_marca(nombre_busqueda)
        principio_objetivo = _principio_objetivo_ium(
            nombre_busqueda
        )

        try:
            cantidad_entera = int(float(cantidad))
        except (TypeError, ValueError):
            cantidad_entera = None

        palabras_fuente = set(fuente_norm.split())
        resultados: List[Tuple[float, Dict[str, Any]]] = []

        for _, fila in self.df.iterrows():
            principio = fila["_principio_norm"]
            if not principio:
                continue

            palabras_principio = set(principio.split())
            principio_compacto = _compactar(principio)

            if principio_objetivo:
                if principio_compacto != principio_objetivo:
                    continue
            else:
                contiene_principio = bool(
                    palabras_principio
                    and palabras_principio.issubset(palabras_fuente)
                )
                if (
                    not contiene_principio
                    and principio_compacto not in fuente_compacta
                ):
                    continue

            fortalezas_candidata = fila["_fortalezas"]
            if fortalezas:
                if not fortalezas_candidata:
                    continue
                if not fortalezas.issubset(fortalezas_candidata):
                    continue

            forma_candidata = fila["_forma_general"]
            if (
                forma_fuente
                and forma_candidata
                and forma_fuente != forma_candidata
            ):
                continue

            via_candidata = fila["_via"]
            if via_fuente and via_candidata and via_fuente != via_candidata:
                continue

            puntaje = 100.0
            criterios: List[str] = ["principio activo"]

            if fortalezas:
                if fortalezas.issubset(fortalezas_candidata):
                    puntaje += 40.0
                    criterios.append("concentración exacta")
                elif fortalezas.intersection(fortalezas_candidata):
                    puntaje += 18.0
                    criterios.append("concentración parcial")

            if forma_fuente and forma_fuente == forma_candidata:
                puntaje += 30.0
                criterios.append("forma farmacéutica")

            if via_fuente and via_fuente == via_candidata:
                puntaje += 12.0
                criterios.append("vía de administración")

            marca_candidata = fila["_marca"]
            if marca_fuente:
                if (
                    marca_candidata
                    and _compactar(marca_fuente)
                    == _compactar(marca_candidata)
                ):
                    puntaje += 65.0
                    criterios.append("marca comercial")
                elif marca_candidata:
                    puntaje -= 15.0
            elif bool(fila["_es_generico"]):
                puntaje += 22.0
                criterios.append("equivalente genérico")

            if bool(fila["_comercializable"]):
                puntaje += 18.0
                criterios.append("comercializable")
            else:
                puntaje -= 80.0

            if bool(fila["_es_muestra"]):
                puntaje -= 35.0
            else:
                puntaje += 8.0

            similitud = _similitud_texto(nombre_busqueda, fila["Nombre"])
            puntaje += similitud * 20.0

            empaque = fila["_empaque"]
            diferencia_empaque = None

            if cantidad_entera and empaque:
                diferencia_empaque = abs(cantidad_entera - empaque)
                cercania = max(
                    0.0,
                    1.0
                    - diferencia_empaque
                    / max(cantidad_entera, empaque),
                )
                puntaje += cercania * 12.0

                if cantidad_entera == empaque:
                    puntaje += 18.0
                    criterios.append("presentación igual a la cantidad")
                elif cantidad_entera % empaque == 0:
                    puntaje += 1.0
                    criterios.append("presentación divisible")
                elif empaque <= cantidad_entera:
                    puntaje += 2.0

            resultados.append(
                (
                    puntaje,
                    {
                        "Codigo": fila["Codigo"],
                        "Nombre": fila["Nombre"],
                        "PrincipioActivo": fila.get(
                            "Extra_II:PrincipioActivo",
                            "",
                        ),
                        "FormaFarmaceutica": fila.get(
                            "Extra_IV:FormaFarmaceutica",
                            "",
                        ),
                        "Puntaje": round(puntaje, 2),
                        "Marca": marca_candidata,
                        "Empaque": empaque,
                        "EsGenerico": bool(fila["_es_generico"]),
                        "Comercializable": bool(
                            fila["_comercializable"]
                        ),
                        "EsMuestra": bool(fila["_es_muestra"]),
                        "DiferenciaEmpaque": (
                            diferencia_empaque
                            if diferencia_empaque is not None
                            else 999999
                        ),
                        "Criterios": ", ".join(criterios),
                    },
                )
            )

        # La puntuación domina. En empates se prefiere:
        # comercializable, genérico cuando no hay marca, no muestra,
        # presentación más cercana y por último el código menor.
        resultados.sort(
            key=lambda item: (
                -item[0],
                not item[1]["Comercializable"],
                (
                    not item[1]["EsGenerico"]
                    if not marca_fuente
                    else False
                ),
                item[1]["EsMuestra"],
                item[1]["DiferenciaEmpaque"],
                item[1]["Codigo"],
            )
        )
        return resultados

    def resolver(
        self,
        codigo_actual: Any,
        nombre: Any,
        concentracion: Any,
        forma: Any,
        cantidad: Any,
    ) -> Dict[str, Any]:
        codigo_original = (
            ""
            if codigo_actual is None
            else str(codigo_actual).strip()
        )
        codigo_sin_espacios = re.sub(r"\s+", "", codigo_original)

        if codigo_sin_espacios in self.codigos:
            return {
                "codigo": codigo_sin_espacios,
                "estado": "IUM_VALIDO_CONSERVADO",
                "detalle": (
                    "El código ya existe y está habilitado en la tabla IUM."
                ),
                "candidatos": [],
            }

        nombre_compacto = _compactar(nombre)
        equivalencia_directa = MAPA_IUM_DIRECTO_SURA.get(
            nombre_compacto
        )

        if equivalencia_directa:
            codigo_directo = equivalencia_directa["codigo"]

            if codigo_directo not in self.codigos:
                raise ValueError(
                    "La equivalencia directa "
                    f"{nombre_compacto} -> {codigo_directo} no existe o "
                    "no está habilitada en TablaReferenciaIUM.xlsx."
                )

            return {
                "codigo": codigo_directo,
                "estado": "IUM_EQUIVALENTE_SELECCIONADO",
                "detalle": (
                    "Se aplicó una equivalencia directa validada por "
                    "principio activo, concentración, vía y presentación: "
                    + equivalencia_directa["descripcion"]
                    + "."
                ),
                "candidatos": [],
            }

        cum = None
        coincidencia_cum = PATRON_CUM_INICIAL.match(codigo_original)
        if coincidencia_cum:
            cum = coincidencia_cum.group(1)

        nota_aproximacion = (
            _nota_equivalencia_aproximada_ium(nombre)
        )

        candidatos = self._candidatos(
            str(nombre or ""),
            concentracion,
            forma,
            cantidad,
        )

        codigos_unicos = []
        vistos = set()
        for _, candidato in candidatos:
            if candidato["Codigo"] in vistos:
                continue
            codigos_unicos.append(candidato)
            vistos.add(candidato["Codigo"])

        if codigos_unicos:
            mejor = codigos_unicos[0]
            alternativas = codigos_unicos[:5]

            return {
                "codigo": mejor["Codigo"],
                "estado": "IUM_EQUIVALENTE_SELECCIONADO",
                "detalle": (
                    (
                        nota_aproximacion + " "
                        if nota_aproximacion
                        else ""
                    )
                    + "Se seleccionó el IUM habilitado más semejante por "
                    "principio activo, concentración, forma farmacéutica, "
                    "vía, condición comercial y presentación. "
                    f"Puntaje: {mejor['Puntaje']}. "
                    f"Criterios: {mejor['Criterios']}."
                ),
                "candidatos": alternativas,
            }

        if cum:
            return {
                "codigo": cum,
                "estado": "CUM_LIMPIADO_SIN_IUM",
                "detalle": (
                    "No se encontró un IUM equivalente en la tabla. "
                    "Se conservó el CUM limpio."
                ),
                "candidatos": [],
            }

        return {
            "codigo": codigo_sin_espacios or codigo_original,
            "estado": "SIN_COINCIDENCIA",
            "detalle": (
                "No se encontró IUM por nombre, concentración y forma, "
                "y no se pudo extraer un CUM del código original."
            ),
            "candidatos": [],
        }




class CatalogoCUPS:
    """Consulta la tabla CUPS oficial sin depender de openpyxl."""

    def __init__(self, ruta_excel: str | os.PathLike[str]):
        ruta = Path(ruta_excel)
        if not ruta.exists():
            raise FileNotFoundError(
                "No se encontró TablaReferencia_CUPS.xlsx en la carpeta "
                "'Generador Json'."
            )

        df = _leer_xlsx_sin_openpyxl(str(ruta))

        columnas_requeridas = {
            "Codigo",
            "Nombre",
            "Habilitado",
            "Descripcion",
            "Extra_VII",
            "Extra_VIII",
            "Extra_IX",
        }
        faltantes = columnas_requeridas.difference(df.columns)
        if faltantes:
            raise ValueError(
                "La tabla CUPS no contiene estas columnas requeridas: "
                + ", ".join(sorted(faltantes))
            )

        df = df.fillna("")
        df["Codigo"] = (
            df["Codigo"]
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .str.zfill(6)
        )
        df["Habilitado"] = (
            df["Habilitado"].astype(str).map(_normalizar_texto)
        )

        # Se conservan solamente registros habilitados.
        df = df[df["Habilitado"] == "SI"].copy()

        self.registros: Dict[str, Dict[str, Any]] = {}
        for _, fila in df.iterrows():
            codigo = str(fila["Codigo"]).strip()
            if not codigo:
                continue

            self.registros[codigo] = {
                "Codigo": codigo,
                "Nombre": str(fila.get("Nombre", "")).strip(),
                "Descripcion": str(
                    fila.get("Descripcion", "")
                ).strip(),
                "Categoria": str(
                    fila.get("Extra_IX", "")
                ).strip(),
                "Grupo": str(
                    fila.get("Extra_VIII", "")
                ).strip(),
                "Subgrupo": str(
                    fila.get("Extra_VII", "")
                ).strip(),
            }

    def obtener(self, codigo: Any) -> Optional[Dict[str, Any]]:
        cups = str(codigo or "").strip().replace(".0", "").zfill(6)
        return self.registros.get(cups)

    def resolver_servicio(
        self,
        codigo: Any,
    ) -> Optional[Dict[str, Any]]:
        """Infiere servicio, grupo y finalidad según el CUPS.

        Las reglas explícitas del proyecto siguen teniendo prioridad.
        La tabla permite resolver automáticamente familias completas,
        evitando agregar individualmente cada examen de laboratorio.
        """
        registro = self.obtener(codigo)
        if registro is None:
            return None

        texto_clasificacion = _normalizar_texto(
            " ".join(
                [
                    registro["Nombre"],
                    registro["Descripcion"],
                    registro["Categoria"],
                    registro["Grupo"],
                    registro["Subgrupo"],
                ]
            )
        )

        # Capítulo 17 / Laboratorio clínico:
        # servicio REPS 706, apoyo diagnóstico, finalidad diagnóstico.
        if "LABORATORIO CLINICO" in texto_clasificacion:
            return {
                "codServicio": 706,
                "grupoServicios": "02",
                "finalidadTecnologiaSalud": "15",
                "descripcion": registro["Nombre"],
                "origen": "TablaReferencia_CUPS.xlsx",
            }

        return {
            "sinRegla": True,
            "descripcion": registro["Nombre"],
            "categoria": registro["Categoria"],
        }



FAMILIAS_CUPS_LABORATORIO: Dict[str, str] = {
    "902": "HEMATOLOGÍA",
    "903": "QUÍMICA SANGUÍNEA Y DE OTROS FLUIDOS CORPORALES",
    "904": "ENDOCRINOLOGÍA",
    "905": "MONITOREO DE MEDICAMENTOS Y TOXICOLOGÍA",
    "906": "MICROBIOLOGÍA",
    "907": "INMUNOLOGÍA",
    "908": "ANATOMÍA PATOLÓGICA Y CITOLOGÍA",
    "909": "OTROS PROCEDIMIENTOS DE LABORATORIO",
}


def _configuracion_provisional_cups(
    codigo: Any,
) -> Optional[Dict[str, Any]]:
    """Obtiene una clasificación provisional por familia CUPS.

    Esta función no reemplaza la tabla oficial. Permite que un lote continúe
    cuando un examen de laboratorio no aparece todavía en el XLSX local.
    """
    cups = str(codigo or "").strip().replace(".0", "").zfill(6)
    familia = cups[:3]
    nombre_familia = FAMILIAS_CUPS_LABORATORIO.get(familia)

    if not nombre_familia:
        return None

    return {
        "codServicio": 706,
        "grupoServicios": "02",
        "finalidadTecnologiaSalud": "15",
        "descripcion": nombre_familia,
        "origen": "clasificación provisional por prefijo",
        "provisional": True,
    }


# Correspondencia inicial para los procedimientos facturados por SURA.
# Los códigos de servicio corresponden a la tabla de servicios de
# habilitación:
# - 890613: Terapia ocupacional -> servicio 728.
# - 943102: Psicología -> servicio 344.
MAPA_CONSULTAS_SURA: Dict[str, Dict[str, Any]] = {
    # Consulta de control o seguimiento por psiquiatría.
    "890384": {
        "codServicio": 345,
        "grupoServicios": "01",
        "finalidadTecnologiaSalud": "16",
        "descripcion": "PSIQUIATRIA - TRATAMIENTO",
    },
    # Asistencia intrahospitalaria por trabajo social.
    # Se relaciona con el servicio donde se presta: hospitalización
    # en salud mental.
    "890609": {
        "codServicio": 131,
        "grupoServicios": "03",
        "finalidadTecnologiaSalud": "16",
        "descripcion": "HOSPITALIZACION EN SALUD MENTAL",
    },
}


MAPA_CUPS_SERVICIO_SURA: Dict[str, Dict[str, Any]] = {
    "890613": {
        "codServicio": 728,
        "grupoServicios": "02",
        "finalidadTecnologiaSalud": "17",
        "descripcion": "TERAPIA OCUPACIONAL - REHABILITACION",
    },
    "943102": {
        "codServicio": 344,
        "grupoServicios": "01",
        "finalidadTecnologiaSalud": "16",
        "descripcion": "PSICOLOGIA - TRATAMIENTO",
    },
}


class CreadorJsonRipsSura(CreadorJsonRips):
    def __init__(
        self,
        regimen: Optional[str] = None,
        regimen_por_factura: Optional[Dict[str, str]] = None,
        numero_documento_profesional: Optional[str] = None,
        ruta_tabla_ium: Optional[str] = None,
        ruta_tabla_cups: Optional[str] = None,
        registro_medico_prescriptor: Optional[str] = None,
    ):
        super().__init__()

        self.regimen = None
        if regimen:
            regimen_normalizado = regimen.strip().lower()
            if regimen_normalizado not in (
                "contributivo",
                "subsidiado",
            ):
                raise ValueError(
                    "El régimen debe ser Contributivo o Subsidiado."
                )
            self.regimen = regimen_normalizado

        self.regimen_por_factura: Dict[str, str] = {}
        for factura, valor_regimen in (
            regimen_por_factura or {}
        ).items():
            factura_normalizada = normalizar_factura(str(factura))
            regimen_normalizado = str(valor_regimen).strip().lower()

            if regimen_normalizado not in (
                "contributivo",
                "subsidiado",
            ):
                raise ValueError(
                    f"Régimen inválido para {factura_normalizada}: "
                    f"{valor_regimen}."
                )

            self.regimen_por_factura[factura_normalizada] = (
                regimen_normalizado
            )

        if not self.regimen and not self.regimen_por_factura:
            raise ValueError(
                "Cargue los XML corregidos para detectar el régimen "
                "de cada factura."
            )

        # Documento real del profesional/especialista.
        # El tipo queda fijo en CC y el número se captura en la interfaz.
        # registro_medico_prescriptor se conserva solo como alias de
        # compatibilidad con versiones anteriores del módulo.
        numero_recibido = (
            numero_documento_profesional
            or registro_medico_prescriptor
            or ""
        )
        self.tipo_documento_profesional = "CC"
        self.numero_documento_profesional = (
            str(numero_recibido).strip() or None
        )

        if ruta_tabla_ium is None:
            ruta_tabla_ium = str(
                Path(__file__).with_name("TablaReferenciaIUM.xlsx")
            )

        if ruta_tabla_cups is None:
            ruta_tabla_cups = str(
                Path(__file__).with_name("TablaReferencia_CUPS.xlsx")
            )

        self.catalogo_ium = CatalogoIUM(ruta_tabla_ium)
        self.catalogo_cups = CatalogoCUPS(ruta_tabla_cups)
        self.ultimo_reporte_ium: List[Dict[str, Any]] = []
        self.advertencias: List[str] = []
        self.errores_factura: List[Dict[str, str]] = []

    def _regimen_de_factura(self, numero_factura: str) -> str:
        factura_normalizada = normalizar_factura(numero_factura)

        if factura_normalizada in self.regimen_por_factura:
            return self.regimen_por_factura[factura_normalizada]

        if self.regimen:
            return self.regimen

        raise ValueError(
            f"No se cargó un XML corregido para la factura "
            f"{factura_normalizada}."
        )

    @staticmethod
    def zip_contiene_medicamentos(zip_bytes: bytes) -> bool:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archivo_zip:
            return any(
                os.path.basename(nombre).upper().startswith("AM")
                and nombre.lower().endswith(".txt")
                for nombre in archivo_zip.namelist()
            )

    def _contexto_hospitalario(
        self,
        usuario: Dict[str, Any],
    ) -> Dict[str, Any]:
        servicios = usuario.get("servicios", {})
        hospitalizaciones = servicios.get("hospitalizacion", [])

        fecha_inicio = None
        fecha_egreso = None
        diagnostico = None
        diagnostico_relacionado = None

        if hospitalizaciones:
            hosp = hospitalizaciones[0]
            fecha_inicio = hosp.get("fechaInicioAtencion")
            fecha_egreso = hosp.get("fechaEgreso")
            diagnostico = hosp.get("codDiagnosticoPrincipal")
            diagnostico_relacionado = hosp.get("codDiagnosticoPrincipalE")

        if not diagnostico:
            consultas = servicios.get("consultas", [])
            if consultas:
                diagnostico = consultas[0].get("codDiagnosticoPrincipal")

        if not diagnostico:
            procedimientos = servicios.get("procedimientos", [])
            if procedimientos:
                diagnostico = procedimientos[0].get(
                    "codDiagnosticoPrincipal"
                )

        dias = 30
        if fecha_inicio and fecha_egreso:
            try:
                inicio = pd.to_datetime(fecha_inicio, errors="raise")
                egreso = pd.to_datetime(fecha_egreso, errors="raise")
                diferencia = max(
                    1,
                    math.ceil(
                        (egreso - inicio).total_seconds() / 86400
                    ),
                )
                dias = min(999, diferencia)
            except Exception:
                pass

        return {
            "fecha_inicio": fecha_inicio,
            "diagnostico": diagnostico or "Z000",
            "diagnostico_relacionado": (
                diagnostico_relacionado
                if diagnostico_relacionado
                and diagnostico_relacionado != diagnostico
                else None
            ),
            "dias": dias,
        }

    def _ajustar_diagnosticos_sura(
        self,
        factura: str,
        usuario: Dict[str, Any],
    ) -> None:
        """Normaliza todos los diagnósticos CIE-10 de los servicios."""
        cambios = []

        for nombre_lista, registros in usuario.get(
            "servicios",
            {},
        ).items():
            if not isinstance(registros, list):
                continue

            for indice, registro in enumerate(registros, start=1):
                if not isinstance(registro, dict):
                    continue

                for campo, valor in list(registro.items()):
                    if not campo.startswith("codDiagnostico"):
                        continue
                    if campo.endswith("CIE11"):
                        continue
                    if valor in (None, ""):
                        continue

                    normalizado = _normalizar_diagnostico_sura(valor)
                    if normalizado != valor:
                        registro[campo] = normalizado
                        cambios.append(
                            f"{nombre_lista}[{indice}].{campo}: "
                            f"{valor} -> {normalizado}"
                        )

                # Después de normalizar, un diagnóstico relacionado no puede
                # ser igual al principal. También se eliminan relacionados
                # repetidos dentro del mismo servicio.
                diagnosticos_principales = {
                    str(valor).strip()
                    for campo, valor in registro.items()
                    if campo.startswith("codDiagnosticoPrincipal")
                    and not campo.endswith("CIE11")
                    and valor not in (None, "")
                }

                relacionados_vistos = set()
                for campo, valor in list(registro.items()):
                    if not campo.startswith("codDiagnosticoRelacionado"):
                        continue
                    if campo.endswith("CIE11"):
                        continue
                    if valor in (None, ""):
                        continue

                    codigo = str(valor).strip()

                    if codigo in diagnosticos_principales:
                        registro[campo] = None
                        cambios.append(
                            f"{nombre_lista}[{indice}].{campo}: "
                            f"{codigo} -> null "
                            "(igual al diagnóstico principal)"
                        )
                        continue

                    if codigo in relacionados_vistos:
                        registro[campo] = None
                        cambios.append(
                            f"{nombre_lista}[{indice}].{campo}: "
                            f"{codigo} -> null "
                            "(diagnóstico relacionado repetido)"
                        )
                        continue

                    relacionados_vistos.add(codigo)

        if cambios:
            self.advertencias.append(
                f"{factura}: diagnósticos normalizados: "
                + " | ".join(cambios[:10])
                + (
                    f" | y {len(cambios) - 10} cambio(s) adicional(es)"
                    if len(cambios) > 10
                    else ""
                )
            )

    @staticmethod
    def _registros_con_recaudo(
        factura: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Lista servicios que contienen campos de pago moderador."""
        orden = (
            "consultas",
            "procedimientos",
            "medicamentos",
            "otrosServicios",
            "urgencias",
        )
        resultado: List[Dict[str, Any]] = []

        for usuario in factura.get("usuarios", []):
            servicios = usuario.get("servicios", {})
            for nombre_lista in orden:
                for registro in servicios.get(nombre_lista, []) or []:
                    if (
                        isinstance(registro, dict)
                        and "valorPagoModerador" in registro
                        and "conceptoRecaudo" in registro
                    ):
                        resultado.append(registro)

        return resultado

    def _aplicar_recaudos_af(
        self,
        numero_factura: str,
        factura: Dict[str, Any],
    ) -> None:
        """Hace coincidir el total RIPS con copago/cuota del archivo AF.

        Los RIPS planos pueden informar el recaudo solo en AF y dejar cero en
        todos los registros AC/AP/AM/AT. En el JSON vigente el total debe
        aparecer en los detalles de servicios, por lo que se registra una sola
        vez por concepto y se evita duplicarlo.
        """
        factura_normalizada = normalizar_factura(numero_factura)
        recaudos = self._recaudos_af_actuales.get(
            factura_normalizada,
            {"01": 0, "02": 0},
        )

        objetivos = [
            (codigo, int(valor))
            for codigo, valor in recaudos.items()
            if int(valor) > 0
        ]

        registros = self._registros_con_recaudo(factura)
        total_existente = sum(
            int(registro.get("valorPagoModerador") or 0)
            for registro in registros
        )
        total_af = sum(valor for _, valor in objetivos)

        if total_existente not in (0, total_af):
            raise ValueError(
                f"{factura_normalizada}: el total de pagos moderadores "
                f"en los servicios ({total_existente}) no coincide con "
                f"el archivo AF ({total_af})."
            )

        if len(registros) < len(objetivos):
            raise ValueError(
                f"{factura_normalizada}: no existen suficientes servicios "
                "para informar separadamente los conceptos de recaudo del AF."
            )

        # Se reconstruye para evitar duplicados o clasificaciones antiguas.
        for registro in registros:
            registro["conceptoRecaudo"] = "05"
            registro["valorPagoModerador"] = 0
            registro["numFEVPagoModerador"] = None

        for registro, (concepto, valor) in zip(registros, objetivos):
            registro["conceptoRecaudo"] = concepto
            registro["valorPagoModerador"] = valor
            registro["numFEVPagoModerador"] = factura_normalizada

        if objetivos:
            descripcion = ", ".join(
                (
                    "COPAGO" if concepto == "01" else "CUOTA_MODERADORA"
                )
                + f"={valor}"
                for concepto, valor in objetivos
            )
            self.advertencias.append(
                f"{factura_normalizada}: recaudos AF aplicados una sola "
                f"vez en los detalles RIPS: {descripcion}."
            )

    def _ajustar_consultas(
        self,
        factura: str,
        usuario: Dict[str, Any],
    ) -> None:
        """Corrige servicio y finalidad de las consultas de SURA."""
        consultas = usuario.get("servicios", {}).get("consultas", [])
        if not consultas:
            return

        cups_sin_configuracion: List[str] = []

        for consulta in consultas:
            cups = str(consulta.get("codConsulta") or "").strip()
            configuracion = MAPA_CONSULTAS_SURA.get(cups)

            if not configuracion:
                cups_sin_configuracion.append(cups or "SIN_CUPS")
                continue

            consulta["codServicio"] = int(
                configuracion["codServicio"]
            )
            consulta["grupoServicios"] = str(
                configuracion["grupoServicios"]
            )
            consulta["finalidadTecnologiaSalud"] = str(
                configuracion["finalidadTecnologiaSalud"]
            )

        if cups_sin_configuracion:
            self.advertencias.append(
                f"{factura}: no se encontró configuración especial para "
                "estas consultas: "
                + ", ".join(sorted(set(cups_sin_configuracion)))
            )

    def _aplicar_documento_profesional(
        self,
        factura: str,
        usuario: Dict[str, Any],
    ) -> None:
        """Aplica el documento del especialista a los servicios clínicos.

        No modifica el documento del objeto usuario.

        Se actualizan todos los registros de servicios que incluyan los
        campos tipoDocumentoIdentificacion o numDocumentoIdentificacion:
        consultas, procedimientos, medicamentos, otrosServicios y cualquier
        otro objeto vigente que contenga esas propiedades.

        No se agregan propiedades desconocidas a objetos cuyo esquema no las
        define, como hospitalización cuando no las trae en su estructura.
        """
        if not self.numero_documento_profesional:
            raise ValueError(
                f"{factura}: ingrese el número de documento del "
                "profesional o especialista."
            )

        servicios = usuario.get("servicios", {})
        total_actualizados = 0

        for nombre_objeto, registros in servicios.items():
            if not isinstance(registros, list):
                continue

            for registro in registros:
                if not isinstance(registro, dict):
                    continue

                tiene_campos_profesional = (
                    "tipoDocumentoIdentificacion" in registro
                    or "numDocumentoIdentificacion" in registro
                )
                if not tiene_campos_profesional:
                    continue

                registro["tipoDocumentoIdentificacion"] = (
                    self.tipo_documento_profesional
                )
                registro["numDocumentoIdentificacion"] = (
                    self.numero_documento_profesional
                )
                total_actualizados += 1

        if total_actualizados:
            self.advertencias.append(
                f"{factura}: se aplicó el documento "
                f"CC {self.numero_documento_profesional} a "
                f"{total_actualizados} registro(s) de servicios."
            )

    def _ajustar_procedimientos(
        self,
        factura: str,
        usuario: Dict[str, Any],
    ) -> None:
        """Completa y valida los procedimientos de SURA.

        Los RIPS planos AP anteriores no contienen codServicio y en algunos
        casos llegan sin diagnóstico. Por eso:

        1. Se determina codServicio y grupoServicios según el CUPS.
        2. Si AP no trae diagnóstico, se toma el diagnóstico principal de
           la hospitalización del mismo usuario y factura.
        3. Se evita enviar el documento del paciente como si fuera el
           documento del profesional.
        4. La generación se detiene cuando queda algún dato obligatorio
           sin resolver.
        """
        servicios = usuario.get("servicios", {})
        procedimientos = servicios.get("procedimientos", [])
        if not procedimientos:
            return

        if not self.numero_documento_profesional:
            raise ValueError(
                f"{factura}: ingrese el número de documento del "
                "profesional o especialista."
            )

        contexto = self._contexto_hospitalario(usuario)
        diagnostico_fallback = str(
            contexto.get("diagnostico") or ""
        ).strip()
        diagnostico_relacionado = str(
            contexto.get("diagnostico_relacionado") or ""
        ).strip()

        # Z000 era solamente un valor interno de respaldo para medicamentos.
        # No debe utilizarse automáticamente en procedimientos.
        if diagnostico_fallback == "Z000":
            diagnostico_fallback = ""

        tipo_usuario = str(
            usuario.get("tipoDocumentoIdentificacion") or ""
        ).strip()
        numero_usuario = str(
            usuario.get("numDocumentoIdentificacion") or ""
        ).strip()

        cups_sin_servicio = []
        cups_sin_regla = []
        cups_actualizados = []
        procedimientos_sin_diagnostico = []
        documento_paciente_retirado = False

        for indice, procedimiento in enumerate(
            procedimientos,
            start=1,
        ):
            cups_original = str(
                procedimiento.get("codProcedimiento") or ""
            ).strip()
            cups = cups_original

            equivalencia = MAPA_CUPS_EQUIVALENTES_VIGENTES.get(
                cups_original
            )
            if equivalencia:
                cups = equivalencia["codigo"]
                procedimiento["codProcedimiento"] = cups
                cups_actualizados.append(
                    (
                        cups_original,
                        cups,
                        equivalencia["descripcion"],
                    )
                )

            configuracion = MAPA_CUPS_SERVICIO_SURA.get(cups)

            registro_cups = None
            configuracion_catalogo = None

            if configuracion is None:
                registro_cups = self.catalogo_cups.obtener(cups)

                if registro_cups is not None:
                    configuracion_catalogo = (
                        self.catalogo_cups.resolver_servicio(cups)
                    )
                    if (
                        configuracion_catalogo
                        and not configuracion_catalogo.get("sinRegla")
                    ):
                        configuracion = configuracion_catalogo

            codigo_actual = procedimiento.get("codServicio")
            try:
                codigo_actual_num = int(codigo_actual)
                codigo_actual_valido = (
                    100 <= codigo_actual_num <= 9999
                )
            except (TypeError, ValueError):
                codigo_actual_valido = False

            if configuracion:
                procedimiento["codServicio"] = int(
                    configuracion["codServicio"]
                )
                procedimiento["grupoServicios"] = str(
                    configuracion["grupoServicios"]
                )
                procedimiento["finalidadTecnologiaSalud"] = str(
                    configuracion["finalidadTecnologiaSalud"]
                )
            elif registro_cups is not None:
                cups_sin_regla.append(
                    (
                        cups,
                        registro_cups.get("Nombre", ""),
                        registro_cups.get("Categoria", ""),
                    )
                )
            else:
                cups_sin_servicio.append(
                    cups or f"registro {indice}"
                )

            diagnostico_actual = str(
                procedimiento.get("codDiagnosticoPrincipal") or ""
            ).strip()

            if not diagnostico_actual and diagnostico_fallback:
                procedimiento["codDiagnosticoPrincipal"] = (
                    diagnostico_fallback
                )
                diagnostico_actual = diagnostico_fallback

            if not diagnostico_actual:
                procedimientos_sin_diagnostico.append(
                    cups or f"registro {indice}"
                )

            if (
                not procedimiento.get("codDiagnosticoRelacionado")
                and diagnostico_relacionado
                and diagnostico_relacionado != diagnostico_actual
            ):
                procedimiento["codDiagnosticoRelacionado"] = (
                    diagnostico_relacionado
                )

            # En AP, las columnas 2 y 3 corresponden al paciente.
            # Se reemplazan por el documento real del profesional.
            tipo_profesional = str(
                procedimiento.get(
                    "tipoDocumentoIdentificacion"
                ) or ""
            ).strip()
            numero_profesional = str(
                procedimiento.get(
                    "numDocumentoIdentificacion"
                ) or ""
            ).strip()

            if (
                tipo_profesional == tipo_usuario
                and numero_profesional == numero_usuario
            ):
                documento_paciente_retirado = True

            procedimiento["tipoDocumentoIdentificacion"] = (
                self.tipo_documento_profesional
            )
            procedimiento["numDocumentoIdentificacion"] = (
                self.numero_documento_profesional
            )

        if cups_sin_servicio:
            cups_unicos = sorted(set(cups_sin_servicio))
            raise ValueError(
                "CUPS inexistentes o no habilitados en "
                "TablaReferencia_CUPS.xlsx: "
                + ", ".join(cups_unicos)
            )

        if cups_sin_regla:
            detalles = []
            vistos = set()

            for codigo, nombre, categoria in cups_sin_regla:
                if codigo in vistos:
                    continue
                vistos.add(codigo)
                detalles.append(
                    f"{codigo} ({nombre}; categoría: {categoria})"
                )

            raise ValueError(
                "CUPS existentes en la tabla, pero sin una regla segura "
                "para asignar codServicio: "
                + " | ".join(detalles)
            )

        if procedimientos_sin_diagnostico:
            cups_unicos = sorted(
                set(procedimientos_sin_diagnostico)
            )
            raise ValueError(
                f"{factura}: no fue posible obtener el diagnóstico "
                "principal para estos procedimientos: "
                + ", ".join(cups_unicos)
            )

        if cups_actualizados:
            equivalencias_unicas = []
            vistos_equivalencias = set()
            for anterior, vigente, descripcion in cups_actualizados:
                clave = (anterior, vigente)
                if clave in vistos_equivalencias:
                    continue
                vistos_equivalencias.add(clave)
                equivalencias_unicas.append(
                    f"{anterior} -> {vigente} ({descripcion})"
                )

            self.advertencias.append(
                f"{factura}: se actualizaron CUPS históricos: "
                + ", ".join(equivalencias_unicas)
            )

        if documento_paciente_retirado:
            self.advertencias.append(
                f"{factura}: se retiró de los procedimientos el "
                "documento del paciente y se reemplazó por "
                f"CC {self.numero_documento_profesional}, correspondiente "
                "al profesional o especialista."
            )

    def _ajustar_fechas_otros_servicios(
        self,
        factura: str,
        usuario: Dict[str, Any],
    ) -> None:
        """Evita fechas fuera del periodo real de hospitalización.

        Los RIPS AT antiguos no incluyen fecha. El generador general usa
        fechas auxiliares y, para algunos insumos, puede terminar usando la
        fecha de expedición de la factura. Cuando esa fecha queda fuera del
        periodo de atención, se reemplaza por la fecha de ingreso.
        """
        servicios = usuario.get("servicios", {})
        otros = servicios.get("otrosServicios", [])
        hospitalizaciones = servicios.get("hospitalizacion", [])

        if not otros or not hospitalizaciones:
            return

        hospitalizacion = hospitalizaciones[0]
        inicio_texto = hospitalizacion.get("fechaInicioAtencion")
        fin_texto = hospitalizacion.get("fechaEgreso")

        if not inicio_texto or not fin_texto:
            return

        try:
            inicio = datetime.strptime(
                str(inicio_texto),
                "%Y-%m-%d %H:%M",
            )
            fin = datetime.strptime(
                str(fin_texto),
                "%Y-%m-%d %H:%M",
            )
        except ValueError:
            return

        corregidos = 0

        for servicio in otros:
            fecha_texto = servicio.get("fechaSuministroTecnologia")
            if not fecha_texto:
                servicio["fechaSuministroTecnologia"] = (
                    inicio.strftime("%Y-%m-%d %H:%M")
                )
                corregidos += 1
                continue

            try:
                fecha = datetime.strptime(
                    str(fecha_texto),
                    "%Y-%m-%d %H:%M",
                )
            except ValueError:
                servicio["fechaSuministroTecnologia"] = (
                    inicio.strftime("%Y-%m-%d %H:%M")
                )
                corregidos += 1
                continue

            if fecha < inicio or fecha > fin:
                servicio["fechaSuministroTecnologia"] = (
                    inicio.strftime("%Y-%m-%d %H:%M")
                )
                corregidos += 1

        if corregidos:
            self.advertencias.append(
                f"{factura}: se corrigieron {corregidos} fecha(s) de "
                "otrosServicios que estaban fuera del periodo de atención."
            )

    def _ajustar_medicamentos(
        self,
        factura: str,
        usuario: Dict[str, Any],
    ) -> None:
        """Reconstruye el objeto medicamentos con los campos vigentes.

        Documento Técnico 1 - Resolución 948 de 2026:
        - M02 numAutorizacion fue eliminado.
        - M16/M17 identifican el documento personal del prescriptor.
        - El registro médico no tiene una propiedad propia en el JSON.
        - codigoVIDA se mantiene en null mientras no esté implementado.
        """
        servicios = usuario.get("servicios", {})
        medicamentos = servicios.get("medicamentos", [])
        if not medicamentos:
            return

        if not self.numero_documento_profesional:
            raise ValueError(
                f"{factura}: ingrese el número de documento del "
                "profesional o especialista."
            )

        contexto = self._contexto_hospitalario(usuario)
        usuario_id = (
            f"{usuario.get('tipoDocumentoIdentificacion', '')}-"
            f"{usuario.get('numDocumentoIdentificacion', '')}"
        )

        medicamentos_actualizados: List[Dict[str, Any]] = []
        medicamentos_sin_codigo_valido: List[str] = []

        for indice, medicamento in enumerate(medicamentos, start=1):
            codigo_original = str(
                medicamento.get("codTecnologiaSalud") or ""
            )
            nombre_original = str(
                medicamento.get("nomTecnologiaSalud") or ""
            )

            decision = self.catalogo_ium.resolver(
                codigo_actual=codigo_original,
                nombre=nombre_original,
                concentracion=medicamento.get(
                    "concentracionMedicamento"
                ),
                forma=medicamento.get("formaFarmaceutica"),
                cantidad=medicamento.get("cantidadMedicamento"),
            )

            tipo_medicamento = str(
                medicamento.get("tipoMedicamento") or "01"
            ).zfill(2)

            try:
                cantidad = int(
                    float(medicamento.get("cantidadMedicamento") or 0)
                )
            except (TypeError, ValueError):
                cantidad = 0
            cantidad = max(1, cantidad)

            try:
                valor_unitario = int(
                    float(medicamento.get("vrUnitMedicamento") or 0)
                )
            except (TypeError, ValueError):
                valor_unitario = 0
            valor_unitario = max(0, valor_unitario)

            # Pago por evento: RVC094 valida cantidad * valor unitario.
            valor_servicio = cantidad * valor_unitario

            try:
                unidad_minima = int(
                    float(medicamento.get("unidadMinDispensa") or 1)
                )
            except (TypeError, ValueError):
                unidad_minima = 1
            unidad_minima = max(1, unidad_minima)

            try:
                dias_tratamiento = int(contexto["dias"])
            except (TypeError, ValueError):
                dias_tratamiento = 1
            dias_tratamiento = min(999, max(1, dias_tratamiento))

            # Los datos clínicos y de presentación se toman del AM.
            # No se reemplazan por valores fijos cuando la fuente ya los trae.
            nombre_tecnologia = nombre_original or None
            concentracion, unidad_medida = (
                _normalizar_concentracion_unidad_am(
                    medicamento.get("concentracionMedicamento"),
                    medicamento.get("unidadMedida"),
                )
            )
            forma_farmaceutica = _forma_farmaceutica_am(
                medicamento.get("formaFarmaceutica")
            )

            actualizado = {
                "codPrestador": medicamento.get("codPrestador"),
                "idMIPRES": medicamento.get("idMIPRES"),
                "fechaDispensAdmon": (
                    contexto["fecha_inicio"]
                    or medicamento.get("fechaDispensAdmon")
                ),
                "codDiagnosticoPrincipal": contexto["diagnostico"],
                "codDiagnosticoPrincipalCIE11": None,
                "nomCodDiagnosticoPrincipalCIE11": None,
                "codDiagnosticoRelacionado": contexto[
                    "diagnostico_relacionado"
                ],
                "codDiagnosticoRelacionadoCIE11": None,
                "nomCodDiagnosticoRelacionadoCIE11": None,
                "tipoMedicamento": tipo_medicamento,
                "codTecnologiaSalud": decision["codigo"],
                "nomTecnologiaSalud": nombre_tecnologia,
                "concentracionMedicamento": concentracion,
                "unidadMedida": unidad_medida,
                "formaFarmaceutica": forma_farmaceutica,
                "unidadMinDispensa": unidad_minima,
                "cantidadMedicamento": cantidad,
                "diasTratamiento": dias_tratamiento,
                "tipoDocumentoIdentificacion": (
                    self.tipo_documento_profesional
                ),
                "numDocumentoIdentificacion": (
                    self.numero_documento_profesional
                ),
                "vrUnitMedicamento": valor_unitario,
                "vrDispensacion": 0,
                "vrServicio": valor_servicio,
                "conceptoRecaudo": medicamento.get(
                    "conceptoRecaudo"
                ) or "05",
                "valorPagoModerador": int(
                    medicamento.get("valorPagoModerador") or 0
                ),
                "numFEVPagoModerador": medicamento.get(
                    "numFEVPagoModerador"
                ),
                "consecutivo": indice,
                "codigoVIDA": None,
            }

            medicamentos_actualizados.append(actualizado)

            if (
                medicamento.get("unidadMedida")
                and unidad_medida == 0
            ):
                self.advertencias.append(
                    f"{factura}: no se pudo convertir la unidad de medida "
                    f"'{medicamento.get('unidadMedida')}' del medicamento "
                    f"'{nombre_original}' a un código UMM."
                )

            candidatos_texto = " | ".join(
                f"{c['Codigo']}: {c['Nombre']}"
                for c in decision["candidatos"]
            )
            self.ultimo_reporte_ium.append(
                DecisionIUM(
                    factura=factura,
                    usuario=usuario_id,
                    medicamento=nombre_original,
                    codigo_original=codigo_original,
                    codigo_final=decision["codigo"],
                    estado=decision["estado"],
                    detalle=decision["detalle"],
                    candidatos=candidatos_texto,
                    numero_documento_profesional=(
                        self.numero_documento_profesional
                    ),
                ).to_dict()
            )

            if decision["estado"] == "SIN_COINCIDENCIA":
                medicamentos_sin_codigo_valido.append(
                    f"{nombre_original} ({codigo_original or 'SIN CODIGO'})"
                )

        servicios["medicamentos"] = medicamentos_actualizados

        if medicamentos_sin_codigo_valido:
            raise ValueError(
                "Medicamentos sin IUM o CUM válido en la tabla: "
                + ", ".join(medicamentos_sin_codigo_valido)
                + ". Se requiere el CUM/IUM exacto del producto; no se "
                "reemplazó por otra concentración o forma farmacéutica."
            )

        if self.numero_documento_profesional:
            self.advertencias.append(
                f"{factura}: en medicamentos se asignó el documento "
                f"CC {self.numero_documento_profesional} al profesional."
            )

    def _procesar_factura_sura(
        self,
        numero_factura: str,
        factura: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Procesa una sola factura manteniendo la auditoría completa.

        Primero se revisan todos los medicamentos. Después se validan
        régimen, consultas, procedimientos y demás servicios. De esta forma,
        si la factura falla por un CUPS o por otro dato, sus medicamentos
        permanecen en el reporte de auditoría.
        """
        usuarios = factura.get("usuarios", [])

        # Primera fase: normalización común y auditoría de todos los AM.
        for usuario in usuarios:
            for nombre_lista, lista_servicios in usuario.get(
                "servicios", {}
            ).items():
                for servicio in lista_servicios:
                    if "numAutorizacion" in servicio:
                        servicio["numAutorizacion"] = (
                            _normalizar_autorizacion(
                                servicio.get("numAutorizacion")
                            )
                        )
                    if nombre_lista == "otrosServicios":
                        servicio.setdefault("vrDispensacion", 0)
                        servicio["codigoVIDA"] = None

            # Los diagnósticos se normalizan antes de usarlos como
            # contexto de medicamentos y procedimientos.
            self._ajustar_diagnosticos_sura(
                numero_factura,
                usuario,
            )

            # Debe ejecutarse antes de cualquier validación que pueda
            # detener la factura.
            self._ajustar_medicamentos(
                numero_factura,
                usuario,
            )

        # Segunda fase: validaciones y ajustes restantes.
        regimen_factura = self._regimen_de_factura(numero_factura)

        for usuario in usuarios:
            tipo_actual = str(
                usuario.get("tipoUsuario") or ""
            ).zfill(2)

            if regimen_factura == "contributivo":
                usuario["tipoUsuario"] = (
                    tipo_actual
                    if tipo_actual in ("01", "02", "03")
                    else "01"
                )
            else:
                usuario["tipoUsuario"] = "04"

            self._ajustar_consultas(
                numero_factura,
                usuario,
            )
            self._ajustar_procedimientos(
                numero_factura,
                usuario,
            )
            self._ajustar_fechas_otros_servicios(
                numero_factura,
                usuario,
            )
            self._aplicar_documento_profesional(
                numero_factura,
                usuario,
            )

        return factura

    def generar_desde_zip(
        self,
        zip_bytes: bytes,
    ) -> Dict[str, Dict[str, Any]]:
        self.ultimo_reporte_ium = []
        self.advertencias = []
        self.errores_factura = []
        self._recaudos_af_actuales = _extraer_recaudos_af_sura(
            zip_bytes
        )

        facturas_origen = super().generar_desde_zip(zip_bytes)
        facturas_validas: Dict[str, Dict[str, Any]] = {}

        for numero_factura, factura_origen in facturas_origen.items():
            factura_normalizada = normalizar_factura(numero_factura)

            inicio_reporte_ium = len(self.ultimo_reporte_ium)
            inicio_advertencias = len(self.advertencias)

            try:
                factura_trabajo = copy.deepcopy(factura_origen)
                factura_lista = self._procesar_factura_sura(
                    factura_normalizada,
                    factura_trabajo,
                )
                self._aplicar_recaudos_af(
                    factura_normalizada,
                    factura_lista,
                )
                facturas_validas[factura_normalizada] = factura_lista

                for registro in self.ultimo_reporte_ium[
                    inicio_reporte_ium:
                ]:
                    registro["factura_generada"] = "SI"
                    registro["error_factura"] = ""

            except Exception as exc:
                error_texto = str(exc)

                # Se retiran advertencias parciales, pero NO la auditoría de
                # medicamentos producida antes del error.
                del self.advertencias[inicio_advertencias:]

                for registro in self.ultimo_reporte_ium[
                    inicio_reporte_ium:
                ]:
                    registro["factura_generada"] = "NO"
                    registro["error_factura"] = error_texto

                self.errores_factura.append(
                    {
                        "factura": factura_normalizada,
                        "error": error_texto,
                    }
                )

        return facturas_validas

