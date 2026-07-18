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
from corrector_xml import (  # noqa: E402
    corregir_xml_con_plantilla,
    inferir_regimen_desde_csv_mutual,
)
from generador_json import CreadorJsonRips, normalizar_factura  # noqa: E402
from limpiador_rips_sura import limpiar_zip_sura  # noqa: E402
from corrector_xml_sura import corregir_xml_sura  # noqa: E402
from generador_json_sura import (  # noqa: E402
    CreadorJsonRipsSura,
    extraer_factura_regimen_xml_sura,
)

st.set_page_config(
    page_title="Herramientas EPS - FEV Salud",
    page_icon="🏥",
    layout="wide",
)

st.title("🏥 Herramientas EPS — Facturación Electrónica (FEV Salud)")

with st.sidebar:
    st.header("🏢 EPS")
    eps_seleccionada = st.selectbox(
        "Selecciona la EPS",
        options=["Mutual", "SURA"],
        index=0,
        help="Cada EPS usa reglas propias para RIPS, XML y JSON.",
    )
    st.caption(
        "La configuración específica aparece dentro de cada herramienta."
    )

tab1, tab2, tab3 = st.tabs(
    ["🧹 Limpiador de RIPS", "🩺 Corrector de XML", "📝 Creador de JSON"]
)

