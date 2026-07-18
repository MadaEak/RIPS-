"""
Corrector XML para EPS SURA - Resolución 000948 de 2026
=======================================================

No requiere una plantilla externa. Toma la factura de SURA, conserva sus datos
propios y reconstruye el nodo Sector Salud con la estructura vigente.

El régimen se selecciona manualmente:
- Contributivo -> cobertura 16
- Subsidiado   -> cobertura 17

La modalidad se genera como pago por evento (código 04) y el CUCON se escribe
en NUMERO_CONTRATO.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from lxml import etree


NS_ATTACHED = "urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
NS_EXT = "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2"

XP_DESCRIPTION = (
    f".//{{{NS_CAC}}}Attachment/"
    f"{{{NS_CAC}}}ExternalReference/"
    f"{{{NS_CBC}}}Description"
)

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

PATRON_AUTORIZACION_SURA = re.compile(
    r"(?<!\d)(139610)(?!-)(\d+)"
)


@dataclass
class ResultadoCorreccionSura:
    nombre: str
    ok: bool = True
    mensaje: str = ""
    cambios: List[str] = field(default_factory=list)
    xml_bytes: Optional[bytes] = None
    invoice_corregido: str = ""


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return etree.QName(tag).localname


def _normalizar_nombre(valor: str) -> str:
    return re.sub(r"\s+", " ", (valor or "").strip()).upper()


def _normalizar_autorizacion_sura(valor: str) -> str:
    return PATRON_AUTORIZACION_SURA.sub(r"\1-\2", valor or "")


def _extraer_invoice(description_text: str):
    texto = (description_text or "").strip()
    if not texto:
        raise ValueError("El AttachedDocument no contiene la factura embebida.")

    inicio = texto.find("<Invoice")
    if inicio < 0:
        raise ValueError("No se encontró el nodo Invoice embebido.")

    declaracion = texto.rfind("<?xml", 0, inicio)
    if declaracion >= 0:
        texto = texto[declaracion:]

    return etree.fromstring(texto.encode("utf-8"))


def _buscar_custom_tag_sector_salud(invoice):
    for elemento in invoice.iter():
        if _local(elemento.tag) != "CustomTagGeneral":
            continue
        for descendiente in elemento.iter():
            if (
                _local(descendiente.tag) == "Group"
                and (descendiente.get("schemeName") or "").strip().lower()
                == "sector salud"
            ):
                return elemento
    return None


def _crear_custom_tag_sector_salud(invoice):
    extensiones = invoice.find(f"{{{NS_EXT}}}UBLExtensions")
    if extensiones is None:
        extensiones = etree.Element(f"{{{NS_EXT}}}UBLExtensions")
        invoice.insert(0, extensiones)

    extension = etree.SubElement(
        extensiones,
        f"{{{NS_EXT}}}UBLExtension",
    )
    contenido = etree.SubElement(
        extension,
        f"{{{NS_EXT}}}ExtensionContent",
    )
    custom_tag = etree.SubElement(contenido, "CustomTagGeneral")

    responsable = etree.SubElement(custom_tag, "Name")
    responsable.text = "Responsable"
    responsable_valor = etree.SubElement(custom_tag, "Value")
    responsable_valor.text = "url www.minsalud.gov.co"

    acto = etree.SubElement(custom_tag, "Name")
    acto.text = "Tipo, identificador:año del acto administrativo"
    acto_valor = etree.SubElement(custom_tag, "Value")
    acto_valor.text = "Resolución 000948:2026"

    return custom_tag


def _actualizar_resolucion(custom_tag, cambios: List[str]) -> None:
    hijos = [h for h in custom_tag if isinstance(h.tag, str)]

    for indice, hijo in enumerate(hijos):
        if _local(hijo.tag) != "Name":
            continue
        nombre = _normalizar_nombre(hijo.text or "")
        if "ACTO ADMIN" not in nombre:
            continue

        for siguiente in hijos[indice + 1:]:
            if _local(siguiente.tag) == "Name":
                break
            if _local(siguiente.tag) == "Value":
                anterior = (siguiente.text or "").strip()
                siguiente.text = "Resolución 000948:2026"
                if anterior != siguiente.text:
                    cambios.append(
                        "Acto administrativo actualizado a "
                        "Resolución 000948:2026"
                    )
                return

    nombre = etree.Element("Name")
    nombre.text = "Tipo, identificador:año del acto administrativo"
    valor = etree.Element("Value")
    valor.text = "Resolución 000948:2026"

    posicion = 2 if len(custom_tag) >= 2 else len(custom_tag)
    custom_tag.insert(posicion, nombre)
    custom_tag.insert(posicion + 1, valor)
    cambios.append(
        "Agregada identificación de la Resolución 000948 de 2026"
    )


def _obtener_interoperabilidad(custom_tag):
    for hijo in custom_tag:
        if _local(hijo.tag) == "Interoperabilidad":
            return hijo

    return etree.SubElement(custom_tag, "Interoperabilidad")


def _obtener_collection_usuario(interoperabilidad):
    group = None
    for hijo in interoperabilidad:
        if (
            _local(hijo.tag) == "Group"
            and (hijo.get("schemeName") or "").strip().lower()
            == "sector salud"
        ):
            group = hijo
            break

    if group is None:
        group = etree.SubElement(interoperabilidad, "Group")
        group.set("schemeName", "Sector Salud")

    for hijo in group:
        if (
            _local(hijo.tag) == "Collection"
            and (hijo.get("schemeName") or "").strip().lower()
            == "usuario"
        ):
            return hijo

    collection = etree.SubElement(group, "Collection")
    collection.set("schemeName", "Usuario")
    return collection


def _leer_campos_sector_salud(collection):
    valores = {}
    atributos = {}

    for info in collection:
        if _local(info.tag) != "AdditionalInformation":
            continue

        name_el = next(
            (c for c in info if _local(c.tag) == "Name"),
            None,
        )
        value_el = next(
            (c for c in info if _local(c.tag) == "Value"),
            None,
        )
        if name_el is None:
            continue

        nombre = _normalizar_nombre(name_el.text or "")
        valores[nombre] = (
            (value_el.text or "").strip()
            if value_el is not None
            else ""
        )
        atributos[nombre] = (
            dict(value_el.attrib)
            if value_el is not None
            else {}
        )

    return valores, atributos


def _extraer_anticipo(invoice, valores) -> str:
    anticipo = (valores.get("ANTICIPO") or "").strip()
    if anticipo:
        return anticipo

    for elemento in invoice.iter():
        if _local(elemento.tag) != "CustomField":
            continue
        if (elemento.get("Name") or "").strip().lower() == "anticipo":
            return (elemento.get("Value") or "0.00").strip() or "0.00"

    return "0.00"


def _normalizar_codigo_prestador(valor: str) -> str:
    digitos = re.sub(r"\D", "", valor or "")
    if len(digitos) >= 10:
        return digitos[:10]
    return digitos or "1300101145"



def _extraer_autorizaciones_custom_fields(invoice) -> List[str]:
    autorizaciones: List[str] = []

    for elemento in invoice.iter():
        if _local(elemento.tag) != "CustomField":
            continue

        categoria = (elemento.get("Category") or "").strip().upper()
        nombre = _normalizar_nombre(elemento.get("Name") or "")
        valor = (elemento.get("Value") or "").strip()

        if categoria != "AUT":
            continue
        if nombre not in ("NUMERO AUTORIZACIÓN", "NUMERO AUTORIZACION"):
            continue
        if not valor:
            continue

        autorizacion = _normalizar_autorizacion_sura(valor)
        if autorizacion:
            autorizaciones.append(autorizacion)

    return autorizaciones


def _construir_campos_repetidos_por_autorizacion(
    invoice,
    valores,
    cambios: List[str],
):
    autorizaciones = _extraer_autorizaciones_custom_fields(invoice)

    if autorizaciones:
        numero_autorizacion = ";".join(autorizaciones)
        vacios = ";".join([""] * len(autorizaciones))
        cambios.append(
            f"NUMERO_AUTORIZACION reconstruido desde "
            f"{len(autorizaciones)} registros AUT"
        )
        cambios.append(
            "NUMERO_MIPRES y NUMERO_ENTREGA_MIPRES ajustados "
            "a la cantidad de autorizaciones"
        )
        return numero_autorizacion, vacios, vacios

    cambios.append(
        "No se encontraron registros AUT; se conservaron los campos heredados"
    )
    return (
        _normalizar_autorizacion_sura(
            valores.get("NUMERO_AUTORIZACION", "")
        ),
        valores.get("NUMERO_ENTREGA_MIPRES", ""),
        valores.get("NUMERO_MIPRES", ""),
    )


def _normalizar_autorizaciones_invoice(invoice, cambios: List[str]) -> None:
    total = 0

    for elemento in invoice.iter():
        if elemento.text:
            nuevo = _normalizar_autorizacion_sura(elemento.text)
            if nuevo != elemento.text:
                elemento.text = nuevo
                total += 1

        for atributo, valor in list(elemento.attrib.items()):
            nuevo = _normalizar_autorizacion_sura(valor)
            if nuevo != valor:
                elemento.set(atributo, nuevo)
                total += 1

    if total:
        cambios.append(
            "Autorizaciones SURA normalizadas con guion después de 139610"
        )


def _reconstruir_sector_salud(
    invoice,
    cucon: str,
    regimen: str,
    cambios: List[str],
) -> None:
    custom_tag = _buscar_custom_tag_sector_salud(invoice)
    if custom_tag is None:
        custom_tag = _crear_custom_tag_sector_salud(invoice)
        cambios.append("Creado nodo de sector salud")

    _actualizar_resolucion(custom_tag, cambios)

    interoperabilidad = _obtener_interoperabilidad(custom_tag)

    for hijo in list(interoperabilidad):
        if _local(hijo.tag) == "InteroperabilidadPT":
            interoperabilidad.remove(hijo)
            cambios.append("Eliminado bloque heredado InteroperabilidadPT")

    collection = _obtener_collection_usuario(interoperabilidad)
    valores, _ = _leer_campos_sector_salud(collection)

    regimen_normalizado = regimen.strip().lower()
    if regimen_normalizado == "contributivo":
        cobertura_id = "16"
        cobertura_texto = "Plan UPC — Régimen Contributivo"
    elif regimen_normalizado == "subsidiado":
        cobertura_id = "17"
        cobertura_texto = "Plan UPC — Régimen Subsidiado"
    else:
        raise ValueError(
            "Seleccione manualmente el régimen Contributivo o Subsidiado."
        )

    (
        autorizacion,
        numero_entrega_mipres,
        numero_mipres,
    ) = _construir_campos_repetidos_por_autorizacion(
        invoice,
        valores,
        cambios,
    )

    datos = {
        "CODIGO_PRESTADOR": (
            _normalizar_codigo_prestador(
                valores.get("CODIGO_PRESTADOR", "")
            ),
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
            autorizacion,
            {},
        ),
        "NUMERO_ENTREGA_MIPRES": (
            numero_entrega_mipres,
            {},
        ),
        "NUMERO_MIPRES": (
            numero_mipres,
            {},
        ),
        "FACTURA_SIN_CONTRATO": (
            "",
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
            valores.get("CUOTA_RECUPERACION") or "0.00",
            {},
        ),
        "PAGOS_COMPARTIDOS": (
            valores.get("PAGOS_COMPARTIDOS") or "0.00",
            {},
        ),
        "ANTICIPO": (
            _extraer_anticipo(invoice, valores),
            {},
        ),
        "NUMERO_CONTRATO": (
            cucon,
            {},
        ),
        "NUMERO_POLIZA": (
            valores.get("NUMERO_POLIZA", ""),
            {},
        ),
    }

    for hijo in list(collection):
        if _local(hijo.tag) == "AdditionalInformation":
            collection.remove(hijo)

    for nombre in CAMPOS_SECTOR_SALUD_948:
        valor, atributos = datos[nombre]

        info = etree.SubElement(collection, "AdditionalInformation")
        name_el = etree.SubElement(info, "Name")
        name_el.text = nombre

        value_el = etree.SubElement(info, "Value")
        value_el.text = valor
        for atributo, contenido in atributos.items():
            value_el.set(atributo, contenido)

    cambios.append(
        "Reconstruido nodo Sector Salud con los 14 campos requeridos"
    )
    cambios.append(
        f"Cobertura configurada como régimen {regimen_normalizado}"
    )
    cambios.append("CUCON colocado en NUMERO_CONTRATO")


def corregir_xml_sura(
    xml_bytes: bytes,
    cucon: str,
    nombre: str = "factura.xml",
    regimen: Optional[str] = None,
) -> ResultadoCorreccionSura:
    resultado = ResultadoCorreccionSura(nombre=nombre)

    try:
        cucon_limpio = (cucon or "").strip()
        if not cucon_limpio or cucon_limpio == "0":
            raise ValueError(
                "Escriba un CUCON válido; NUMERO_CONTRATO no puede quedar vacío "
                "ni en cero."
            )

        root = etree.fromstring(xml_bytes)
        description = root.find(XP_DESCRIPTION)

        if description is None:
            raise ValueError(
                "No se encontró Attachment/ExternalReference/Description."
            )

        invoice = _extraer_invoice(description.text or "")

        _normalizar_autorizaciones_invoice(
            invoice,
            resultado.cambios,
        )
        _reconstruir_sector_salud(
            invoice,
            cucon_limpio,
            regimen or "",
            resultado.cambios,
        )

        invoice_text = etree.tostring(
            invoice,
            encoding="unicode",
            pretty_print=False,
        )
        invoice_text = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + invoice_text
        )

        description.text = etree.CDATA(invoice_text)

        resultado.xml_bytes = etree.tostring(
            root,
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=False,
        )
        resultado.invoice_corregido = invoice_text
        resultado.mensaje = "XML de SURA corregido correctamente."

    except Exception as exc:
        resultado.ok = False
        resultado.mensaje = str(exc)

    return resultado
