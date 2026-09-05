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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, HRFlowable, PageBreak
from reportlab.lib.enums import TA_CENTER
import os

# ==========================================
# CONFIGURACIÓN DE RUTAS UNIVERSALES Y API
# ==========================================
DB_PATH = "base_datos_jaaprv.db"
CHANNEL_ID = "2941382"

# --- OPENWEATHER API ---
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")

# Coordenadas de la zona de monitoreo JAAPRV Sinchal
OW_LAT  = -1.940948   # Latitud
OW_LON  = -80.697497   # Longitud
OW_CITY = "Sinchal, EC"

# Detectar el directorio de ejecución actual
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

# Tabla de precipitación y clima (OpenWeatherMap) — EXPANDIDA
cursor.execute('''
    CREATE TABLE IF NOT EXISTS precipitacion (
        fecha TEXT UNIQUE,
        lluvia_mm REAL,
        humedad_pct REAL,
        temp_c REAL,
        velocidad_viento_ms REAL,
        presion_atm_hpa REAL,
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
            
            # Extraer TODAS las variables climáticas disponibles
            lluvia_mm = ow_data.get("rain", {}).get("1h", 0.0)
            humedad   = ow_data.get("main", {}).get("humidity", 0.0)
            temp_c    = ow_data.get("main", {}).get("temp", 0.0)
            velocidad_viento = ow_data.get("wind", {}).get("speed", 0.0)
            presion_atm = ow_data.get("main", {}).get("pressure", 0.0)
            descripcion = ow_data.get("weather", [{}])[0].get("description", "")
            
            try:
                cursor.execute('''
                    INSERT INTO precipitacion (fecha, lluvia_mm, humedad_pct, temp_c, velocidad_viento_ms, presion_atm_hpa, descripcion)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (fecha_str, lluvia_mm, humedad, temp_c, velocidad_viento, presion_atm, descripcion))
                conn.commit()
                print(f"   -> Éxito. Clima: Temp {temp_c}°C, Humedad {humedad}%, Viento {velocidad_viento} m/s, Presión {presion_atm} hPa")
                precipitacion_activa = True
            except sqlite3.IntegrityError:
                print("   -> El clima de este momento ya estaba registrado.")
                precipitacion_activa = True
        else:
            print(f"   -> Error HTTP {resp.status_code} al consultar OpenWeather: {resp.text[:120]}")

    except Exception as e:
        print(f"   -> Error al consultar OpenWeatherMap: {e}")
        print("      El reporte continuará sin datos climáticos.")

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

