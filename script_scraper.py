import requests
import pandas as pd
import sqlite3
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from datetime import datetime, timedelta
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

# --- OPENWEATHER API ---
# La API Key se lee desde la variable de entorno OPENWEATHER_API_KEY
# En GitHub Actions: Settings > Secrets > New repository secret
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")

# Coordenadas de la zona de monitoreo JAAPRV Sinchal
# (Ajustar latitud/longitud exactas de la captación hídrica)
OW_LAT  = -1.940948   # Latitud  — Ajustar al punto exacto de la captación
OW_LON  = -80.697497   # Longitud — Ajustar al punto exacto de la captación
OW_CITY = "Sinchal, EC"

# Detectar el directorio de ejecución actual de forma automática
BASE_DIR = os.getcwd()
GRAFICAS_DIR = os.path.join(BASE_DIR, "graficas")
os.makedirs(GRAFICAS_DIR, exist_ok=True)

PDF_OUTPUT_PATH = os.path.join(BASE_DIR, "reporte_jaaprv_presion.pdf")

# ==========================================
# INICIALIZACIÓN DE LA BASE DE DATOS SQLite
# ==========================================
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Tabla de lecturas del sensor (ThingSpeak)
cursor.execute('''
    CREATE TABLE IF NOT EXISTS lecturas (
        fecha TEXT UNIQUE,
        nivel_cm REAL,
        presion_psi REAL
    )
''')

# Tabla de precipitación histórica (OpenWeatherMap)
cursor.execute('''
    CREATE TABLE IF NOT EXISTS precipitacion (
        fecha TEXT UNIQUE,
        lluvia_mm REAL,
        humedad_pct REAL,
        temp_c REAL,
        descripcion TEXT
    )
''')
conn.commit()

# ==========================================
# MÓDULO 1: DESCARGA DE THINGSPEAK
# ==========================================
print("\n[1/3] Descargando datos del sensor desde ThingSpeak...")
url = f"https://api.thingspeak.com/channels/{CHANNEL_ID}/feeds.json?results=3450"

try:
    data = requests.get(url, timeout=30).json()
    df_api = pd.DataFrame(data["feeds"])

    # CORRECCIÓN DE ZONA HORARIA: UTC → Hora Local de Ecuador
    df_api["created_at"] = pd.to_datetime(df_api["created_at"])
    df_api["created_at"] = df_api["created_at"].dt.tz_convert('America/Guayaquil')
    df_api["created_at"] = df_api["created_at"].dt.tz_localize(None)
    df_api["fecha_texto"] = df_api["created_at"].dt.strftime('%Y-%m-%d %H:%M:%S')

    df_api["nivel_cm"]    = pd.to_numeric(df_api["field1"], errors="coerce")
    df_api["presion_psi"] = pd.to_numeric(df_api["field2"], errors="coerce")

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
    print(f"   -> Éxito. Se añadieron {registros_nuevos} filas nuevas de ThingSpeak.")
except Exception as e:
    print(f"   -> Alerta: No se pudo conectar con ThingSpeak ({e}). Usando histórico local.")

# ==========================================
# MÓDULO 2: OPENWEATHER — PRECIPITACIÓN HISTÓRICA (ÚLTIMOS 6 MESES)
# ==========================================
print("\n[2/3] Consultando precipitación histórica desde OpenWeatherMap...")

precipitacion_activa = False   # Se activa si la integración funcionó

# --- OPENWEATHER API ---
# La API Key se lee desde la variable de entorno OPENWEATHER_API_KEY
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")

# ==========================================
# MÓDULO 2: OPENWEATHER — CLIMA ACTUAL (RECOLECCIÓN DIARIA)
# ==========================================
print("\n[2/3] Consultando clima actual desde OpenWeatherMap...")

precipitacion_activa = False

