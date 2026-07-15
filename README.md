# Herramientas EPS Mutual — Facturación Electrónica (FEV Salud)

Aplicación Streamlit con tres pestañas integradas para el manejo de RIPS y facturación electrónica en salud (Colombia).

## Herramientas incluidas

### 🧹 Limpiador de RIPS
Normaliza archivos RIPS ZIP según las reglas de la EPS Mutual:
- `FEC00…` → `FEC…` (corrección de prefijo de factura)
- Eliminación del archivo AD y sus referencias en CT
- NIT `806009230-2` → `806009230`
- Régimen contributivo: `ESS207` → `ESSC07` y régimen `2` → `1` en US
- Cruce automático del régimen usando el CSV de autorizaciones de Mutual

### 🩺 Corrector de XML
Completa facturas electrónicas (AttachedDocument) incompletas:
- Inserta los campos faltantes usando una plantilla de referencia interna
- Completa el grupo sector salud (TotalesCop, FabricanteSoftware, etc.)
- Coloca el CUCON en `NUMERO_CONTRATO` (Res. 0948 de 2026, Art. 11)
- Conserva la firma electrónica intacta

### 📝 Creador de JSON RIPS (Resolución 2275 de 2023)
Genera los archivos JSON exigidos por el Ministerio de Salud:
- **Solo CSV de Mutual**: genera un JSON consolidado para la factura indicada
- **Solo RIPS ZIP**: parsea los RIPS clásicos y genera un JSON por factura
- **Ambos**: cruza RIPS con el CSV para obtener fechas de nacimiento exactas
- Descarga individual o masiva (ZIP con todos los JSON)

## Estructura del proyecto

```
CEMIC/
├── app.py                      # App Streamlit unificada (punto de entrada)
├── Limpiador Rips/
│   └── limpiador_rips.py       # Lógica de limpieza de RIPS
├── Corrector Xml/
│   ├── corrector_xml.py        # Lógica de corrección de XML
│   └── plantilla_referencia.xml
├── Generador Json/
│   └── generador_json.py       # Lógica de generación de JSON (Res. 2275/2023)
├── json ejemplo.json           # Ejemplo de JSON de referencia
└── requirements.txt
```

## Instalación y uso

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Requisitos

- Python 3.9+
- streamlit
- pandas
- lxml
