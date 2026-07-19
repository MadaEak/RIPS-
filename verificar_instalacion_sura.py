from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path


VERSION_ESPERADA = "2026.07.18-v6-equivalencias-aproximadas"

PRUEBAS = [
    (
        "CLONAZEPAM0.5MGTABLETAS",
        "N03AE0102",
        30,
        "1C1043641000109",
    ),
    (
        "CARBONATODELITIOTABLETA300",
        "N05AN0101",
        30,
        "1L1029741000100",
    ),
    (
        "DIVALPROATODESODIO250MGTA",
        "N03AG01",
        30,
        "1V1006681000101",
    ),
    (
        "DIVALPROATODESODIO500MGTA",
        "N03AG01",
        30,
        "1V1006691000101",
    ),
    (
        "MELATONINA3MGTABLETAS",
        "N05CM17",
        30,
        "1M1015831000102",
    ),
]


def cargar_modulo(ruta: Path):
    contenido = ruta.read_bytes()
    huella = hashlib.sha256(contenido).hexdigest()[:12]
    nombre = f"_verificador_sura_{huella}"

    spec = importlib.util.spec_from_file_location(nombre, ruta)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No fue posible cargar {ruta}")

    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo
    spec.loader.exec_module(modulo)
    return modulo, huella


def main() -> int:
    raiz = Path(__file__).resolve().parent
    carpeta = raiz / "Generador Json"
    ruta_generador = carpeta / "generador_json_sura.py"
    ruta_mutual = carpeta / "generador_json.py"
    ruta_ium = carpeta / "TablaReferenciaIUM.xlsx"
    ruta_cups = carpeta / "TablaReferencia_CUPS.xlsx"

    print("=" * 72)
    print("VERIFICACIÓN DEL GENERADOR SURA")
    print("=" * 72)
    print(f"Raíz revisada: {raiz}")
    print()

    archivos = [
        ruta_mutual,
        ruta_generador,
        ruta_ium,
        ruta_cups,
    ]

    faltantes = [ruta for ruta in archivos if not ruta.exists()]
    if faltantes:
        print("ERROR: faltan archivos:")
        for ruta in faltantes:
            print(f"- {ruta}")
        return 1

    sys.path.insert(0, str(carpeta))
    modulo, huella = cargar_modulo(ruta_generador)

    version = getattr(
        modulo,
        "VERSION_GENERADOR_SURA",
        "SIN_VERSION",
    )

    print(f"Generador cargado: {ruta_generador}")
    print(f"SHA-256 abreviado: {huella}")
    print(f"Versión encontrada: {version}")
    print(f"Versión esperada:  {VERSION_ESPERADA}")
    print()

    if version != VERSION_ESPERADA:
        print("RESULTADO: VERSIÓN INCORRECTA")
        print(
            "Reemplaza app.py y Generador Json/generador_json_sura.py, "
            "elimina __pycache__ y reinicia Streamlit."
        )
        return 2

    catalogo = modulo.CatalogoIUM(str(ruta_ium))
    errores = []

    print("PRUEBAS DE EQUIVALENCIAS")
    for medicamento, codigo_original, cantidad, esperado in PRUEBAS:
        decision = catalogo.resolver(
            codigo_original,
            medicamento,
            None,
            None,
            cantidad,
        )
        obtenido = decision.get("codigo")
        estado = decision.get("estado")
        correcto = (
            estado == "IUM_EQUIVALENTE_SELECCIONADO"
            and obtenido == esperado
        )

        marca = "OK" if correcto else "ERROR"
        print(
            f"[{marca}] {medicamento}\n"
            f"       Estado: {estado}\n"
            f"       IUM:    {obtenido}\n"
            f"       Esperado: {esperado}"
        )

        if not correcto:
            errores.append(medicamento)

    print()
    if errores:
        print("RESULTADO: LA VERSIÓN ES CORRECTA, PERO FALLARON REGLAS")
        print("Medicamentos:", ", ".join(errores))
        return 3

    print("RESULTADO: INSTALACIÓN CORRECTA")
    print(
        "Con esta instalación, clonazepam y carbonato de litio no deben "
        "aparecer como medicamentos sin IUM."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