if not OPENWEATHER_API_KEY:
    print("   -> AVISO: Variable OPENWEATHER_API_KEY no configurada.")
    print("      Para activar: exporta la variable o agrégala como Secret en GitHub Actions.")
    print("      El script continuará con datos del sensor solamente.")
else:
    try:
        # Usamos la API Current Weather de OpenWeather (100% Gratuita)
        ow_url = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?lat={OW_LAT}&lon={OW_LON}"
            f"&appid={OPENWEATHER_API_KEY}&units=metric"
        )
        
        resp = requests.get(ow_url, timeout=30)
        if resp.status_code == 200:
            ow_data = resp.json()
            
            # Hora actual local
            fecha_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # Extraer datos (la lluvia viene si llovió en la última hora)
            lluvia_mm = ow_data.get("rain", {}).get("1h", 0.0)
            humedad   = ow_data.get("main", {}).get("humidity", 0.0)
            temp_c    = ow_data.get("main", {}).get("temp", 0.0)
            descripcion = ow_data.get("weather", [{}])[0].get("description", "")
            
            try:
                cursor.execute('''
                    INSERT INTO precipitacion (fecha, lluvia_mm, humedad_pct, temp_c, descripcion)
                    VALUES (?, ?, ?, ?, ?)
                ''', (fecha_str, lluvia_mm, humedad, temp_c, descripcion))
                conn.commit()
                print(f"   -> Éxito. Se guardó el clima actual: Temp {temp_c}°C, Lluvia {lluvia_mm}mm")
                precipitacion_activa = True
            except sqlite3.IntegrityError:
                print("   -> El clima de este momento ya estaba registrado.")
                precipitacion_activa = True
        else:
            print(f"   -> Error HTTP {resp.status_code} al consultar OpenWeather: {resp.text[:120]}")

    except Exception as e:
        print(f"   -> Error al consultar OpenWeatherMap: {e}")
        print("      El reporte continuará sin datos de lluvia.")

# ==========================================
# MÓDULO 3: LECTURA Y ANÁLISIS DE DATOS
# ==========================================
print("\n[3/3] Procesando datos para análisis estadístico y gráficos...")

# --- Datos de presión y nivel ---
query_sensor = """
    SELECT fecha, nivel_cm, presion_psi
    FROM lecturas
    WHERE presion_psi IS NOT NULL
    ORDER BY fecha ASC
"""
df_presion = pd.read_sql_query(query_sensor, conn)

# --- Datos de precipitación ---
df_lluvia = pd.DataFrame()
if precipitacion_activa:
    query_lluvia = """
        SELECT fecha, lluvia_mm, humedad_pct, temp_c
        FROM precipitacion
        ORDER BY fecha ASC
    """
    df_lluvia = pd.read_sql_query(query_lluvia, conn)

conn.close()

# ==========================================
# PROCESAMIENTO MATEMÁTICO Y ESTADÍSTICO
# ==========================================
if len(df_presion) == 0:
    print("   -> Alerta: La base de datos está vacía. No hay registros de presión válidos.")