# ===========================================================================
# PESTAÑA 1 — LIMPIADOR DE RIPS
# ===========================================================================
with tab1:
    if eps_seleccionada == "Mutual":
            st.subheader("🧹 Limpiador de RIPS — EPS Mutual")
            st.caption(
                "Reglas: FEC00→FEC · eliminación de AD y sus referencias · "
                "NIT 806009230-2→806009230 · (contributivo) ESS207→ESSC07 y régimen 2→1 en US"
            )

            with st.expander("⚙️ Configuración de limpieza — EPS Mutual", expanded=True):
                col_config, col_reglas = st.columns([1.15, 1])

                with col_config:
                    st.markdown(
                        "1. (Opcional) Carga el CSV de autorizaciones de Mutual para "
                        "deducir el régimen automáticamente.\n"
                        "2. Carga uno o varios `.zip` con los RIPS.\n"
                        "3. Ajusta el régimen y procesa."
                    )

                    csv_file = st.file_uploader(
                        "CSV de autorizaciones (plataforma Mutual)",
                        type=["csv"],
                        key="csv_autorizaciones_mutual_limpiador",
                        help=(
                            "Columnas C_DOCUMENTO_AFILIADO y C_ADMINISTRADORA "
                            "(ESS207=subsidiado, ESSC07=contributivo)"
                        ),
                    )

                    if csv_file:
                        mapa = cargar_csv_autorizaciones(csv_file.getvalue())
                        st.session_state.mapa_rips = mapa
                        if mapa.doc_a_regimen:
                            st.success(
                                f"CSV cargado: {mapa.total_filas} filas, "
                                f"{len(mapa.doc_a_regimen)} afiliados."
                            )
                        else:
                            st.error(
                                "El CSV no tiene las columnas "
                                "C_DOCUMENTO_AFILIADO / C_ADMINISTRADORA esperadas."
                            )
                    else:
                        st.session_state.mapa_rips = None

                with col_reglas:
                    st.markdown("**Reglas de limpieza de Mutual**")
                    st.markdown(
                        "- FEC que empiezan en `FEC00` → `FEC`\n"
                        "- Archivo **AD** y su referencia en CT → eliminados\n"
                        "- NIT `806009230-2` → `806009230`\n"
                        "- **Contributivo**: `ESS207`→`ESSC07` en AF/US y "
                        "régimen `2`→`1` en US"
                    )

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

    else:
        st.subheader("🧹 Limpiador de RIPS — EPS SURA")
        st.caption(
            "Reglas SURA: FEC00→FEC · eliminación de AD y referencia en CT · "
            "guion inmediatamente después de 139610 · "
            "NIT 806009230-2→806009230 únicamente en AF"
        )

        with st.expander(
            "⚙️ Configuración de limpieza — EPS SURA",
            expanded=True,
        ):
            col_pasos, col_reglas = st.columns([1, 1.3])

            with col_pasos:
                st.markdown(
                    "1. Carga uno o varios ZIP de RIPS.\n"
                    "2. Revisa las reglas específicas de SURA.\n"
                    "3. Procesa y descarga los ZIP limpios."
                )

            with col_reglas:
                st.markdown("**Reglas aplicadas**")
                st.markdown(
                    "- `FEC00...` → `FEC...`\n"
                    "- Elimina archivo **AD** y referencia en **CT**\n"
                    "- Autorizaciones `139610...`: agrega `-` después "
                    "de los primeros 10 dígitos\n"
                    "- En **AF**: `806009230-2` → `806009230`"
                )

        archivos_sura = st.file_uploader(
            "Cargar archivos ZIP de RIPS de SURA",
            type=["zip"],
            accept_multiple_files=True,
            key="rips_sura_zip",
        )

        if not archivos_sura:
            st.info("👆 Carga uno o más archivos ZIP de RIPS de SURA.")
        else:
            if st.button(
                "🚀 Procesar RIPS SURA",
                type="primary",
                use_container_width=True,
            ):
                resultados_sura = []
                barra = st.progress(0, text="Procesando RIPS SURA...")

                for indice, archivo in enumerate(archivos_sura):
                    resultados_sura.append(
                        limpiar_zip_sura(
                            archivo.getvalue(),
                            archivo.name,
                        )
                    )
                    barra.progress(
                        (indice + 1) / len(archivos_sura),
                        text=f"Procesado {archivo.name}",
                    )

                barra.empty()
                st.session_state.resultados_rips_sura = resultados_sura

            resultados_sura = st.session_state.get(
                "resultados_rips_sura",
                [],
            )
            if resultados_sura:
                st.success("✅ Procesamiento SURA completado.")

                for resultado in resultados_sura:
                    with st.expander(resultado.resumen, expanded=True):
                        if resultado.errores:
                            for error in resultado.errores:
                                st.error(error)
                            continue

                        for archivo in resultado.archivos:
                            if archivo.eliminado:
                                st.markdown(
                                    f"- 🗑️ **{archivo.nombre}** — eliminado"
                                )
                            elif archivo.lineas_modificadas:
                                st.markdown(
                                    f"- ✏️ **{archivo.nombre}** — "
                                    f"{archivo.lineas_modificadas}/"
                                    f"{archivo.total_lineas} líneas"
                                )
                                with st.popover(
                                    f"Ver cambios de {archivo.nombre}"
                                ):
                                    for cambio in archivo.cambios:
                                        st.caption(cambio)
                            else:
                                st.markdown(
                                    f"- ✅ **{archivo.nombre}** — sin cambios"
                                )

                        if resultado.zip_bytes:
                            nombre_base = os.path.splitext(
                                resultado.nombre_zip
                            )[0]
                            st.download_button(
                                label=(
                                    f"⬇️ Descargar "
                                    f"{nombre_base}_SURA_limpio.zip"
                                ),
                                data=resultado.zip_bytes,
                                file_name=(
                                    f"{nombre_base}_SURA_limpio.zip"
                                ),
                                mime="application/zip",
                                key=f"dl_sura_{resultado.nombre_zip}",
                            )