# --- Datos de clima (expandidos) ---
df_clima = pd.DataFrame()
if precipitacion_activa:
    query_clima = """
        SELECT fecha, lluvia_mm, humedad_pct, temp_c, velocidad_viento_ms, presion_atm_hpa, descripcion
        FROM precipitacion
        ORDER BY fecha ASC
    """
    df_clima = pd.read_sql_query(query_clima, conn)

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
    # ANÁLISIS DE CLIMA Y CORRELACIONES
    # ==========================================
    correlacion_lluvia_presion = None
    correlacion_temp_presion = None
    correlacion_humedad_presion = None
    correlacion_viento_presion = None
    
    temp_promedio = 0.0
    humedad_promedio = 0.0
    viento_promedio = 0.0
    presion_atm_promedio = 0.0
    lluvia_total = 0.0
    dias_con_lluvia = 0
    df_merged = pd.DataFrame()

    if not df_clima.empty:
        df_clima["created_at"] = pd.to_datetime(df_clima["fecha"])
        df_clima["lluvia_mm"]  = pd.to_numeric(df_clima["lluvia_mm"], errors="coerce").fillna(0)
        df_clima["humedad_pct"] = pd.to_numeric(df_clima["humedad_pct"], errors="coerce").fillna(0)
        df_clima["temp_c"] = pd.to_numeric(df_clima["temp_c"], errors="coerce").fillna(0)
        df_clima["velocidad_viento_ms"] = pd.to_numeric(df_clima["velocidad_viento_ms"], errors="coerce").fillna(0)
        df_clima["presion_atm_hpa"] = pd.to_numeric(df_clima["presion_atm_hpa"], errors="coerce").fillna(0)

        # Calcular promedios generales
        temp_promedio = df_clima["temp_c"].mean()
        humedad_promedio = df_clima["humedad_pct"].mean()
        viento_promedio = df_clima["velocidad_viento_ms"].mean()
        presion_atm_promedio = df_clima["presion_atm_hpa"].mean()
        lluvia_total = df_clima["lluvia_mm"].sum()
        
        # Resumir por día
        df_clima_diaria = (
            df_clima
            .set_index("created_at")
            .resample("D")[["lluvia_mm", "humedad_pct", "temp_c", "velocidad_viento_ms", "presion_atm_hpa"]]
            .agg({
                "lluvia_mm": "sum",
                "humedad_pct": "mean",
                "temp_c": "mean",
                "velocidad_viento_ms": "mean",
                "presion_atm_hpa": "mean"
            })
            .reset_index()
        )
        df_clima_diaria.rename(columns={"created_at": "fecha_dia"}, inplace=True)

        # Resumir sensor por día (mediana)
        df_sensor_diario = (
            df_presion
            .set_index("created_at")
            .resample("D")[["presion_psi", "nivel_cm"]]
            .median()
            .reset_index()
        )
        df_sensor_diario.rename(columns={"created_at": "fecha_dia"}, inplace=True)

        # Merge por día
        df_merged = pd.merge(df_sensor_diario, df_clima_diaria, on="fecha_dia", how="inner")
        df_merged.dropna(subset=["presion_psi"], inplace=True)

        # Calcular correlaciones (solo si hay suficientes datos)
        if len(df_merged) >= 3:
            if "lluvia_mm" in df_merged.columns and df_merged["lluvia_mm"].notna().sum() > 0:
                correlacion_lluvia_presion = df_merged["lluvia_mm"].corr(df_merged["presion_psi"])
            if "temp_c" in df_merged.columns and df_merged["temp_c"].notna().sum() > 0:
                correlacion_temp_presion = df_merged["temp_c"].corr(df_merged["presion_psi"])
            if "humedad_pct" in df_merged.columns and df_merged["humedad_pct"].notna().sum() > 0:
                correlacion_humedad_presion = df_merged["humedad_pct"].corr(df_merged["presion_psi"])
            if "velocidad_viento_ms" in df_merged.columns and df_merged["velocidad_viento_ms"].notna().sum() > 0:
                correlacion_viento_presion = df_merged["velocidad_viento_ms"].corr(df_merged["presion_psi"])

        dias_con_lluvia = int((df_clima["lluvia_mm"] > 0.1).sum())

        print(f"   -> Temperatura promedio: {temp_promedio:.2f}°C")
        print(f"   -> Humedad promedio: {humedad_promedio:.2f}%")
        print(f"   -> Velocidad del viento promedio: {viento_promedio:.2f} m/s")
        print(f"   -> Presión atmosférica promedio: {presion_atm_promedio:.2f} hPa")

    # ==========================================
    # GENERACIÓN DE GRÁFICOS
    # ==========================================
    fmt = mdates.DateFormatter("%d/%m %H:%M")

    # GRÁFICA 1: Serie temporal de presión
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

    # GRÁFICA 2.1: BOXPLOT
    fig, ax = plt.subplots(figsize=(8, 5))
    bp = ax.boxplot([df_operativo["presion_psi"]], 
                     patch_artist=True,
                     widths=0.5,
                     showmeans=True,
                     meanprops=dict(marker='D', markerfacecolor='red', markersize=8, label='Media'))
    ax.set_xticklabels(["Presión Operativa"])
    
    for patch in bp['boxes']:
        patch.set_facecolor('#FF9800')
        patch.set_alpha(0.7)
    
    for whisker in bp['whiskers']:
        whisker.set(color='#1565C0', linewidth=1.5)
    for cap in bp['caps']:
        cap.set(color='#1565C0', linewidth=1.5)
    for median in bp['medians']:
        median.set(color='darkred', linewidth=2)
    
    ax.set_ylabel("Presión [psi]", fontsize=11)
    ax.set_title("Distribución Estadística de Presión — Caja y Bigotes", fontsize=12, fontweight='bold')
    ax.grid(True, alpha=0.3, axis='y')
    
    ax.text(1.15, p_mean, f"Media: {p_mean:.2f} psi", fontsize=9, color='red', fontweight='bold')
    ax.text(1.15, p_median, f"Mediana: {p_median:.2f} psi", fontsize=9, color='darkred', fontweight='bold')
    
    plt.tight_layout()
    plt.savefig(os.path.join(GRAFICAS_DIR, "boxplot_presion.png"), dpi=150)
    plt.close()

    # GRÁFICA 3: Patrón horario de presión
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

    # GRÁFICA 4: Precipitación diaria
    if not df_clima.empty:
        df_lluvia_diaria = (
            df_clima
            .set_index("created_at")
            .resample("D")["lluvia_mm"]
            .sum()
            .reset_index()
        )
        fig, ax = plt.subplots(figsize=(12, 3.5))
        ax.bar(df_lluvia_diaria["created_at"], df_lluvia_diaria["lluvia_mm"],
               color="#1565C0", alpha=0.75, width=0.8, label="Lluvia diaria (mm)")
        ax.set_xlabel("Fecha")
        ax.set_ylabel("Precipitación [mm/día]")
        ax.set_title(f"Precipitación — Recolección Diaria desde 04 de Septiembre — {OW_CITY}")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m/%y"))
        ax.tick_params(axis="x", rotation=25)
        ax.grid(True, alpha=0.2, axis="y")
        ax.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(GRAFICAS_DIR, "precipitacion_historica.png"), dpi=150)
        plt.close()

    # GRÁFICA 5: Serie temporal de temperatura
    if not df_clima.empty:
        fig, ax = plt.subplots(figsize=(12, 3.5))
        ax.plot(df_clima["created_at"], df_clima["temp_c"], color="#E74C3C", linewidth=1.2, marker='o', markersize=3, label="Temperatura")
        ax.axhline(temp_promedio, color="darkred", linestyle="--", linewidth=1, label=f"Promedio: {temp_promedio:.1f}°C")
        ax.set_xlabel("Fecha")
        ax.set_ylabel("Temperatura [°C]")
        ax.set_title(f"Temperatura Ambiental — {OW_CITY}")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        ax.tick_params(axis="x", rotation=25)
        ax.grid(True, alpha=0.2)
        ax.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(GRAFICAS_DIR, "temperatura.png"), dpi=150)
        plt.close()

    # GRÁFICA 6: Humedad relativa
    if not df_clima.empty:
        fig, ax = plt.subplots(figsize=(12, 3.5))
        ax.plot(df_clima["created_at"], df_clima["humedad_pct"], color="#27AE60", linewidth=1.2, marker='s', markersize=3, label="Humedad Relativa")
        ax.axhline(humedad_promedio, color="darkgreen", linestyle="--", linewidth=1, label=f"Promedio: {humedad_promedio:.1f}%")
        ax.fill_between(df_clima["created_at"], 0, 30, alpha=0.1, color="red", label="Riesgo de sequía (<30%)")
        ax.set_xlabel("Fecha")
        ax.set_ylabel("Humedad Relativa [%]")
        ax.set_title(f"Humedad Relativa (Sequía Ambiental) — {OW_CITY}")
        ax.set_ylim(0, 100)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        ax.tick_params(axis="x", rotation=25)
        ax.grid(True, alpha=0.2)
        ax.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(GRAFICAS_DIR, "humedad.png"), dpi=150)
        plt.close()

    # GRÁFICA 7: Velocidad del viento
    if not df_clima.empty:
        fig, ax = plt.subplots(figsize=(12, 3.5))
        ax.bar(df_clima["created_at"], df_clima["velocidad_viento_ms"], color="#3498DB", alpha=0.75, width=0.02, label="Velocidad del Viento")
        ax.axhline(viento_promedio, color="darkblue", linestyle="--", linewidth=1, label=f"Promedio: {viento_promedio:.2f} m/s")
        ax.set_xlabel("Fecha")
        ax.set_ylabel("Velocidad [m/s]")
        ax.set_title(f"Velocidad del Viento — {OW_CITY}")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        ax.tick_params(axis="x", rotation=25)
        ax.grid(True, alpha=0.2, axis="y")
        ax.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(GRAFICAS_DIR, "viento.png"), dpi=150)
        plt.close()

    # GRÁFICA 8: Matriz de correlaciones (heatmap simplificado)
    if not df_merged.empty and len(df_merged) >= 3:
        variables_corr = ["presion_psi", "lluvia_mm", "temp_c", "humedad_pct", "velocidad_viento_ms"]
        df_corr_subset = df_merged[variables_corr].dropna()
        
        if len(df_corr_subset) >= 3:
            corr_matrix = df_corr_subset.corr()
            
            fig, ax = plt.subplots(figsize=(8, 6))
            im = ax.imshow(corr_matrix, cmap='coolwarm', vmin=-1, vmax=1, aspect='auto')
            
            labels = ["Presión", "Lluvia", "Temp", "Humedad", "Viento"]
            ax.set_xticks(range(len(labels)))
            ax.set_yticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=45, ha='right')
            ax.set_yticklabels(labels)
            
            # Anotaciones de valores
            for i in range(len(labels)):
                for j in range(len(labels)):
                    text = ax.text(j, i, f'{corr_matrix.iloc[i, j]:.2f}',
                                   ha="center", va="center", color="black", fontsize=9)
            
            ax.set_title("Matriz de Correlaciones — Variables Climáticas vs Presión", fontweight='bold')
            plt.colorbar(im, ax=ax, label="Coeficiente r")
            plt.tight_layout()
            plt.savefig(os.path.join(GRAFICAS_DIR, "matriz_correlaciones.png"), dpi=150)
            plt.close()

    print(f"   -> Todos los gráficos guardados en: '{GRAFICAS_DIR}'")

    # ==========================================
    # CONSTRUCCIÓN DEL REPORTE PDF
    # ==========================================
    fecha_rep = datetime.now().strftime("%d/%m/%Y %H:%M")
    f_ini = df_presion["created_at"].min().strftime("%d/%m/%Y %H:%M")
    f_fin = df_presion["created_at"].max().strftime("%d/%m/%Y %H:%M")

    def add_header_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica-Bold', 10)
        canvas.setFillColor(colors.HexColor("#1565C0"))
        canvas.drawString(2*cm, A4[1] - 1.5*cm, "JAAPRV Sinchal — Monitoreo Hídrico")
        canvas.line(2*cm, A4[1] - 1.7*cm, A4[0] - 2*cm, A4[1] - 1.7*cm)
        
        canvas.setFont('Helvetica', 9)
        canvas.setFillColor(colors.gray)
        fecha_hoy = datetime.now().strftime("%d/%m/%Y")
        page_num = canvas.getPageNumber()
        canvas.drawString(2*cm, 1.5*cm, f"Generado: {fecha_hoy}")
        canvas.drawRightString(A4[0] - 2*cm, 1.5*cm, f"Página {page_num}")
        canvas.restoreState()

    doc = SimpleDocTemplate(PDF_OUTPUT_PATH, pagesize=A4,
                            rightMargin=2*cm, leftMargin=2*cm,
                            topMargin=2.5*cm, bottomMargin=2.5*cm,
                            title="Reporte Hidráulico Técnico",
                            author="Sistema Automático JAAPRV",
                            subject="Monitoreo de Presión y Clima")

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
    story.append(Paragraph("Reporte Técnico: Presión, Nivel y Análisis Climático", title_s))
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
    story.append(Spacer(1, 0.3*cm))

    # --- SECCIÓN 2: Tabla estadística del sensor ---
    story.append(Paragraph("2. Resumen Estadístico del Sensor de Presión", h1_s))
    t_data = [
        ["Métrica Operativa", "Valor Calculado"],
        ["Registros Históricos Procesados",    f"{len(df_presion):,}"],
        ["Presión Mínima de Red",              f"{p_min:.1f} psi"],
        ["Presión Máxima Comercial (< 120)",   f"{p_max_operativa:.1f} psi"],
        ["Presión Promedio Normal de Red",     f"{p_mean:.2f} psi"],
        ["Mediana Operativa",                  f"{p_median:.2f} psi"],
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
    story.append(Spacer(1, 0.4*cm))

    # --- SECCIÓN 2.1: BOXPLOT ---
    story.append(Paragraph("2.1 Distribución Visual — Caja y Bigotes", h1_s))
    story.append(Paragraph(
        "El gráfico de caja y bigotes representa visualmente la distribución de presión: "
        "la caja contiene el 50% central de los datos, la línea roja dentro es la mediana, "
        "los bigotes muestran el rango operativo, y cualquier punto aislado es una anomalía.", norm_s))
    story.append(Spacer(1, 0.2*cm))
    boxplot_png = os.path.join(GRAFICAS_DIR, "boxplot_presion.png")
    if os.path.exists(boxplot_png):
        story.append(Image(boxplot_png, width=12*cm, height=7.5*cm))
    story.append(Spacer(1, 0.3*cm))
    story.append(PageBreak())

    # --- SECCIÓN 3: Análisis climático ---
    story.append(Paragraph("3. Análisis Climático Integrado — OpenWeatherMap (Recolección Diaria)", h1_s))

    if precipitacion_activa and not df_clima.empty:
        story.append(Paragraph(
            f"Se integró la fuente de datos climáticos de OpenWeatherMap para las coordenadas "
            f"de la zona de captación ({OW_LAT}°, {OW_LON}°). Se recopilan registros diarios desde "
            f"el 4 de septiembre para enriquecer el modelo matemático con múltiples variables ambientales.", norm_s))
        story.append(Spacer(1, 0.2*cm))

        # Tabla de métricas climáticas
        climat_data = [
            ["Métrica Climática", "Valor"],
            ["Temperatura Promedio",  f"{temp_promedio:.2f}°C"],
            ["Humedad Relativa Promedio",  f"{humedad_promedio:.2f}%"],
            ["Velocidad del Viento Promedio", f"{viento_promedio:.2f} m/s"],
            ["Presión Atmosférica Media",  f"{presion_atm_promedio:.2f} hPa"],
            ["Precipitación Total (Acumulada)",  f"{lluvia_total:.1f} mm"],
            ["Días con lluvia registrada",     str(dias_con_lluvia)],
        ]
        climat_t = Table(climat_data, colWidths=[8.5*cm, 5.5*cm])
        climat_t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#1565C0")),
            ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
            ("ALIGN",         (0,0), (-1,-1), "CENTER"),
            ("GRID",          (0,0), (-1,-1), 0.5, colors.HexColor("#CCCCCC")),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.HexColor("#EDF7FF"), colors.white]),
            ("PADDING",       (0,0), (-1,-1), 5),
        ]))
        story.append(climat_t)
        story.append(Spacer(1, 0.3*cm))

        # Tabla de correlaciones
        story.append(Paragraph("3.1 Estado de Correlaciones con Presión de Red", h1_s))
        corr_data = [
            ["Variable Climática", "Correlación"],
            ["Lluvia ↔ Presión",   "No calculable"],
            ["Temperatura ↔ Presión",   "No calculable"],
            ["Humedad ↔ Presión",   "No calculable"],
            ["Viento ↔ Presión",   "No calculable"],
        ]
        corr_t = Table(corr_data, colWidths=[8.5*cm, 5.5*cm])
        corr_t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#F39C12")),
            ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
            ("ALIGN",         (0,0), (-1,-1), "CENTER"),
            ("GRID",          (0,0), (-1,-1), 0.5, colors.HexColor("#CCCCCC")),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.HexColor("#FEF5E7"), colors.white]),
            ("PADDING",       (0,0), (-1,-1), 5),
        ]))
        story.append(corr_t)
        story.append(Spacer(1, 0.3*cm))

        # Interpretación narrativa
        story.append(Paragraph(
            "<b>Nota sobre correlaciones:</b> Las correlaciones requieren mínimo 90 días de histórico para ser estadísticamente significativas. "
            "Objetivo: Diciembre 2026. Una vez acumulado este período, se podrá determinar si la temperatura, humedad, viento y lluvia "
            "influyen directamente en la presión de la red de distribución.", norm_s))

        # Interpretación de sequía
        if humedad_promedio < 30:
            sequedad_interp = (
                f"<b>Alerta: Riesgo de Sequía.</b> La humedad promedio es {humedad_promedio:.1f}%, "
                "indicativo de condiciones de sequía ambiental. Esto puede afectar la disponibilidad de agua en el reservorio."
            )
        elif humedad_promedio < 50:
            sequedad_interp = (
                f"<b>Humedad baja.</b> El promedio de {humedad_promedio:.1f}% sugiere condiciones moderadamente secas. "
                "Monitorear la recarga del reservorio en los próximos días."
            )
        else:
            sequedad_interp = (
                f"<b>Humedad normal.</b> El promedio de {humedad_promedio:.1f}% indica condiciones húmedas adecuadas. "
                "No se reportan señales inmediatas de sequía."
            )
        story.append(Paragraph(sequedad_interp, norm_s))
        story.append(Spacer(1, 0.3*cm))

    else:
        story.append(Paragraph(
            "⚠ INTEGRACIÓN OPENWEATHER NO ACTIVA en esta ejecución. "
            "Para activarla, configure el Secret OPENWEATHER_API_KEY en GitHub Actions.", warn_s))

    story.append(Spacer(1, 0.3*cm))
    story.append(PageBreak())

    # --- SECCIÓN 4: Gráficos del sensor ---
    story.append(Paragraph("4. Comportamiento Temporal e Histogramas Operativos", h1_s))
    story.append(Image(os.path.join(GRAFICAS_DIR, "serie_temporal_presion.png"),
                       width=15*cm, height=5*cm))
    story.append(Spacer(1, 0.3*cm))
    story.append(Image(os.path.join(GRAFICAS_DIR, "histograma_presion.png"),
                       width=11*cm, height=5.5*cm))
    story.append(Spacer(1, 0.3*cm))
    story.append(PageBreak())

    # --- SECCIÓN 5: Patrón horario ---
    story.append(Paragraph("5. Perfil Operativo Horario Continuo", h1_s))
    story.append(Image(os.path.join(GRAFICAS_DIR, "presion_por_hora.png"),
                       width=15*cm, height=5*cm))
    story.append(Spacer(1, 0.4*cm))
    story.append(PageBreak())

    # --- SECCIÓN 6: Gráficos climáticos ---
    story.append(Paragraph("6. Análisis de Variables Climáticas", h1_s))
    
    lluvia_png = os.path.join(GRAFICAS_DIR, "precipitacion_historica.png")
    temp_png = os.path.join(GRAFICAS_DIR, "temperatura.png")
    humedad_png = os.path.join(GRAFICAS_DIR, "humedad.png")
    viento_png = os.path.join(GRAFICAS_DIR, "viento.png")

    if os.path.exists(lluvia_png):
        story.append(Paragraph("6.1 Precipitación Diaria", h1_s))
        story.append(Image(lluvia_png, width=12*cm, height=4*cm))
        story.append(Spacer(1, 0.2*cm))

    if os.path.exists(temp_png):
        story.append(Paragraph("6.2 Temperatura Ambiental", h1_s))
        story.append(Image(temp_png, width=12*cm, height=4*cm))
        story.append(Spacer(1, 0.2*cm))

    if os.path.exists(humedad_png):
        story.append(Paragraph("6.3 Humedad Relativa (Indicador de Sequía)", h1_s))
        story.append(Image(humedad_png, width=12*cm, height=4*cm))
        story.append(Spacer(1, 0.2*cm))

    if os.path.exists(viento_png):
        story.append(Paragraph("6.4 Velocidad del Viento", h1_s))
        story.append(Image(viento_png, width=12*cm, height=4*cm))
        story.append(Spacer(1, 0.2*cm))

    story.append(PageBreak())

    # --- SECCIÓN 7: Matriz de correlaciones ---
    matriz_png = os.path.join(GRAFICAS_DIR, "matriz_correlaciones.png")
    if os.path.exists(matriz_png):
        story.append(Paragraph("7. Matriz de Correlaciones Climáticas (Estado Actual)", h1_s))
        story.append(Image(matriz_png, width=12*cm, height=9*cm))
        story.append(Spacer(1, 0.3*cm))
        story.append(Paragraph(
            "<b>Nota metodológica:</b> La matriz muestra correlaciones entre presión de red y variables climáticas. "
            "Valores cercanos a +1 indican relación positiva fuerte; cercanos a -1, relación negativa fuerte; "
            "cercanos a 0, independencia estadística. Requiere mínimo 90 días para validez estadística.", norm_s))
        story.append(Spacer(1, 0.3*cm))

    story.append(PageBreak())

    # --- SECCIÓN 8: Conclusiones ---
    story.append(Paragraph("Conclusiones y Recomendaciones", title_s))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#1565C0")))
    story.append(Spacer(1, 0.5*cm))
    
    story.append(Paragraph("Estado general del sistema", h1_s))
    story.append(Paragraph(
        f"La red muestra una presión media operativa de <b>{p_mean:.2f} psi</b> (mediana: <b>{p_median:.2f} psi</b>). "
        f"Se han identificado <b>{len(anomalias)}</b> eventos transitorios. "
        f"El 50% de las lecturas se concentran entre <b>{df_operativo['presion_psi'].quantile(0.25):.1f}</b> "
        f"y <b>{df_operativo['presion_psi'].quantile(0.75):.1f} psi</b>. "
        f"Condiciones climáticas: Temperatura <b>{temp_promedio:.1f}°C</b>, "
        f"Humedad <b>{humedad_promedio:.1f}%</b>, Viento <b>{viento_promedio:.2f} m/s</b>.", norm_s))
    story.append(Spacer(1, 0.3*cm))
    
    story.append(Paragraph("Próximos pasos recomendados", h1_s))
    story.append(Paragraph(
        "• <b>Monitorear continuamente</b> los picos máximos para descartar golpe de ariete.<br/>"
        "• <b>Correlacionar variables climáticas</b> con presión cuando se acumule 90 días de datos (dic 2026).<br/>"
        "• <b>Vigilar humedad relativa</b>: valores < 30% indican riesgo de sequía y menor recarga del reservorio.<br/>"
        "• <b>Registrar eventos de viento fuerte</b> para validar su impacto en presión y demanda.<br/>"
        "• <b>Inspeccionar infraestructura</b> si la frecuencia de anomalías aumenta.", norm_s))
    story.append(Spacer(1, 0.3*cm))
    
    story.append(Paragraph("Limitaciones y próximas fases", h1_s))
    story.append(Paragraph(
        "<b>Sensor de nivel:</b> No operativo. Prioritario para validar recarga por precipitación.<br/><br/>"
        "<b>Datos climáticos:</b> Recolección desde 04 sept 2026. Fase 2 (dic 2026): Correlaciones estadísticas. "
        "Fase 3 (2027): Modelado predictivo con ARIMA/Prophet integrando variables climáticas.", norm_s))
    story.append(Spacer(1, 0.5*cm))

    # Compilar PDF
    doc.build(story, onFirstPage=add_header_footer, onLaterPages=add_header_footer)
    print(f"\n✓ REPORTE PDF GENERADO CON ÉXITO: '{PDF_OUTPUT_PATH}'")
    print(f"  Variables climáticas: {'SÍ' if precipitacion_activa else 'NO'}")
    print(f"  Gráficos: presión, temperatura, humedad, viento, matriz de correlaciones")