else:
    print(f"   -> Cargados {len(df_presion):,} registros históricos del sensor.")

    # Formatear columnas de análisis
    df_presion["created_at"] = pd.to_datetime(df_presion["fecha"])
    df_presion["presion_psi"] = pd.to_numeric(df_presion["presion_psi"])
    df_presion["nivel_cm"]    = pd.to_numeric(df_presion["nivel_cm"], errors="coerce")

    # Frecuencia real de envío del sensor
    df_presion['diferencia_tiempo'] = df_presion['created_at'].diff()
    frecuencia_promedio = df_presion['diferencia_tiempo'].mean()
    print(f"   -> Frecuencia operativa real del sensor: {frecuencia_promedio}")

    # FILTRADO: separar comportamiento operativo de picos extremos (> 120 psi)
    df_operativo = df_presion[df_presion["presion_psi"] <= 120].copy()

    # Métricas estadísticas base
    p_min           = df_operativo["presion_psi"].min()
    p_max_operativa = df_operativo["presion_psi"].max()
    p_mean          = df_operativo["presion_psi"].mean()
    p_median        = df_operativo["presion_psi"].median()
    p_std           = df_operativo["presion_psi"].std()
    p_max_absoluto  = df_presion["presion_psi"].max()

    # Anomalías por Z-Score
    df_presion["zscore"] = (df_presion["presion_psi"] - p_mean) / (p_std if p_std > 0 else 1)
    anomalias = df_presion[df_presion["zscore"].abs() > 3]
    print(f"   -> Anomalías/Transitorios detectados: {len(anomalias)}")

    # ==========================================
    # ANÁLISIS DE CORRELACIÓN LLUVIA ↔ PRESIÓN ↔ NIVEL
    # ==========================================
    correlacion_lluvia_presion = None
    correlacion_lluvia_nivel   = None
    lluvia_total_6m   = 0.0
    lluvia_promedio   = 0.0
    dias_con_lluvia   = 0
    df_merged         = pd.DataFrame()

    if not df_lluvia.empty:
        df_lluvia["created_at"] = pd.to_datetime(df_lluvia["fecha"])
        df_lluvia["lluvia_mm"]  = pd.to_numeric(df_lluvia["lluvia_mm"], errors="coerce").fillna(0)

        # Resumir precipitación por día para correlacionar con el sensor
        df_lluvia_diaria = (
            df_lluvia
            .set_index("created_at")
            .resample("D")["lluvia_mm"]
            .sum()
            .reset_index()
        )
        df_lluvia_diaria.rename(columns={"created_at": "fecha_dia"}, inplace=True)

        # Resumir sensor por día (mediana — robusta a outliers)
        df_sensor_diario = (
            df_presion
            .set_index("created_at")
            .resample("D")[["presion_psi", "nivel_cm"]]
            .median()
            .reset_index()
        )
        df_sensor_diario.rename(columns={"created_at": "fecha_dia"}, inplace=True)

        # Merge por día
        df_merged = pd.merge(df_sensor_diario, df_lluvia_diaria, on="fecha_dia", how="inner")
        df_merged.dropna(subset=["presion_psi", "lluvia_mm"], inplace=True)

        if len(df_merged) >= 3:
            correlacion_lluvia_presion = df_merged["lluvia_mm"].corr(df_merged["presion_psi"])
            if "nivel_cm" in df_merged.columns:
                correlacion_lluvia_nivel = df_merged["lluvia_mm"].corr(df_merged["nivel_cm"])

        lluvia_total_6m = df_lluvia["lluvia_mm"].sum()
        lluvia_promedio  = df_lluvia["lluvia_mm"].mean()
        dias_con_lluvia  = int((df_lluvia_diaria["lluvia_mm"] > 0.1).sum())

        print(f"   -> Lluvia total (6 meses): {lluvia_total_6m:.1f} mm")
        if correlacion_lluvia_presion is not None:
            print(f"   -> Correlación lluvia↔presión: {correlacion_lluvia_presion:.3f}")
        if correlacion_lluvia_nivel is not None:
            print(f"   -> Correlación lluvia↔nivel:   {correlacion_lluvia_nivel:.3f}")

    # ==========================================
    # GENERACIÓN DE GRÁFICOS
    # ==========================================
    fmt = mdates.DateFormatter("%d/%m %H:%M")

    # GRÁFICA 1: Serie temporal de presión completa
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(df_presion["created_at"], df_presion["presion_psi"],
            color="#FF9800", linewidth=0.9, label="Presión Red")
    if len(anomalias) > 0:
        ax.scatter(anomalias["created_at"], anomalias["presion_psi"],
                   color="red", s=25, zorder=5, label="Anomalías/Picos")
    ax.axhline(p_mean, color="blue", linestyle="--", linewidth=0.8,
               label=f"Media Operativa: {p_mean:.1f} psi")
    ax.set_ylabel("Presión [psi]")
    ax.set_title("Evolución Temporal de la Presión — JAAPRV Sinchal")
    ax.xaxis.set_major_formatter(fmt)
    ax.tick_params(axis="x", rotation=20)
    ax.grid(True, alpha=0.3)
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(GRAFICAS_DIR, "serie_temporal_presion.png"), dpi=150)
    plt.close()

    # GRÁFICA 2: Histograma de distribución operativa
    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.hist(df_operativo["presion_psi"], bins=15, color="#FF9800", edgecolor="white", alpha=0.85)
    ax.axvline(p_mean, color="red", linestyle="--", label=f"Media: {p_mean:.1f}")
    ax.set_xlabel("Presión [psi]")
    ax.set_ylabel("Frecuencia")
    ax.set_title("Distribución del Comportamiento Operativo")
    ax.set_xlim(0, 120)
    ax.grid(True, alpha=0.2)
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(GRAFICAS_DIR, "histograma_presion.png"), dpi=150)
    plt.close()

    # GRÁFICA 3: Patrón horario de presión (mediana)
    df_presion["hora"] = df_presion["created_at"].dt.hour
    p_hora = df_presion.groupby("hora")["presion_psi"].median()

    fig, ax = plt.subplots(figsize=(10, 3.5))
    ax.bar(p_hora.index, p_hora.values, color="#FF9800", alpha=0.85)
    ax.set_xlabel("Hora del Día")
    ax.set_ylabel("Presión Mediana [psi]")
    ax.set_title("Patrón Diario de Presión por Hora (Filtro de Tendencia)")
    ax.set_xticks(range(0, 24))
    ax.grid(True, alpha=0.2, axis="y")
    plt.tight_layout()
    plt.savefig(os.path.join(GRAFICAS_DIR, "presion_por_hora.png"), dpi=150)
    plt.close()

    # GRÁFICA 4: Precipitación histórica diaria (6 meses) — solo si hay datos de OpenWeather
    if not df_lluvia.empty:
        df_lluvia_diaria_plot = (
            df_lluvia
            .set_index("created_at")
            .resample("D")["lluvia_mm"]
            .sum()
            .reset_index()
        )
        fig, ax = plt.subplots(figsize=(12, 3.5))
        ax.bar(df_lluvia_diaria_plot["created_at"], df_lluvia_diaria_plot["lluvia_mm"],
               color="#1565C0", alpha=0.75, width=0.8, label="Lluvia diaria (mm)")
        ax.set_xlabel("Fecha")
        ax.set_ylabel("Precipitación [mm/día]")
        ax.set_title(f"Precipitación Histórica Últimos 6 Meses — {OW_CITY}")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m/%y"))
        ax.tick_params(axis="x", rotation=25)
        ax.grid(True, alpha=0.2, axis="y")
        ax.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(GRAFICAS_DIR, "precipitacion_historica.png"), dpi=150)
        plt.close()
        print("   -> Gráfica de precipitación histórica generada.")

    # GRÁFICA 5: Correlación Lluvia ↔ Presión (scatter) — solo si hay datos fusionados
    if not df_merged.empty and correlacion_lluvia_presion is not None:
        fig, axes = plt.subplots(1, 2, figsize=(12, 4))

        # Scatter lluvia vs presión
        axes[0].scatter(df_merged["lluvia_mm"], df_merged["presion_psi"],
                        color="#1565C0", alpha=0.6, edgecolors="white", linewidths=0.5)
        m1, b1 = np.polyfit(df_merged["lluvia_mm"], df_merged["presion_psi"], 1)
        x_line = np.linspace(df_merged["lluvia_mm"].min(), df_merged["lluvia_mm"].max(), 100)
        axes[0].plot(x_line, m1 * x_line + b1, color="#FF9800", linewidth=1.5,
                     label=f"y = {m1:.2f}x + {b1:.1f}")
        axes[0].set_xlabel("Precipitación [mm/día]")
        axes[0].set_ylabel("Presión Mediana [psi]")
        axes[0].set_title(f"Lluvia vs Presión  (r = {correlacion_lluvia_presion:.3f})")
        axes[0].legend()
        axes[0].grid(True, alpha=0.2)

        # Scatter lluvia vs nivel (si hay nivel)
        if correlacion_lluvia_nivel is not None and "nivel_cm" in df_merged.columns:
            df_nivel_valid = df_merged.dropna(subset=["nivel_cm"])
            if len(df_nivel_valid) >= 3:
                axes[1].scatter(df_nivel_valid["lluvia_mm"], df_nivel_valid["nivel_cm"],
                                color="#4CAF50", alpha=0.6, edgecolors="white", linewidths=0.5)
                m2, b2 = np.polyfit(df_nivel_valid["lluvia_mm"], df_nivel_valid["nivel_cm"], 1)
                x_line2 = np.linspace(df_nivel_valid["lluvia_mm"].min(), df_nivel_valid["lluvia_mm"].max(), 100)
                axes[1].plot(x_line2, m2 * x_line2 + b2, color="#FF9800", linewidth=1.5,
                             label=f"y = {m2:.2f}x + {b2:.1f}")
                axes[1].set_xlabel("Precipitación [mm/día]")
                axes[1].set_ylabel("Nivel Mediano [cm]")
                axes[1].set_title(f"Lluvia vs Nivel  (r = {correlacion_lluvia_nivel:.3f})")
                axes[1].legend()
                axes[1].grid(True, alpha=0.2)
            else:
                axes[1].set_visible(False)
        else:
            axes[1].set_visible(False)

        plt.tight_layout()
        plt.savefig(os.path.join(GRAFICAS_DIR, "correlacion_lluvia.png"), dpi=150)
        plt.close()
        print("   -> Gráfica de correlación lluvia↔presión/nivel generada.")

    print(f"   -> Todos los gráficos guardados en: '{GRAFICAS_DIR}'")

    # ==========================================
    # CONSTRUCCIÓN DEL REPORTE PDF
    # ==========================================
    fecha_rep = datetime.now().strftime("%d/%m/%Y %H:%M")
    f_ini = df_presion["created_at"].min().strftime("%d/%m/%Y %H:%M")
    f_fin = df_presion["created_at"].max().strftime("%d/%m/%Y %H:%M")

    doc = SimpleDocTemplate(PDF_OUTPUT_PATH, pagesize=A4,
                            rightMargin=2*cm, leftMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)

    styles = getSampleStyleSheet()
    title_s = ParagraphStyle("T", parent=styles["Title"],    fontSize=16, spaceAfter=4,
                              textColor=colors.HexColor("#1565C0"), alignment=TA_CENTER)
    sub_s   = ParagraphStyle("S", parent=styles["Normal"],   fontSize=10, spaceAfter=12,
                              textColor=colors.HexColor("#555555"), alignment=TA_CENTER)
    h1_s    = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=12, spaceBefore=12,
                              spaceAfter=6, textColor=colors.HexColor("#1565C0"))
    norm_s  = ParagraphStyle("N", parent=styles["Normal"],   fontSize=9.5, leading=14, spaceAfter=6)
    warn_s  = ParagraphStyle("W", parent=styles["Normal"],   fontSize=9,   leading=13, spaceAfter=4,
                              textColor=colors.HexColor("#B71C1C"), backColor=colors.HexColor("#FFF3F3"))

    story = []
    story.append(Paragraph("Fortalecimiento del Sistema Hídrico — JAAPRV Sinchal", sub_s))
    story.append(Paragraph("Reporte Técnico: Presión, Nivel y Precipitación", title_s))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#1565C0")))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(
        f"Generado (Hora Local): {fecha_rep} | "
        f"Rango sensor: {f_ini} a {f_fin}", sub_s))
    story.append(Spacer(1, 0.4*cm))

    # --- SECCIÓN 1: Resumen del análisis ---
    story.append(Paragraph("1. Resumen de Análisis", h1_s))
    story.append(Paragraph(
        f"Se procesó el histórico consolidado en SQLite con un total de "
        f"<b>{len(df_presion):,} lecturas</b> del sensor ThingSpeak. Al filtrar transitorios "
        f"eléctricos (>120 psi), la red exhibe una presión operativa promedio de "
        f"<b>{p_mean:.2f} psi</b>, con una variación estándar de <b>{p_std:.2f} psi</b> y "
        f"un pico máximo puntual de <b>{p_max_absoluto:.1f} psi</b> (posible golpe de ariete).", norm_s))

    # --- SECCIÓN 2: Tabla estadística del sensor ---
    story.append(Paragraph("2. Resumen Estadístico del Sensor", h1_s))
    t_data = [
        ["Métrica Operativa", "Valor Calculado"],
        ["Registros Históricos Procesados",    f"{len(df_presion):,}"],
        ["Presión Mínima de Red",              f"{p_min:.1f} psi"],
        ["Presión Máxima Comercial (< 120)",   f"{p_max_operativa:.1f} psi"],
        ["Presión Promedio Normal de Red",     f"{p_mean:.2f} psi"],
        ["Desviación Estándar (Dispersión)",   f"{p_std:.2f} psi"],
        ["Pico Máximo Absoluto (Posible Ariete)", f"{p_max_absoluto:.1f} psi"],
        ["Anomalías / Transitorios Eléctricos",   str(len(anomalias))],
    ]
    t = Table(t_data, colWidths=[8.5*cm, 5.5*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#1565C0")),
        ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
        ("ALIGN",        (0,0), (-1,-1), "CENTER"),
        ("GRID",         (0,0), (-1,-1), 0.5, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.HexColor("#F9F9F9"), colors.white]),
        ("PADDING",      (0,0), (-1,-1), 5),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.5*cm))

    # --- SECCIÓN 3: OpenWeatherMap — precipitación e impacto ---
    story.append(Paragraph("3. Análisis de Precipitación — OpenWeatherMap (Recolección Diaria)", h1_s))

    if precipitacion_activa and not df_lluvia.empty:
        # Interpretación de la correlación
        def interpretar_correlacion(r):
            if r is None: return "No calculable (datos insuficientes)"
            if abs(r) >= 0.7: fuerza = "fuerte"
            elif abs(r) >= 0.4: fuerza = "moderada"
            else: fuerza = "débil"
            direccion = "positiva" if r >= 0 else "negativa"
            return f"{r:.3f} — correlación {fuerza} {direccion}"

        story.append(Paragraph(
            f"Se integró la fuente de precipitación actual de OpenWeatherMap para las coordenadas "
            f"de la zona de captación ({OW_LAT}°, {OW_LON}°). Se recopila un registro diario desde "
            f"el 4 de septiembre para enriquecer el modelo matemático con la lluvia como entrada al sistema.", norm_s))

        ow_data_table = [
            ["Métrica Climática", "Valor"],
            ["Precipitación Total (Acumulada)",  f"{lluvia_total_6m:.1f} mm"],
            ["Precipitación Promedio Diaria",  f"{lluvia_promedio:.2f} mm/día"],
            ["Días con lluvia registrada",     str(dias_con_lluvia)],
            ["Correlación Lluvia ↔ Presión",   interpretar_correlacion(correlacion_lluvia_presion)],
            ["Correlación Lluvia ↔ Nivel",     interpretar_correlacion(correlacion_lluvia_nivel)],
        ]
        ow_t = Table(ow_data_table, colWidths=[8.5*cm, 5.5*cm])
        ow_t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#1565C0")),
            ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
            ("ALIGN",         (0,0), (-1,-1), "CENTER"),
            ("GRID",          (0,0), (-1,-1), 0.5, colors.HexColor("#CCCCCC")),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.HexColor("#EDF7FF"), colors.white]),
            ("PADDING",       (0,0), (-1,-1), 5),
        ]))
        story.append(ow_t)
        story.append(Spacer(1, 0.4*cm))

        # Interpretación narrativa
        if correlacion_lluvia_presion is not None:
            if correlacion_lluvia_presion > 0.4:
                interp = (
                    "La correlación positiva indica que los eventos de lluvia intensa incrementan "
                    "la presión en la red, posiblemente por recarga de la captación y mayor columna "
                    "de agua disponible."
                )
            elif correlacion_lluvia_presion < -0.4:
                interp = (
                    "La correlación negativa sugiere que la lluvia reduce la demanda de agua de la "
                    "red (los usuarios disminuyen el consumo en días lluviosos), lo que puede "
                    "explicar las variaciones de presión observadas."
                )
            else:
                interp = (
                    "La correlación es débil, lo que indica que la presión en la red no depende "
                    "directamente de la lluvia a nivel diario. Otros factores (apertura de válvulas, "
                    "demanda horaria) dominan el comportamiento de la red."
                )
            story.append(Paragraph(f"<b>Interpretación:</b> {interp}", norm_s))

    else:
        story.append(Paragraph(
            "⚠ INTEGRACIÓN OPENWEATHER NO ACTIVA en esta ejecución. "
            "Para activarla, configure el Secret OPENWEATHER_API_KEY en GitHub Actions "
            "o exporte la variable de entorno antes de ejecutar el script.", warn_s))

    story.append(Spacer(1, 0.3*cm))

    # --- SECCIÓN 4: Gráficos del sensor ---
    story.append(Paragraph("4. Comportamiento Temporal e Histogramas Operativos", h1_s))
    story.append(Image(os.path.join(GRAFICAS_DIR, "serie_temporal_presion.png"),
                       width=16*cm, height=5.3*cm))
    story.append(Spacer(1, 0.3*cm))
    story.append(Image(os.path.join(GRAFICAS_DIR, "histograma_presion.png"),
                       width=12*cm, height=6*cm))
    story.append(Spacer(1, 0.3*cm))

    # --- SECCIÓN 5: Patrón horario ---
    story.append(Paragraph("5. Perfil Operativo Horario Continuo", h1_s))
    story.append(Image(os.path.join(GRAFICAS_DIR, "presion_por_hora.png"),
                       width=15*cm, height=5*cm))

    # --- SECCIÓN 6: Gráficos de precipitación (solo si activo) ---
    lluvia_png = os.path.join(GRAFICAS_DIR, "precipitacion_historica.png")
    correl_png = os.path.join(GRAFICAS_DIR, "correlacion_lluvia.png")

    if os.path.exists(lluvia_png):
        story.append(Spacer(1, 0.3*cm))
        story.append(Paragraph("6. Precipitación Histórica (OpenWeatherMap)", h1_s))
        story.append(Image(lluvia_png, width=16*cm, height=5.3*cm))

    if os.path.exists(correl_png):
        story.append(Spacer(1, 0.3*cm))
        story.append(Paragraph("7. Correlación Lluvia ↔ Presión / Nivel", h1_s))
        story.append(Image(correl_png, width=16*cm, height=5.5*cm))
        story.append(Paragraph(
            "<b>Nota metodológica:</b> La correlación se calculó agregando ambas fuentes "
            "por día calendario (mediana del sensor + suma de lluvia). El coeficiente r de "
            "Pearson mide la relación lineal entre variables: r cercano a +1 o -1 indica "
            "correlación fuerte; r cercano a 0 indica independencia estadística.", norm_s))

    # Compilar el PDF
    doc.build(story)
    print(f"\n✓ REPORTE PDF GENERADO CON ÉXITO: '{PDF_OUTPUT_PATH}'")
    print(f"  Incluye datos OpenWeatherMap: {'SÍ' if precipitacion_activa else 'NO (configura OPENWEATHER_API_KEY)'}")