# ===========================================================================
# PESTAÑA 2 — CORRECTOR DE XML
# ===========================================================================
with tab2:
    if eps_seleccionada == "Mutual":
            st.subheader("🩺 Corrector de Facturas Electrónicas (FEV Salud)")
            st.caption(
                "Completa facturas (AttachedDocument) incompletas usando como plantilla un XML "
                "de referencia válido. Inserta el grupo sector salud y coloca el CUCON en "
                "NUMERO_CONTRATO. La firma electrónica se conserva intacta."
            )

            with st.expander("⚙️ Configuración del corrector XML — EPS Mutual", expanded=True):
                col_pasos, col_funciones = st.columns([1.15, 1])

                with col_pasos:
                    st.markdown(
                        "1. Carga uno o varios XML de facturas **incompletas**.\n"
                        "2. Carga el CSV de autorizaciones de Mutual.\n"
                        "3. Escribe el **CUCON**.\n"
                        "4. Detecta o selecciona el régimen y procesa.\n\n"
                        "La plantilla de referencia es **interna**; no necesitas subirla."
                    )

                with col_funciones:
                    st.markdown("**Qué hace el corrector**")
                    st.markdown(
                        "- Completa los campos requeridos del sector salud\n"
                        "- Coloca el **CUCON** en `NUMERO_CONTRATO`\n"
                        "- Define cobertura contributiva o subsidiada\n"
                        "- Ajusta la estructura a la Resolución 000948 de 2026"
                    )

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

            csv_mutual_xml = st.file_uploader(
                "CSV de autorizaciones de Mutual para detectar el régimen",
                type=["csv"],
                key="csv_mutual_corrector_xml",
                help=(
                    "Se busca cada factura por NUMERO_AUTORIZACION y, como respaldo, "
                    "por C_DOCUMENTO_AFILIADO. ESSC07 = contributivo; "
                    "ESS207 = subsidiado."
                ),
            )

            regimen_xml = st.selectbox(
                "Régimen manual de respaldo",
                options=[
                    "Detectar automáticamente con CSV",
                    "Contributivo",
                    "Subsidiado",
                ],
                index=0,
                help=(
                    "El modo automático usa el CSV de Mutual. La selección manual "
                    "solo se usa cuando el CSV no encuentra una coincidencia."
                ),
            )

            if not xmls_subidos:
                st.info("👆 Carga al menos una factura incompleta y escribe el CUCON.")
            else:
                st.divider()
                if st.button("🚀 Corregir XML", type="primary", use_container_width=True):
                    resultados = []
                    barra = st.progress(0, text="Corrigiendo...")
                    csv_bytes = (
                        csv_mutual_xml.getvalue()
                        if csv_mutual_xml is not None
                        else None
                    )

                    for i, f in enumerate(xmls_subidos):
                        xml_bytes = f.getvalue()
                        regimen_valor = None
                        detalle_regimen = ""

                        if regimen_xml == "Detectar automáticamente con CSV":
                            if csv_bytes is None:
                                from corrector_xml import ResultadoCorreccion
                                res = ResultadoCorreccion(nombre=f.name)
                                res.ok = False
                                res.mensaje = (
                                    "Cargue el CSV de Mutual o seleccione un régimen "
                                    "manual de respaldo."
                                )
                                resultados.append(res)
                                continue

                            regimen_valor, detalle_regimen = (
                                inferir_regimen_desde_csv_mutual(
                                    xml_bytes,
                                    csv_bytes,
                                )
                            )

                            if regimen_valor is None:
                                from corrector_xml import ResultadoCorreccion
                                res = ResultadoCorreccion(nombre=f.name)
                                res.ok = False
                                res.mensaje = (
                                    detalle_regimen
                                    + " Seleccione Contributivo o Subsidiado "
                                    "manualmente y procese nuevamente."
                                )
                                resultados.append(res)
                                continue
                        else:
                            regimen_valor = regimen_xml.lower()
                            detalle_regimen = (
                                f"Régimen seleccionado manualmente: {regimen_xml}."
                            )

                        res = corregir_xml_con_plantilla(
                            xml_bytes,
                            cucon,
                            f.name,
                            regimen=regimen_valor,
                        )
                        if res.ok and detalle_regimen:
                            res.cambios.insert(0, detalle_regimen)

                        resultados.append(res)
                        barra.progress(
                            (i + 1) / len(xmls_subidos),
                            text=f"Corregido {f.name}",
                        )

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

    else:
        st.subheader("🩺 Corrector de XML — EPS SURA")
        st.caption(
            "Reconstruye directamente el nodo de sector salud según la "
            "Resolución 000948 de 2026. No requiere cargar una plantilla."
        )

        with st.expander(
            "⚙️ Configuración del corrector XML — EPS SURA",
            expanded=True,
        ):
            col_pasos, col_datos = st.columns([1.15, 1])

            with col_pasos:
                st.markdown(
                    "1. Carga los XML de SURA que vas a corregir.\n"
                    "2. Escribe el CUCON.\n"
                    "3. Selecciona el régimen.\n"
                    "4. Procesa y descarga los XML corregidos."
                )

            with col_datos:
                st.markdown("**Qué completa**")
                st.markdown(
                    "- Resolución `000948:2026`\n"
                    "- Modalidad de pago por evento (`04`)\n"
                    "- Cobertura contributiva (`16`) o subsidiada (`17`)\n"
                    "- Los 14 campos del nodo Sector Salud\n"
                    "- CUCON en `NUMERO_CONTRATO`"
                )

        xmls_sura = st.file_uploader(
            "Facturas XML de SURA a corregir",
            type=["xml"],
            accept_multiple_files=True,
            key="xmls_sura_corregir",
        )

        cucon_sura = st.text_input(
            "CUCON de SURA",
            key="cucon_sura",
            help=(
                "Se escribe en NUMERO_CONTRATO. No puede quedar vacío ni en cero."
            ),
        )

        regimen_sura = st.selectbox(
            "Régimen del usuario",
            options=["Seleccione...", "Contributivo", "Subsidiado"],
            key="regimen_xml_sura",
        )

        if not xmls_sura:
            st.info("👆 Carga uno o varios XML de SURA para corregir.")
        elif st.button(
            "🚀 Corregir XML SURA",
            type="primary",
            use_container_width=True,
        ):
            if not cucon_sura.strip() or cucon_sura.strip() == "0":
                st.error("Escriba un CUCON válido.")
            elif regimen_sura == "Seleccione...":
                st.error(
                    "Seleccione si el usuario es Contributivo o Subsidiado."
                )
            else:
                resultados_xml_sura = []
                barra = st.progress(0, text="Corrigiendo XML SURA...")

                for indice, archivo in enumerate(xmls_sura):
                    resultado = corregir_xml_sura(
                        xml_bytes=archivo.getvalue(),
                        cucon=cucon_sura,
                        nombre=archivo.name,
                        regimen=regimen_sura.lower(),
                    )
                    resultados_xml_sura.append(resultado)
                    barra.progress(
                        (indice + 1) / len(xmls_sura),
                        text=f"Corregido {archivo.name}",
                    )

                barra.empty()
                st.session_state.resultados_xml_sura = (
                    resultados_xml_sura
                )

        resultados_xml_sura = st.session_state.get(
            "resultados_xml_sura",
            [],
        )
        if resultados_xml_sura:
            for resultado in resultados_xml_sura:
                with st.expander(
                    f"{resultado.nombre} — "
                    f"{'✅' if resultado.ok else '❌'}",
                    expanded=True,
                ):
                    if not resultado.ok:
                        st.error(resultado.mensaje)
                        continue

                    if resultado.cambios:
                        st.markdown("**Cambios aplicados:**")
                        for cambio in resultado.cambios:
                            st.markdown(f"- {cambio}")

                    if resultado.xml_bytes:
                        nombre_base = os.path.splitext(
                            resultado.nombre
                        )[0]
                        st.download_button(
                            label=(
                                f"⬇️ Descargar "
                                f"{nombre_base}_SURA_corregido.xml"
                            ),
                            data=resultado.xml_bytes,
                            file_name=(
                                f"{nombre_base}_SURA_corregido.xml"
                            ),
                            mime="application/xml",
                            key=f"xml_sura_{resultado.nombre}",
                        )

