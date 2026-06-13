import requests
import pandas as pd
import sqlite3
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, HRFlowable
from reportlab.lib.enums import TA_CENTER
import os

# ==========================================
# CONFIGURACIÓN DE RUTAS UNIVERSALES Y API
# ==========================================
DB_PATH = "base_datos_jaaprv.db"
CHANNEL_ID = "2941382"

# Detectar el directorio de ejecución actual de forma automática
BASE_DIR = os.getcwd()
GRAFICAS_DIR = os.path.join(BASE_DIR, "graficas")
os.makedirs(GRAFICAS_DIR, exist_ok=True)

PDF_OUTPUT_PATH = os.path.join(BASE_DIR, "reporte_jaaprv_presion.pdf")

# Conectar e inicializar la base de datos local SQLite
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS lecturas (
        fecha TEXT UNIQUE,
        nivel_cm REAL,
        presion_psi REAL
    )
''')
conn.commit()

# ==========================================
# DESCARGA Y SINCRONIZACIÓN DE LA API
# ==========================================
print("Descargando datos desde la API de ThingSpeak...")
url = f"https://api.thingspeak.com/channels/{CHANNEL_ID}/feeds.json?results=3450"

try:
    data = requests.get(url).json()
    df_api = pd.DataFrame(data["feeds"])

    # 1. CORRECCIÓN DE ZONA HORARIA: Convertir marcas de tiempo UTC a Hora Local de Ecuador
    df_api["created_at"] = pd.to_datetime(df_api["created_at"])
    df_api["created_at"] = df_api["created_at"].dt.tz_convert('America/Guayaquil') # Cambiar zona si aplica
    df_api["created_at"] = df_api["created_at"].dt.tz_localize(None) # Quitar etiqueta de zona para SQLite
    df_api["fecha_texto"] = df_api["created_at"].dt.strftime('%Y-%m-%d %H:%M:%S')

    df_api["nivel_cm"] = pd.to_numeric(df_api["field1"], errors="coerce")
    df_api["presion_psi"] = pd.to_numeric(df_api["field2"], errors="coerce")

    # Insertar en la Base de Datos ignorando los registros duplicados automáticamente
    registros_nuevos = 0
    for _, row in df_api.iterrows():
        try:
            cursor.execute('''
                INSERT INTO lecturas (fecha, nivel_cm, presion_psi)
                VALUES (?, ?, ?)
            ''', (row["fecha_texto"], row["nivel_cm"], row["presion_psi"]))
            registros_nuevos += 1
        except sqlite3.IntegrityError:
            continue

    conn.commit()
    print(f"-> ¡Éxito! Sincronización completada. Se añadieron {registros_nuevos} filas nuevas.")
except Exception as e:
    print(f"¡Alerta! No se pudo descargar datos de la API ({e}). Usando el histórico local.")

# Leer todo el histórico acumulado en la base de datos local
query = "SELECT fecha, presion_psi FROM lecturas WHERE presion_psi IS NOT NULL ORDER BY fecha ASC"
df_presion = pd.read_sql_query(query, conn)
conn.close()

# ==========================================
# PROCESAMIENTO MATEMÁTICO Y ESTADÍSTICO
# ==========================================
if len(df_presion) == 0:
    print("¡Alerta! La base de datos está vacía o no tiene registros de presión válidos.")
else:
    print(f"-> ¡Éxito! Se cargaron {len(df_presion):,} registros históricos totales para procesar.") # [cite: 5]

    # Formatear columnas de análisis
    df_presion["created_at"] = pd.to_datetime(df_presion["fecha"])
    df_presion["presion_psi"] = pd.to_numeric(df_presion["presion_psi"])

    # 2. ANÁLISIS TELEMÁTICO: Calcular frecuencia real de envío del sensor
    df_presion['diferencia_tiempo'] = df_presion['created_at'].diff()
    frecuencia_promedio = df_presion['diferencia_tiempo'].mean()
    print(f"-> Frecuencia operativa real del sensor: {frecuencia_promedio}")

    # 3. FILTRADO TÉCNICO: Separar comportamiento operativo de picos extremos/ruido (>120 psi)
    df_operativo = df_presion[df_presion["presion_psi"] <= 120].copy()

    # Métricas del comportamiento real y continuo de la red
    p_min = df_operativo["presion_psi"].min() # [cite: 8]
    p_max_operativa = df_operativo["presion_psi"].max()
    p_mean = df_operativo["presion_psi"].mean()
    p_median = df_operativo["presion_psi"].median()
    p_std = df_operativo["presion_psi"].std()

    # El valor monstruoso (ej. 449.0 psi) se guarda de forma aislada como registro de evento técnico
    p_max_absoluto = df_presion["presion_psi"].max() # [cite: 6, 8]

    # Identificar anomalías estadísticas mediante Z-Score basado en el perfil operativo
    df_presion["zscore"] = (df_presion["presion_psi"] - p_mean) / (p_std if p_std > 0 else 1)
    anomalias = df_presion[df_presion["zscore"].abs() > 3]
    print(f"-> Eventos críticos/Anomalías detectadas: {len(anomalias)}") # [cite: 8]

    # Formato de fechas para los ejes gráficos
    fmt = mdates.DateFormatter("%d/%m %H:%M")

    # ------------------------------------------
    # GENERACIÓN DE GRÁFICOS OPTIMIZADOS
    # ------------------------------------------

    # Gráfica 1: Línea de tiempo completa (Muestra el evento crítico)
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(df_presion["created_at"], df_presion["presion_psi"], color="#FF9800", linewidth=0.9, label="Presión Red")
    if len(anomalias) > 0:
        ax.scatter(anomalias["created_at"], anomalias["presion_psi"], color="red", s=25, zorder=5, label="Anomalías/Picos")
    ax.axhline(p_mean, color="blue", linestyle="--", linewidth=0.8, label=f"Media Operativa: {p_mean:.1f} psi")
    ax.set_ylabel("Presión [psi]")
    ax.set_title("Evolución Temporal de la Presión — JAAPRV Sinchal") # [cite: 10]
    ax.xaxis.set_major_formatter(fmt)
    ax.tick_params(axis="x", rotation=20)
    ax.grid(True, alpha=0.3)
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(GRAFICAS_DIR, "serie_temporal_presion.png"), dpi=150)
    plt.close()

    # Gráfica 2: Histograma Enfocado (Evita que el gráfico salga deformado o vacío)
    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.hist(df_operativo["presion_psi"], bins=15, color="#FF9800", edgecolor="white", alpha=0.85)
    ax.axvline(p_mean, color="red", linestyle="--", label=f"Media: {p_mean:.1f}")
    ax.set_xlabel("Presión [psi]")
    ax.set_ylabel("Frecuencia")
    ax.set_title("Distribución del Comportamiento Operativo")
    ax.set_xlim(0, 120)  # <-- Obliga a hacer zoom en la zona donde operan los datos reales
    ax.grid(True, alpha=0.2)
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(GRAFICAS_DIR, "histograma_presion.png"), dpi=150)
    plt.close()

    # Gráfica 3: Promedio Operativo por Hora (Usa la mediana para proteger la tendencia)
    df_presion["hora"] = df_presion["created_at"].dt.hour
    p_hora = df_presion.groupby("hora")["presion_psi"].median() # La mediana anula el efecto de ruidos eléctricos

    fig, ax = plt.subplots(figsize=(10, 3.5))
    ax.bar(p_hora.index, p_hora.values, color="#FF9800", alpha=0.85)
    ax.set_xlabel("Hora del Día")
    ax.set_ylabel("Presión Mediana [psi]")
    ax.set_title("Patrón Diario de Presión por Hora (Filtro de Tendencia)") # [cite: 41]
    ax.set_xticks(range(0, 24))
    ax.grid(True, alpha=0.2, axis="y")
    plt.tight_layout()
    plt.savefig(os.path.join(GRAFICAS_DIR, "presion_por_hora.png"), dpi=150)
    plt.close()
    print(f"-> Gráficos procesados y guardados con éxito en: '{GRAFICAS_DIR}'")

    # ==========================================
    # CONSTRUCCIÓN INTERNA DEL REPORTE PDF
    # ==========================================
    fecha_rep = datetime.now().strftime("%d/%m/%Y %H:%M")
    f_ini = df_presion["created_at"].min().strftime("%d/%m/%Y %H:%M") # [cite: 3]
    f_fin = df_presion["created_at"].max().strftime("%d/%m/%Y %H:%M") # [cite: 3]

    doc = SimpleDocTemplate(PDF_OUTPUT_PATH, pagesize=A4,
                            rightMargin=2*cm, leftMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)

    styles = getSampleStyleSheet()
    title_s = ParagraphStyle("T", parent=styles["Title"], fontSize=16, spaceAfter=4, textColor=colors.HexColor("#1565C0"), alignment=TA_CENTER)
    sub_s = ParagraphStyle("S", parent=styles["Normal"], fontSize=10, spaceAfter=12, textColor=colors.HexColor("#555555"), alignment=TA_CENTER)
    h1_s = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=12, spaceBefore=12, spaceAfter=6, textColor=colors.HexColor("#1565C0"))
    norm_s = ParagraphStyle("N", parent=styles["Normal"], fontSize=9.5, leading=14, spaceAfter=6)

    story = []
    story.append(Paragraph("Fortalecimiento del sistema hidrico", sub_s)) # [cite: 1]
    story.append(Paragraph("Reporte Hidráulico Técnico: Monitoreo de Presión", title_s)) # [cite: 2]
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#1565C0")))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(f"Generado (Hora Local): {fecha_rep} | Rango Operativo: {f_ini} a {f_fin}", sub_s))
    story.append(Spacer(1, 0.4*cm))

    # Sección 1: Resumen del análisis de datos
    story.append(Paragraph("1. Resumen de Análisis", h1_s)) # [cite: 4]
    story.append(Paragraph(
        f"Se procesó de forma exitosa el histórico consolidado en el archivo SQLite, analizando un total de "
        f"<b>{len(df_presion):,} lecturas independientes</b>[cite: 5]. Al aplicar ingeniería de datos para separar los eventos transitorios "
        f"del suministro normal, la red exhibe un promedio operativo estable de <b>{p_mean:.2f} psi</b>  con una variación de diseño "
        f"controlada, registrando un evento crítico de presión máxima puntual de <b>{p_max_absoluto:.1f} psi</b>.", norm_s))

    # Sección 2: Tabla estadística avanzada
    story.append(Paragraph("2. Resumen Estadístico Corregido", h1_s))
    t_data = [
        ["Métrica Operativa", "Valor Calculado"],
        ["Registros Históricos Procesados", f"{len(df_presion):,}"], # [cite: 8]
        ["Presión Mínima de Red", f"{p_min:.1f} psi"], # [cite: 8]
        ["Presión Máxima Comercial/Filtro", f"{p_max_operativa:.1f} psi"],
        ["Presión Promedio Normal de Red", f"{p_mean:.2f} psi"], # [cite: 8]
        ["Desviación Estándar Real (Dispersión)", f"{p_std:.2f} psi"], # [cite: 8]
        ["Pico Máximo Absoluto (Posible Ariete)", f"{p_max_absoluto:.1f} psi"], # [cite: 8]
        ["Anomalías de Voltaje / Transitorios", str(len(anomalias))] # [cite: 8]
    ]
    t = Table(t_data, colWidths=[8.5*cm, 5.5*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1565C0")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.HexColor("#F9F9F9"), colors.white]),
        ("PADDING", (0,0), (-1,-1), 5)
    ]))
    story.append(t)
    story.append(Spacer(1, 0.5*cm))

    # Sección 3: Inserción de gráficos
    story.append(Paragraph("3. Comportamiento Temporal e Histogramas Operativos", h1_s)) # [cite: 9]
    story.append(Image(os.path.join(GRAFICAS_DIR, "serie_temporal_presion.png"), width=16*cm, height=5.3*cm))
    story.append(Spacer(1, 0.3*cm))
    story.append(Image(os.path.join(GRAFICAS_DIR, "histograma_presion.png"), width=12*cm, height=6*cm))
    story.append(Spacer(1, 0.3*cm))

    # Sección 4: Patrón por Hora
    story.append(Paragraph("4. Perfil Operativo Horario Continuo", h1_s)) # [cite: 40]
    story.append(Image(os.path.join(GRAFICAS_DIR, "presion_por_hora.png"), width=15*cm, height=5*cm))

    # Compilar el PDF
    doc.build(story)
    print(f"\n✓ ¡REPORTE PDF CONSTRUIDO CON ÉXITO EN: '{PDF_OUTPUT_PATH}'!")