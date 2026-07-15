"""
Herramientas EPS Mutual - Facturación Electrónica (FEV Salud)
==============================================================
App unificada con dos pestañas:
  🧹 Limpiador de RIPS  -> completa/normaliza RIPS (subsidiado/contributivo)
  🩺 Corrector XML      -> completa facturas electrónicas incompletas (AttachedDocument)
                           y coloca el CUCON en el grupo sector salud.

Ejecutar:
    streamlit run app.py
"""

from __future__ import annotations

import io
import os
import sys

import streamlit as st

# Rutas a los módulos de cada herramienta
BASE = os.path.dirname(os.path.abspath(__file__))
RUTA_LIMPIADOR = os.path.join(BASE, "Limpiador Rips")
RUTA_CORRECTOR = os.path.join(BASE, "Corrector Xml")
RUTA_GENERADOR = os.path.join(BASE, "Generador Json")
for p in (RUTA_LIMPIADOR, RUTA_CORRECTOR, RUTA_GENERADOR):
    if p not in sys.path:
        sys.path.insert(0, p)

import json
import zipfile

from limpiador_rips import (  # noqa: E402
    limpiar_zip_bytes,
    cargar_csv_autorizaciones,
    inferir_regimen_zip,
)
from corrector_xml import corregir_xml_con_plantilla  # noqa: E402
from generador_json import CreadorJsonRips, normalizar_factura  # noqa: E402

st.set_page_config(page_title="EPS Mutual - FEV Salud", page_icon="🏥", layout="wide")

st.title("🏥 Herramientas EPS Mutual — Facturación Electrónica (FEV Salud)")

tab1, tab2, tab3 = st.tabs(["🧹 Limpiador de RIPS", "🩺 Corrector de XML", "📝 Creador de JSON"])