# ===========================================================================
# PESTAÑA 3 — CREADOR DE JSON (Resolución 2275 de 2023)
# ===========================================================================
with tab3:
    if eps_seleccionada == "Mutual":
            st.subheader("📝 Creador de JSON RIPS (Resolución 2275 de 2023)")
            st.caption(
                "Genera los archivos JSON exigidos por el Ministerio de Salud a partir de RIPS ZIP planos, "
                "del CSV de autorizaciones de Mutual, o de ambos combinados."
            )

            with st.expander("⚙️ Configuración del creador JSON — EPS Mutual", expanded=True):
                col_pasos, col_origenes = st.columns([1.1, 1])

                with col_pasos:
                    st.markdown(
                        "1. Elige el **origen de los datos**.\n"
                        "2. Carga los archivos correspondientes.\n"
                        "3. Usa el CSV de Mutual para completar datos del afiliado cuando aplique.\n"
                        "4. Genera y revisa los JSON antes de descargarlos."
                    )

                with col_origenes:
                    st.markdown("**Opciones disponibles**")
                    st.markdown(
                        "- **Solo RIPS:** genera desde uno o varios ZIP\n"
                        "- **Solo CSV:** genera una factura desde el CSV\n"
                        "- **Ambos:** usa los RIPS y completa datos con el CSV"
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
    else:
        st.subheader("📝 Creador de JSON RIPS — EPS SURA")
        st.caption(
            "Genera el JSON desde los RIPS limpios y revisa los medicamentos "
            "contra TablaReferenciaIUM.xlsx. Conserva IUM válidos, limpia CUM "
            "con ATC concatenado y selecciona el IUM habilitado más semejante."
        )

        with st.expander(
            "⚙️ Configuración del creador JSON — EPS SURA",
            expanded=True,
        ):
            col_pasos, col_medicamentos = st.columns([1, 1.25])

            with col_pasos:
                st.markdown(
                    "1. Carga uno o varios XML corregidos de SURA.\n"
                    "2. Carga uno o varios ZIP de RIPS limpios.\n"
                    "3. El régimen se identifica por cada factura desde el XML.\n"
                    "4. Genera, revisa el reporte IUM/CUM y descarga."
                )

            with col_medicamentos:
                st.markdown("**Reglas de medicamentos**")
                st.markdown(
                    "- `numAutorizacion` se elimina de medicamentos\n"
                    "- IUM existente y habilitado: se conserva\n"
                    "- Varias presentaciones: se elige la más semejante\n"
                    "- No magistrales: nombre y forma en `null`; "
                    "concentración y unidad en `0`\n"
                    "- `vrDispensacion` queda en `0` y `codigoVIDA` en `null`"
                )
                st.caption(
                    "La tabla debe estar en "
                    "`Generador Json/TablaReferenciaIUM.xlsx`."
                )

        xmls_regimen_sura = st.file_uploader(
            "XML corregidos de SURA para identificar el régimen",
            type=["xml"],
            accept_multiple_files=True,
            key="xmls_regimen_json_sura",
            help=(
                "Puedes cargar varios XML. Cada XML se relaciona con "
                "su factura y se lee COBERTURA_PLAN_BENEFICIOS: "
                "16 = contributivo, 17 = subsidiado."
            ),
        )

        mapa_regimen_sura = {}
        errores_regimen_sura = []

        for xml_regimen in xmls_regimen_sura or []:
            try:
                factura_xml, regimen_xml = (
                    extraer_factura_regimen_xml_sura(
                        xml_regimen.getvalue(),
                        xml_regimen.name,
                    )
                )

                anterior = mapa_regimen_sura.get(factura_xml)
                if anterior and anterior != regimen_xml:
                    errores_regimen_sura.append(
                        f"{factura_xml}: aparece con regímenes "
                        f"diferentes en los XML cargados."
                    )
                else:
                    mapa_regimen_sura[factura_xml] = regimen_xml
            except Exception as exc:
                errores_regimen_sura.append(str(exc))

        if mapa_regimen_sura:
            st.success(
                f"Se identificó el régimen de "
                f"{len(mapa_regimen_sura)} factura(s)."
            )
            st.dataframe(
                [
                    {
                        "Factura": factura,
                        "Régimen": regimen.capitalize(),
                        "tipoUsuario JSON": (
                            "01/02/03"
                            if regimen == "contributivo"
                            else "04"
                        ),
                    }
                    for factura, regimen in sorted(
                        mapa_regimen_sura.items()
                    )
                ],
                use_container_width=True,
                hide_index=True,
            )

        for error_regimen in errores_regimen_sura:
            st.error(error_regimen)

        st.markdown("#### Profesional o especialista")

        col_tipo_profesional, col_num_profesional = st.columns([1, 2])

        with col_tipo_profesional:
            st.text_input(
                "Tipo de identificación",
                value="CC",
                disabled=True,
                key="tipo_documento_profesional_sura",
            )

        with col_num_profesional:
            numero_documento_profesional = st.text_input(
                "Número de identificación",
                key="numero_documento_profesional_sura",
                help=(
                    "Documento real del profesional o especialista. "
                    "Se aplicará a los campos de identificación de los "
                    "servicios, sin modificar el documento del usuario."
                ),
            )

        st.caption(
            "Se aplicará CC y el número ingresado a consultas, "
            "procedimientos, medicamentos, otros servicios y a cualquier "
            "otro registro que tenga campos de identificación profesional. "
            "También se ajustan las finalidades, códigos de servicio y "
            "fechas fuera del periodo de atención."
        )

        zip_files_json_sura = st.file_uploader(
            "Cargar ZIP de RIPS limpios de SURA",
            type=["zip"],
            accept_multiple_files=True,
            key="zip_json_sura",
        )

        if st.button(
            "🚀 Generar JSON RIPS SURA",
            type="primary",
            use_container_width=True,
        ):
            if not numero_documento_profesional.strip():
                st.error(
                    "Ingrese el número de identificación del profesional "
                    "o especialista."
                )
            elif not xmls_regimen_sura:
                st.error(
                    "Cargue al menos un XML corregido de SURA para "
                    "identificar el régimen."
                )
            elif errores_regimen_sura:
                st.error(
                    "Corrija los errores de los XML antes de generar."
                )
            elif not zip_files_json_sura:
                st.error("Cargue al menos un ZIP de RIPS de SURA.")
            else:
                try:
                    all_jsons_sura = {}
                    reporte_ium_sura = []
                    advertencias_sura = []

                    barra = st.progress(
                        0,
                        text="Generando JSON y revisando medicamentos...",
                    )

                    for indice, archivo in enumerate(zip_files_json_sura):
                        creador_sura = CreadorJsonRipsSura(
                            regimen_por_factura=mapa_regimen_sura,
                            numero_documento_profesional=(
                                numero_documento_profesional.strip() or None
                            ),
                        )

                        generados = creador_sura.generar_desde_zip(
                            archivo.getvalue()
                        )
                        all_jsons_sura.update(generados)
                        reporte_ium_sura.extend(
                            creador_sura.ultimo_reporte_ium
                        )
                        advertencias_sura.extend(
                            creador_sura.advertencias
                        )

                        barra.progress(
                            (indice + 1) / len(zip_files_json_sura),
                            text=f"Procesado {archivo.name}",
                        )

                    barra.empty()
                    st.session_state.jsons_sura = all_jsons_sura
                    st.session_state.reporte_ium_sura = reporte_ium_sura
                    st.session_state.advertencias_json_sura = (
                        advertencias_sura
                    )

                except Exception as exc:
                    st.error(f"Error durante la generación SURA: {exc}")

        all_jsons_sura = st.session_state.get("jsons_sura", {})
        reporte_ium_sura = st.session_state.get(
            "reporte_ium_sura",
            [],
        )
        advertencias_sura = st.session_state.get(
            "advertencias_json_sura",
            [],
        )

        if all_jsons_sura:
            st.success(
                f"✅ Se generaron {len(all_jsons_sura)} facturas JSON de SURA."
            )

            for advertencia in advertencias_sura:
                st.warning(advertencia)

            if reporte_ium_sura:
                st.subheader("💊 Auditoría IUM/CUM")
                st.dataframe(
                    reporte_ium_sura,
                    use_container_width=True,
                    hide_index=True,
                    column_order=[
                        "factura",
                        "medicamento",
                        "codigo_original",
                        "codigo_final",
                        "estado",
                        "detalle",
                        "candidatos",
                    ],
                )

            st.divider()
            st.subheader("📄 JSON generados")

            for num_factura, json_data in all_jsons_sura.items():
                with st.expander(
                    f"Factura {num_factura}",
                    expanded=False,
                ):
                    st.json(json_data)
                    json_str = json.dumps(
                        json_data,
                        indent=2,
                        ensure_ascii=False,
                    )
                    st.download_button(
                        label=f"⬇️ Descargar {num_factura}.json",
                        data=json_str,
                        file_name=f"{num_factura}.json",
                        mime="application/json",
                        key=f"dl_json_sura_{num_factura}",
                    )

            buf_sura = io.BytesIO()
            with zipfile.ZipFile(
                buf_sura,
                "w",
                zipfile.ZIP_DEFLATED,
            ) as salida_zip:
                for num_factura, json_data in all_jsons_sura.items():
                    salida_zip.writestr(
                        f"{num_factura}.json",
                        json.dumps(
                            json_data,
                            indent=2,
                            ensure_ascii=False,
                        ),
                    )

            st.download_button(
                label=(
                    f"⬇️ Descargar TODOS los JSON SURA (.zip) — "
                    f"{len(all_jsons_sura)}"
                ),
                data=buf_sura.getvalue(),
                file_name="RIPS_JSON_SURA_Resolucion948_2026.zip",
                mime="application/zip",
                use_container_width=True,
            )
