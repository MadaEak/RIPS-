"""
Corrector de Facturas Electrónicas de Salud (FEV-RIPS) - EPS Mutual
==============================================================

Completa y corrige facturas electrónicas de salud entregadas "incompletas"
(formato AttachedDocument de la DIAN) usando como plantilla un XML de
referencia válido (el provisto por la Resolución / por la EPS).

Principios:
  * NO se toca la firma electrónica (Signature, SignatureValue, KeyInfo,
    DigestValue) que vive en el AttachedDocument, fuera de la factura.
  * Se conservan los datos propios de la factura incompleta (ID, fechas,
    totales, autorización, CustomizationID, datos de emisor/receptor).
  * Se insertan los campos faltantes copiando la estructura de la plantilla.
  * Solo se sobrescriben valores de CONFIGURACIÓN DEL EMISOR (lista blanca),
    nunca los que dependen del pagador / tipo de factura.
  * El CUCON se coloca en <Name>NUMERO_CONTRATO</Name><Value>...</Value>.

Referencia normativa: Resolución 0948 de 2026 (Art. 11 num. 4: CUCON en lugar
del número de contrato).
"""

from __future__ import annotations

import copy
import csv
import io
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from lxml import etree

# ---------------------------------------------------------------------------
# Namespaces relevantes
# ---------------------------------------------------------------------------
NS = {
    "adb": "urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2",
    "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
    "dian": "dian:gov:co:facturaelectronica:Structures-2-1",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}

XP_DESCRIPTION = (
    ".//{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}"
    "Attachment/"
    "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}"
    "ExternalReference/"
    "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Description"
)

# Tags que NUNCA deben copiarse de la plantilla (firma electrónica y derivados).
EXCLUIR_TAGS = {
    "Signature", "SignatureValue", "KeyInfo", "DigestValue",
    "QualifyingProperties", "SignedProperties", "SignedSignatureProperties",
    "SignedDataObjectProperties", "Object", "X509Certificate", "X509IssuerSerial",
    "X509IssuerName", "X509SerialNumber", "SignatureMethod", "SignaturePolicyId",
    "SignaturePolicyIdentifier", "SigPolicyId", "CanonicalizationMethod",
    "Transforms", "Transform", "Reference",
}

# Rutas (por tag local) de valores de CONFIGURACIÓN DEL EMISOR que SÍ deben
# tomarse de la plantilla (porque son fijos del facturador). Se expresan como
# subruta de tags locales desde la raíz de la Invoice.
# SOLO inserta si faltan; no reemplaza (para eso está CONFIG_SOBRESCRIBIR).
CONFIG_FIJA = {
    "AccountingCustomerParty/TaxScheme/ID",
    "AccountingCustomerParty/TaxScheme/Name",
    "AccountingCustomerParty/TaxLevelCode",
    "FabricanteSoftware",
    "NotificationPreferences",
    "ForeignCurrencyExtension",
}

# Rutas del emisor (CEMIC/AccountingSupplierParty) cuyos valores deben
# SOBRESCRIBIRSE desde la plantilla aunque ya existan en la factura incompleta,
# porque la factura incompleta puede traerlos incorrectos:
#   TaxScheme/ID: 01 (IVA) -> debe ser ZZ (No aplica, exento)
#   TaxScheme/Name: IVA -> debe ser No aplica
# NOTA: CorporateRegistrationScheme/ID (FE vs FEC) NO se toca porque depende
# del consecutivo de la factura, no es un campo de configuración del emisor.
CONFIG_SOBRESCRIBIR = {
    "AccountingSupplierParty/Party/PartyTaxScheme/TaxScheme/ID",
    "AccountingSupplierParty/Party/PartyTaxScheme/TaxScheme/Name",
}

# Rutas que NUNCA deben sobrescribirse de la plantilla (conservar lo propio)
# (por si acaso el merge intenta tocarlas).
NO_SOBRESCRIBIR = {
    "CustomizationID",
    "ID",
    "UUID",
    "IssueDate",
    "IssueTime",
    "LineExtensionAmount",
    "TaxExclusiveAmount",
    "PayableAmount",
    "InvoiceLine/InvoicedQuantity",
    "InvoiceLine/LineExtensionAmount",
    "InvoiceLine/Price",
    "AccountingCustomerParty",
}

# Tag local que envuelve la factura embebida dentro del Description
RE_INVOICE = re.compile(r"<Invoice\b.*?</Invoice>", re.DOTALL)

VERSION_CORRECTOR_XML_MUTUAL = (
    "2026.07.19-v4-descripciones-at-truncadas"
)

TRATAMIENTOS_CONTRATADOS_MUTUAL: Dict[str, Dict[str, Any]] = {
    "132P01": {
        "descripcion": (
            "INTERNACIÓN PARCIAL EN HOSPITAL (HOSPITAL DÍA) "
            "PSIQUIATRÍA GENERAL"
        ),
        "obligatorias": ("INTERNACION", "PARCIAL"),
        "alternativas": (
            ("HOSPITAL", "HOSPITALARIA", "INSTITUCION"),
            ("PSIQUIATRIA", "HOSPITAL DIA", "INSTITUCION HOSPITALARIA"),
        ),
    },
    "135M02": {
        "descripcion": (
            "INTERNACIÓN HOSPITALARIA EN EL CONSUMIDOR DE "
            "SUSTANCIAS PSICOACTIVAS"
        ),
        "obligatorias": ("INTERNACION",),
        "alternativas": (
            (
                "SUSTANCIAS PSICOACTIVAS",
                "CONSUMO DE SUSTANCIAS",
                "FARMACODEPENDENCIA",
            ),
            ("HOSPITALARIA", "COMPLEJIDAD MEDIANA", "HABITACION MULTIPLE"),
        ),
    },
    "131M02": {
        "descripcion": (
            "INTERNACIÓN EN UNIDAD DE SALUD MENTAL, "
            "COMPLEJIDAD MEDIANA"
        ),
        "obligatorias": (
            "INTERNACION",
            "SALUD MENTAL",
            "COMPLEJIDAD MEDIANA",
        ),
        "alternativas": (("UNIDAD",),),
    },
}


# ---------------------------------------------------------------------------
# Utilidades de rutas
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _path_local(el) -> str:
    """Ruta de tags locales desde la raíz hasta el elemento."""
    parts = []
    cur = el
    while cur is not None:
        parts.append(_local(cur.tag))
        cur = cur.getparent()
    return "/".join(reversed(parts))


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

@dataclass
class ResultadoCorreccion:
    nombre: str
    ok: bool = True
    mensaje: str = ""
    cambios: List[str] = field(default_factory=list)
    xml_bytes: Optional[bytes] = None
    # Para diff/preview
    invoice_original: str = ""
    invoice_corregido: str = ""


@dataclass
class ResultadoValidacionMutual:
    nombre: str
    factura: str = ""
    ok: bool = False
    estado: str = "PENDIENTE"
    autorizaciones_xml: str = ""
    autorizaciones_at: str = ""
    autorizacion_csv: str = ""
    estado_autorizacion: str = ""
    codigo_xml: str = ""
    codigo_at: str = ""
    tratamiento_esperado: str = ""
    descripcion_xml: str = ""
    descripcion_at: str = ""
    mensaje: str = ""
    detalles: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "archivo": self.nombre,
            "factura": self.factura,
            "estado": self.estado,
            "autorización XML": self.autorizaciones_xml,
            "autorización AT": self.autorizaciones_at,
            "autorización CSV EPS": self.autorizacion_csv,
            "estado autorización": self.estado_autorizacion,
            "código XML": self.codigo_xml,
            "código AT": self.codigo_at,
            "tratamiento contratado": self.tratamiento_esperado,
            "descripción AT": self.descripcion_at,
            "observación": self.mensaje,
        }


# ---------------------------------------------------------------------------
# Extracción de la Invoice embebida y (re)empaquetado
# ---------------------------------------------------------------------------

