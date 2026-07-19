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

import csv
import io
import json
import re
import zipfile
from pathlib import Path
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

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

VERSION_CORRECTOR_XML_SURA = "2026.07.18-v5-recaudo-exacto-xml-rips"


CONCEPTOS_RECAUDO = {
    "01": "COPAGO",
    "02": "CUOTA_MODERADORA",
    "03": "PAGOS_COMPARTIDOS",
    "04": "ANTICIPO",
    "05": "NO_APLICA",
}


@dataclass
class RecaudoRipsFactura:
    factura: str
    copago: Decimal = Decimal("0")
    cuota_moderadora: Decimal = Decimal("0")
    pagos_compartidos: Decimal = Decimal("0")
    anticipo: Decimal = Decimal("0")
    fuentes: List[str] = field(default_factory=list)
    detalles: List[str] = field(default_factory=list)
    errores: List[str] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return (
            self.copago
            + self.cuota_moderadora
            + self.pagos_compartidos
            + self.anticipo
        )

    def valores(self) -> Dict[str, Decimal]:
        return {
            "01": self.copago,
            "02": self.cuota_moderadora,
            "03": self.pagos_compartidos,
            "04": self.anticipo,
        }

    def resumen(self) -> Dict[str, Any]:
        return {
            "factura": self.factura,
            "copago": float(self.copago),
            "cuota_moderadora": float(self.cuota_moderadora),
            "pagos_compartidos": float(self.pagos_compartidos),
            "anticipo": float(self.anticipo),
            "total_recaudo": float(self.total),
            "fuentes": ", ".join(sorted(set(self.fuentes))),
            "estado": (
                "ERROR"
                if self.errores
                else ("CON RECAUDO" if self.total > 0 else "SIN RECAUDO")
            ),
            "observaciones": " | ".join(self.errores or self.detalles),
        }



@dataclass
class ResultadoCorreccionSura:
    nombre: str
    ok: bool = True
    mensaje: str = ""
    cambios: List[str] = field(default_factory=list)
    xml_bytes: Optional[bytes] = None
    invoice_corregido: str = ""



def _normalizar_factura_sura(valor: Any) -> str:
    texto = re.sub(r"\s+", "", str(valor or "")).upper()
    texto = re.sub(r"^FEC0+", "FEC", texto)
    return texto


def _decimal_seguro(valor: Any) -> Decimal:
    texto = str(valor or "").strip().replace(",", ".")
    if not texto or texto.lower() in {"null", "none", "nan"}:
        return Decimal("0")

    try:
        resultado = Decimal(texto)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Valor monetario inválido: {valor!r}")

    if resultado < 0:
        raise ValueError(f"El valor monetario no puede ser negativo: {valor!r}")
    return resultado


def _formatear_moneda(valor: Decimal) -> str:
    return f"{valor.quantize(Decimal('0.01')):.2f}"


def _decodificar_archivo_rips(contenido: bytes) -> str:
    for codificacion in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return contenido.decode(codificacion)
        except UnicodeDecodeError:
            continue
    return contenido.decode("latin-1", errors="replace")


def _obtener_recaudo(
    mapa: Dict[str, RecaudoRipsFactura],
    factura: Any,
) -> RecaudoRipsFactura:
    numero = _normalizar_factura_sura(factura)
    if not numero:
        raise ValueError("Se encontró un registro RIPS sin número de factura.")

    if numero not in mapa:
        mapa[numero] = RecaudoRipsFactura(factura=numero)
    return mapa[numero]


