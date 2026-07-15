"""
Limpiador de RIPS - EPS Mutual
Interfaz Streamlit para cargar múltiples ZIP de RIPS, elegir el régimen
(contributivo/subsidiado) de cada uno — automáticamente a partir de un CSV de
autorizaciones de la plataforma Mutual, o manualmente — y descargar los RIPS limpios.

Ejecutar:
    streamlit run app.py
"""

from __future__ import annotations

import io
import os

import streamlit as st

from limpiador_rips import (
    limpiar_zip_bytes,
    cargar_csv_autorizaciones,
    inferir_regimen_zip,
)

st.set_page_config(page_title="Limpiador de RIPS - EPS Mutual", page_icon="🧹", layout="wide")

# ---------------------------------------------------------------------------
# Estado de sesión
# ---------------------------------------------------------------------------
for k in ("procesado", "resultados", "mapa_cargado", "regimenes_auto"):
    if k not in st.session_state:
        st.session_state[k] = False if k in ("procesado", "mapa_cargado") else (
            [] if k == "resultados" else {})


def _nombre_salida(nombre_zip: str) -> str:
    base, _ = os.path.splitext(nombre_zip)
    return f"{base}_limpio.zip"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("🧹 Limpiador de RIPS — EPS Mutual")
st.caption(
    "Reglas aplicadas: FEC00→FEC · eliminación de AD y sus referencias · "
    "NIT 806009230-2→806009230 · (contributivo) ESS207→ESSC07 y régimen 2→1 en US"
)

with st.sidebar:
    st.header("⚙️ Configuración")
    st.markdown(
        "1. (Opcional) Carga el CSV de autorizaciones de Mutual para deducir el "
        "régimen automáticamente.\n"
        "2. Carga uno o varios `.zip` con los RIPS.\n"
        "3. Ajusta el régimen si es necesario y procesa."
    )
    st.divider()
    csv_file = st.file_uploader(
        "CSV de autorizaciones (plataforma Mutual)",
        type=["csv"],
        help="Columnas C_DOCUMENTO_AFILIADO y C_ADMINISTRADORA (ESS207=subsidiado, ESSC07=contributivo)",
    )
    if csv_file:
        mapa = cargar_csv_autorizaciones(csv_file.getvalue())
        st.session_state.mapa = mapa
        st.session_state.mapa_cargado = bool(mapa.doc_a_regimen)
        if mapa.doc_a_regimen:
            st.success(f"CSV cargado: {mapa.total_filas} filas, "
                       f"{len(mapa.doc_a_regimen)} afiliados.")
        else:
            st.error("El CSV no tiene las columnas C_DOCUMENTO_AFILIADO / "
                     "C_ADMINISTRADORA esperadas.")
    else:
        st.session_state.mapa_cargado = False

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
    st.stop()

st.divider()
st.subheader("📋 Régimen de cada archivo")
mapa = st.session_state.get("mapa", None)
hay_csv = bool(mapa and mapa.doc_a_regimen)

if hay_csv:
    st.info("🔎 El régimen se deduce del CSV (cruce por documento del afiliado). "
            "Pulsa **Revisar cruce CSV** para asignarlo automáticamente, o "
            "sobreescribirlo manualmente.")

# Botón de revisión automática (requiere CSV cargado)
if hay_csv:
    if st.button("🔍 Revisar cruce CSV", use_container_width=True,
                 help="Detecta automáticamente el régimen de cada ZIP según el CSV."):
        for f in archivos_subidos:
            regimen_inf, no_enc, detalles = inferir_regimen_zip(f.getvalue(), mapa)
            if regimen_inf is not None:
                st.session_state[f"reg_{f.name}"] = (
                    "Contributivo" if regimen_inf == "contributivo" else "Subsidiado"
                )
        # Notificar los no encontrados
        no_encontrados_todos = []
        for f in archivos_subidos:
            _, no_enc, _ = inferir_regimen_zip(f.getvalue(), mapa)
            for d in no_enc:
                no_encontrados_todos.append(f"{f.name} → doc {d}")
        if no_encontrados_todos:
            st.warning("⚠️ No se encontraron en el CSV: " +
                       "; ".join(no_encontrados_todos))
        else:
            st.success("✅ Todos los RIPS cruzados encontraron coincidencia en el CSV.")

# Tabla de régimen por archivo
opciones = {"Subsidiado": False, "Contributivo": True, "Automático (CSV)": None}
regimenes: dict[str, bool] = {}

cols = st.columns([3, 2, 1, 3])
cols[0].write("**Archivo**")
cols[1].write("**Régimen**")
cols[2].write("**Tamaño**")
cols[3].write("**Cruce CSV**")