def _extraer_invoice(description_text: str) -> Optional[str]:
    m = RE_INVOICE.search(description_text)
    return m.group(0) if m else None


def _xml_prettify(xml_bytes: bytes) -> bytes:
    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.fromstring(xml_bytes, parser)
    return etree.tostring(tree, pretty_print=True, encoding="UTF-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Merge selectivo: copia a 'target' las HOJAS que faltan, desde 'source'
# ---------------------------------------------------------------------------

def _collect_leaves(root):
    """Devuelve lista de (ruta_local, elemento_hoja) para cada hoja de root."""
    leaves = []

    def rec(el, path):
        hijos = [c for c in el if isinstance(c.tag, str)]
        if not hijos:
            leaves.append((path, el))
            return
        for c in hijos:
            if _local(c.tag) in EXCLUIR_TAGS:
                continue
            rec(c, f"{path}/{_local(c.tag)}")

    rec(root, _local(root.tag))
    return leaves


def _find_parent(target_root, ruta_padre):
    """Navega por ruta de tags locales; devuelve el elemento padre o None.
    Cuenta ocurrencias para soportar hermanos repetidos (p.ej. UBLExtension[5]).
    """
    if ruta_padre == _local(target_root.tag):
        return target_root
    partes = ruta_padre.split("/")[1:]  # quitar raíz
    cur = target_root
    for p in partes:
        hijos = [c for c in cur if _local(c.tag) == p]
        if not hijos:
            return None
        cur = hijos[0] if len(hijos) == 1 else hijos[-1]
    return cur


def _merge_invoice(source_root, target_root, cambios: List[str]) -> None:
    """Inserta en target las HOJAS de source que no existan (por ruta local).

    Solo se añaden hojas faltantes; nunca se duplican contenedores. Si falta un
    contenedor intermedio, se crea replicando la estructura de la plantilla.
    """
    target_leaves = set(p for p, _ in _collect_leaves(target_root))
    source_leaves = _collect_leaves(source_root)

    for ruta, src_el in source_leaves:
        if ruta in target_leaves:
            continue  # ya existe -> no tocar
        if _local(src_el.tag) in EXCLUIR_TAGS:
            continue
        # Ruta del padre = todo menos el último segmento
        idx = ruta.rfind("/")
        ruta_padre = ruta[:idx]
        parent = _find_parent(target_root, ruta_padre)
        if parent is None:
            # Crear la ruta de padres replicando la plantilla
            parent = _crear_ruta(target_root, source_root, ruta_padre, cambios)
            if parent is None:
                cambios.append(f"⚠️ No se pudo ubicar '{ruta_padre}'")
                continue
        # Insertar la hoja (deepcopy) al final del padre
        dup = copy.deepcopy(src_el)
        # limpiar restos de firma por seguridad
        for bad in list(dup.iter()):
            if _local(bad.tag) in EXCLUIR_TAGS and bad.getparent() is not None:
                bad.getparent().remove(bad)
        parent.append(dup)
        cambios.append(f"Insertado '{ruta}'")


def _crear_ruta(target_root, source_root, ruta_padre, cambios: List[str]):
    """Crea los padres faltantes en target replicando la estructura de source."""
    partes = ruta_padre.split("/")
    cur = target_root
    acc = _local(target_root.tag)
    for i, p in enumerate(partes[1:], start=1):
        sub = "/".join([acc] + partes[1:i + 1])
        siguiente = _find_parent(target_root, sub)
        if siguiente is None:
            src_node = _find_parent(source_root, sub)
            if src_node is None:
                return None
            nuevo = copy.deepcopy(src_node)
            for c in list(nuevo):
                nuevo.remove(c)
            cur.append(nuevo)
            cur = nuevo
            cambios.append(f"Creada ruta '{sub}'")
        else:
            cur = siguiente
    return cur


# ---------------------------------------------------------------------------
# CDATA en Description con XML embebido
# ---------------------------------------------------------------------------

def _asegurar_cdata_descriptions(root) -> None:
    """Envuelve en CDATA el contenido de cada <Description> que contenga XML
    embebido (empieza con '<' o '<?xml').

    Cuando lxml parsea un CDATA lo convierte internamente a texto plano; al
    serializar de vuelta sin marcarlo de nuevo como CDATA escaparía los '<' y
    '>' dejando el AttachedDocument con '&lt;' en lugar de '<', lo que haría
    inválida la factura embebida para los validadores de la DIAN.
    """
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if _local(el.tag) != "Description":
            continue
        txt = el.text or ""
        if txt.lstrip().startswith("<"):
            el.text = etree.CDATA(txt)


# ---------------------------------------------------------------------------
# Sobrescritura de configuración fija del emisor
# ---------------------------------------------------------------------------

def _aplicar_config_fija(source_root, target_root, cambios: List[str]) -> None:
    """Para las rutas en CONFIG_FIJA: SOLO inserta si faltan en target.

    NO sobrescribe valores que la factura incompleta YA trae (para no romper
    datos correctos del emisor). El merge previo ya copió la estructura faltante.
    """
    for ruta in CONFIG_FIJA:
        src_els = _find_by_local_path(source_root, ruta)
        tgt_els = _find_by_local_path(target_root, ruta)
        if not src_els or tgt_els:
            continue  # ya existe en target -> no tocar
        for src_el in src_els:
            dup = copy.deepcopy(src_el)
            # limpiar restos de firma por seguridad
            for bad in dup.iter():
                if _local(bad.tag) in EXCLUIR_TAGS and bad.getparent() is not None:
                    bad.getparent().remove(bad)
            target_root.append(dup)
            cambios.append(f"Config fija insertada: '{ruta}'")


def _find_by_local_path(root, ruta_local: str):
    """Encuentra elementos cuya ruta de tags locales termina en ruta_local.
    ruta_local tipo 'TaxScheme/ID' o 'FabricanteSoftware'."""
    objetivo = ruta_local.split("/")
    resultados = []

    def rec(el, acc):
        acc = acc + [_local(el.tag)]
        if acc[-len(objetivo):] == objetivo:
            resultados.append(el)
        for c in el:
            rec(c, acc)

    rec(root, [])
    return resultados


def _aplicar_config_sobrescribir(source_root, target_root, cambios: List[str]) -> None:
    """Para las rutas en CONFIG_SOBRESCRIBIR: sobrescribe el texto de la hoja
    en target con el valor de la plantilla, aunque ya exista con otro valor.

    Corrige campos de configuración del emisor (CEMIC) que la factura incompleta
    puede traer incorrectos, p.ej. TaxScheme/ID=01 (IVA) en lugar de ZZ (No aplica).
    Solo actúa cuando el valor difiere del de la plantilla.
    """
    for ruta in CONFIG_SOBRESCRIBIR:
        src_els = _find_by_local_path(source_root, ruta)
        if not src_els:
            continue
        src_val = (src_els[0].text or "").strip()
        tgt_els = _find_by_local_path(target_root, ruta)
        if tgt_els:
            tgt_val = (tgt_els[0].text or "").strip()
            if tgt_val != src_val:
                tgt_els[0].text = src_val
                cambios.append(
                    f"Config emisor corregida: '{ruta}' ({tgt_val!r} -> {src_val!r})"
                )
        else:
            # No existe: insertar (caso raro, pero posible)
            dup = copy.deepcopy(src_els[0])
            target_root.append(dup)
            cambios.append(f"Config emisor insertada: '{ruta}'")



# ---------------------------------------------------------------------------
# Modalidad de pago
# ---------------------------------------------------------------------------

MODALIDAD_PAGO_PREDETERMINADA = "Por evento"
MODALIDAD_PAGO_SCHEME_ID = "04"
MODALIDAD_PAGO_SCHEME_NAME = "salud_modalidad_pago.gc"

COBERTURAS_POR_TIPO_USUARIO = {
    "CONTRIBUTIVO": (
        "Plan UPC — Régimen Contributivo",
        "16",
    ),
    "SUBSIDIADO": (
        "Plan UPC — Régimen Subsidiado",
        "17",
    ),
}


def _leer_additional_information(target_inv, nombre: str):
    """Busca un AdditionalInformation por el contenido exacto de Name."""
    nombre = nombre.strip().upper()
    for ai in target_inv.iter():
        if not isinstance(ai.tag, str) or _local(ai.tag) != "AdditionalInformation":
            continue
        name_el = next(
            (
                c for c in ai
                if isinstance(c.tag, str) and _local(c.tag) == "Name"
            ),
            None,
        )
        if name_el is None:
            continue
        if (name_el.text or "").strip().upper() == nombre:
            value_el = next(
                (
                    c for c in ai
                    if isinstance(c.tag, str) and _local(c.tag) == "Value"
                ),
                None,
            )
            return ai, name_el, value_el
    return None, None, None


def _crear_additional_information(target_inv, nombre: str):
    """Crea un AdditionalInformation clonando la estructura de uno existente."""
    referencia = next(
        (
            el for el in target_inv.iter()
            if isinstance(el.tag, str)
            and _local(el.tag) == "AdditionalInformation"
        ),
        None,
    )
    if referencia is None or referencia.getparent() is None:
        return None, None, None

    nuevo = copy.deepcopy(referencia)
    for child in list(nuevo):
        nuevo.remove(child)

    name_ref = next(
        (
            c for c in referencia
            if isinstance(c.tag, str) and _local(c.tag) == "Name"
        ),
        None,
    )
    value_ref = next(
        (
            c for c in referencia
            if isinstance(c.tag, str) and _local(c.tag) == "Value"
        ),
        None,
    )

    name_tag = (
        name_ref.tag
        if name_ref is not None
        else _tag_como_hijo(nuevo, "Name")
    )
    value_tag = (
        value_ref.tag
        if value_ref is not None
        else _tag_como_hijo(nuevo, "Value")
    )

    name_el = etree.SubElement(nuevo, name_tag)
    name_el.text = nombre
    value_el = etree.SubElement(nuevo, value_tag)

    padre = referencia.getparent()
    padre.insert(padre.index(referencia) + 1, nuevo)
    return nuevo, name_el, value_el


def _asegurar_grupo(target_inv, nombre: str):
    ai, name_el, value_el = _leer_additional_information(
        target_inv,
        nombre,
    )
    if ai is None:
        ai, name_el, value_el = _crear_additional_information(
            target_inv,
            nombre,
        )
    elif value_el is None:
        value_el = etree.SubElement(
            ai,
            _tag_como_hijo(ai, "Value"),
        )
    return ai, name_el, value_el


def _aplicar_modalidad_y_cobertura(target_inv, cambios: List[str]) -> None:
    """Corrige modalidad de pago y cobertura con los códigos SISPRO.

    La clínica factura por evento:
      MODALIDAD_PAGO = Por evento
      schemeID = 04
      schemeName = salud_modalidad_pago.gc

    La cobertura se determina a partir de TIPO_USUARIO para evitar que un
    usuario subsidiado quede reportado como plan complementario.
    """
    _, _, modalidad_value = _asegurar_grupo(
        target_inv,
        "MODALIDAD_PAGO",
    )
    if modalidad_value is None:
        cambios.append("⚠️ No se pudo crear MODALIDAD_PAGO")
        return

    anterior = (modalidad_value.text or "").strip()
    attrs_anteriores = dict(modalidad_value.attrib)

    modalidad_value.text = MODALIDAD_PAGO_PREDETERMINADA
    modalidad_value.set("schemeID", MODALIDAD_PAGO_SCHEME_ID)
    modalidad_value.set(
        "schemeName",
        MODALIDAD_PAGO_SCHEME_NAME,
    )

    if (
        anterior != MODALIDAD_PAGO_PREDETERMINADA
        or attrs_anteriores.get("schemeID") != MODALIDAD_PAGO_SCHEME_ID
        or attrs_anteriores.get("schemeName") != MODALIDAD_PAGO_SCHEME_NAME
    ):
        cambios.append(
            "MODALIDAD_PAGO corregida a "
            "'Por evento' (schemeID=04)"
        )

    _, _, tipo_usuario_value = _leer_additional_information(
        target_inv,
        "TIPO_USUARIO",
    )
    tipo_usuario = (
        (tipo_usuario_value.text or "").strip().upper()
        if tipo_usuario_value is not None
        else ""
    )

    cobertura = None
    for clave, datos in COBERTURAS_POR_TIPO_USUARIO.items():
        if clave in tipo_usuario:
            cobertura = datos
            break

    if cobertura is None:
        cambios.append(
            "⚠️ No se ajustó COBERTURA_PLAN_BENEFICIOS porque "
            "TIPO_USUARIO no indica Contributivo o Subsidiado"
        )
        return

    cobertura_texto, cobertura_id = cobertura
    _, _, cobertura_value = _asegurar_grupo(
        target_inv,
        "COBERTURA_PLAN_BENEFICIOS",
    )
    if cobertura_value is None:
        cambios.append(
            "⚠️ No se pudo crear COBERTURA_PLAN_BENEFICIOS"
        )
        return

    cobertura_anterior = (cobertura_value.text or "").strip()
    cobertura_id_anterior = cobertura_value.get("schemeID")

    cobertura_value.text = cobertura_texto
    cobertura_value.set("schemeID", cobertura_id)
    cobertura_value.set("schemeName", "salud_cobertura.gc")

    if (
        cobertura_anterior != cobertura_texto
        or cobertura_id_anterior != cobertura_id
    ):
        cambios.append(
            "COBERTURA_PLAN_BENEFICIOS corregida a "
            f"'{cobertura_texto}' (schemeID={cobertura_id})"
        )


def _tag_como_hijo(parent, local_name: str) -> str:
    """Construye un tag con el mismo namespace del contenedor."""
    if isinstance(parent.tag, str) and parent.tag.startswith("{"):
        namespace = parent.tag.split("}", 1)[0][1:]
        return f"{{{namespace}}}{local_name}"
    return local_name



# ---------------------------------------------------------------------------
# Código del prestador
# ---------------------------------------------------------------------------

CODIGO_PRESTADOR_CEMIC = "1300101145"


def _aplicar_codigo_prestador(target_inv, cambios: List[str]) -> None:
    """Corrige CODIGO_PRESTADOR con el código habilitado para CEMIC."""
    ai, _, value_el = _leer_additional_information(
        target_inv,
        "CODIGO_PRESTADOR",
    )
    if ai is None:
        ai, _, value_el = _crear_additional_information(
            target_inv,
            "CODIGO_PRESTADOR",
        )
    elif value_el is None:
        value_el = etree.SubElement(
            ai,
            _tag_como_hijo(ai, "Value"),
        )

    if value_el is None:
        cambios.append("⚠️ No se pudo crear CODIGO_PRESTADOR")
        return

    anterior = (value_el.text or "").strip()
    if anterior != CODIGO_PRESTADOR_CEMIC:
        value_el.text = CODIGO_PRESTADOR_CEMIC
        cambios.append(
            "CODIGO_PRESTADOR corregido "
            f"({anterior!r} -> {CODIGO_PRESTADOR_CEMIC!r})"
        )




# ---------------------------------------------------------------------------
# Detección de régimen desde CSV de Mutual
# ---------------------------------------------------------------------------

MAPA_ADMINISTRADORA_REGIMEN = {
    "ESSC07": "contributivo",
    "ESS207": "subsidiado",
}


def _normalizar_valor_csv(valor) -> str:
    if valor is None:
        return ""
    texto = str(valor).strip()
    if texto.endswith(".0"):
        texto = texto[:-2]
    return texto


def _leer_csv_mutual(csv_bytes: bytes):
    """Lee el CSV de Mutual tolerando separador y codificación."""
    texto = None
    for encoding in ("utf-8-sig", "latin-1", "cp1252"):
        try:
            texto = csv_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if texto is None:
        raise ValueError("No fue posible leer el CSV de Mutual.")

    muestra = texto[:10000]
    try:
        dialecto = csv.Sniffer().sniff(muestra, delimiters=",;|\t")
        delimitador = dialecto.delimiter
    except csv.Error:
        delimitador = ","

    lector = csv.DictReader(io.StringIO(texto), delimiter=delimitador)
    return list(lector)


def _extraer_identificadores_xml(xml_bytes: bytes):
    """Extrae documentos y autorizaciones de la factura embebida."""
    root = etree.fromstring(xml_bytes)
    desc = root.find(XP_DESCRIPTION)
    if desc is None or not (desc.text or "").strip():
        return set(), set()

    invoice_text = _extraer_invoice(desc.text)
    if not invoice_text:
        return set(), set()

    invoice = etree.fromstring(invoice_text.encode("utf-8"))
    documentos = set()
    autorizaciones = set()

    for ai in invoice.iter():
        if not isinstance(ai.tag, str) or _local(ai.tag) != "AdditionalInformation":
            continue

        name_el = next(
            (
                c for c in ai
                if isinstance(c.tag, str) and _local(c.tag) == "Name"
            ),
            None,
        )
        value_el = next(
            (
                c for c in ai
                if isinstance(c.tag, str) and _local(c.tag) == "Value"
            ),
            None,
        )
        if name_el is None or value_el is None:
            continue

        nombre = (name_el.text or "").strip().upper()
        valor = (value_el.text or "").strip()

        if nombre == "NUMERO_DOCUMENTO_IDENTIFICACION" and valor:
            documentos.add(_normalizar_valor_csv(valor))

        if nombre == "NUMERO_AUTORIZACION" and valor:
            for parte in re.split(r"[;,\s]+", valor):
                parte = _normalizar_valor_csv(parte)
                if parte:
                    autorizaciones.add(parte)

    return documentos, autorizaciones



def _normalizar_texto_contrato(valor: Any) -> str:
    texto = "" if valor is None else str(valor)
    texto = "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", texto)
        if unicodedata.category(caracter) != "Mn"
    )
    texto = texto.upper()
    texto = re.sub(r"[^A-Z0-9]+", " ", texto)
    return " ".join(texto.split())


