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
import io
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

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

MODALIDAD_PAGO_PREDETERMINADA = "Pago por evento"


def _aplicar_modalidad_pago_evento(target_inv, cambios: List[str]) -> None:
    """Crea o corrige MODALIDAD_PAGO con el valor 'Pago por evento'.

    La extensión de salud representa los campos mediante elementos
    AdditionalInformation con hijos Name y Value. Para conservar namespaces y
    atributos propios del XML, cuando falta el grupo se clona la estructura de
    otro AdditionalInformation existente.
    """
    adicionales = [
        el for el in target_inv.iter()
        if isinstance(el.tag, str) and _local(el.tag) == "AdditionalInformation"
    ]

    referencia = None
    modalidad = None

    for ai in adicionales:
        name_el = next(
            (c for c in ai if isinstance(c.tag, str) and _local(c.tag) == "Name"),
            None,
        )
        value_el = next(
            (c for c in ai if isinstance(c.tag, str) and _local(c.tag) == "Value"),
            None,
        )

        if referencia is None and name_el is not None:
            referencia = ai

        if (
            name_el is not None
            and (name_el.text or "").strip().upper() == "MODALIDAD_PAGO"
        ):
            modalidad = ai
            if value_el is None:
                value_el = etree.SubElement(ai, _tag_como_hijo(ai, "Value"))

            valor_anterior = (value_el.text or "").strip()
            if valor_anterior != MODALIDAD_PAGO_PREDETERMINADA:
                value_el.text = MODALIDAD_PAGO_PREDETERMINADA
                cambios.append(
                    "MODALIDAD_PAGO corregida "
                    f"({valor_anterior!r} -> {MODALIDAD_PAGO_PREDETERMINADA!r})"
                )
            return

    if referencia is None:
        cambios.append(
            "⚠️ No se pudo crear MODALIDAD_PAGO: "
            "no existe un AdditionalInformation de referencia"
        )
        return

    nuevo = copy.deepcopy(referencia)
    for child in list(nuevo):
        nuevo.remove(child)

    name_ref = next(
        (c for c in referencia if isinstance(c.tag, str) and _local(c.tag) == "Name"),
        None,
    )
    value_ref = next(
        (c for c in referencia if isinstance(c.tag, str) and _local(c.tag) == "Value"),
        None,
    )

    name_tag = name_ref.tag if name_ref is not None else _tag_como_hijo(nuevo, "Name")
    value_tag = value_ref.tag if value_ref is not None else _tag_como_hijo(nuevo, "Value")

    name_el = etree.SubElement(nuevo, name_tag)
    name_el.text = "MODALIDAD_PAGO"
    value_el = etree.SubElement(nuevo, value_tag)
    value_el.text = MODALIDAD_PAGO_PREDETERMINADA

    padre = referencia.getparent()
    posicion = padre.index(referencia) + 1
    padre.insert(posicion, nuevo)
    cambios.append("Insertado MODALIDAD_PAGO='Pago por evento'")


def _tag_como_hijo(parent, local_name: str) -> str:
    """Construye un tag con el mismo namespace del contenedor."""
    if isinstance(parent.tag, str) and parent.tag.startswith("{"):
        namespace = parent.tag.split("}", 1)[0][1:]
        return f"{{{namespace}}}{local_name}"
    return local_name


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

def corregir_xml(xml_bytes: bytes, plantilla_bytes: bytes, cucon: str,
                 nombre: str = "factura.xml") -> ResultadoCorreccion:
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
        _aplicar_modalidad_pago_evento(target_inv, res.cambios)

        # 7) CUCON dentro de la factura embebida
        if cucon and cucon.strip():
            _aplicar_cucon(target_inv, cucon.strip(), res.cambios)

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


def corregir_xml_con_plantilla(xml_bytes: bytes, cucon: str,
                               nombre: str = "factura.xml") -> ResultadoCorreccion:
    """Igual que corregir_xml pero usando la plantilla de referencia interna.

    El usuario sólo sube su factura incompleta; la plantilla es fija.
    """
    try:
        plantilla = cargar_plantilla()
    except FileNotFoundError:
        res = ResultadoCorreccion(nombre=nombre)
        res.ok = False
        res.mensaje = "No se encontró la plantilla de referencia interna."
        return res
    return corregir_xml(xml_bytes, plantilla, cucon, nombre)
