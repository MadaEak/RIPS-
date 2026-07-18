"""
Generador de JSON RIPS para EPS SURA
====================================

Extiende el generador general de RIPS y agrega:

- Selección manual del régimen.
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

import math
import os
import re
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from generador_json import CreadorJsonRips


PATRON_AUTORIZACION_SURA = re.compile(r"(?<!\d)(139610)(?!-)(\d+)")
PATRON_CUM_INICIAL = re.compile(r"^\s*(\d{4,8}-\d{1,3})(?:\s*-\s*[A-Z0-9]+)?", re.I)
PATRON_FORTALEZA = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(MCG|UG|MG|G|ML|UI|U)\b",
    re.I,
)


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
        texto = _normalizar_texto(valor)
        for numero, unidad in PATRON_FORTALEZA.findall(texto):
            numero = numero.replace(",", ".")
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
    registro_medico_prescriptor: Optional[str] = None

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

        df = pd.read_excel(ruta, dtype=str)
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
        fuente_norm = _normalizar_texto(nombre)
        fuente_compacta = _compactar(nombre)
        fortalezas = _extraer_fortalezas(nombre, concentracion)
        forma_fuente = _forma_general(nombre, forma)
        via_fuente = _extraer_via(nombre, forma)
        marca_fuente = _extraer_marca(nombre)

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
            if fortalezas and fortalezas_candidata:
                if fortalezas.isdisjoint(fortalezas_candidata):
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

            similitud = _similitud_texto(nombre, fila["Nombre"])
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

        cum = None
        coincidencia_cum = PATRON_CUM_INICIAL.match(codigo_original)
        if coincidencia_cum:
            cum = coincidencia_cum.group(1)

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
                    "Se seleccionó el IUM habilitado más semejante por "
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


class CreadorJsonRipsSura(CreadorJsonRips):
    def __init__(
        self,
        regimen: str,
        registro_medico_prescriptor: Optional[str] = None,
        ruta_tabla_ium: Optional[str] = None,
    ):
        super().__init__()

        regimen_normalizado = (regimen or "").strip().lower()
        if regimen_normalizado not in ("contributivo", "subsidiado"):
            raise ValueError(
                "Seleccione el régimen Contributivo o Subsidiado para SURA."
            )

        self.regimen = regimen_normalizado
        # El anexo no incluye un campo para registro médico.
        # Se conserva únicamente para el reporte de auditoría local.
        self.registro_medico_prescriptor = (
            (registro_medico_prescriptor or "").strip() or None
        )

        if ruta_tabla_ium is None:
            ruta_tabla_ium = str(
                Path(__file__).with_name("TablaReferenciaIUM.xlsx")
            )

        self.catalogo_ium = CatalogoIUM(ruta_tabla_ium)
        self.ultimo_reporte_ium: List[Dict[str, Any]] = []
        self.advertencias: List[str] = []

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

        contexto = self._contexto_hospitalario(usuario)
        usuario_id = (
            f"{usuario.get('tipoDocumentoIdentificacion', '')}-"
            f"{usuario.get('numDocumentoIdentificacion', '')}"
        )

        medicamentos_actualizados: List[Dict[str, Any]] = []

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

            if tipo_medicamento == "03":
                nombre_tecnologia = (
                    medicamento.get("nomTecnologiaSalud") or None
                )
                concentracion = medicamento.get(
                    "concentracionMedicamento"
                )
                unidad_medida = medicamento.get("unidadMedida")
                forma_farmaceutica = (
                    medicamento.get("formaFarmaceutica") or None
                )
            else:
                # Para medicamentos no magistrales:
                # M09 y M12 permiten null; M10 y M11 deben ir en cero.
                nombre_tecnologia = None
                concentracion = 0
                unidad_medida = 0
                forma_farmaceutica = None

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
                # No se cuenta con el documento personal del prescriptor.
                # El número de registro médico no reemplaza este dato.
                "tipoDocumentoIdentificacion": None,
                "numDocumentoIdentificacion": None,
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
                    registro_medico_prescriptor=(
                        self.registro_medico_prescriptor
                    ),
                ).to_dict()
            )

        servicios["medicamentos"] = medicamentos_actualizados

        if self.registro_medico_prescriptor:
            self.advertencias.append(
                f"{factura}: el registro médico "
                f"{self.registro_medico_prescriptor} se conservó únicamente "
                "en la auditoría. No se envió en M16/M17 porque esos campos "
                "corresponden al documento personal del prescriptor."
            )

    def generar_desde_zip(
        self,
        zip_bytes: bytes,
    ) -> Dict[str, Dict[str, Any]]:
        self.ultimo_reporte_ium = []
        self.advertencias = []

        facturas = super().generar_desde_zip(zip_bytes)

        tipo_usuario = "01" if self.regimen == "contributivo" else "04"

        for numero_factura, factura in facturas.items():
            for usuario in factura.get("usuarios", []):
                usuario["tipoUsuario"] = tipo_usuario

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

                self._ajustar_medicamentos(
                    numero_factura,
                    usuario,
                )

        return facturas