def _normalizar_codigo_tecnologia(valor: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _normalizar_texto_contrato(valor))


def _variantes_autorizacion(valor: Any) -> set[str]:
    digitos = re.sub(r"\D", "", str(valor or ""))
    if not digitos or set(digitos) == {"0"}:
        return set()

    variantes = {digitos}
    if len(digitos) > 7:
        variantes.add(digitos[-7:])
    return variantes


def _separar_autorizaciones(valor: Any) -> set[str]:
    resultado: set[str] = set()
    for parte in re.split(r"[;,\s|]+", str(valor or "")):
        resultado.update(_variantes_autorizacion(parte))
    return resultado


def _autorizaciones_mostrables(valores: set[str]) -> List[str]:
    if not valores:
        return []

    largas = sorted(
        {valor for valor in valores if len(valor) >= 13}
    )
    if largas:
        return largas

    longitud = max(len(valor) for valor in valores)
    return sorted(
        {valor for valor in valores if len(valor) == longitud}
    )


def _texto_hijo_directo(elemento, nombre_local: str) -> str:
    for hijo in elemento:
        if isinstance(hijo.tag, str) and _local(hijo.tag) == nombre_local:
            return (hijo.text or "").strip()
    return ""


def _propiedades_item(item) -> Dict[str, str]:
    propiedades: Dict[str, str] = {}
    for propiedad in item.iter():
        if not isinstance(propiedad.tag, str):
            continue
        if _local(propiedad.tag) != "AdditionalItemProperty":
            continue

        nombre = ""
        valor = ""
        for hijo in propiedad:
            if not isinstance(hijo.tag, str):
                continue
            if _local(hijo.tag) == "Name":
                nombre = _normalizar_texto_contrato(hijo.text)
            elif _local(hijo.tag) == "Value":
                valor = (hijo.text or "").strip()
        if nombre:
            propiedades[nombre] = valor
    return propiedades


