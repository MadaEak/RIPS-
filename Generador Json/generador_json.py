"""
Generador de JSON RIPS - Resolución 2275 de 2023
================================================

Este módulo realiza la conversión de RIPS clásicos (en formato ZIP)
o de reportes de autorizaciones de Mutual (en formato CSV)
al formato unificado JSON requerido por la Resolución 2275 de 2023 en Colombia.
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
import unicodedata
import pandas as pd
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

# Mapeo de tipos de documento largos a abreviados
MAP_TIPO_DOCUMENTO = {
    "CEDULA DE CIUDADANIA": "CC",
    "CEDULA DE EXTRANJERIA": "CE",
    "TARJETA DE IDENTIDAD": "TI",
    "REGISTRO CIVIL": "RC",
    "PASAPORTE": "PA",
    "PERMISO ESPECIAL DE PERMANENCIA": "PE",
    "PEP": "PE",
    "PERMISO POR PROTECCION TEMPORAL": "PT",
    "PPT": "PT",
    "MENOR SIN IDENTIFICAR": "MS",
    "ADULTO SIN IDENTIFICAR": "AS",
    "CC": "CC",
    "CE": "CE",
    "TI": "TI",
    "RC": "RC",
    "PA": "PA",
    "PE": "PE",
    "PT": "PT",
    "MS": "MS",
    "AS": "AS"
}

def normalizar_tipo_documento(tipo: str) -> str:
    if not tipo:
        return "CC"
    tipo_up = tipo.strip().upper()
    return MAP_TIPO_DOCUMENTO.get(tipo_up, tipo_up)

# Normalización del NIT
def normalizar_nit(nit: str) -> str:
    if not nit:
        return ""
    nit = nit.strip().split("-")[0]
    return re.sub(r"\D", "", nit)

# Normalización del número de factura
def normalizar_factura(factura: str) -> str:
    if not factura:
        return ""
    factura = factura.strip()
    if factura.upper().startswith("FEC00"):
        return "FEC" + factura[5:]
    return factura

# Helper para fechas
def parsed_date_to_str(date_str: str) -> str:
    """Convierte DD/MM/YYYY o YYYY-MM-DD a YYYY-MM-DD"""
    if not date_str:
        return ""
    date_str = date_str.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", date_str):
        return date_str[:10]
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", date_str)
    if match:
        day, month, year = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return date_str

def parsed_datetime_to_str(date_str: str, time_str: str = "00:00") -> str:
    """Convierte fecha y hora a YYYY-MM-DD HH:MM"""
    f = parsed_date_to_str(date_str)
    if not f:
        return ""
    t = "00:00"
    if time_str:
        time_str = time_str.strip()
        if re.match(r"^\d{1,2}:\d{2}", time_str):
            parts = time_str.split(":")
            t = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    return f"{f} {t}"

def normalizar_descripcion(desc: str) -> str:
    """Quita el código de barras/cups inicial, acentos, espacios y deja en mayúsculas."""
    if not desc:
        return ""
    desc = desc.strip()
    # Eliminar código inicial (ej. "131M02 ") si lo tiene
    desc = re.sub(r'^[A-Z0-9-]+\s+', '', desc)
    # Quitar acentos
    desc = ''.join(c for c in unicodedata.normalize('NFD', desc) if unicodedata.category(c) != 'Mn')
    # Dejar solo caracteres alfanuméricos y pasar a mayúsculas
    desc = re.sub(r'[^A-Za-z0-9]', '', desc).upper()
    return desc

def calcular_fecha_nacimiento(edad: str, unidad_medida: str, fecha_ref_str: str = "") -> str:
    try:
        val_edad = int(edad)
    except:
        val_edad = 30
    
    ref_year = 2026
    if fecha_ref_str:
        f = parsed_date_to_str(fecha_ref_str)
        if f:
            ref_year = int(f.split("-")[0])
            
    if unidad_medida == "1":
        year = ref_year - val_edad
        return f"{year}-01-01"
    elif unidad_medida == "2":
        year = ref_year - (val_edad // 12)
        month = max(1, 12 - (val_edad % 12))
        return f"{year}-{month:02d}-01"
    else:
        year = ref_year - (val_edad // 365)
        return f"{year}-01-01"

def parsear_lineas_rips(contenido: str) -> List[List[str]]:
    lineas = []
    for linea in contenido.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        parts = [p.strip() for p in linea.split(",")]
        lineas.append(parts)
    return lineas

class CreadorJsonRips:
    def __init__(self):
        self.mapa_afiliados: Dict[str, Dict[str, Any]] = {}
        
    def cargar_datos_mutual_csv(self, csv_bytes: bytes) -> int:
        try:
            text = csv_bytes.decode("utf-8", errors="ignore")
            sep = ";" if text.count(";") > text.count(",") else ","
            df = pd.read_csv(io.StringIO(text), sep=sep, on_bad_lines="skip")
            df.columns = [c.strip().upper() for c in df.columns]
            
            col_doc = next((c for c in df.columns if "DOCUMENTO_AFILIADO" in c), None)
            col_tipo_doc = next((c for c in df.columns if "TIPO_DOC_AFILIADO" in c or "TIPO_DOCUMENTO_AFILIADO" in c), None)
            col_nac = next((c for c in df.columns if "FECHA_NACIMIENTO" in c or "NACIMIENTO_AFILIADO" in c), None)
            col_sexo = next((c for c in df.columns if "SEXO" in c or "GENERO" in c), None)
            col_tipo_u = next((c for c in df.columns if "TIPO_USUARIO" in c), None)
            col_depto = next((c for c in df.columns if "DEPARTAMENTO_AFILIADO" in c), None)
            col_mun = next((c for c in df.columns if "MUNICIPIO_AFILIADO" in c), None)
            
            if not col_doc or not col_tipo_doc:
                return 0
                
            cargados = 0
            for _, row in df.iterrows():
                doc = str(row[col_doc]).strip()
                tipo_doc = normalizar_tipo_documento(str(row[col_tipo_doc]))
                if pd.isna(row[col_doc]) or not doc:
                    continue
                    
                key = f"{tipo_doc}_{doc}".upper()
                
                fecha_nac = ""
                if col_nac and not pd.isna(row[col_nac]):
                    val_nac = str(row[col_nac]).strip()
                    date_part = val_nac.split(" ")[0]
                    fecha_nac = parsed_date_to_str(date_part)
                
                sexo = ""
                if col_sexo and not pd.isna(row[col_sexo]):
                    sexo = str(row[col_sexo]).strip().upper()
                    if sexo in ("M", "HOMBRE", "MASCULINO", "1"):
                        sexo = "M"
                    elif sexo in ("F", "MUJER", "FEMENINO", "2"):
                        sexo = "F"
                
                mpio_completo = ""
                if col_depto and col_mun and not pd.isna(row[col_depto]) and not pd.isna(row[col_mun]):
                    try:
                        dpto_val = int(float(row[col_depto]))
                        mun_val = int(float(row[col_mun]))
                        if mun_val > 1000:
                            mpio_completo = f"{mun_val:05d}"
                        else:
                            dpto_str = str(dpto_val)[:2]
                            mpio_completo = f"{int(dpto_str):02d}{mun_val:03d}"
                    except:
                        pass
                
                tipo_u = ""
                if col_tipo_u and not pd.isna(row[col_tipo_u]):
                    val_tu = str(row[col_tipo_u]).strip().lower()
                    if "contributivo" in val_tu:
                        tipo_u = "01"
                    elif "subsidio" in val_tu or "subsidiado" in val_tu:
                        tipo_u = "04"
                
                self.mapa_afiliados[key] = {
                    "fechaNacimiento": fecha_nac,
                    "sexo": sexo,
                    "codMunicipioResidencia": mpio_completo,
                    "tipoUsuario": tipo_u,
                    "row_raw": row.to_dict()
                }
                cargados += 1
            return cargados
        except Exception as e:
            print(f"Error cargando CSV: {e}")
            return 0

    def buscar_afiliado(self, tipo_doc: str, doc: str) -> Optional[Dict[str, Any]]:
        key = f"{normalizar_tipo_documento(tipo_doc)}_{doc}".upper()
        return self.mapa_afiliados.get(key)

    def generar_desde_zip(self, zip_bytes: bytes) -> Dict[str, Dict[str, Any]]:
        archivos: Dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for name in z.namelist():
                base = os.path.basename(name).upper()
                if len(base) >= 2 and base[:2] in ("US", "AF", "AC", "AP", "AT", "AM", "AH", "AU", "AN", "CT"):
                    tipo = base[:2]
                    try:
                        archivos[tipo] = z.read(name).decode("utf-8", errors="ignore")
                    except Exception:
                        pass
                        
        if "AF" not in archivos or "US" not in archivos:
            raise ValueError("El archivo ZIP no contiene los archivos AF y/o US mínimos requeridos.")

        lineas_af = parsear_lineas_rips(archivos["AF"])
        facturas: Dict[str, Dict[str, Any]] = {}
        for parts in lineas_af:
            if len(parts) < 10:
                continue
            nit = normalizar_nit(parts[3])
            num_factura = normalizar_factura(parts[4])
            facturas[num_factura] = {
                "numDocumentoIdObligado": nit or "806009230",
                "numFactura": num_factura,
                "tipoNota": None,
                "numNota": None,
                "fechaFactura": parts[5],
                "usuarios_ids": set(),
                "usuarios": []
            }

        lineas_us = parsear_lineas_rips(archivos["US"])
        mapa_us: Dict[str, Dict[str, Any]] = {}
        for parts in lineas_us:
            if len(parts) < 14:
                continue
            tipo_doc = normalizar_tipo_documento(parts[0])
            num_doc = parts[1]
            key_us = f"{tipo_doc}_{num_doc}".upper()
            
            info_csv = self.buscar_afiliado(tipo_doc, num_doc)
            
            fecha_nac = ""
            if info_csv and info_csv.get("fechaNacimiento"):
                fecha_nac = info_csv["fechaNacimiento"]
            else:
                fecha_nac = calcular_fecha_nacimiento(parts[8], parts[9])
                
            mpio = ""
            if info_csv and info_csv.get("codMunicipioResidencia"):
                mpio = info_csv["codMunicipioResidencia"]
            else:
                try:
                    mpio = f"{int(parts[11]):02d}{int(parts[12]):03d}"
                except:
                    mpio = "13001"
                    
            tipo_u = ""
            if info_csv and info_csv.get("tipoUsuario"):
                tipo_u = info_csv["tipoUsuario"]
            else:
                admin = parts[2].upper()
                val_u = parts[3]
                if admin == "ESSC07" or "CONTRIBUTIVO" in admin:
                    if val_u == "1": tipo_u = "01"
                    elif val_u == "2": tipo_u = "02"
                    elif val_u == "3": tipo_u = "03"
                    else: tipo_u = "01"
                else:
                    tipo_u = "04"
            
            zona = parts[13].upper()
            cod_zona = "01" if zona == "U" else ("02" if zona == "R" else "01")
            
            mapa_us[key_us] = {
                "tipoDocumentoIdentificacion": tipo_doc,
                "numDocumentoIdentificacion": num_doc,
                "tipoUsuario": tipo_u,
                "fechaNacimiento": fecha_nac,
                "codSexo": parts[10].upper(),
                "codPaisResidencia": "170",
                "codMunicipioResidencia": mpio,
                "codZonaTerritorialResidencia": cod_zona,
                "incapacidad": "NO",
                "codPaisOrigen": "170",
                "servicios": {
                    "consultas": [],
                    "procedimientos": [],
                    "urgencias": [],
                    "hospitalizacion": [],
                    "recienNacidos": [],
                    "medicamentos": [],
                    "otrosServicios": []
                }
            }

        # AC -> consultas
        def mapear_ac(parts: List[str], idx: int) -> Tuple[str, str, Dict[str, Any]]:
            fact = normalizar_factura(parts[0])
            u_key = f"{normalizar_tipo_documento(parts[2])}_{parts[3]}".upper()
            
            copago = 0
            try:
                copago = int(float(parts[15]))
            except:
                pass
            concepto_rec = "02" if copago > 0 else "05"
            
            val_serv = 0
            try:
                val_serv = int(float(parts[14]))
            except:
                pass
                
            fin = parts[7].strip()
            if len(fin) == 1: fin = f"0{fin}"
            causa = parts[8].strip()
            if len(causa) == 1: causa = f"0{causa}"
            tdx = parts[13].strip()
            if len(tdx) == 1: tdx = f"0{tdx}"
            
            serv_obj = {
                "codPrestador": parts[1],
                "fechaInicioAtencion": parsed_datetime_to_str(parts[4]),
                "numAutorizacion": parts[5] if parts[5] and parts[5].lower() != "null" else None,
                "codConsulta": parts[6],
                "modalidadGrupoServicioTecSal": "01",
                "grupoServicios": "01",
                "codServicio": 381,
                "finalidadTecnologiaSalud": fin or "10",
                "causaMotivoAtencion": causa or "21",
                "codDiagnosticoPrincipal": parts[9],
                "codDiagnosticoRelacionado1": parts[10] if parts[10] and parts[10].lower() != "null" else None,
                "codDiagnosticoRelacionado2": parts[11] if parts[11] and parts[11].lower() != "null" else None,
                "codDiagnosticoRelacionado3": parts[12] if parts[12] and parts[12].lower() != "null" else None,
                "tipoDiagnosticoPrincipal": tdx or "01",
                "tipoDocumentoIdentificacion": normalizar_tipo_documento(parts[2]),
                "numDocumentoIdentificacion": parts[3],
                "vrServicio": val_serv,
                "conceptoRecaudo": concepto_rec,
                "valorPagoModerador": copago,
                "numFEVPagoModerador": None,
                "consecutivo": idx
            }
            return fact, u_key, serv_obj

        # AP -> procedimientos
        def mapear_ap(parts: List[str], idx: int) -> Tuple[str, str, Dict[str, Any]]:
            fact = normalizar_factura(parts[0])
            u_key = f"{normalizar_tipo_documento(parts[2])}_{parts[3]}".upper()
            
            val_serv = 0
            try:
                val_serv = int(float(parts[14]))
            except:
                pass
                
            ambito = parts[7].strip()
            via = "01"
            if ambito == "2": via = "03"
            elif ambito == "3": via = "02"
            
            fin = parts[8].strip()
            if len(fin) == 1: fin = f"0{fin}"
            
            serv_obj = {
                "codPrestador": parts[1],
                "fechaInicioAtencion": parsed_datetime_to_str(parts[4]),
                "idMIPRES": None,
                "numAutorizacion": parts[5] if parts[5] and parts[5].lower() != "null" else None,
                "codProcedimiento": parts[6],
                "viaIngresoServicioSalud": via,
                "modalidadGrupoServicioTecSal": "01",
                "grupoServicios": "02",
                "codServicio": 1,
                "finalidadTecnologiaSalud": fin or "01",
                "tipoDocumentoIdentificacion": normalizar_tipo_documento(parts[2]),
                "numDocumentoIdentificacion": parts[3],
                "codDiagnosticoPrincipal": parts[10],
                "codDiagnosticoRelacionado": parts[11] if parts[11] and parts[11].lower() != "null" else None,
                "codComplicacion": parts[12] if parts[12] and parts[12].lower() != "null" else None,
                "vrServicio": val_serv,
                "conceptoRecaudo": "05",
                "valorPagoModerador": 0,
                "numFEVPagoModerador": None,
                "consecutivo": idx
            }
            return fact, u_key, serv_obj

        # AM -> medicamentos
        def mapear_am(parts: List[str], idx: int) -> Tuple[str, str, Dict[str, Any]]:
            fact = normalizar_factura(parts[0])
            u_key = f"{normalizar_tipo_documento(parts[2])}_{parts[3]}".upper()
            
            cant = 1
            try:
                cant = int(float(parts[11]))
            except:
                pass
            u_val = 0
            try:
                u_val = int(float(parts[12]))
            except:
                pass
            tot = cant * u_val
            try:
                tot = int(float(parts[13]))
            except:
                pass
                
            tmed = parts[6].strip()
            if len(tmed) == 1: tmed = f"0{tmed}"
            
            serv_obj = {
                "codPrestador": parts[1],
                "numAutorizacion": parts[4] if parts[4] and parts[4].lower() != "null" else None,
                "idMIPRES": None,
                "fechaDispensAdmon": parsed_datetime_to_str(facturas[fact]["fechaFactura"]) if fact in facturas else "2026-01-01 00:00",
                "codDiagnosticoPrincipal": "Z000",
                "codDiagnosticoRelacionado": None,
                "tipoMedicamento": tmed or "01",
                "codTecnologiaSalud": parts[5],
                "nomTecnologiaSalud": parts[7],
                "concentracionMedicamento": parts[9] or "0",
                "unidadMedida": parts[10] or "0",
                "formaFarmaceutica": parts[8] or "NINGUNA",
                "unidadMinDispensa": 1,
                "cantidadMedicamento": cant,
                "diasTratamiento": 30,
                "tipoDocumentoIdentificacion": normalizar_tipo_documento(parts[2]),
                "numDocumentoIdentificacion": parts[3],
                "vrUnitMedicamento": u_val,
                "vrServicio": tot,
                "conceptoRecaudo": "05",
                "valorPagoModerador": 0,
                "numFEVPagoModerador": None,
                "consecutivo": idx
            }
            return fact, u_key, serv_obj

        # AT -> otrosServicios
        def mapear_at(parts: List[str], idx: int) -> Tuple[str, str, Dict[str, Any]]:
            fact = normalizar_factura(parts[0])
            u_key = f"{normalizar_tipo_documento(parts[2])}_{parts[3]}".upper()
            
            ts = parts[5].strip()
            if len(ts) == 1: ts = f"0{ts}"
            cant = 1
            try:
                cant = int(float(parts[8]))
            except:
                pass
            u_val = 0
            try:
                u_val = int(float(parts[9]))
            except:
                pass
            tot = cant * u_val
            try:
                tot = int(float(parts[10]))
            except:
                pass
                
            serv_obj = {
                "codPrestador": parts[1],
                "numAutorizacion": parts[4] if parts[4] and parts[4].lower() != "null" else None,
                "idMIPRES": None,
                "fechaSuministroTecnologia": parsed_datetime_to_str(facturas[fact]["fechaFactura"]) if fact in facturas else "2026-01-01 00:00",
                "tipoOS": ts or "03",
                "codTecnologiaSalud": parts[6],
                "nomTecnologiaSalud": normalizar_descripcion(parts[7]),
                "cantidadOS": cant,
                "tipoDocumentoIdentificacion": normalizar_tipo_documento(parts[2]),
                "numDocumentoIdentificacion": parts[3],
                "vrUnitOS": u_val,
                "vrServicio": tot,
                "conceptoRecaudo": "05",
                "valorPagoModerador": 0,
                "numFEVPagoModerador": None,
                "consecutivo": idx
            }
            return fact, u_key, serv_obj

        # AH -> hospitalizacion
        def mapear_ah(parts: List[str], idx: int) -> Tuple[str, str, Dict[str, Any]]:
            fact = normalizar_factura(parts[0])
            u_key = f"{normalizar_tipo_documento(parts[2])}_{parts[3]}".upper()
            
            via = parts[4].strip()
            if len(via) == 1: via = f"0{via}"
            causa = parts[8].strip()
            if len(causa) == 1: causa = f"0{causa}"
            dest = parts[15].strip()
            if len(dest) == 1: dest = f"0{dest}"
            
            serv_obj = {
                "codPrestador": parts[1],
                "viaIngresoServicioSalud": via or "02",
                "fechaInicioAtencion": parsed_datetime_to_str(parts[5], parts[6]),
                "numAutorizacion": parts[7] if parts[7] and parts[7].lower() != "null" else None,
                "causaMotivoAtencion": causa or "38",
                "codDiagnosticoPrincipal": parts[9],
                "codDiagnosticoPrincipalE": parts[10],
                "codDiagnosticoRelacionadoE1": parts[11] if parts[11] and parts[11].lower() != "null" else None,
                "codDiagnosticoRelacionadoE2": parts[12] if parts[12] and parts[12].lower() != "null" else None,
                "codDiagnosticoRelacionadoE3": parts[13] if parts[13] and parts[13].lower() != "null" else None,
                "codComplicacion": parts[14] if parts[14] and parts[14].lower() != "null" else None,
                "condicionDestinoUsuarioEgreso": dest or "01",
                "codDiagnosticoCausaMuerte": parts[16] if parts[16] and parts[16].lower() != "null" else None,
                "fechaEgreso": parsed_datetime_to_str(parts[17], parts[18]),
                "consecutivo": idx
            }
            return fact, u_key, serv_obj

        # AU -> urgencias
        def mapear_au(parts: List[str], idx: int) -> Tuple[str, str, Dict[str, Any]]:
            fact = normalizar_factura(parts[0])
            u_key = f"{normalizar_tipo_documento(parts[2])}_{parts[3]}".upper()
            
            causa = parts[7].strip()
            if len(causa) == 1: causa = f"0{causa}"
            dest = parts[12].strip()
            if len(dest) == 1: dest = f"0{dest}"
            
            serv_obj = {
                "codPrestador": parts[1],
                "fechaInicioAtencion": parsed_datetime_to_str(parts[4], parts[5]),
                "numAutorizacion": parts[6] if parts[6] and parts[6].lower() != "null" else None,
                "causaMotivoAtencion": causa or "21",
                "codDiagnosticoPrincipal": parts[8],
                "codDiagnosticoRelacionadoE1": parts[9] if parts[9] and parts[9].lower() != "null" else None,
                "codDiagnosticoRelacionadoE2": parts[10] if parts[10] and parts[10].lower() != "null" else None,
                "codDiagnosticoRelacionadoE3": parts[11] if parts[11] and parts[11].lower() != "null" else None,
                "condicionDestinoUsuarioEgreso": dest or "01",
                "codDiagnosticoCausaMuerte": parts[13] if parts[13] and parts[13].lower() != "null" else None,
                "fechaEgreso": parsed_datetime_to_str(parts[14], parts[15]),
                "consecutivo": idx
            }
            return fact, u_key, serv_obj

        # AN -> recienNacidos
        def mapear_an(parts: List[str], idx: int) -> Tuple[str, str, Dict[str, Any]]:
            fact = normalizar_factura(parts[0])
            u_key = f"{normalizar_tipo_documento(parts[2])}_{parts[3]}".upper()
            
            gest = 40
            try:
                gest = int(float(parts[6]))
            except:
                pass
            ctrl = 0
            try:
                ctrl = int(float(parts[7]))
            except:
                pass
            peso = 3000
            try:
                peso = int(float(parts[9]))
            except:
                pass
                
            sexo = parts[8].strip().upper()
            cod_sexo = "01" if sexo == "M" else ("02" if sexo == "F" else "03")
            
            fecha_egreso = parsed_datetime_to_str(parts[4], parts[5])
            if parts[12] and parts[12].lower() != "null":
                fecha_egreso = parsed_datetime_to_str(parts[12], parts[13])
                
            serv_obj = {
                "codPrestador": parts[1],
                "tipoDocumentoIdentificacion": normalizar_tipo_documento(parts[2]),
                "numDocumentoIdentificacion": parts[3],
                "fechaNacimiento": parsed_datetime_to_str(parts[4], parts[5]),
                "edadGestacional": gest,
                "numConsultasCPrenatal": ctrl,
                "codSexoBiologico": cod_sexo,
                "peso": peso,
                "codDiagnosticoPrincipal": parts[10],
                "condicionDestinoUsuarioEgreso": "01" if not parts[11] else "03",
                "codDiagnosticoCausaMuerte": parts[11] if parts[11] and parts[11].lower() != "null" else None,
                "fechaEgreso": fecha_egreso,
                "consecutivo": idx
            }
            return fact, u_key, serv_obj

        mapeadores = {
            "AC": ("consultas", mapear_ac),
            "AP": ("procedimientos", mapear_ap),
            "AM": ("medicamentos", mapear_am),
            "AT": ("otrosServicios", mapear_at),
            "AH": ("hospitalizacion", mapear_ah),
            "AU": ("urgencias", mapear_au),
            "AN": ("recienNacidos", mapear_an)
        }

        # Procesar cada archivo de servicio que esté presente en el ZIP
        for tipo_file, (llave_json, mapeador) in mapeadores.items():
            if tipo_file in archivos:
                lineas_serv = parsear_lineas_rips(archivos[tipo_file])
                consecutivos: Dict[str, int] = {}
                
                for parts in lineas_serv:
                    if len(parts) < 8:
                        continue
                    try:
                        fact_num, u_key, serv_obj = mapeador(parts, 1)
                        if fact_num in facturas:
                            facturas[fact_num]["usuarios_ids"].add(u_key)
                            
                            cons_key = f"{fact_num}_{u_key}_{llave_json}"
                            consecutivos[cons_key] = consecutivos.get(cons_key, 0) + 1
                            serv_obj["consecutivo"] = consecutivos[cons_key]
                    except Exception as e:
                        pass

        # 5. Consolidar usuarios para cada factura
        for num_factura, fact_info in facturas.items():
            usuarios_factura = []
            user_idx = 1
            for u_key in fact_info["usuarios_ids"]:
                if u_key in mapa_us:
                    user_obj = json.loads(json.dumps(mapa_us[u_key]))
                    user_obj["consecutivo"] = user_idx
                    user_idx += 1
                    
                    servicios_encontrados = False
                    for tipo_file, (llave_json, mapeador) in mapeadores.items():
                        if tipo_file in archivos:
                            lineas_serv = parsear_lineas_rips(archivos[tipo_file])
                            c = 1
                            for parts in lineas_serv:
                                if len(parts) < 8:
                                    continue
                                try:
                                    fact_num, u_key_serv, serv_obj = mapeador(parts, c)
                                    if fact_num == num_factura and u_key_serv == u_key:
                                        if tipo_file == "AT":
                                            fecha_ref = None
                                            for ah_serv in user_obj["servicios"]["hospitalizacion"]:
                                                if ah_serv.get("fechaInicioAtencion"):
                                                    fecha_ref = ah_serv["fechaInicioAtencion"]
                                                    break
                                            if not fecha_ref:
                                                fecha_ref = parsed_datetime_to_str(fact_info["fechaFactura"])
                                            serv_obj["fechaSuministroTecnologia"] = fecha_ref
                                            
                                        serv_obj["consecutivo"] = c
                                        c += 1
                                        user_obj["servicios"][llave_json].append(serv_obj)
                                        servicios_encontrados = True
                                except Exception:
                                    pass
                                    
                    if servicios_encontrados:
                        servicios_filtrados = {}
                        for k, v in user_obj["servicios"].items():
                            if len(v) > 0:
                                servicios_filtrados[k] = v
                        user_obj["servicios"] = servicios_filtrados
                        
                        usuarios_factura.append(user_obj)
                        
            fact_info["usuarios"] = usuarios_factura
            del fact_info["usuarios_ids"]
            del fact_info["fechaFactura"]

        return facturas

    def generar_desde_csv(self, csv_bytes: bytes, num_factura: str) -> Dict[str, Any]:
        """Genera un archivo JSON de RIPS a partir del CSV de Mutual directamente para una factura dada."""
        self.cargar_datos_mutual_csv(csv_bytes)
        
        text = csv_bytes.decode("utf-8", errors="ignore")
        sep = ";" if text.count(";") > text.count(",") else ","
        df = pd.read_csv(io.StringIO(text), sep=sep, on_bad_lines="skip")
        df.columns = [c.strip().upper() for c in df.columns]
        
        col_nit = next((c for c in df.columns if "NIT" in c), None)
        nit_obligado = "806009230"
        if col_nit and len(df) > 0:
            val_nit = str(df.iloc[0][col_nit]).strip()
            nit_obligado = normalizar_nit(val_nit) or nit_obligado
            
        col_doc = next((c for c in df.columns if "DOCUMENTO_AFILIADO" in c), None)
        col_tipo_doc = next((c for c in df.columns if "TIPO_DOC_AFILIADO" in c or "TIPO_DOCUMENTO_AFILIADO" in c), None)
        
        if not col_doc or not col_tipo_doc:
            raise ValueError("El CSV de Mutual no contiene columnas de documento del afiliado.")
            
        usuarios_servicios: Dict[str, List[Dict[str, Any]]] = {}
        for _, row in df.iterrows():
            doc = str(row[col_doc]).strip()
            tipo_doc = normalizar_tipo_documento(str(row[col_tipo_doc]))
            if pd.isna(row[col_doc]) or not doc:
                continue
            key = f"{tipo_doc}_{doc}".upper()
            if key not in usuarios_servicios:
                usuarios_servicios[key] = []
            usuarios_servicios[key].append(row.to_dict())

        usuarios_json = []
        user_idx = 1
        
        for u_key, rows in usuarios_servicios.items():
            first_row = rows[0]
            tipo_doc = normalizar_tipo_documento(str(first_row[col_tipo_doc]))
            num_doc = str(first_row[col_doc]).strip()
            
            lookup = self.buscar_afiliado(tipo_doc, num_doc) or {}
            
            fecha_nac = lookup.get("fechaNacimiento")
            if not fecha_nac:
                col_nac = next((c for c in first_row.keys() if "FECHA_NACIMIENTO" in c), None)
                if col_nac and first_row[col_nac]:
                    date_part = str(first_row[col_nac]).strip().split(" ")[0]
                    fecha_nac = parsed_date_to_str(date_part)
                else:
                    fecha_nac = "1990-01-01"
                    
            sexo = lookup.get("sexo") or "F"
            mpio = lookup.get("codMunicipioResidencia") or "13001"
            tipo_u = lookup.get("tipoUsuario") or "04"
            
            user_obj = {
                "tipoDocumentoIdentificacion": tipo_doc,
                "numDocumentoIdentificacion": num_doc,
                "tipoUsuario": tipo_u,
                "fechaNacimiento": fecha_nac,
                "codSexo": sexo,
                "codPaisResidencia": "170",
                "codMunicipioResidencia": mpio,
                "codZonaTerritorialResidencia": "01",
                "incapacidad": "NO",
                "consecutivo": user_idx,
                "codPaisOrigen": "170",
                "servicios": {
                    "consultas": [],
                    "procedimientos": [],
                    "urgencias": [],
                    "hospitalizacion": [],
                    "recienNacidos": [],
                    "medicamentos": [],
                    "otrosServicios": []
                }
            }
            
            c_hosp = 1
            c_os = 1
            
            for row in rows:
                cod_prod = str(row.get("C_CODIGO_PRODUCTO", "")).strip()
                desc_prod = str(row.get("C_DESCRIPCION", "")).strip()
                cant = 1
                try:
                    cant = int(float(row.get("N_CANTIDAD_PRODUCTO", 1)))
                except:
                    pass
                val_prod = 0
                try:
                    val_prod = int(float(row.get("VALOR_PRODUCTO", 0)))
                except:
                    pass
                val_serv = cant * val_prod
                
                # Número de autorización: preferir N_NUMERO_AUTORIZACION / NUMERO_AUTORIZACION
                aut = None
                for col_name in ("NUMERO_AUTORIZACION", "N_NUMERO_AUTORIZACION"):
                    val_aut = str(row.get(col_name, "0")).strip()
                    if val_aut and val_aut not in ("nan", "0", "0.0", ""):
                        aut = val_aut
                        break
                # Fallback a CODIGO_BDUA si no hay autorización real
                if not aut:
                    val_bdua = str(row.get("CODIGO_BDUA", "0")).strip()
                    if val_bdua and val_bdua not in ("nan", "0", "0.0", ""):
                        aut = val_bdua

                hab = str(row.get("C_CODIGO_HABILITACION", "130010114501")).strip()

                # Fecha/hora de la notificación (inicio de atención real en el CSV)
                f_notif_raw = str(row.get("F_FECHA_NOTIFICACION", row.get("F_FECHA", ""))).strip()
                f_hora_raw = str(row.get("F_HORA", "")).strip()
                # Extraer solo fecha de la columna de notificación
                f_notif_date = f_notif_raw.split(" ")[0] if f_notif_raw else ""
                # Extraer hora de F_HORA (formato "DD/MM/YYYY HH:MM AM/PM" o "HH:MM")
                hora_part = "00:00"
                if f_hora_raw:
                    # Puede ser "02/07/2026 03:10 PM" o "15:10"
                    parts_hora = f_hora_raw.split(" ")
                    for ph in parts_hora:
                        if ":" in ph and len(ph) <= 5:
                            hora_part = ph
                            break
                    # Convertir a 24h si hay AM/PM
                    if "PM" in f_hora_raw.upper() or "AM" in f_hora_raw.upper():
                        try:
                            import datetime as _dt
                            fmt = "%I:%M %p" if len(hora_part) <= 5 else "%I:%M"
                            ampm = "PM" if "PM" in f_hora_raw.upper() else "AM"
                            t_obj = _dt.datetime.strptime(f"{hora_part} {ampm}", "%I:%M %p")
                            hora_part = t_obj.strftime("%H:%M")
                        except:
                            pass

                fecha_atencion_base = parsed_date_to_str(f_notif_date) if f_notif_date else "2026-01-01"
                fecha_atencion = f"{fecha_atencion_base} {hora_part}"
                
                dx = str(row.get("C_DIAGNOSTICO_PPAL", "F323")).strip()
                if pd.isna(row.get("C_DIAGNOSTICO_PPAL")) or dx == "nan":
                    dx = "F323"
                # Diagnóstico externo (usado en codDiagnosticoPrincipalE) = primer dx relacionado
                dx1 = str(row.get("C_DIAGNOSTICO_1", "")).strip()
                if not dx1 or dx1 in ("nan", "N", "0"):
                    dx1 = None
                dx_ext = dx1 or dx  # Si no hay relacionado, usar el principal
                
                if cod_prod.startswith("131") or cod_prod.startswith("135") or "HOSPITAL" in str(row.get("C_TIPO_SERVICIO", "")).upper():
                    # Fecha de egreso: usar F_FECHA_NOTIFICACION con F_HORA
                    fecha_egreso = fecha_atencion  # mismo campo (notificación)

                    # Fecha inicio real de la atención: F_FECHA_HORA_INGRESO_PTE
                    f_ingreso_raw = str(row.get("F_FECHA_HORA_INGRESO_PTE", "")).strip()
                    f_ingreso_date = f_ingreso_raw.split(" ")[0] if f_ingreso_raw else ""
                    fecha_inicio_hosp = parsed_date_to_str(f_ingreso_date) + " " + hora_part if f_ingreso_date else fecha_atencion

                    hosp_obj = {
                        "codPrestador": hab,
                        "viaIngresoServicioSalud": "02",
                        "fechaInicioAtencion": fecha_inicio_hosp,
                        "numAutorizacion": aut,
                        "causaMotivoAtencion": "38",
                        "codDiagnosticoPrincipal": dx,
                        "codDiagnosticoPrincipalE": dx_ext,
                        "codDiagnosticoRelacionadoE1": None,
                        "codDiagnosticoRelacionadoE2": None,
                        "codDiagnosticoRelacionadoE3": None,
                        "codComplicacion": None,
                        "condicionDestinoUsuarioEgreso": "01",
                        "codDiagnosticoCausaMuerte": None,
                        "fechaEgreso": fecha_egreso,
                        "consecutivo": c_hosp
                    }
                    user_obj["servicios"]["hospitalizacion"].append(hosp_obj)
                    c_hosp += 1

                    # Valor unitario diario = VALOR_PRODUCTO / N_CANTIDAD_PRODUCTO
                    vr_unit = val_prod
                    if cant > 1:
                        vr_unit = round(val_prod / cant)

                    # Crear registros diarios en otrosServicios (uno por día de hospitalización)
                    for day_idx in range(cant):
                        try:
                            import datetime as _dt2
                            dt_base = _dt2.datetime.strptime(fecha_inicio_hosp, "%Y-%m-%d %H:%M")
                            dt_new = dt_base + _dt2.timedelta(days=day_idx)
                            fecha_sumin = dt_new.strftime("%Y-%m-%d %H:%M")
                        except:
                            fecha_sumin = fecha_inicio_hosp

                        os_obj = {
                            "codPrestador": hab,
                            "numAutorizacion": aut,
                            "idMIPRES": None,
                            "fechaSuministroTecnologia": fecha_sumin,
                            "tipoOS": "03",
                            "codTecnologiaSalud": cod_prod,
                            "nomTecnologiaSalud": normalizar_descripcion(desc_prod),
                            "cantidadOS": 1,
                            "tipoDocumentoIdentificacion": tipo_doc,
                            "numDocumentoIdentificacion": num_doc,
                            "vrUnitOS": vr_unit,
                            "vrServicio": vr_unit,
                            "conceptoRecaudo": "05",
                            "valorPagoModerador": 0,
                            "numFEVPagoModerador": None,
                            "consecutivo": c_os
                        }
                        user_obj["servicios"]["otrosServicios"].append(os_obj)
                        c_os += 1
                else:
                    os_obj = {
                        "codPrestador": hab,
                        "numAutorizacion": aut,
                        "idMIPRES": None,
                        "fechaSuministroTecnologia": fecha_atencion,
                        "tipoOS": "01" if "DISPOSITIVO" in desc_prod.upper() else "04",
                        "codTecnologiaSalud": cod_prod,
                        "nomTecnologiaSalud": normalizar_descripcion(desc_prod),
                        "cantidadOS": cant,
                        "tipoDocumentoIdentificacion": tipo_doc,
                        "numDocumentoIdentificacion": num_doc,
                        "vrUnitOS": val_prod,
                        "vrServicio": val_serv,
                        "conceptoRecaudo": "05",
                        "valorPagoModerador": 0,
                        "numFEVPagoModerador": None,
                        "consecutivo": c_os
                    }
                    user_obj["servicios"]["otrosServicios"].append(os_obj)
                    c_os += 1

            servicios_filtrados = {}
            for k, v in user_obj["servicios"].items():
                if len(v) > 0:
                    servicios_filtrados[k] = v
            user_obj["servicios"] = servicios_filtrados
            
            if len(user_obj["servicios"]) > 0:
                usuarios_json.append(user_obj)
                user_idx += 1
                
        json_output = {
            "numDocumentoIdObligado": nit_obligado,
            "numFactura": num_factura,
            "tipoNota": None,
            "numNota": None,
            "usuarios": usuarios_json
        }
        
        return json_output