# ===========================================================================
# PESTAÑA 1 — LIMPIADOR DE RIPS
# ===========================================================================
with tab1:
    st.subheader("🧹 Limpiador de RIPS — EPS Mutual")
    st.caption(
        "Reglas: FEC00→FEC · eliminación de AD y sus referencias · "
        "NIT 806009230-2→806009230 · (contributivo) ESS207→ESSC07 y régimen 2→1 en US"
    )

    with st.sidebar:
        st.header("⚙️ Configuración (RIPS)")
        st.markdown(
            "1. (Opcional) Carga el CSV de autorizaciones de Mutual para deducir el "
            "régimen automáticamente.\n"
            "2. Carga uno o varios `.zip` con los RIPS.\n"
            "3. Ajusta el régimen y procesa."
        )
        st.divider()
        csv_file = st.file_uploader(
            "CSV de autorizaciones (plataforma Mutual)",
            type=["csv"],
            help="Columnas C_DOCUMENTO_AFILIADO y C_ADMINISTRADORA (ESS207=subsidiado, ESSC07=contributivo)",
        )
        if csv_file:
            mapa = cargar_csv_autorizaciones(csv_file.getvalue())
            st.session_state.mapa_rips = mapa
            if mapa.doc_a_regimen:
                st.success(f"CSV cargado: {mapa.total_filas} filas, "
                           f"{len(mapa.doc_a_regimen)} afiliados.")
            else:
                st.error("El CSV no tiene las columnas C_DOCUMENTO_AFILIADO / "
                         "C_ADMINISTRADORA esperadas.")
        else:
            st.session_state.mapa_rips = None

        st.divider()
        st.markdown("**Reglas de limpieza**")
        st.markdown("- FEC que empiezan en `FEC00` → `FEC`")
        st.markdown("- Archivo **AD** y su referencia en CT → eliminados")
        st.markdown("- NIT `806009230-2` → `806009230`")
        st.markdown("- **Contributivo**: `ESS207`→`ESSC07` (AF/US); régimen `2`→`1` (US)")

    archivos_subidos = st.file_uploader(
        "Cargar archivos ZIP de RIPS",
        type=["zip"],
        accept_multiple_files=True,
        help="Puedes seleccionar varios ZIP a la vez.",
    )

    if not archivos_subidos:
        st.info("👆 Carga uno o más archivos .zip para comenzar.")
    else:
        st.divider()
        st.subheader("📋 Régimen de cada archivo")
        mapa = st.session_state.get("mapa_rips", None)
        hay_csv = bool(mapa and mapa.doc_a_regimen)

        if hay_csv:
            st.info("🔎 El régimen se deduce del CSV (cruce por documento del afiliado). "
                    "Pulsa **Revisar cruce CSV** para asignarlo automáticamente.")
            if st.button("🔍 Revisar cruce CSV", use_container_width=True,
                         help="Detecta automáticamente el régimen de cada ZIP según el CSV."):
                for f in archivos_subidos:
                    regimen_inf, no_enc, _ = inferir_regimen_zip(f.getvalue(), mapa)
                    if regimen_inf is not None:
                        st.session_state[f"reg_{f.name}"] = (
                            "Contributivo" if regimen_inf == "contributivo" else "Subsidiado"
                        )
                no_todos = []
                for f in archivos_subidos:
                    _, no_enc, _ = inferir_regimen_zip(f.getvalue(), mapa)
                    for d in no_enc:
                        no_todos.append(f"{f.name} → doc {d}")
                if no_todos:
                    st.warning("⚠️ No se encontraron en el CSV: " + "; ".join(no_todos))
                else:
                    st.success("✅ Todos los RIPS cruzados encontraron coincidencia en el CSV.")

        opciones = {"Subsidiado": False, "Contributivo": True, "Automático (CSV)": None}
        regimenes: dict[str, bool] = {}
        cols = st.columns([3, 2, 1, 3])
        cols[0].write("**Archivo**"); cols[1].write("**Régimen**")
        cols[2].write("**Tamaño**"); cols[3].write("**Cruce CSV**")

        for f in archivos_subidos:
            c1, c2, c3, c4 = st.columns([3, 2, 1, 3])
            c1.write(f.name)
            defecto = "Automático (CSV)" if hay_csv else "Subsidiado"
            sel = c2.selectbox("Régimen", options=list(opciones.keys()),
                               index=list(opciones.keys()).index(defecto),
                               key=f"reg_{f.name}", label_visibility="collapsed")
            regimenes[f.name] = opciones[sel]
            c3.write(f"{f.size/1024:.1f} KB")
            if hay_csv:
                regimen_inf, no_enc, detalles = inferir_regimen_zip(f.getvalue(), mapa)
                if regimen_inf == "contributivo":
                    c4.markdown("✅ ESSC07")
                elif regimen_inf == "subsidiado":
                    c4.markdown("✅ ESS207")
                else:
                    c4.markdown("⚠️ no cruzado")
                for det in detalles:
                    if det.get("encontrado"):
                        aut = det.get("autorizacion") or "—"
                        est = (det.get("estado") or "—").upper()
                        badge = "✅" if est in ("APROBADO", "", "—") else "⚠️"
                        c4.caption(f"{badge} doc {det['doc']}: aut {aut} · {est}")
                if no_enc:
                    c4.caption(f"⚠️ docs sin match: {', '.join(no_enc[:3])}{'…' if len(no_enc)>3 else ''}")
            else:
                c4.write("—")

        st.divider()
        if st.button("🚀 Procesar RIPS", type="primary", use_container_width=True):
            bloqueados = []
            for f in archivos_subidos:
                reg_sel = regimenes[f.name]
                if reg_sel is None:
                    if not (mapa and mapa.doc_a_regimen):
                        bloqueados.append(f.name)
                    else:
                        regimen_inf, _, _ = inferir_regimen_zip(f.getvalue(), mapa)
                        if regimen_inf is None:
                            bloqueados.append(f.name)
            if bloqueados:
                st.error("🚫 Procesamiento bloqueado. Los siguientes RIPS no tienen régimen "
                         "definido de forma segura (no cruzaron en el CSV y siguen en "
                         "'Automático'). Elígelos manualmente y vuelve a procesar:")
                for b in bloqueados:
                    st.markdown(f"- ⚠️ **{b}**")
            else:
                resultados = []
                barra = st.progress(0, text="Procesando...")
                for i, f in enumerate(archivos_subidos):
                    bytes_zip = f.getvalue()
                    reg_sel = regimenes[f.name]
                    if reg_sel is None:
                        regimen_inf, _, _ = inferir_regimen_zip(bytes_zip, mapa)
                        es_cont = (regimen_inf == "contributivo")
                    else:
                        es_cont = reg_sel
                    resultados.append(limpiar_zip_bytes(bytes_zip, f.name, es_cont))
                    barra.progress((i + 1) / len(archivos_subidos), text=f"Procesado {f.name}")
                barra.empty()
                st.session_state.resultados_rips = resultados
                st.session_state.procesado_rips = True

        if st.session_state.get("procesado_rips") and st.session_state.get("resultados_rips"):
            st.success("✅ Procesamiento completado.")
            st.divider()
            st.subheader("📊 Resultado por archivo")
            for res in st.session_state.resultados_rips:
                with st.expander(res.resumen, expanded=True):
                    if res.errores:
                        for err in res.errores:
                            st.error(err)
                        continue
                    modificados = [a for a in res.archivos if a.fue_modificado]
                    if not modificados:
                        st.write("Sin cambios necesarios.")
                    for a in res.archivos:
                        if a.eliminado:
                            st.markdown(f"- 🗑️ **{a.nombre}** — eliminado (AD)")
                        elif a.lineas_modificadas > 0:
                            st.markdown(f"- ✏️ **{a.nombre}** — {a.lineas_modificadas}/{a.total_lineas} líneas")
                            with st.popover("Ver cambios"):
                                for c in a.cambios:
                                    st.caption(c)
                        else:
                            st.markdown(f"- ✅ **{a.nombre}** — sin cambios")
                    if res.zip_bytes:
                        st.download_button(
                            label=f"⬇️ Descargar {os.path.splitext(res.nombre_zip)[0]}_limpio.zip",
                            data=res.zip_bytes, file_name=f"{os.path.splitext(res.nombre_zip)[0]}_limpio.zip",
                            mime="application/zip", key=f"dl_{res.nombre_zip}")