def _extraer_datos_validacion_xml_mutual(xml_bytes: bytes) -> Dict[str, Any]:
    root = etree.fromstring(xml_bytes)
    desc = root.find(XP_DESCRIPTION)
    if desc is None or not (desc.text or "").strip():
        raise ValueError(
            "No se encontró la factura embebida en el AttachedDocument."
        )

    invoice_text = _extraer_invoice(desc.text)
    if not invoice_text:
        raise ValueError("No se encontró un <Invoice> dentro del Description.")

    invoice = etree.fromstring(invoice_text.encode("utf-8"))
    factura = _texto_hijo_directo(invoice, "ID")
    autorizaciones_globales: set[str] = set()

    for ai in invoice.iter():
        if not isinstance(ai.tag, str) or _local(ai.tag) != "AdditionalInformation":
            continue
        nombre = _texto_hijo_directo(ai, "Name").upper()
        valor = _texto_hijo_directo(ai, "Value")
        if nombre == "NUMERO_AUTORIZACION":
            autorizaciones_globales.update(_separar_autorizaciones(valor))

    for campo in invoice.iter():
        if not isinstance(campo.tag, str) or _local(campo.tag) != "CustomField":
            continue
        if _normalizar_texto_contrato(campo.get("Name")) == "NUMERO AUTORIZACION":
            autorizaciones_globales.update(
                _separar_autorizaciones(campo.get("Value"))
            )

    lineas = []
    for posicion, linea in enumerate(
        [h for h in invoice if isinstance(h.tag, str) and _local(h.tag) == "InvoiceLine"],
        start=1,
    ):
        numero_linea = _texto_hijo_directo(linea, "ID") or str(posicion)
        item = next(
            (
                hijo
                for hijo in linea
                if isinstance(hijo.tag, str) and _local(hijo.tag) == "Item"
            ),
            None,
        )
        if item is None:
            lineas.append(
                {
                    "linea": numero_linea,
                    "codigo": "",
                    "descripcion": "",
                    "autorizaciones": set(),
                }
            )
            continue

        descripcion = _texto_hijo_directo(item, "Description")
        codigo = ""
        autorizaciones_linea: set[str] = set()

        for elemento in item.iter():
            if not isinstance(elemento.tag, str):
                continue
            local = _local(elemento.tag)
            if local == "StandardItemIdentification":
                codigo = _texto_hijo_directo(elemento, "ID") or codigo
            elif local == "BuyersItemIdentification":
                autorizaciones_linea.update(
                    _separar_autorizaciones(
                        _texto_hijo_directo(elemento, "ID")
                    )
                )

        propiedades = _propiedades_item(item)
        if not codigo:
            codigo = (
                propiedades.get("CODIGO ITEM ERP")
                or propiedades.get("CODIGO ITEM")
                or ""
            )
        if not descripcion:
            descripcion = propiedades.get("DESCRIPCION", "")

        autorizaciones_linea.update(
            _separar_autorizaciones(
                propiedades.get("NUMERO AUTORIZACION", "")
            )
        )

        lineas.append(
            {
                "linea": numero_linea,
                "codigo": _normalizar_codigo_tecnologia(codigo),
                "descripcion": descripcion,
                "autorizaciones": autorizaciones_linea,
            }
        )

    if not lineas:
        raise ValueError("La factura no contiene líneas de servicio.")

    return {
        "factura": factura,
        "autorizaciones_globales": autorizaciones_globales,
        "lineas": lineas,
    }


def _autorizaciones_fila_csv(fila: Dict[str, Any]) -> set[str]:
    resultado: set[str] = set()
    for campo in ("NUMERO_AUTORIZACION", "N_NUMERO_AUTORIZACION"):
        resultado.update(_separar_autorizaciones(fila.get(campo)))
    return resultado