for f in archivos_subidos:
    c1, c2, c3, c4 = st.columns([3, 2, 1, 3])
    c1.write(f.name)
    defecto = "Automático (CSV)" if hay_csv else "Subsidiado"
    sel = c2.selectbox(
        "Régimen",
        options=list(opciones.keys()),
        index=list(opciones.keys()).index(defecto),
        key=f"reg_{f.name}",
        label_visibility="collapsed",
    )
    regimenes[f.name] = opciones[sel]
    c3.write(f"{f.size/1024:.1f} KB")

    # mostrar estado del cruce
    if hay_csv:
        regimen_inf, no_enc, detalles = inferir_regimen_zip(f.getvalue(), mapa)
        if regimen_inf == "contributivo":
            c4.markdown("✅ ESSC07")
        elif regimen_inf == "subsidiado":
            c4.markdown("✅ ESS207")
        else:
            c4.markdown("⚠️ no cruzado")
        # mostrar autorización/estado de cada doc encontrado
        for det in detalles:
            if det.get("encontrado"):
                aut = det.get("autorizacion") or "—"
                est = (det.get("estado") or "—").upper()
                aprobado = est in ("APROBADO", "", "—")
                badge = "✅" if aprobado else "⚠️"
                c4.caption(f"{badge} doc {det['doc']}: aut {aut} · {est}")
        if no_enc:
            c4.caption(f"⚠️ docs sin match: {', '.join(no_enc[:3])}{'…' if len(no_enc)>3 else ''}")
    else:
        c4.write("—")

st.divider()

procesar = st.button("🚀 Procesar RIPS", type="primary", use_container_width=True)

if procesar:
    # Validar que ningún ZIP quede sin régimen definido de forma segura.
    # Se bloquea si: selección = "Automático (CSV)" Y (no hay CSV O no cruza).
    bloqueados = []
    for f in archivos_subidos:
        reg_sel = regimenes[f.name]
        if reg_sel is None:  # "Automático (CSV)"
            if not (mapa and mapa.doc_a_regimen):
                bloqueados.append(f.name)
            else:
                regimen_inf, _, _ = inferir_regimen_zip(f.getvalue(), mapa)
                if regimen_inf is None:
                    bloqueados.append(f.name)

    if bloqueados:
        st.error("🚫 Procesamiento bloqueado. Los siguientes RIPS no tienen régimen "
                 "definido de forma segura (no cruzaron en el CSV y siguen en "
                 "'Automático'). Elígelos manualmente (Contributivo/Subsidiado) y "
                 "vuelve a procesar:")
        for b in bloqueados:
            st.markdown(f"- ⚠️ **{b}**")
        st.stop()

    resultados = []
    barra = st.progress(0, text="Procesando...")
    for i, f in enumerate(archivos_subidos):
        bytes_zip = f.getvalue()
        reg_sel = regimenes[f.name]
        # "Automático" => deducir del CSV (ya validado que cruza arriba)
        if reg_sel is None:
            regimen_inf, _, _ = inferir_regimen_zip(bytes_zip, mapa)
            es_cont = (regimen_inf == "contributivo")
        else:
            es_cont = reg_sel
        res = limpiar_zip_bytes(bytes_zip, f.name, es_cont)
        resultados.append(res)
        barra.progress((i + 1) / len(archivos_subidos), text=f"Procesado {f.name}")
    barra.empty()
    st.session_state.resultados = resultados
    st.session_state.procesado = True

if st.session_state.procesado and st.session_state.resultados:
    st.success("✅ Procesamiento completado.")
    st.divider()
    st.subheader("📊 Resultado por archivo")

    for res in st.session_state.resultados:
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
                    st.markdown(
                        f"- ✏️ **{a.nombre}** — {a.lineas_modificadas}/{a.total_lineas} líneas modificadas"
                    )
                    with st.popover("Ver cambios"):
                        for c in a.cambios:
                            st.caption(c)
                else:
                    st.markdown(f"- ✅ **{a.nombre}** — sin cambios")

            if res.zip_bytes:
                st.download_button(
                    label=f"⬇️ Descargar {_nombre_salida(res.nombre_zip)}",
                    data=res.zip_bytes,
                    file_name=_nombre_salida(res.nombre_zip),
                    mime="application/zip",
                    key=f"dl_{res.nombre_zip}",
                )

    if any(r.zip_bytes for r in st.session_state.resultados):
        st.divider()
        st.subheader("📦 Descarga masiva")
        buf = io.BytesIO()
        import zipfile

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for res in st.session_state.resultados:
                if res.zip_bytes:
                    zout.writestr(_nombre_salida(res.nombre_zip), res.zip_bytes)
        st.download_button(
            label="⬇️ Descargar TODOS los RIPS limpios (.zip)",
            data=buf.getvalue(),
            file_name="RIPS_limpios_Mutual.zip",
            mime="application/zip",
        )