# ===========================================================================
# PESTAÑA 2 — CORRECTOR DE XML
# ===========================================================================
with tab2:
    st.subheader("🩺 Corrector de Facturas Electrónicas (FEV Salud)")
    st.caption(
        "Completa facturas (AttachedDocument) incompletas usando como plantilla un XML "
        "de referencia válido. Inserta el grupo sector salud y coloca el CUCON en "
        "NUMERO_CONTRATO. La firma electrónica se conserva intacta."
    )

    with st.sidebar:
        st.header("⚙️ Configuración (XML)")
        st.markdown(
            "1. Carga uno o varios XML de facturas **incompletas**.\n"
            "2. Escribe el **CUCON** y procesa.\n\n"
            "La plantilla de referencia es **interna** (la usa la herramienta para "
            "saber qué campos completar; tú no la subes)."
        )
        st.divider()
        st.markdown("**Qué hace**")
        st.markdown("- Inserta los campos faltantes usando la plantilla interna")
        st.markdown("- Completa el **grupo sector salud** (TotalesCop, FabricanteSoftware, etc.)")
        st.markdown("- Coloca el **CUCON** en `NUMERO_CONTRATO`")
        st.markdown("- **No** toca la firma electrónica")

    xmls_subidos = st.file_uploader(
        "Facturas XML incompletas a corregir",
        type=["xml"],
        accept_multiple_files=True,
        help="Uno o varios AttachedDocument de factura incompleta.",
    )
    cucon = st.text_input(
        "CUCON (Código Único de Contrato)",
        placeholder="5607359a4137900018a0bf1dcf9db7a92fc5a568efd939843ae980e9cb24e0a9",
        help="Resolución 0948 de 2026, Art. 11: va en NUMERO_CONTRATO en lugar del nº de contrato.",
    )

    if not xmls_subidos:
        st.info("👆 Carga al menos una factura incompleta y escribe el CUCON.")
    else:
        st.divider()
        if st.button("🚀 Corregir XML", type="primary", use_container_width=True):
            resultados = []
            barra = st.progress(0, text="Corrigiendo...")
            for i, f in enumerate(xmls_subidos):
                res = corregir_xml_con_plantilla(f.getvalue(), cucon, f.name)
                resultados.append(res)
                barra.progress((i + 1) / len(xmls_subidos), text=f"Corregido {f.name}")
            barra.empty()
            st.session_state.resultados_xml = resultados

        if st.session_state.get("resultados_xml"):
            st.success("✅ Corrección completada.")
            st.divider()
            for res in st.session_state.resultados_xml:
                with st.expander(f"{res.nombre} — {'✅' if res.ok else '❌'}", expanded=True):
                    if not res.ok:
                        st.error(res.mensaje)
                        continue
                    if res.cambios:
                        st.markdown("**Cambios aplicados:**")
                        for c in res.cambios:
                            st.markdown(f"- {c}")
                    else:
                        st.write("Sin cambios necesarios.")
                    if res.xml_bytes:
                        st.download_button(
                            label=f"⬇️ Descargar {os.path.splitext(res.nombre)[0]}_corregido.xml",
                            data=res.xml_bytes,
                            file_name=f"{os.path.splitext(res.nombre)[0]}_corregido.xml",
                            mime="application/xml",
                            key=f"xml_{res.nombre}",
                        )
                        with st.expander("👁️ Ver XML corregido (factura embebida)"):
                            st.code(res.invoice_corregido, language="xml")

            # Descarga masiva
            buenos = [r for r in st.session_state.resultados_xml if r.ok and r.xml_bytes]
            if buenos:
                st.divider()
                st.subheader("📦 Descarga masiva")
                buf = io.BytesIO()
                import zipfile

                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
                    for r in buenos:
                        nombre_salida = f"{os.path.splitext(r.nombre)[0]}_corregido.xml"
                        zout.writestr(nombre_salida, r.xml_bytes)
                st.download_button(
                    label=f"⬇️ Descargar TODOS los XML corregidos (.zip) — {len(buenos)}",
                    data=buf.getvalue(),
                    file_name="XML_corregidos_Mutual.zip",
                    mime="application/zip",
                )