def _normalizar_factura_mutual(valor: Any) -> str:
    return re.sub(r"\s+", "", str(valor or "")).upper()


def _decodificar_rips_mutual(contenido: bytes) -> str:
    for codificacion in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return contenido.decode(codificacion)
        except UnicodeDecodeError:
            continue
    return contenido.decode("latin-1", errors="replace")


def consolidar_at_mutual(
    archivos_zip: Iterable[Tuple[str, bytes]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Lee los archivos AT de uno o varios ZIP RIPS.

    Estructura AT utilizada:
    0 factura
    4 autorización
    6 código de tecnología
    7 descripción de tecnología
    """
    mapa: Dict[str, List[Dict[str, Any]]] = {}

    for nombre_zip, contenido_zip in archivos_zip:
        try:
            with zipfile.ZipFile(io.BytesIO(contenido_zip)) as archivo:
                nombres_at = [
                    nombre
                    for nombre in archivo.namelist()
                    if (
                        Path(nombre).name.upper().startswith("AT")
                        and nombre.lower().endswith(".txt")
                    )
                ]

                if not nombres_at:
                    raise ValueError(
                        f"{nombre_zip}: no contiene un archivo AT."
                    )

                for nombre_at in nombres_at:
                    texto = _decodificar_rips_mutual(
                        archivo.read(nombre_at)
                    )
                    lector = csv.reader(io.StringIO(texto))

                    for numero_linea, fila in enumerate(lector, start=1):
                        if not fila:
                            continue

                        if len(fila) < 8:
                            raise ValueError(
                                f"{nombre_zip}/{nombre_at}, línea "
                                f"{numero_linea}: registro AT incompleto."
                            )

                        factura = _normalizar_factura_mutual(fila[0])
                        if not factura:
                            raise ValueError(
                                f"{nombre_zip}/{nombre_at}, línea "
                                f"{numero_linea}: factura vacía."
                            )

                        mapa.setdefault(factura, []).append(
                            {
                                "factura": factura,
                                "autorizaciones": _separar_autorizaciones(
                                    fila[4]
                                ),
                                "codigo": _normalizar_codigo_tecnologia(
                                    fila[6]
                                ),
                                "descripcion": str(fila[7] or "").strip(),
                                "archivo": f"{nombre_zip}/{nombre_at}",
                                "linea": numero_linea,
                            }
                        )
        except zipfile.BadZipFile as exc:
            raise ValueError(
                f"{nombre_zip}: no es un ZIP válido."
            ) from exc

    if not mapa:
        raise ValueError(
            "No se encontraron registros AT en los ZIP cargados."
        )

    return mapa


def _descripcion_valida_para_codigo(codigo: str, descripcion: Any) -> bool:
    """Valida la descripción AT tolerando truncamientos del sistema fuente.

    Mutual puede limitar la longitud del campo y entregar, por ejemplo:

    INTERNACIONHOSPITALARIAENELCONSUMIDORDESUSTANCIASPSIC

    en lugar de terminar la palabra PSICOACTIVAS. Se acepta cuando la
    descripción recibida es un prefijo suficientemente largo de la
    descripción contractual del mismo código.

    No se acepta una descripción correspondiente a otro tratamiento.
    """
    regla = TRATAMIENTOS_CONTRATADOS_MUTUAL.get(codigo)
    if regla is None:
        return False

    texto = _normalizar_texto_contrato(descripcion)
    compacto = _normalizar_codigo_tecnologia(descripcion)
    contractual = _normalizar_codigo_tecnologia(
        regla["descripcion"]
    )

    if not texto and not compacto:
        return False

    # Coincidencia exacta o texto contractual con información adicional.
    if compacto == contractual or compacto.startswith(contractual):
        return True

    # Descripción truncada: debe ser un prefijo inequívoco y conservar
    # al menos el 70 % de la descripción contractual, con mínimo 30
    # caracteres para evitar coincidencias demasiado generales.
    minimo_prefijo = max(
        30,
        int(len(contractual) * 0.70),
    )
    if (
        contractual.startswith(compacto)
        and len(compacto) >= minimo_prefijo
    ):
        return True

    def contiene(termino: str) -> bool:
        termino_texto = _normalizar_texto_contrato(termino)
        termino_compacto = _normalizar_codigo_tecnologia(termino)
        return (
            termino_texto in texto
            or termino_compacto in compacto
        )

    for obligatorio in regla["obligatorias"]:
        if not contiene(obligatorio):
            return False

    for alternativas in regla["alternativas"]:
        if not any(contiene(alternativa) for alternativa in alternativas):
            return False

    return True


def validar_factura_mutual_con_csv(
    xml_bytes: bytes,
    csv_bytes: bytes,
    nombre: str = "factura.xml",
    mapa_at_mutual: Optional[
        Dict[str, List[Dict[str, Any]]]
    ] = None,
) -> ResultadoValidacionMutual:
    """Valida autorización EPS y tratamiento AT de forma independiente.

    La autorización no determina el código del servicio. El CSV se usa para
    confirmar que la autorización fue emitida por la EPS y está aprobada.
    El código y la descripción del tratamiento se obtienen del archivo AT.
    """
    resultado = ResultadoValidacionMutual(nombre=nombre)

    try:
        datos_xml = _extraer_datos_validacion_xml_mutual(xml_bytes)
        resultado.factura = datos_xml["factura"]
        factura_normalizada = _normalizar_factura_mutual(
            datos_xml["factura"]
        )

        filas_csv = _leer_csv_mutual(csv_bytes)
        errores: List[str] = []
        detalles: List[str] = []

        autorizaciones_globales = datos_xml["autorizaciones_globales"]
        autorizaciones_lineas_xml = set().union(
            *(
                linea["autorizaciones"]
                for linea in datos_xml["lineas"]
            )
        )
        autorizaciones_xml = (
            autorizaciones_lineas_xml or autorizaciones_globales
        )
        resultado.autorizaciones_xml = "; ".join(
            _autorizaciones_mostrables(autorizaciones_xml)
        )

        if not autorizaciones_xml:
            errores.append(
                "La factura XML no contiene número de autorización."
            )

        # ------------------------------------------------------------------
        # Autorización: XML + AT + CSV EPS
        # ------------------------------------------------------------------
        registros_at = (
            (mapa_at_mutual or {}).get(factura_normalizada, [])
        )

        if not registros_at:
            errores.append(
                f"{datos_xml['factura']}: no se encontró en el archivo AT "
                "de los ZIP RIPS cargados."
            )

        autorizaciones_at = set().union(
            *(
                registro["autorizaciones"]
                for registro in registros_at
            )
        ) if registros_at else set()

        resultado.autorizaciones_at = "; ".join(
            _autorizaciones_mostrables(autorizaciones_at)
        )

        if registros_at and not autorizaciones_at:
            errores.append(
                "Los registros AT no contienen número de autorización."
            )

        if (
            autorizaciones_xml
            and autorizaciones_at
            and not (autorizaciones_xml & autorizaciones_at)
        ):
            errores.append(
                "La autorización del XML no coincide con la informada "
                "en el archivo AT."
            )

        candidatas_csv = [
            fila
            for fila in filas_csv
            if autorizaciones_xml & _autorizaciones_fila_csv(fila)
        ] if autorizaciones_xml else []

        if autorizaciones_xml and not candidatas_csv:
            errores.append(
                "La autorización del XML no aparece en el CSV de Mutual."
            )

        autorizaciones_csv = set().union(
            *(
                _autorizaciones_fila_csv(fila)
                for fila in candidatas_csv
            )
        ) if candidatas_csv else set()

        resultado.autorizacion_csv = "; ".join(
            _autorizaciones_mostrables(autorizaciones_csv)
        )

        estados = sorted(
            {
                _normalizar_texto_contrato(
                    fila.get("C_ESTADO_SOLICITUD")
                )
                for fila in candidatas_csv
                if fila.get("C_ESTADO_SOLICITUD")
            }
        )
        resultado.estado_autorizacion = "; ".join(estados)

        aprobadas = [
            fila
            for fila in candidatas_csv
            if _normalizar_texto_contrato(
                fila.get("C_ESTADO_SOLICITUD")
            ) == "APROBADO"
        ]

        if candidatas_csv and not aprobadas:
            errores.append(
                "La autorización aparece en el CSV, pero ninguna fila "
                "está en estado APROBADO."
            )

        # C_CODIGO_PRODUCTO y C_CONTRATADO NO se comparan con el AT.
        # Son datos independientes de la autorización.
        if aprobadas:
            detalles.append(
                "Autorización confirmada en el CSV de la EPS y en "
                "estado APROBADO. El código de producto del CSV no se "
                "usó para determinar el tratamiento."
            )

        # ------------------------------------------------------------------
        # Tratamiento: código y descripción del AT
        # ------------------------------------------------------------------
        codigos_at = sorted(
            {
                registro["codigo"]
                for registro in registros_at
                if registro["codigo"]
            }
        )
        descripciones_at = [
            registro["descripcion"]
            for registro in registros_at
            if registro["descripcion"]
        ]

        resultado.codigo_at = "; ".join(codigos_at)
        resultado.descripcion_at = " | ".join(
            dict.fromkeys(descripciones_at)
        )

        if registros_at and not codigos_at:
            errores.append(
                "Los registros AT no contienen código de tecnología."
            )

        tratamientos = []
        for registro in registros_at:
            codigo_at = registro["codigo"]
            descripcion_at = registro["descripcion"]
            numero_linea = registro["linea"]

            if not codigo_at:
                errores.append(
                    f"AT línea {numero_linea}: código de tecnología vacío."
                )
                continue

            if codigo_at not in TRATAMIENTOS_CONTRATADOS_MUTUAL:
                permitidos = ", ".join(
                    TRATAMIENTOS_CONTRATADOS_MUTUAL
                )
                errores.append(
                    f"AT línea {numero_linea}: código {codigo_at} no está "
                    f"dentro de los tratamientos contratados: {permitidos}."
                )
                continue

            tratamientos.append(
                TRATAMIENTOS_CONTRATADOS_MUTUAL[codigo_at][
                    "descripcion"
                ]
            )

            if not _descripcion_valida_para_codigo(
                codigo_at,
                descripcion_at,
            ):
                errores.append(
                    f"AT línea {numero_linea}: la descripción no "
                    f"corresponde al tratamiento {codigo_at}."
                )

        resultado.tratamiento_esperado = " | ".join(
            dict.fromkeys(tratamientos)
        )

        # ------------------------------------------------------------------
        # Coherencia XML frente al AT
        # ------------------------------------------------------------------
        codigos_xml = sorted(
            {
                _normalizar_codigo_tecnologia(linea["codigo"])
                for linea in datos_xml["lineas"]
                if linea["codigo"]
            }
        )
        descripciones_xml = [
            linea["descripcion"]
            for linea in datos_xml["lineas"]
            if linea["descripcion"]
        ]

        resultado.codigo_xml = "; ".join(codigos_xml)
        resultado.descripcion_xml = " | ".join(
            dict.fromkeys(descripciones_xml)
        )

        if codigos_xml and codigos_at and set(codigos_xml) != set(codigos_at):
            errores.append(
                "El código facturado en el XML no coincide con el código "
                "del tratamiento reportado en AT: "
                f"XML={', '.join(codigos_xml)}; "
                f"AT={', '.join(codigos_at)}."
            )

        if codigos_at:
            detalles.append(
                "Tratamiento validado desde AT contra el catálogo "
                "contractual configurado: "
                + ", ".join(codigos_at)
                + "."
            )

        resultado.detalles = detalles

        if errores:
            resultado.ok = False
            resultado.estado = "RECHAZADO"
            resultado.mensaje = " | ".join(
                dict.fromkeys(errores)
            )
        else:
            resultado.ok = True
            resultado.estado = "VÁLIDO"
            resultado.mensaje = (
                "Autorización EPS aprobada y tratamiento AT validado "
                "contra el catálogo contractual."
            )

    except Exception as exc:
        resultado.ok = False
        resultado.estado = "ERROR"
        resultado.mensaje = str(exc)

    return resultado



def inferir_regimen_desde_csv_mutual(
    xml_bytes: bytes,
    csv_bytes: bytes,
):
    """Determina el régimen usando C_ADMINISTRADORA del CSV de Mutual.

    Busca primero por NUMERO_AUTORIZACION y luego por documento del afiliado.
    Devuelve (regimen, detalle). Si no puede resolverlo de forma inequívoca,
    devuelve (None, detalle).
    """
    filas = _leer_csv_mutual(csv_bytes)
    documentos_xml, autorizaciones_xml = _extraer_identificadores_xml(xml_bytes)

    coincidencias = []

    for fila in filas:
        administradora = _normalizar_valor_csv(
            fila.get("C_ADMINISTRADORA")
        ).upper()
        regimen = MAPA_ADMINISTRADORA_REGIMEN.get(administradora)
        if not regimen:
            continue

        documento = _normalizar_valor_csv(
            fila.get("C_DOCUMENTO_AFILIADO")
        )
        autorizaciones_fila = {
            _normalizar_valor_csv(fila.get("NUMERO_AUTORIZACION")),
            _normalizar_valor_csv(fila.get("N_NUMERO_AUTORIZACION")),
        }
        autorizaciones_fila.discard("")

        coincide_autorizacion = bool(
            autorizaciones_xml.intersection(autorizaciones_fila)
        )
        coincide_documento = bool(
            documento and documento in documentos_xml
        )

        if coincide_autorizacion or coincide_documento:
            prioridad = 2 if coincide_autorizacion else 1
            coincidencias.append(
                {
                    "regimen": regimen,
                    "administradora": administradora,
                    "documento": documento,
                    "autorizaciones": sorted(autorizaciones_fila),
                    "prioridad": prioridad,
                }
            )

    if not coincidencias:
        return None, (
            "No se encontró coincidencia por autorización ni documento "
            "entre el XML y el CSV de Mutual."
        )

    maxima_prioridad = max(c["prioridad"] for c in coincidencias)
    mejores = [
        c for c in coincidencias
        if c["prioridad"] == maxima_prioridad
    ]
    regimenes = {c["regimen"] for c in mejores}

    if len(regimenes) != 1:
        return None, (
            "El CSV contiene coincidencias con regímenes diferentes para "
            "la misma factura; debe revisarse manualmente."
        )

    elegida = mejores[0]
    criterio = (
        "autorización"
        if maxima_prioridad == 2
        else "documento"
    )
    detalle = (
        f"Régimen detectado por {criterio}: "
        f"{elegida['regimen'].capitalize()} "
        f"({elegida['administradora']})."
    )
    return elegida["regimen"], detalle


# ---------------------------------------------------------------------------
# Estructura sector salud - Resolución 000948 de 2026
# ---------------------------------------------------------------------------

CAMPOS_SECTOR_SALUD_948 = (
    "CODIGO_PRESTADOR",
    "MODALIDAD_PAGO",
    "COBERTURA_PLAN_BENEFICIOS",
    "NUMERO_AUTORIZACION",
    "NUMERO_ENTREGA_MIPRES",
    "NUMERO_MIPRES",
    "FACTURA_SIN_CONTRATO",
    "COPAGO",
    "CUOTA_MODERADORA",
    "CUOTA_RECUPERACION",
    "PAGOS_COMPARTIDOS",
    "ANTICIPO",
    "NUMERO_CONTRATO",
    "NUMERO_POLIZA",
)


def _normalizar_nombre_campo(valor: str) -> str:
    return re.sub(r"\s+", " ", (valor or "").strip()).upper()


def _buscar_collection_sector_salud(target_inv):
    for interoperabilidad in target_inv.iter():
        if (
            not isinstance(interoperabilidad.tag, str)
            or _local(interoperabilidad.tag) != "Interoperabilidad"
        ):
            continue

        for group in interoperabilidad:
            if not isinstance(group.tag, str) or _local(group.tag) != "Group":
                continue
            if (group.get("schemeName") or "").strip().lower() != "sector salud":
                continue

            for collection in group:
                if (
                    isinstance(collection.tag, str)
                    and _local(collection.tag) == "Collection"
                    and (collection.get("schemeName") or "").strip().lower()
                    == "usuario"
                ):
                    return interoperabilidad, group, collection
    return None, None, None


def _actualizar_resolucion_948(target_inv, cambios: List[str]) -> None:
    for custom_tag in target_inv.iter():
        if (
            not isinstance(custom_tag.tag, str)
            or _local(custom_tag.tag) != "CustomTagGeneral"
        ):
            continue

        hijos = [h for h in custom_tag if isinstance(h.tag, str)]
        for indice, hijo in enumerate(hijos):
            if _local(hijo.tag) != "Name":
                continue

            nombre = _normalizar_nombre_campo(hijo.text or "")
            if "ACTO ADMIN" not in nombre:
                continue

            value_el = None
            for siguiente in hijos[indice + 1:]:
                if _local(siguiente.tag) == "Value":
                    value_el = siguiente
                    break
                if _local(siguiente.tag) == "Name":
                    break

            if value_el is not None:
                anterior = (value_el.text or "").strip()
                nuevo = "Resolución 000948:2026"
                if anterior != nuevo:
                    value_el.text = nuevo
                    cambios.append(
                        "Acto administrativo corregido "
                        f"({anterior!r} -> {nuevo!r})"
                    )
                return


def _reconstruir_sector_salud_948(
    target_inv,
    cambios: List[str],
    regimen: Optional[str] = None,
) -> None:
    """Reconstruye Collection Usuario con la estructura aceptada por Mutual."""
    interoperabilidad, _, collection = _buscar_collection_sector_salud(target_inv)
    if collection is None:
        cambios.append(
            "⚠️ No se encontró Group/Collection del nodo Sector Salud"
        )
        return

    valores = {}
    atributos = {}

    for ai in list(collection):
        if not isinstance(ai.tag, str) or _local(ai.tag) != "AdditionalInformation":
            continue

        name_el = next(
            (
                c for c in ai
                if isinstance(c.tag, str) and _local(c.tag) == "Name"
            ),
            None,
        )
        value_el = next(
            (
                c for c in ai
                if isinstance(c.tag, str) and _local(c.tag) == "Value"
            ),
            None,
        )
        if name_el is None:
            continue

        nombre = _normalizar_nombre_campo(name_el.text or "")
        valores[nombre] = (value_el.text or "").strip() if value_el is not None else ""
        atributos[nombre] = dict(value_el.attrib) if value_el is not None else {}

    tipo_usuario = valores.get("TIPO_USUARIO", "").upper()
    cobertura_id_actual = atributos.get(
        "COBERTURA_PLAN_BENEFICIOS", {}
    ).get("schemeID", "")

    regimen_normalizado = (regimen or "").strip().lower()

    if regimen_normalizado in ("contributivo", "01", "1"):
        cobertura_texto = "Plan UPC — Régimen Contributivo"
        cobertura_id = "16"
        cambios.append(
            "Cobertura definida manualmente como Régimen Contributivo"
        )
    elif regimen_normalizado in ("subsidiado", "04", "4"):
        cobertura_texto = "Plan UPC — Régimen Subsidiado"
        cobertura_id = "17"
        cambios.append(
            "Cobertura definida manualmente como Régimen Subsidiado"
        )
    elif "CONTRIBUTIVO" in tipo_usuario or cobertura_id_actual == "16":
        cobertura_texto = "Plan UPC — Régimen Contributivo"
        cobertura_id = "16"
        cambios.append(
            "Cobertura detectada automáticamente como Régimen Contributivo"
        )
    elif "SUBSIDIADO" in tipo_usuario or cobertura_id_actual == "17":
        cobertura_texto = "Plan UPC — Régimen Subsidiado"
        cobertura_id = "17"
        cambios.append(
            "Cobertura detectada automáticamente como Régimen Subsidiado"
        )
    else:
        raise ValueError(
            "No fue posible determinar el régimen del usuario. "
            "Seleccione Contributivo o Subsidiado antes de corregir el XML."
        )

    datos = {
        "CODIGO_PRESTADOR": (
            valores.get("CODIGO_PRESTADOR") or CODIGO_PRESTADOR_CEMIC,
            {},
        ),
        "MODALIDAD_PAGO": (
            "Por evento",
            {
                "schemeName": "salud_modalidad_pago.gc",
                "schemeID": "04",
            },
        ),
        "COBERTURA_PLAN_BENEFICIOS": (
            cobertura_texto,
            {
                "schemeName": "salud_cobertura.gc",
                "schemeID": cobertura_id,
            },
        ),
        "NUMERO_AUTORIZACION": (
            valores.get("NUMERO_AUTORIZACION", ""),
            {},
        ),
        "NUMERO_ENTREGA_MIPRES": (
            valores.get("NUMERO_ENTREGA_MIPRES", ""),
            {},
        ),
        "NUMERO_MIPRES": (
            valores.get("NUMERO_MIPRES", ""),
            {},
        ),
        "FACTURA_SIN_CONTRATO": (
            valores.get("FACTURA_SIN_CONTRATO", ""),
            {
                "schemeName": "salud_cobertura.gc",
                "schemeID": "",
            },
        ),
        "COPAGO": (
            valores.get("COPAGO") or "0.00",
            {},
        ),
        "CUOTA_MODERADORA": (
            valores.get("CUOTA_MODERADORA") or "0.00",
            {},
        ),
        "CUOTA_RECUPERACION": (
            valores.get("CUOTA_RECUPERACION", ""),
            {},
        ),
        "PAGOS_COMPARTIDOS": (
            valores.get("PAGOS_COMPARTIDOS") or "0.00",
            {},
        ),
        "ANTICIPO": (
            valores.get("ANTICIPO") or "0.00",
            {},
        ),
        "NUMERO_CONTRATO": (
            valores.get("NUMERO_CONTRATO", ""),
            {},
        ),
        "NUMERO_POLIZA": (
            valores.get("NUMERO_POLIZA", ""),
            {},
        ),
    }

    if interoperabilidad is not None:
        for child in list(interoperabilidad):
            if (
                isinstance(child.tag, str)
                and _local(child.tag) == "InteroperabilidadPT"
            ):
                interoperabilidad.remove(child)
                cambios.append("Eliminado bloque heredado InteroperabilidadPT")

    for child in list(collection):
        if (
            isinstance(child.tag, str)
            and _local(child.tag) == "AdditionalInformation"
        ):
            collection.remove(child)

    for nombre in CAMPOS_SECTOR_SALUD_948:
        valor, attrs = datos[nombre]

        ai = etree.SubElement(
            collection,
            _tag_como_hijo(collection, "AdditionalInformation"),
        )
        name_el = etree.SubElement(
            ai,
            _tag_como_hijo(ai, "Name"),
        )
        name_el.text = nombre

        value_el = etree.SubElement(
            ai,
            _tag_como_hijo(ai, "Value"),
        )
        value_el.text = valor
        for clave, contenido in attrs.items():
            value_el.set(clave, contenido)

    cambios.append(
        "Reconstruido nodo Sector Salud con los 14 campos "
        "de la Resolución 000948 de 2026"
    )


def _aplicar_estructura_sector_salud_948(
    target_inv,
    cambios: List[str],
    regimen: Optional[str] = None,
) -> None:
    _actualizar_resolucion_948(target_inv, cambios)
    _reconstruir_sector_salud_948(target_inv, cambios, regimen)


# ---------------------------------------------------------------------------
# CUCON
# ---------------------------------------------------------------------------

def _aplicar_cucon(target_inv, cucon: str, cambios: List[str]) -> None:
    """Coloca el CUCON en <Name>NUMERO_CONTRATO</Name><Value>...</Value>.

    Reemplaza el valor existente (p.ej. el número de contrato viejo) por el
    CUCON. El campo vive DENTRO de la factura embebida (UBLExtensions /
    CustomTagGeneral del prestador). Se busca por tag local para ser tolerante
    a namespaces (el XML usa namespace por defecto, no prefijo cbc).
    """
    encontrado = False
    for ai in target_inv.iter():
        if _local(ai.tag) != "AdditionalInformation":
            continue
        name_el = None
        value_el = None
        for child in ai:
            if not isinstance(child.tag, str):
                continue
            ln = _local(child.tag)
            if ln == "Name":
                name_el = child
            elif ln == "Value":
                value_el = child
        if name_el is not None and (name_el.text or "").strip() == "NUMERO_CONTRATO":
            if value_el is None:
                value_el = etree.SubElement(ai, "Value")
            if (value_el.text or "").strip() != cucon:
                valor_viejo = (value_el.text or "").strip()
                value_el.text = cucon
                cambios.append(
                    f"CUCON asignado en NUMERO_CONTRATO "
                    f"(reemplaza '{valor_viejo}' -> {cucon[:12]}…)"
                )
            encontrado = True
            break
    if not encontrado:
        cambios.append("⚠️ No se encontró NUMERO_CONTRATO en la factura")


# ---------------------------------------------------------------------------
# Plantilla de referencia (fija, provista por el equipo). El usuario NO la sube:
# la herramienta la usa internamente para saber qué campos completar.
# ---------------------------------------------------------------------------
_RUTA_PLANTILLA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "plantilla_referencia.xml")


def cargar_plantilla() -> bytes:
    with open(_RUTA_PLANTILLA, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------------

def corregir_xml(
    xml_bytes: bytes,
    plantilla_bytes: bytes,
    cucon: str,
    nombre: str = "factura.xml",
    regimen: Optional[str] = None,
) -> ResultadoCorreccion:
    res = ResultadoCorreccion(nombre=nombre)

    try:
        # 1) AttachedDocument completo (con firma)
        adb_tree = etree.fromstring(xml_bytes)
        adb_root = adb_tree

        # 2) Extraer la Invoice embebida del Description
        desc = adb_root.find(XP_DESCRIPTION)
        if desc is None or not (desc.text or "").strip():
            res.ok = False
            res.mensaje = "No se encontró la factura embebida (Attachment/ExternalReference/Description)."
            return res

        invoice_text = _extraer_invoice(desc.text)
        if not invoice_text:
            res.ok = False
            res.mensaje = "No se encontró un <Invoice> dentro del Description."
            return res

        res.invoice_original = _xml_prettify(invoice_text.encode("utf-8")).decode("utf-8")

        # 3) Plantilla
        plantilla_tree = etree.fromstring(plantilla_bytes)
        pdesc = plantilla_tree.find(XP_DESCRIPTION)
        pinvoice_text = _extraer_invoice(pdesc.text) if pdesc is not None else None
        if not pinvoice_text:
            # la plantilla podría ser una Invoice directa
            pinvoice_text = etree.tostring(plantilla_tree, encoding="unicode")
        plantilla_inv = etree.fromstring(pinvoice_text.encode("utf-8"))

        # 4) Merge selectivo desde la plantilla hacia la incompleta
        target_inv = etree.fromstring(invoice_text.encode("utf-8"))
        _merge_invoice(plantilla_inv, target_inv, res.cambios)

        # 5) Configuración fija del emisor (inserta si falta)
        _aplicar_config_fija(plantilla_inv, target_inv, res.cambios)

        # 5b) Corrección de campos del emisor con valor incorrecto
        #     (sobrescribe TaxScheme/ID=01->ZZ, Name=IVA->No aplica)
        _aplicar_config_sobrescribir(plantilla_inv, target_inv, res.cambios)

        # 6) Modalidad de pago obligatoria para la clínica
        _aplicar_modalidad_y_cobertura(target_inv, res.cambios)
        _aplicar_codigo_prestador(target_inv, res.cambios)

        # 7) CUCON dentro de la factura embebida
        if cucon and cucon.strip():
            _aplicar_cucon(target_inv, cucon.strip(), res.cambios)

        # Estructura definitiva del nodo Sector Salud según Resolución 948.
        _aplicar_estructura_sector_salud_948(
            target_inv,
            res.cambios,
            regimen,
        )

        # 7) Re-empaquetar: reemplazar el Description con la invoice corregida.
        #    Se reemplaza SOLO el texto del Description; el resto del
        #    AttachedDocument (incluida la firma) se conserva intacto.
        #    IMPORTANTE: se usa etree.CDATA para que lxml NO escape los < y >
        #    de la invoice embebida al serializar el AttachedDocument exterior.
        #    Sin CDATA, lxml convertiría '<Invoice>' en '&lt;Invoice&gt;' (que
        #    aparece como 'gt' en el XML resultante), haciendo el archivo inválido.
        new_invoice_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + etree.tostring(
            target_inv, encoding="unicode"
        )
        desc.text = etree.CDATA(new_invoice_xml)

        # Asegurar que TODOS los Description con XML embebido usen CDATA.
        # Cuando lxml parsea un CDATA lo convierte a texto plano internamente;
        # al serializar de vuelta, si no se marca como CDATA, escapa los </>.
        # Esto afecta al ApplicationResponse y a cualquier otro XML embebido.
        _asegurar_cdata_descriptions(adb_root)

        # Pretty-print SOLO de la invoice (para legibilidad del preview), sin
        # tocar el exterior del AttachedDocument (preserva la firma).
        res.invoice_corregido = _xml_prettify(
            etree.tostring(target_inv, encoding="utf-8")
        ).decode("utf-8")

        # 8) Serializar el AttachedDocument completo (firma intacta).
        #    Sin pretty_print en el exterior para no alterar los bytes que
        #    cubre la firma.
        out = etree.tostring(adb_root, encoding="UTF-8", xml_declaration=True)
        res.xml_bytes = out
        if not res.cambios:
            res.mensaje = "No fue necesario corregir campos (ya completa)."

    except etree.XMLSyntaxError as e:
        res.ok = False
        res.mensaje = f"XML inválido: {e}"
    except Exception as e:  # noqa: BLE001
        res.ok = False
        res.mensaje = f"Error corrigiendo {nombre}: {e}"

    return res


def corregir_xml_con_plantilla(
    xml_bytes: bytes,
    cucon: str,
    nombre: str = "factura.xml",
    regimen: Optional[str] = None,
    csv_mutual_bytes: Optional[bytes] = None,
    mapa_at_mutual: Optional[
        Dict[str, List[Dict[str, Any]]]
    ] = None,
) -> ResultadoCorreccion:
    """Igual que corregir_xml pero usando la plantilla de referencia interna.

    El usuario sólo sube su factura incompleta; la plantilla es fija.
    """
    if csv_mutual_bytes is not None:
        validacion = validar_factura_mutual_con_csv(
            xml_bytes,
            csv_mutual_bytes,
            nombre,
            mapa_at_mutual=mapa_at_mutual,
        )
        if not validacion.ok:
            res = ResultadoCorreccion(nombre=nombre)
            res.ok = False
            res.mensaje = validacion.mensaje
            return res

    try:
        plantilla = cargar_plantilla()
    except FileNotFoundError:
        res = ResultadoCorreccion(nombre=nombre)
        res.ok = False
        res.mensaje = "No se encontró la plantilla de referencia interna."
        return res
    resultado = corregir_xml(
        xml_bytes,
        plantilla,
        cucon,
        nombre,
        regimen,
    )
    if (
        resultado.ok
        and csv_mutual_bytes is not None
        and validacion.detalles
    ):
        resultado.cambios[0:0] = validacion.detalles
    return resultado