def _leer_recaudos_json_rips(
    contenido: bytes,
    nombre_archivo: str,
    mapa: Dict[str, RecaudoRipsFactura],
) -> None:
    try:
        documento = json.loads(contenido.decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError(
            f"No fue posible leer el JSON {nombre_archivo}: {exc}"
        ) from exc

    documentos = documento if isinstance(documento, list) else [documento]

    for factura_json in documentos:
        if not isinstance(factura_json, dict):
            continue

        recaudo = _obtener_recaudo(
            mapa,
            factura_json.get("numFactura"),
        )
        recaudo.fuentes.append(nombre_archivo)

        encontrados = 0
        for usuario in factura_json.get("usuarios", []) or []:
            servicios = usuario.get("servicios", {}) or {}

            for lista in servicios.values():
                if not isinstance(lista, list):
                    continue

                for servicio in lista:
                    if not isinstance(servicio, dict):
                        continue

                    concepto = str(
                        servicio.get("conceptoRecaudo") or "05"
                    ).zfill(2)
                    valor = _decimal_seguro(
                        servicio.get("valorPagoModerador")
                    )

                    if valor <= 0 or concepto == "05":
                        continue

                    encontrados += 1
                    if concepto == "01":
                        recaudo.copago += valor
                    elif concepto == "02":
                        recaudo.cuota_moderadora += valor
                    elif concepto == "03":
                        recaudo.pagos_compartidos += valor
                    elif concepto == "04":
                        recaudo.anticipo += valor
                    else:
                        recaudo.errores.append(
                            f"Concepto de recaudo no reconocido: {concepto}"
                        )

        recaudo.detalles.append(
            f"{nombre_archivo}: {encontrados} registros de recaudo en JSON"
        )


def _leer_recaudos_planos_rips(
    archivos: List[Tuple[str, bytes]],
    mapa: Dict[str, RecaudoRipsFactura],
    nombre_zip: str,
) -> None:
    valores_af: Dict[str, Decimal] = {}
    valores_ac: Dict[str, Decimal] = {}

    for nombre_archivo, contenido in archivos:
        nombre_base = Path(nombre_archivo).name.upper()
        texto = _decodificar_archivo_rips(contenido)

        if nombre_base.startswith("AF") and nombre_base.endswith(".TXT"):
            lector = csv.reader(io.StringIO(texto))
            for numero_linea, fila in enumerate(lector, start=1):
                if not fila:
                    continue
                if len(fila) < 14:
                    raise ValueError(
                        f"{nombre_archivo}, línea {numero_linea}: "
                        "registro AF incompleto."
                    )

                factura = _normalizar_factura_sura(fila[4])
                valor = _decimal_seguro(fila[13])
                valores_af[factura] = valores_af.get(
                    factura, Decimal("0")
                ) + valor

                recaudo = _obtener_recaudo(mapa, factura)
                recaudo.fuentes.append(f"{nombre_zip}/{nombre_archivo}")

        elif nombre_base.startswith("AC") and nombre_base.endswith(".TXT"):
            lector = csv.reader(io.StringIO(texto))
            for numero_linea, fila in enumerate(lector, start=1):
                if not fila:
                    continue
                if len(fila) < 16:
                    raise ValueError(
                        f"{nombre_archivo}, línea {numero_linea}: "
                        "registro AC incompleto."
                    )

                factura = _normalizar_factura_sura(fila[0])
                valor = _decimal_seguro(fila[15])
                valores_ac[factura] = valores_ac.get(
                    factura, Decimal("0")
                ) + valor

                recaudo = _obtener_recaudo(mapa, factura)
                recaudo.fuentes.append(f"{nombre_zip}/{nombre_archivo}")

    facturas = set(valores_af) | set(valores_ac)

    for factura in facturas:
        recaudo = _obtener_recaudo(mapa, factura)
        af = valores_af.get(factura, Decimal("0"))
        ac = valores_ac.get(factura, Decimal("0"))

        # En los RIPS planos usados por este proyecto, el valor de usuario
        # del archivo AC corresponde a cuota moderadora. El AF contiene el
        # total general denominado históricamente "copago".
        if ac > 0:
            if af > 0 and af != ac:
                recaudo.errores.append(
                    "Inconsistencia entre AF y AC: "
                    f"AF={_formatear_moneda(af)} y "
                    f"AC={_formatear_moneda(ac)}."
                )
            else:
                recaudo.cuota_moderadora += ac
                recaudo.detalles.append(
                    "Cuota moderadora obtenida de la suma del campo "
                    f"usuario en AC: {_formatear_moneda(ac)}."
                )
                if af == ac:
                    recaudo.detalles.append(
                        "El total AF coincide con AC y no se sumó dos veces."
                    )
        elif af > 0:
            recaudo.copago += af
            recaudo.detalles.append(
                "Copago obtenido del total AF: "
                f"{_formatear_moneda(af)}."
            )
        else:
            recaudo.detalles.append(
                "AF y AC no reportan valores pagados por el usuario."
            )


def extraer_recaudos_rips_zip(
    zip_bytes: bytes,
    nombre_zip: str = "rips.zip",
) -> Dict[str, RecaudoRipsFactura]:
    """Extrae recaudos por factura desde RIPS planos o JSON."""
    mapa: Dict[str, RecaudoRipsFactura] = {}

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archivo_zip:
            archivos = [
                (nombre, archivo_zip.read(nombre))
                for nombre in archivo_zip.namelist()
                if not nombre.endswith("/")
            ]
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{nombre_zip} no es un ZIP válido.") from exc

    jsons = [
        (nombre, contenido)
        for nombre, contenido in archivos
        if nombre.lower().endswith(".json")
    ]

    if jsons:
        for nombre, contenido in jsons:
            _leer_recaudos_json_rips(
                contenido,
                f"{nombre_zip}/{nombre}",
                mapa,
            )
    else:
        _leer_recaudos_planos_rips(
            archivos,
            mapa,
            nombre_zip,
        )

    if not mapa:
        raise ValueError(
            f"{nombre_zip}: no se encontraron facturas en AF, AC o JSON."
        )

    return mapa


def consolidar_recaudos_rips(
    archivos_zip: Iterable[Tuple[str, bytes]],
) -> Dict[str, RecaudoRipsFactura]:
    """Combina varios ZIP y detecta duplicados incompatibles."""
    consolidado: Dict[str, RecaudoRipsFactura] = {}

    for nombre_zip, contenido in archivos_zip:
        parcial = extraer_recaudos_rips_zip(contenido, nombre_zip)

        for factura, nuevo in parcial.items():
            anterior = consolidado.get(factura)
            if anterior is None:
                consolidado[factura] = nuevo
                continue

            if anterior.valores() != nuevo.valores():
                anterior.errores.append(
                    f"La factura también aparece en {nombre_zip} con "
                    "valores de recaudo diferentes."
                )
                anterior.fuentes.extend(nuevo.fuentes)
                anterior.detalles.extend(nuevo.detalles)
                anterior.errores.extend(nuevo.errores)
                continue

            anterior.fuentes.extend(nuevo.fuentes)
            anterior.detalles.extend(nuevo.detalles)
            anterior.errores.extend(nuevo.errores)

    return consolidado


def extraer_numero_factura_xml_sura(xml_bytes: bytes) -> str:
    root = etree.fromstring(xml_bytes)
    description = root.find(XP_DESCRIPTION)
    if description is None:
        raise ValueError(
            "No se encontró Attachment/ExternalReference/Description."
        )

    invoice = _extraer_invoice(description.text or "")
    id_element = next(
        (
            hijo
            for hijo in invoice
            if _local(hijo.tag) == "ID"
        ),
        None,
    )
    if id_element is None or not (id_element.text or "").strip():
        raise ValueError("La factura embebida no contiene cbc:ID.")

    return _normalizar_factura_sura(id_element.text)


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
    """Obtiene únicamente el ANTICIPO explícito del Sector Salud.

    Los CustomField genéricos llamados "Anticipo" suelen representar el
    total prepagado de la factura y pueden corresponder a copagos o cuotas
    moderadoras. No se deben convertir automáticamente en concepto 04.
    """
    anticipo = (valores.get("ANTICIPO") or "").strip()
    return anticipo or "0.00"



def _valor_monetario_decimal(valor: str) -> Decimal:
    texto = str(valor or "").strip().replace(",", ".")
    if not texto:
        return Decimal("0")

    try:
        return Decimal(texto)
    except (InvalidOperation, ValueError):
        raise ValueError(
            f"Valor monetario inválido en el XML de sector salud: {valor!r}"
        )


def _leer_recaudos_sector_salud(invoice) -> Dict[str, Decimal]:
    custom_tag = _buscar_custom_tag_sector_salud(invoice)
    if custom_tag is None:
        return {
            "01": Decimal("0"),
            "02": Decimal("0"),
            "03": Decimal("0"),
            "04": Decimal("0"),
        }

    interoperabilidad = _obtener_interoperabilidad(custom_tag)
    collection = _obtener_collection_usuario(interoperabilidad)
    valores, _ = _leer_campos_sector_salud(collection)

    return {
        "01": _valor_monetario_decimal(valores.get("COPAGO", "0")),
        "02": _valor_monetario_decimal(
            valores.get("CUOTA_MODERADORA", "0")
        ),
        "03": _valor_monetario_decimal(
            valores.get("PAGOS_COMPARTIDOS", "0")
        ),
        "04": _valor_monetario_decimal(
            _extraer_anticipo(invoice, valores)
        ),
    }



def _leer_totales_recaudo_exactos_xml(
    invoice,
) -> Dict[str, Decimal]:
    """Obtiene totales exactos de recaudo desde varios nodos del XML."""
    resultados: Dict[str, Decimal] = {}

    valores_paid = []
    for hijo in invoice:
        if _local(hijo.tag) != "PrepaidPayment":
            continue
        paid = next(
            (
                elemento
                for elemento in hijo
                if _local(elemento.tag) == "PaidAmount"
            ),
            None,
        )
        if paid is not None and (paid.text or "").strip():
            valores_paid.append(
                _valor_monetario_decimal(paid.text or "0")
            )

    if valores_paid:
        resultados["PrepaidPayment/PaidAmount"] = sum(
            valores_paid,
            Decimal("0"),
        )

    legal = _obtener_elemento_directo(invoice, "LegalMonetaryTotal")
    if legal is not None:
        prepaid_amount = next(
            (
                elemento
                for elemento in legal
                if _local(elemento.tag) == "PrepaidAmount"
            ),
            None,
        )
        if prepaid_amount is not None and (prepaid_amount.text or "").strip():
            resultados["LegalMonetaryTotal/PrepaidAmount"] = (
                _valor_monetario_decimal(prepaid_amount.text or "0")
            )

    for elemento in invoice.iter():
        local = _local(elemento.tag)
        if local == "TotAnticiposCop" and (elemento.text or "").strip():
            resultados["TotalesCop/TotAnticiposCop"] = (
                _valor_monetario_decimal(elemento.text or "0")
            )
            break

    for elemento in invoice.iter():
        if _local(elemento.tag) != "CustomField":
            continue
        if (elemento.get("Name") or "").strip().lower() != "anticipo":
            continue
        valor = (elemento.get("Value") or "").strip()
        if valor:
            resultados["CustomField/Anticipo"] = (
                _valor_monetario_decimal(valor)
            )
        break

    return resultados


def _recaudo_xml_exactamente_respaldado(
    invoice,
    valores_rips: Dict[str, Decimal],
    diferencias: List[Tuple[str, Decimal, Decimal]],
) -> Tuple[bool, List[str]]:
    conceptos_no_cero = [
        codigo
        for codigo in ("01", "02", "03", "04")
        if valores_rips.get(codigo, Decimal("0")) > 0
    ]

    if len(conceptos_no_cero) != 1 or len(diferencias) != 1:
        return False, []

    codigo_diferente, _, valor_rips = diferencias[0]
    if codigo_diferente != conceptos_no_cero[0]:
        return False, []

    exactos = _leer_totales_recaudo_exactos_xml(invoice)
    exactos_no_cero = {
        origen: valor
        for origen, valor in exactos.items()
        if valor > 0
    }
    coincidencias = [
        origen
        for origen, valor in exactos_no_cero.items()
        if valor == valor_rips
    ]
    valores_distintos = set(exactos_no_cero.values())

    if (
        len(coincidencias) >= 2
        and len(valores_distintos) == 1
        and next(iter(valores_distintos)) == valor_rips
    ):
        return True, coincidencias

    return False, coincidencias


def _resolver_recaudos_xml_rips(
    invoice,
    recaudo_rips: RecaudoRipsFactura,
    cambios: List[str],
) -> Dict[str, Decimal]:
    if recaudo_rips.errores:
        raise ValueError(
            f"{recaudo_rips.factura}: errores en los RIPS: "
            + " | ".join(recaudo_rips.errores)
        )

    valores_xml = _leer_recaudos_sector_salud(invoice)
    valores_rips = recaudo_rips.valores()

    # El anticipo solo existe a nivel de FEV; no se obtiene del RIPS.
    valores_rips["04"] = valores_xml["04"]

    diferencias: List[
        Tuple[str, Decimal, Decimal]
    ] = []

    for codigo in ("01", "02", "03"):
        valor_xml = valores_xml[codigo]
        valor_rips = valores_rips[codigo]

        if valor_xml != valor_rips:
            diferencias.append((codigo, valor_xml, valor_rips))

    if diferencias:
        respaldado, fuentes = _recaudo_xml_exactamente_respaldado(
            invoice,
            valores_rips,
            diferencias,
        )

        if respaldado:
            codigo, valor_xml, valor_rips = diferencias[0]
            cambios.append(
                "Se corrigió una pérdida de precisión del campo "
                f"{CONCEPTOS_RECAUDO[codigo]} del Sector Salud: "
                f"XML resumido={_formatear_moneda(valor_xml)}, "
                f"valor exacto={_formatear_moneda(valor_rips)}. "
                "Confirmado por: "
                + ", ".join(fuentes)
            )
        else:
            detalles = [
                (
                    f"{CONCEPTOS_RECAUDO[codigo]}: "
                    f"XML={_formatear_moneda(valor_xml)}, "
                    f"RIPS={_formatear_moneda(valor_rips)}"
                )
                for codigo, valor_xml, valor_rips in diferencias
            ]
            raise ValueError(
                f"{recaudo_rips.factura}: los recaudos del XML original "
                "y los RIPS no coinciden. No se modificó la factura: "
                + "; ".join(detalles)
            )

    cambios.append(
        "Recaudos cruzados con RIPS: "
        + ", ".join(
            f"{CONCEPTOS_RECAUDO[codigo]}="
            f"{_formatear_moneda(valor)}"
            for codigo, valor in valores_rips.items()
        )
    )
    return valores_rips


def _obtener_elemento_directo(invoice, nombre_local: str):
    return next(
        (
            hijo
            for hijo in invoice
            if _local(hijo.tag) == nombre_local
        ),
        None,
    )


def _ajustar_totales_prepaid(
    invoice,
    nuevo_total: Decimal,
    tipo_operacion: str,
    cambios: List[str],
) -> None:
    legal = _obtener_elemento_directo(invoice, "LegalMonetaryTotal")
    if legal is None:
        raise ValueError("El XML no contiene cac:LegalMonetaryTotal.")

    prepaid_amount = next(
        (
            hijo
            for hijo in legal
            if _local(hijo.tag) == "PrepaidAmount"
        ),
        None,
    )
    if prepaid_amount is None:
        prepaid_amount = etree.SubElement(
            legal,
            f"{{{NS_CBC}}}PrepaidAmount",
        )

    total_anterior = _valor_monetario_decimal(prepaid_amount.text or "0")
    prepaid_amount.text = _formatear_moneda(nuevo_total)
    prepaid_amount.set("currencyID", "COP")

    # En los modos que acreditan el recaudo a la factura, el valor reduce
    # el saldo pagadero. En SS-Reporte es informativo.
    modos_acreditan = {"SS-CUFE", "SS-CUDE", "SS-POS", "SS-NUM"}
    if tipo_operacion.upper() in modos_acreditan:
        payable = next(
            (
                hijo
                for hijo in legal
                if _local(hijo.tag) == "PayableAmount"
            ),
            None,
        )
        if payable is not None:
            payable_anterior = _valor_monetario_decimal(
                payable.text or "0"
            )
            payable_nuevo = (
                payable_anterior + total_anterior - nuevo_total
            )
            if payable_nuevo < 0:
                raise ValueError(
                    "El recaudo es mayor al saldo pagadero de la factura."
                )
            payable.text = _formatear_moneda(payable_nuevo)
            if not (payable.get("currencyID") or "").strip():
                payable.set("currencyID", "COP")

    cambios.append(
        "LegalMonetaryTotal/PrepaidAmount ajustado a "
        f"{_formatear_moneda(nuevo_total)}"
    )


def _ajustar_prepaid_payment(
    invoice,
    recaudos: Dict[str, Decimal],
    cambios: List[str],
) -> None:
    """Reconstruye PrepaidPayment con un grupo por concepto."""
    fechas_por_concepto: Dict[str, str] = {}
    primera_fecha = ""

    prepaid_existentes = [
        hijo
        for hijo in list(invoice)
        if _local(hijo.tag) == "PrepaidPayment"
    ]

    for nodo in prepaid_existentes:
        id_element = next(
            (h for h in nodo if _local(h.tag) == "ID"),
            None,
        )
        fecha_element = next(
            (h for h in nodo if _local(h.tag) == "ReceivedDate"),
            None,
        )
        concepto = (
            (id_element.get("schemeID") or "").strip()
            if id_element is not None
            else ""
        )
        fecha = (
            (fecha_element.text or "").strip()
            if fecha_element is not None
            else ""
        )
        if fecha:
            primera_fecha = primera_fecha or fecha
            if concepto:
                fechas_por_concepto[concepto] = fecha

    for nodo in prepaid_existentes:
        invoice.remove(nodo)

    customization = _obtener_elemento_directo(
        invoice,
        "CustomizationID",
    )
    if customization is None:
        customization = etree.Element(f"{{{NS_CBC}}}CustomizationID")
        invoice.insert(0, customization)

    total = sum(recaudos.values(), Decimal("0"))

    if total == 0:
        customization.text = "SS-SinAporte"
        _ajustar_totales_prepaid(
            invoice,
            Decimal("0"),
            "SS-SinAporte",
            cambios,
        )
        cambios.append(
            "Sin recaudo según RIPS: eliminados los bloques "
            "PrepaidPayment y configurado SS-SinAporte"
        )
        return

    tipo_operacion = (customization.text or "").strip()
    tipos_validos = {
        "SS-CUFE",
        "SS-CUDE",
        "SS-POS",
        "SS-NUM",
        "SS-REPORTE",
    }
    if tipo_operacion.upper() not in tipos_validos:
        raise ValueError(
            "Los RIPS informan recaudo, pero el tipo de operación del XML "
            f"es {tipo_operacion or 'vacío'}. Debe corresponder a "
            "SS-CUFE, SS-CUDE, SS-POS, SS-Num o SS-Reporte."
        )

    issue_date = _obtener_elemento_directo(invoice, "IssueDate")
    fecha_respaldo = (
        primera_fecha
        or ((issue_date.text or "").strip() if issue_date is not None else "")
    )
    if not fecha_respaldo:
        raise ValueError(
            "No fue posible determinar la fecha de recepción del recaudo."
        )

    legal = _obtener_elemento_directo(invoice, "LegalMonetaryTotal")
    if legal is None:
        raise ValueError("El XML no contiene LegalMonetaryTotal.")
    posicion = list(invoice).index(legal)

    consecutivo = 1
    for codigo in ("01", "02", "03", "04"):
        valor = recaudos[codigo]
        if valor <= 0:
            continue

        prepaid = etree.Element(f"{{{NS_CAC}}}PrepaidPayment")
        identificador = etree.SubElement(
            prepaid,
            f"{{{NS_CBC}}}ID",
        )
        identificador.text = str(consecutivo)
        identificador.set("schemeID", codigo)

        paid_amount = etree.SubElement(
            prepaid,
            f"{{{NS_CBC}}}PaidAmount",
        )
        paid_amount.text = _formatear_moneda(valor)
        paid_amount.set("currencyID", "COP")

        received_date = etree.SubElement(
            prepaid,
            f"{{{NS_CBC}}}ReceivedDate",
        )
        received_date.text = (
            fechas_por_concepto.get(codigo) or fecha_respaldo
        )

        invoice.insert(posicion, prepaid)
        posicion += 1
        consecutivo += 1

    _ajustar_totales_prepaid(
        invoice,
        total,
        tipo_operacion,
        cambios,
    )
    cambios.append(
        f"Reconstruidos {consecutivo - 1} bloques PrepaidPayment "
        "con ID consecutivo y schemeID de concepto"
    )



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
    recaudos: Dict[str, Decimal],
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
            _formatear_moneda(recaudos["01"]),
            {},
        ),
        "CUOTA_MODERADORA": (
            _formatear_moneda(recaudos["02"]),
            {},
        ),
        "CUOTA_RECUPERACION": (
            valores.get("CUOTA_RECUPERACION") or "0.00",
            {},
        ),
        "PAGOS_COMPARTIDOS": (
            _formatear_moneda(recaudos["03"]),
            {},
        ),
        "ANTICIPO": (
            _formatear_moneda(recaudos["04"]),
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
    recaudo_rips: Optional[RecaudoRipsFactura] = None,
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

        if recaudo_rips is None:
            raise ValueError(
                "Cargue el ZIP de RIPS correspondiente a esta factura."
            )

        recaudos = _resolver_recaudos_xml_rips(
            invoice,
            recaudo_rips,
            resultado.cambios,
        )

        _normalizar_autorizaciones_invoice(
            invoice,
            resultado.cambios,
        )
        _reconstruir_sector_salud(
            invoice,
            cucon_limpio,
            regimen or "",
            recaudos,
            resultado.cambios,
        )
        _ajustar_prepaid_payment(
            invoice,
            recaudos,
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