# ===========================================================================
# PESTAÑA 3 — CREADOR DE JSON (Resolución 2275 de 2023)
# ===========================================================================
with tab3:
    st.subheader("📝 Creador de JSON RIPS (Resolución 2275 de 2023)")
    st.caption(
        "Genera los archivos JSON exigidos por el Ministerio de Salud a partir de RIPS ZIP planos, "
        "del CSV de autorizaciones de Mutual, o de ambos combinados."
    )

    with st.sidebar:
        st.header("⚙️ Configuración (JSON)")
        st.markdown(
            "1. Elige el **Origen de datos**.\n"
            "2. Carga los archivos correspondientes.\n"
            "3. Opcionalmente, carga el CSV de Mutual para lookups (ej. fecha de nacimiento)."
        )

    origen = st.radio(
        "Origen de datos para la generación",
        options=["Solo RIPS (ZIP)", "Solo CSV de Mutual", "Ambos (RIPS + CSV de Mutual)"],
        index=0,
        help="Elige de qué archivo(s) partir para estructurar el JSON.",
        horizontal=True
    )

    # Permitir cargar el CSV o ZIP según el origen
    if origen == "Solo CSV de Mutual":
        st.info("ℹ️ Al usar solo el CSV, generaremos un JSON consolidado para la factura que indiques a continuación.")
        num_factura_input = st.text_input("Número de factura a asignar", value="FEC39627", help="Por ejemplo, FEC39627")
        csv_file_json = st.file_uploader("Cargar CSV de autorizaciones de Mutual", type=["csv"], key="csv_json_only")
    elif origen == "Solo RIPS (ZIP)":
        zip_files_json = st.file_uploader("Cargar ZIP de RIPS planos", type=["zip"], accept_multiple_files=True, key="zip_json_only")
    else: # Ambos
        st.markdown("### 1. Cargar CSV de Mutual (Base de datos de afiliados)")
        csv_file_json = st.file_uploader("Cargar CSV de autorizaciones de Mutual", type=["csv"], key="csv_json_ambos")
        st.markdown("### 2. Cargar ZIPs de RIPS planos")
        zip_files_json = st.file_uploader("Cargar ZIP de RIPS planos", type=["zip"], accept_multiple_files=True, key="zip_json_ambos")

    if st.button("🚀 Generar JSON RIPS", type="primary", use_container_width=True):
        creador = CreadorJsonRips()
        
        # 1. Caso Solo CSV de Mutual
        if origen == "Solo CSV de Mutual":
            if not csv_file_json:
                st.error("Por favor, carga el CSV de Mutual.")
            elif not num_factura_input:
                st.error("Por favor, ingresa el número de factura.")
            else:
                try:
                    with st.spinner("Generando JSON..."):
                        factura_limpia = normalizar_factura(num_factura_input)
                        json_res = creador.generar_desde_csv(csv_file_json.getvalue(), factura_limpia)
                        
                        st.success(f"✅ JSON generado con éxito para la factura {factura_limpia}.")
                        
                        # Mostrar una vista previa del JSON
                        with st.expander("👁️ Vista previa del JSON generado"):
                            st.json(json_res)
                            
                        # Descargar el JSON
                        json_str = json.dumps(json_res, indent=2, ensure_ascii=False)
                        st.download_button(
                            label=f"⬇️ Descargar {factura_limpia}.json",
                            data=json_str,
                            file_name=f"{factura_limpia}.json",
                            mime="application/json"
                        )
                except Exception as e:
                    st.error(f"Error durante la generación: {e}")
                    
        # 2. Caso Solo RIPS (ZIP)
        elif origen == "Solo RIPS (ZIP)":
            if not zip_files_json:
                st.error("Por favor, carga al menos un ZIP de RIPS.")
            else:
                try:
                    with st.spinner("Generando JSON..."):
                        all_jsons = {}
                        for f in zip_files_json:
                            res = creador.generar_desde_zip(f.getvalue())
                            all_jsons.update(res)
                            
                        st.success(f"✅ Procesados {len(zip_files_json)} ZIPs. Se generaron {len(all_jsons)} facturas JSON.")
                        
                        # Mostrar facturas generadas
                        for num_fac, json_data in all_jsons.items():
                            with st.expander(f"Factura {num_fac} (Ver JSON)", expanded=False):
                                st.json(json_data)
                                json_str = json.dumps(json_data, indent=2, ensure_ascii=False)
                                st.download_button(
                                    label=f"⬇️ Descargar {num_fac}.json",
                                    data=json_str,
                                    file_name=f"{num_fac}.json",
                                    mime="application/json",
                                    key=f"dl_ind_{num_fac}"
                                )
                                
                        # Descarga masiva ZIP
                        if len(all_jsons) > 0:
                            buf = io.BytesIO()
                            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
                                for num_fac, json_data in all_jsons.items():
                                    json_str = json.dumps(json_data, indent=2, ensure_ascii=False)
                                    zout.writestr(f"{num_fac}.json", json_str)
                            st.divider()
                            st.download_button(
                                label=f"⬇️ Descargar TODOS los JSON (.zip) — {len(all_jsons)}",
                                data=buf.getvalue(),
                                file_name="RIPS_JSON_Resolucion2275.zip",
                                mime="application/zip"
                            )
                except Exception as e:
                    st.error(f"Error durante la generación: {e}")
                    
        # 3. Caso Ambos (RIPS + CSV)
        else:
            if not csv_file_json or not zip_files_json:
                st.error("Por favor, carga tanto el CSV como al menos un ZIP de RIPS.")
            else:
                try:
                    with st.spinner("Procesando CSV y ZIPs..."):
                        # Cargar CSV
                        num_afiliados = creador.cargar_datos_mutual_csv(csv_file_json.getvalue())
                        st.info(f"ℹ️ Se cargaron {num_afiliados} afiliados del CSV para lookups.")
                        
                        # Generar desde ZIP cruzando datos
                        all_jsons = {}
                        for f in zip_files_json:
                            res = creador.generar_desde_zip(f.getvalue())
                            all_jsons.update(res)
                            
                        st.success(f"✅ Procesados {len(zip_files_json)} ZIPs con lookup de CSV. Se generaron {len(all_jsons)} facturas JSON.")
                        
                        # Mostrar facturas generadas
                        for num_fac, json_data in all_jsons.items():
                            with st.expander(f"Factura {num_fac} (Ver JSON)", expanded=False):
                                st.json(json_data)
                                json_str = json.dumps(json_data, indent=2, ensure_ascii=False)
                                st.download_button(
                                    label=f"⬇️ Descargar {num_fac}.json",
                                    data=json_str,
                                    file_name=f"{num_fac}.json",
                                    mime="application/json",
                                    key=f"dl_ind_ambos_{num_fac}"
                                )
                                
                        # Descarga masiva ZIP
                        if len(all_jsons) > 0:
                            buf = io.BytesIO()
                            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
                                for num_fac, json_data in all_jsons.items():
                                    json_str = json.dumps(json_data, indent=2, ensure_ascii=False)
                                    zout.writestr(f"{num_fac}.json", json_str)
                            st.divider()
                            st.download_button(
                                label=f"⬇️ Descargar TODOS los JSON (.zip) — {len(all_jsons)}",
                                data=buf.getvalue(),
                                file_name="RIPS_JSON_Cruzado_Resolucion2275.zip",
                                mime="application/zip"
                            )
                except Exception as e:
                    st.error(f"Error durante la generación: {e}")
