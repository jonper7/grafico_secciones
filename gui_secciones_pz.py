"""
================================================================================
 GRÁFICAS POR SECCIÓN GEOTÉCNICA — Niveles piezométricos + Precipitación
================================================================================
 Interfaz de escritorio (Tkinter) conectada a PostgreSQL  gdr_esc / esquema t000

   * Piezómetros  : vista  t000.pz_niveles_piezometros
   * Precipitación: tabla  t000.monitoreo_estaciones

 Filtros en cascada:  Estructura -> Sección geotécnica -> Instrumentos

 Requisitos:
     pip install "psycopg[binary]" pandas matplotlib

 Compilar:
     pyinstaller --onefile --windowed --name GraficasSecciones gui_secciones_pz.py
================================================================================
"""

import json
import os
import queue
import re
import threading
import tkinter as tk
import sys
from tkinter import ttk, filedialog, messagebox

import matplotlib.dates as mdates
import matplotlib.ticker as ticker
import pandas as pd

# IMPORTANTE: nada de pyplot.
# pyplot crea un gestor de ventanas propio del backend; al invocarlo desde un
# hilo secundario levanta un Tk paralelo al de la app y a partir de la segunda
# figura el dibujo deja de funcionar. Con la API orientada a objetos (Figure)
# el problema desaparece.
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.patches import Patch

try:
    import psycopg  # psycopg3
except ImportError:  # pragma: no cover
    psycopg = None


# ==============================================================================
# PALETA CATPPUCCIN MOCHA
# ==============================================================================
C = {
    "base":     "#1e1e2e",
    "mantle":   "#181825",
    "crust":    "#11111b",
    "surface0": "#313244",
    "surface1": "#45475a",
    "surface2": "#585b70",
    "text":     "#cdd6f4",
    "subtext":  "#a6adc8",
    "blue":     "#89b4fa",
    "sky":      "#89dceb",
    "green":    "#a6e3a1",
    "yellow":   "#f9e2af",
    "peach":    "#fab387",
    "red":      "#f38ba8",
    "mauve":    "#cba6f7",
}

FUENTE      = ("Segoe UI", 10)
FUENTE_SM   = ("Segoe UI", 9)
FUENTE_BD   = ("Segoe UI Semibold", 11)
FUENTE_MONO = ("Consolas", 9)

ESQUEMA = "t000"
ARCHIVO_CONFIG = os.path.join(os.path.expanduser("~"), ".gsecc_config.json")
TODAS = "(TODAS)"

# Colores de las series piezométricas
COLORES = ["#00008B", "#DC143C", "#228B22", "#FF8C00", "#9370DB",
           "#20B2AA", "#FF1493", "#8B4513", "#4682B4", "#DAA520",
           "#800000", "#008080", "#B8860B", "#4B0082", "#556B2F"]
COLOR_LLUVIA = "#4997BE"
ALPHA_LLUVIA = 0.55


def nombre_archivo(texto):
    """Limpia un texto para usarlo como nombre de archivo."""
    return re.sub(r"[^\w\-.]+", "_", str(texto)).strip("_") or "sin_nombre"


# ==============================================================================
# CAPA DE DATOS
# ==============================================================================
class BaseDatos:
    """Acceso a PostgreSQL (gdr_esc / t000) usando psycopg3."""

    def __init__(self):
        self.config = {}

    # ------------------------------------------------------------------ core
    def _conectar(self):
        if psycopg is None:
            raise RuntimeError(
                'psycopg3 no instalado -> pip install "psycopg[binary]"\n'
                f'Intérprete actual: {sys.executable}'
            )
        return psycopg.connect(**self.config)

    def consultar(self, sql, params=None):
        with self._conectar() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or {})
                columnas = [d[0] for d in cur.description]
                filas = cur.fetchall()
        return pd.DataFrame(filas, columns=columnas)

    def probar_conexion(self):
        df = self.consultar("SELECT current_database() AS db, current_user AS usuario;")
        return f"{df.iloc[0]['db']} / {df.iloc[0]['usuario']}"

    # ------------------------------------------------------------- catálogos
    def estructuras(self):
        sql = f"""
            SELECT   estructura,
                     COUNT(DISTINCT id_instrumento)     AS n_instrumentos,
                     COUNT(DISTINCT seccion_geotecnica) AS n_secciones
            FROM     {ESQUEMA}.pz_niveles_piezometros
            WHERE    estructura IS NOT NULL
            GROUP BY estructura
            ORDER BY estructura;
        """
        return self.consultar(sql)

    def secciones(self, estructura=None):
        filtro = "AND estructura = %(estructura)s" if estructura else ""
        sql = f"""
            SELECT   seccion_geotecnica,
                     MIN(estructura)                AS estructura,
                     COUNT(DISTINCT id_instrumento) AS n_instrumentos
            FROM     {ESQUEMA}.pz_niveles_piezometros
            WHERE    seccion_geotecnica IS NOT NULL
                     {filtro}
            GROUP BY seccion_geotecnica
            ORDER BY seccion_geotecnica;
        """
        return self.consultar(sql, {"estructura": estructura} if estructura else {})

    def instrumentos(self, estructura=None, seccion=None, solo_activos=False):
        filtro_est = "AND estructura = %(estructura)s"      if estructura else ""
        filtro_sec = "AND seccion_geotecnica = %(seccion)s" if seccion else ""
        filtro_act = "AND fin_registro IS NULL"             if solo_activos else ""
        sql = f"""
            SELECT   id_instrumento,
                     MIN(estructura)         AS estructura,
                     MIN(seccion_geotecnica) AS seccion_geotecnica,
                     MIN(tipo_instrumento)   AS tipo_instrumento,
                     MAX(data_time)          AS ultimo_registro
            FROM     {ESQUEMA}.pz_niveles_piezometros
            WHERE    1 = 1
                     {filtro_est}
                     {filtro_sec}
                     {filtro_act}
            GROUP BY id_instrumento
            ORDER BY id_instrumento;
        """
        params = {}
        if estructura:
            params["estructura"] = estructura
        if seccion:
            params["seccion"] = seccion
        return self.consultar(sql, params)

    def estaciones(self):
        sql = f"""
            SELECT   id_estacion, COUNT(*) AS n
            FROM     {ESQUEMA}.monitoreo_estaciones
            GROUP BY id_estacion
            ORDER BY id_estacion;
        """
        return self.consultar(sql)

    # ------------------------------------------------------------ mediciones
    def piezometros(self, ini, fin, instrumentos):
        sql = f"""
            SELECT  id_instrumento,
                    data_time              AS fecha_hora,
                    elevacion_msnm::float8 AS elevacion_piezometrica
            FROM    {ESQUEMA}.pz_niveles_piezometros
            WHERE   data_time >= %(ini)s
              AND   data_time <  %(fin)s
              AND   elevacion_msnm IS NOT NULL
              AND   id_instrumento = ANY(%(instrumentos)s)
            ORDER BY id_instrumento, data_time;
        """
        return self.consultar(sql, {"ini": ini, "fin": fin,
                                    "instrumentos": list(instrumentos)})

    def precipitacion(self, ini, fin, id_estacion):
        sql = f"""
            SELECT  date_time           AS fecha_hora,
                    rain_mm_tot::float8 AS rain_mm_tot
            FROM    {ESQUEMA}.monitoreo_estaciones
            WHERE   id_estacion = %(est)s
              AND   date_time  >= %(ini)s
              AND   date_time  <  %(fin)s
            ORDER BY date_time;
        """
        return self.consultar(sql, {"est": id_estacion, "ini": ini, "fin": fin})


# ==============================================================================
# LIMPIEZA Y GRAFICADO
# ==============================================================================
def preparar_piezometros(df):
    """Tipos, redondeo al segundo y eliminación de duplicados."""
    if df.empty:
        return df
    df = df.copy()
    df["fecha_hora"] = pd.to_datetime(df["fecha_hora"]).dt.floor("s")
    df["elevacion_piezometrica"] = pd.to_numeric(df["elevacion_piezometrica"],
                                                 errors="coerce")
    df["id_instrumento"] = df["id_instrumento"].astype(str).str.strip()
    return (df.dropna(subset=["elevacion_piezometrica"])
              .drop_duplicates(subset=["id_instrumento", "fecha_hora"])
              .sort_values(["id_instrumento", "fecha_hora"])
              .reset_index(drop=True))


def preparar_precipitacion(df, diaria=True):
    """Convierte el registro horario en acumulado diario (mm/día)."""
    if df.empty:
        return df
    df = df.copy()
    df["fecha_hora"] = pd.to_datetime(df["fecha_hora"])
    df["rain_mm_tot"] = pd.to_numeric(df["rain_mm_tot"], errors="coerce")
    df = df.dropna(subset=["rain_mm_tot"]).sort_values("fecha_hora")
    if diaria:
        df = (df.set_index("fecha_hora")["rain_mm_tot"]
                .resample("D").sum(min_count=1)
                .reset_index()
                .dropna(subset=["rain_mm_tot"]))
    return df


def dibujar_seccion(fig, df_pz, df_rain, instrumentos, nombre_seccion, estructura=None,
                    precip_diaria=True, mostrar_precipitacion=True, log=print):
    """
    Dibuja una sección DENTRO de una figura ya existente y devuelve True/False.

    En la interfaz siempre se reutiliza la misma figura, el mismo canvas y la
    misma barra de herramientas. Destruir y recrear esos widgets provocaba el
    error «invalid command name ...!navigationtoolbar2tk.!button2»: al eliminar
    el canvas se disparan eventos de redibujo que llegan a la toolbar anterior
    (set_history_buttons -> botón Back) cuando ese botón ya no existe en Tk.
    """
    fig.clear()

    df_grupo = df_pz[df_pz["id_instrumento"].isin(instrumentos)].copy()
    if df_grupo.empty:
        log(f"⚠️  Sin datos para la sección '{nombre_seccion}'")
        return False

    ax1 = fig.add_subplot(111)
    ax1.set_facecolor("white")

    # ------------------------------ series piezométricas ------------------
    lineas, etiquetas = [], []
    for instrumento in instrumentos:
        datos = (df_grupo[df_grupo["id_instrumento"] == instrumento]
                 [["fecha_hora", "elevacion_piezometrica"]].dropna())
        if datos.empty:
            continue
        serie, = ax1.plot(datos["fecha_hora"], datos["elevacion_piezometrica"],
                          color=COLORES[len(lineas) % len(COLORES)],
                          linewidth=1.5, alpha=0.85, zorder=3,
                          marker="o" if len(datos) == 1 else None, markersize=6)
        lineas.append(serie)
        etiquetas.append(instrumento)
        log(f"   ✓ {instrumento}: {len(datos):,} puntos")

    if not lineas:
        log(f"⚠️  Ningún instrumento con datos válidos en '{nombre_seccion}'")
        fig.clear()
        return False

    # ------------------------------ eje X ---------------------------------
    fecha_min = df_grupo["fecha_hora"].min()
    fecha_max = df_grupo["fecha_hora"].max()
    if fecha_min == fecha_max:
        fecha_min -= pd.Timedelta(days=1)
        fecha_max += pd.Timedelta(days=1)
    ax1.set_xlim(fecha_min, fecha_max)

    rango_dias = max(1, (fecha_max - fecha_min).days)
    if   rango_dias > 730: loc, fmt = mdates.MonthLocator(interval=3), "%b-%Y"
    elif rango_dias > 365: loc, fmt = mdates.MonthLocator(interval=2), "%b-%Y"
    elif rango_dias > 120: loc, fmt = mdates.MonthLocator(),           "%b-%Y"
    elif rango_dias >  60: loc, fmt = mdates.DayLocator(interval=10),  "%d-%b"
    elif rango_dias >  31: loc, fmt = mdates.DayLocator(interval=7),   "%d-%b"
    else:                  loc, fmt = mdates.DayLocator(interval=1),   "%d-%b"
    ax1.xaxis.set_major_locator(loc)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
    ax1.tick_params(axis="x", rotation=90, labelsize=10, labelcolor="black")

    # ------------------------------ eje Y izquierdo -----------------------
    ax1.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.1f"))
    ax1.set_ylabel("Elevación (msnm)", fontsize=11)
    ax1.tick_params(axis="y", labelsize=11, labelcolor="black")

    y_min = df_grupo["elevacion_piezometrica"].min()
    y_max = df_grupo["elevacion_piezometrica"].max()
    if pd.notna(y_min) and pd.notna(y_max):
        margen = max(0.5, (y_max - y_min) * 0.15)
        ax1.set_ylim(y_min - margen, y_max + margen)

    ax1.grid(True, linestyle="--", color="gray", alpha=0.5)
    for spine in ax1.spines.values():
        spine.set_edgecolor("silver")
        spine.set_linewidth(1.5)

    # ------------------------------ precipitación -------------------------
    if mostrar_precipitacion and df_rain is not None and not df_rain.empty:
        lluvia = df_rain[(df_rain["fecha_hora"] >= fecha_min - pd.Timedelta(days=1)) &
                         (df_rain["fecha_hora"] <= fecha_max + pd.Timedelta(days=1))]
        lluvia = lluvia[lluvia["rain_mm_tot"] > 0]

        if lluvia.empty:
            log("   ⚠️  Sin precipitación registrada en el rango")
        else:
            ax2 = ax1.twinx()
            # ----------------------------------------------------------
            # ax1 va delante (las líneas sobre las barras) PERO con el
            # fondo transparente. Si el patch de ax1 queda visible, su
            # rectángulo blanco tapa por completo las barras de ax2.
            # ----------------------------------------------------------
            ax1.set_zorder(ax2.get_zorder() + 1)
            ax1.patch.set_visible(False)
            ax2.set_facecolor("white")

            ancho = 0.85 if precip_diaria else 0.03
            ax2.bar(lluvia["fecha_hora"], lluvia["rain_mm_tot"],
                    width=ancho, align="center", color=COLOR_LLUVIA,
                    alpha=ALPHA_LLUVIA, edgecolor="none", zorder=1)

            etiqueta_y = "Precipitación (mm/día)" if precip_diaria else "Precipitación (mm/h)"
            ax2.set_ylabel(etiqueta_y, color="black", fontsize=11)
            ax2.tick_params(axis="y", labelcolor="black", labelsize=11)
            ax2.grid(False)
            ax2.set_xlim(ax1.get_xlim())

            max_p = float(lluvia["rain_mm_tot"].max())
            ax2.set_ylim(0, max_p * 1.3 if max_p > 0 else 1.0)

            lineas.append(Patch(facecolor=COLOR_LLUVIA, alpha=ALPHA_LLUVIA,
                                edgecolor="none"))
            etiquetas.append("Precipitación")
            log(f"   ✓ Precipitación: {len(lluvia)} barras | máx {max_p:.1f} mm")

    # ------------------------------ título y leyenda ----------------------
    ax1.set_title(f"SECCIÓN DE ANÁLISIS {nombre_seccion}",
                  fontsize=15, fontweight="bold", pad=18)
 #   if estructura:
#      ax1.text(0.5, 1.012, str(estructura), transform=ax1.transAxes,
#               ha="center", va="bottom", fontsize=10, color="dimgray")

    ax1.legend(lineas, etiquetas, loc="upper center",
               bbox_to_anchor=(0.5, -0.18), ncol=min(9, len(lineas)),
               fontsize=10, frameon=True, facecolor="white",
               edgecolor="silver", shadow=True)
    fig.tight_layout()
    return True


def crear_figura(df_pz, df_rain, instrumentos, nombre_seccion, estructura=None,
                 precip_diaria=True, mostrar_precipitacion=True, log=print):
    """Figura independiente (para exportar a disco). Devuelve None si no hay datos."""
    fig = Figure(figsize=(16, 7), facecolor="white")
    FigureCanvasAgg(fig)                 # canvas por defecto -> habilita savefig
    ok = dibujar_seccion(fig, df_pz, df_rain, instrumentos, nombre_seccion,
                         estructura=estructura, precip_diaria=precip_diaria,
                         mostrar_precipitacion=mostrar_precipitacion, log=log)
    return fig if ok else None


def mensaje_en_figura(fig, texto):
    """Deja la figura en blanco con un mensaje centrado."""
    fig.clear()
    fig.text(0.5, 0.5, texto, ha="center", va="center",
             fontsize=13, color="#7c7f93")


# ==============================================================================
# INTERFAZ
# ==============================================================================
class Aplicacion(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Gráficas por Sección Geotécnica — Piezómetros / Precipitación")
        self.geometry("1500x900")
        self.minsize(1200, 750)
        self.configure(bg=C["base"])

        self.bd = BaseDatos()
        self.cola = queue.Queue()
        self.figura = None               # figura permanente del canvas
        self.hay_grafica = False
        self.canvas = None
        self.toolbar = None
        self.vars_instrumentos = {}      # {id_instrumento: BooleanVar}
        self.df_secciones = pd.DataFrame()
        self.ocupado = False
        self._estacion_guardada = ""

        self._estilos()
        self._construir()
        self._cargar_config()
        self.after(120, self._procesar_cola)

    # ---------------------------------------------------------------- estilo
    def _estilos(self):
        st = ttk.Style(self)
        st.theme_use("clam")

        st.configure(".", background=C["base"], foreground=C["text"], font=FUENTE)
        st.configure("TFrame", background=C["base"])
        st.configure("Panel.TFrame", background=C["mantle"])
        st.configure("TLabel", background=C["base"], foreground=C["text"])
        st.configure("Panel.TLabel", background=C["mantle"], foreground=C["text"])
        st.configure("Titulo.TLabel", background=C["mantle"], foreground=C["blue"],
                     font=FUENTE_BD)
        st.configure("Sub.TLabel", background=C["mantle"], foreground=C["subtext"],
                     font=FUENTE_SM)

        st.configure("TLabelframe", background=C["mantle"], foreground=C["mauve"],
                     bordercolor=C["surface1"])
        st.configure("TLabelframe.Label", background=C["mantle"],
                     foreground=C["mauve"], font=("Segoe UI Semibold", 10))

        st.configure("TEntry", fieldbackground=C["surface0"], foreground=C["text"],
                     bordercolor=C["surface1"], insertcolor=C["text"],
                     lightcolor=C["surface1"], darkcolor=C["surface1"])
        st.configure("TCombobox", fieldbackground=C["surface0"], background=C["surface0"],
                     foreground=C["text"], arrowcolor=C["blue"],
                     bordercolor=C["surface1"], lightcolor=C["surface1"],
                     darkcolor=C["surface1"])
        self.option_add("*TCombobox*Listbox.background", C["surface0"])
        self.option_add("*TCombobox*Listbox.foreground", C["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", C["blue"])
        self.option_add("*TCombobox*Listbox.selectForeground", C["crust"])

        st.configure("TCheckbutton", background=C["mantle"], foreground=C["text"],
                     focuscolor=C["mantle"])
        st.map("TCheckbutton", background=[("active", C["mantle"])])

        st.configure("TButton", background=C["surface1"], foreground=C["text"],
                     borderwidth=0, focusthickness=0, padding=(10, 6))
        st.map("TButton", background=[("active", C["surface2"])])

        st.configure("Accion.TButton", background=C["blue"], foreground=C["crust"],
                     font=("Segoe UI Semibold", 10), padding=(10, 8))
        st.map("Accion.TButton", background=[("active", C["sky"])])

        st.configure("Ok.TButton", background=C["green"], foreground=C["crust"],
                     font=("Segoe UI Semibold", 10), padding=(10, 8))
        st.map("Ok.TButton", background=[("active", "#94e2d5")])

        st.configure("Vertical.TScrollbar", background=C["surface1"],
                     troughcolor=C["mantle"], bordercolor=C["mantle"],
                     arrowcolor=C["text"])

    # ------------------------------------------------------------ estructura
    def _construir(self):
        panel = ttk.Frame(self, style="Panel.TFrame", padding=12)
        panel.pack(side="left", fill="y")
        panel.pack_propagate(False)
        panel.configure(width=340)

        ttk.Label(panel, text="◆  SECCIONES GEOTÉCNICAS",
                  style="Titulo.TLabel").pack(anchor="w", pady=(0, 2))
        ttk.Label(panel, text="gdr_esc · esquema t000",
                  style="Sub.TLabel").pack(anchor="w", pady=(0, 10))

        self._bloque_conexion(panel)
        self._bloque_filtros(panel)
        self._bloque_instrumentos(panel)
        self._bloque_acciones(panel)

        # ---------------- área derecha: gráfica + consola ------------------
        derecha = ttk.Frame(self, padding=(10, 10))
        derecha.pack(side="right", fill="both", expand=True)

        self.marco_grafica = tk.Frame(derecha, bg=C["surface0"],
                                      highlightbackground=C["surface1"],
                                      highlightthickness=1)
        self.marco_grafica.pack(fill="both", expand=True)

        # ------------------------------------------------------------------
        # Canvas y toolbar se crean UNA sola vez y se reutilizan siempre.
        # Cada gráfica nueva limpia y redibuja esta misma figura.
        # ------------------------------------------------------------------
        self.figura = Figure(figsize=(16, 7), facecolor="white")
        mensaje_en_figura(self.figura,
                          "Conéctate a la base, elige una sección y presiona «Graficar»")

        self.canvas = FigureCanvasTkAgg(self.figura, master=self.marco_grafica)
        self.toolbar = NavigationToolbar2Tk(self.canvas, self.marco_grafica,
                                            pack_toolbar=False)
        self.toolbar.configure(bg=C["surface0"])
        for hijo in self.toolbar.winfo_children():
            try:
                hijo.configure(bg=C["surface0"], fg=C["text"])
            except tk.TclError:
                pass
        self.toolbar.update()
        self.toolbar.pack(side="bottom", fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.draw()

        marco_log = tk.Frame(derecha, bg=C["mantle"])
        marco_log.pack(fill="x", pady=(10, 0))
        self.consola = tk.Text(marco_log, height=8, bg=C["crust"], fg=C["subtext"],
                               font=FUENTE_MONO, relief="flat", wrap="word",
                               insertbackground=C["text"])
        self.consola.pack(side="left", fill="both", expand=True, padx=(0, 2))
        sb = ttk.Scrollbar(marco_log, command=self.consola.yview)
        sb.pack(side="right", fill="y")
        self.consola.configure(yscrollcommand=sb.set, state="disabled")

    # ------------------------------------------------------------- conexión
    def _bloque_conexion(self, padre):
        caja = ttk.Labelframe(padre, text=" Conexión ", padding=8)
        caja.pack(fill="x", pady=(0, 8))

        self.v_host = tk.StringVar(value="localhost")
        self.v_port = tk.StringVar(value="5432")
        self.v_db   = tk.StringVar(value="gdr_esc")
        self.v_user = tk.StringVar(value="jonper")
        self.v_pass = tk.StringVar()
        self.v_recordar = tk.BooleanVar(value=False)

        fila = ttk.Frame(caja, style="Panel.TFrame"); fila.pack(fill="x")
        ttk.Entry(fila, textvariable=self.v_host, width=18).pack(side="left",
                                                                fill="x", expand=True)
        ttk.Entry(fila, textvariable=self.v_port, width=6).pack(side="left", padx=(4, 0))

        ttk.Entry(caja, textvariable=self.v_db).pack(fill="x", pady=(4, 0))
        ttk.Entry(caja, textvariable=self.v_user).pack(fill="x", pady=(4, 0))
        ttk.Entry(caja, textvariable=self.v_pass, show="•").pack(fill="x", pady=(4, 0))

        ttk.Checkbutton(caja, text="Recordar contraseña (texto plano)",
                        variable=self.v_recordar).pack(anchor="w", pady=(4, 0))

        self.btn_conectar = ttk.Button(caja, text="Conectar", style="Accion.TButton",
                                       command=self.conectar)
        self.btn_conectar.pack(fill="x", pady=(6, 0))

        self.lbl_estado = ttk.Label(caja, text="● Desconectado", style="Sub.TLabel")
        self.lbl_estado.pack(anchor="w", pady=(4, 0))

    # -------------------------------------------------------------- filtros
    def _bloque_filtros(self, padre):
        caja = ttk.Labelframe(padre, text=" Filtros ", padding=8)
        caja.pack(fill="x", pady=(0, 8))

        # ---------------- Estructura (nivel superior de la cascada) --------
        ttk.Label(caja, text="Estructura", style="Sub.TLabel").pack(anchor="w")
        self.v_estructura = tk.StringVar()
        self.cmb_estructura = ttk.Combobox(caja, textvariable=self.v_estructura,
                                           state="readonly")
        self.cmb_estructura.pack(fill="x", pady=(2, 6))
        self.cmb_estructura.bind("<<ComboboxSelected>>",
                                 lambda e: self.cargar_secciones())

        # ---------------- Sección geotécnica -------------------------------
        ttk.Label(caja, text="Sección geotécnica", style="Sub.TLabel").pack(anchor="w")
        self.v_seccion = tk.StringVar()
        self.cmb_seccion = ttk.Combobox(caja, textvariable=self.v_seccion,
                                        state="readonly")
        self.cmb_seccion.pack(fill="x", pady=(2, 6))
        self.cmb_seccion.bind("<<ComboboxSelected>>",
                              lambda e: self.cargar_instrumentos())

        fechas = ttk.Frame(caja, style="Panel.TFrame"); fechas.pack(fill="x")
        hoy = pd.Timestamp.today().normalize()
        self.v_ini = tk.StringVar(value=(hoy - pd.Timedelta(days=180)).strftime("%Y-%m-%d"))
        self.v_fin = tk.StringVar(value=hoy.strftime("%Y-%m-%d"))

        col_i = ttk.Frame(fechas, style="Panel.TFrame")
        col_i.pack(side="left", fill="x", expand=True)
        ttk.Label(col_i, text="Desde", style="Sub.TLabel").pack(anchor="w")
        ttk.Entry(col_i, textvariable=self.v_ini).pack(fill="x")

        col_f = ttk.Frame(fechas, style="Panel.TFrame")
        col_f.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Label(col_f, text="Hasta", style="Sub.TLabel").pack(anchor="w")
        ttk.Entry(col_f, textvariable=self.v_fin).pack(fill="x")

        ttk.Label(caja, text="Estación meteorológica",
                  style="Sub.TLabel").pack(anchor="w", pady=(6, 0))
        self.v_estacion = tk.StringVar()
        self.cmb_estacion = ttk.Combobox(caja, textvariable=self.v_estacion,
                                         state="readonly")
        self.cmb_estacion.pack(fill="x", pady=(2, 6))

        self.v_precip       = tk.BooleanVar(value=True)
        self.v_diaria       = tk.BooleanVar(value=True)
        self.v_solo_activos = tk.BooleanVar(value=False)
        ttk.Checkbutton(caja, text="Mostrar precipitación",
                        variable=self.v_precip).pack(anchor="w")
        ttk.Checkbutton(caja, text="Acumular lluvia por día (mm/día)",
                        variable=self.v_diaria).pack(anchor="w")
        ttk.Checkbutton(caja, text="Solo instrumentos activos",
                        variable=self.v_solo_activos,
                        command=self.cargar_instrumentos).pack(anchor="w")

    # --------------------------------------------------------- instrumentos
    def _bloque_instrumentos(self, padre):
        caja = ttk.Labelframe(padre, text=" Instrumentos ", padding=8)
        caja.pack(fill="both", expand=True, pady=(0, 8))

        barra = ttk.Frame(caja, style="Panel.TFrame"); barra.pack(fill="x", pady=(0, 4))
        ttk.Button(barra, text="Todos",
                   command=lambda: self._marcar_todos(True)).pack(side="left",
                                                                  expand=True, fill="x")
        ttk.Button(barra, text="Ninguno",
                   command=lambda: self._marcar_todos(False)).pack(side="left",
                                                                   expand=True, fill="x",
                                                                   padx=(4, 0))

        contenedor = tk.Frame(caja, bg=C["surface0"], height=180)
        contenedor.pack(fill="both", expand=True)
        contenedor.pack_propagate(False)

        self.lienzo_inst = tk.Canvas(contenedor, bg=C["surface0"],
                                     highlightthickness=0, height=180)
        sb = ttk.Scrollbar(contenedor, orient="vertical", command=self.lienzo_inst.yview)
        self.marco_inst = tk.Frame(self.lienzo_inst, bg=C["surface0"])

        self.marco_inst.bind("<Configure>", lambda e: self.lienzo_inst.configure(
            scrollregion=self.lienzo_inst.bbox("all")))
        self.lienzo_inst.create_window((0, 0), window=self.marco_inst, anchor="nw")
        self.lienzo_inst.configure(yscrollcommand=sb.set)
        self.lienzo_inst.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # El scroll solo actúa cuando el cursor está sobre la lista
        self.lienzo_inst.bind(
            "<Enter>", lambda e: self.lienzo_inst.bind_all("<MouseWheel>", self._rueda))
        self.lienzo_inst.bind(
            "<Leave>", lambda e: self.lienzo_inst.unbind_all("<MouseWheel>"))

    def _rueda(self, evento):
        try:
            self.lienzo_inst.yview_scroll(int(-evento.delta / 120), "units")
        except tk.TclError:
            pass

    def _marcar_todos(self, valor):
        for var in self.vars_instrumentos.values():
            var.set(valor)

    # ------------------------------------------------------------- acciones
    def _bloque_acciones(self, padre):
        caja = ttk.Frame(padre, style="Panel.TFrame")
        caja.pack(fill="x")
        self.btn_graficar = ttk.Button(caja, text="📈  Graficar", style="Accion.TButton",
                                       command=self.graficar)
        self.btn_graficar.pack(fill="x")
        ttk.Button(caja, text="💾  Guardar PNG",
                   command=self.guardar_png).pack(fill="x", pady=(6, 0))
        self.btn_exportar = ttk.Button(caja, text="📦  Exportar todas las secciones",
                                       style="Ok.TButton", command=self.exportar_todas)
        self.btn_exportar.pack(fill="x", pady=(6, 0))

    # ================================================================= utils
    def log(self, texto):
        self.consola.configure(state="normal")
        self.consola.insert("end", str(texto) + "\n")
        self.consola.see("end")
        self.consola.configure(state="disabled")

    def _bloquear(self, estado):
        """Evita lanzar dos graficados simultáneos."""
        self.ocupado = estado
        nuevo = "disabled" if estado else "normal"
        self.btn_graficar.configure(state=nuevo)
        self.btn_exportar.configure(state=nuevo)

    def _en_hilo(self, funcion, libera=False):
        threading.Thread(target=self._envolver, args=(funcion, libera),
                         daemon=True).start()

    def _envolver(self, funcion, libera):
        try:
            funcion()
        except Exception as exc:                       # noqa: BLE001
            self.cola.put(("error", str(exc)))
            libera = True
        finally:
            if libera:
                self.cola.put(("libre", None))

    def _procesar_cola(self):
        while not self.cola.empty():
            tipo, dato = self.cola.get()
            try:
                if tipo == "log":
                    self.log(dato)
                elif tipo == "error":
                    self.log(f"❌ {dato}")
                    messagebox.showerror("Error", dato)
                elif tipo == "estado":
                    self.lbl_estado.configure(text=dato)
                elif tipo == "estructuras":
                    self._pintar_estructuras(*dato)
                elif tipo == "secciones":
                    self._pintar_secciones(dato)
                elif tipo == "instrumentos":
                    self._pintar_instrumentos(dato)
                elif tipo == "datos":
                    self._pintar_grafica(*dato)
                elif tipo == "libre":
                    self._bloquear(False)
            except Exception as exc:                   # noqa: BLE001
                self.log(f"❌ {exc}")
                self._bloquear(False)
        self.after(120, self._procesar_cola)

    # ============================================================== conexión
    def conectar(self):
        self.bd.config = {
            "host": self.v_host.get().strip(),
            "port": int(self.v_port.get() or 5432),
            "dbname": self.v_db.get().strip(),
            "user": self.v_user.get().strip(),
            "password": self.v_pass.get(),
            "client_encoding": "UTF8",   # evita el error del byte 0xf3
        }
        self.log("Conectando…")
        self._en_hilo(self._tarea_conectar)

    def _tarea_conectar(self):
        info = self.bd.probar_conexion()
        self.cola.put(("estado", f"● Conectado — {info}"))
        self.cola.put(("log", f"✓ Conexión establecida: {info}"))

        df_estr = self.bd.estructuras()
        df_est  = self.bd.estaciones()
        self.cola.put(("estructuras", (df_estr, df_est)))
        self._guardar_config()

    def _pintar_estructuras(self, df_estr, df_est):
        valores = [TODAS] + [
            f"{r.estructura}  ({r.n_secciones} sec · {r.n_instrumentos} inst)"
            for r in df_estr.itertuples()
        ]
        self.cmb_estructura["values"] = valores
        self.cmb_estructura.current(0)

        estaciones = df_est["id_estacion"].tolist()
        self.cmb_estacion["values"] = estaciones
        if self._estacion_guardada in estaciones:
            self.cmb_estacion.set(self._estacion_guardada)
        elif "em_via12" in estaciones:
            self.cmb_estacion.set("em_via12")
        elif estaciones:
            self.cmb_estacion.current(0)

        self.log(f"✓ {len(df_estr)} estructuras · {len(estaciones)} estaciones")
        self.cargar_secciones()

    # =============================================================== cascada
    def estructura_elegida(self):
        texto = self.v_estructura.get()
        if not texto or texto == TODAS:
            return None
        return texto.split("  (")[0]

    def seccion_elegida(self):
        texto = self.v_seccion.get()
        if not texto or texto == TODAS:
            return None
        return texto.split("  (")[0]

    def cargar_secciones(self):
        if not self.bd.config:
            return
        estructura = self.estructura_elegida()
        self._en_hilo(lambda: self.cola.put(
            ("secciones", self.bd.secciones(estructura))))

    def _pintar_secciones(self, df):
        self.df_secciones = df
        valores = [TODAS] + [
            f"{r.seccion_geotecnica}  ({r.n_instrumentos})"
            for r in df.itertuples()
        ]
        self.cmb_seccion["values"] = valores
        self.cmb_seccion.current(1 if len(valores) > 1 else 0)
        self.log(f"✓ {len(df)} secciones disponibles")
        self.cargar_instrumentos()

    def cargar_instrumentos(self):
        if not self.bd.config:
            return
        estructura   = self.estructura_elegida()
        seccion      = self.seccion_elegida()
        solo_activos = self.v_solo_activos.get()
        self._en_hilo(lambda: self.cola.put(
            ("instrumentos", self.bd.instrumentos(estructura, seccion, solo_activos))))

    def _pintar_instrumentos(self, df):
        for w in self.marco_inst.winfo_children():
            w.destroy()
        self.vars_instrumentos = {}

        if df.empty:
            tk.Label(self.marco_inst, text="Sin instrumentos", bg=C["surface0"],
                     fg=C["peach"], font=FUENTE_SM).pack(anchor="w", padx=6, pady=6)
            return

        sin_seccion = self.seccion_elegida() is None
        for fila in df.itertuples():
            var = tk.BooleanVar(value=True)
            self.vars_instrumentos[fila.id_instrumento] = var
            texto = fila.id_instrumento
            if sin_seccion and fila.seccion_geotecnica:
                texto += f"   [{fila.seccion_geotecnica}]"
            tk.Checkbutton(self.marco_inst, text=texto, variable=var,
                           bg=C["surface0"], fg=C["text"], font=FUENTE_SM,
                           selectcolor=C["surface1"], activebackground=C["surface0"],
                           activeforeground=C["blue"], anchor="w",
                           highlightthickness=0, bd=0).pack(fill="x", padx=4)

        self.log(f"✓ {len(df)} instrumentos cargados")

    def instrumentos_marcados(self):
        return [k for k, v in self.vars_instrumentos.items() if v.get()]

    # ============================================================== graficar
    def _rango_fechas(self):
        ini = pd.to_datetime(self.v_ini.get())
        fin = pd.to_datetime(self.v_fin.get()) + pd.Timedelta(days=1)
        if ini >= fin:
            raise ValueError("La fecha inicial debe ser anterior a la final.")
        return ini, fin

    def graficar(self):
        if self.ocupado:
            return
        if not self.bd.config:
            messagebox.showwarning("Sin conexión", "Primero conéctate a la base de datos.")
            return
        seleccion = self.instrumentos_marcados()
        if not seleccion:
            messagebox.showwarning("Sin instrumentos", "Marca al menos un instrumento.")
            return

        nombre     = self.seccion_elegida() or TODAS
        estructura = self.estructura_elegida()
        sufijo = f" — {estructura}" if estructura else ""
        self.log(f"\n📊 Sección '{nombre}'{sufijo} — {len(seleccion)} instrumentos")
        self._bloquear(True)
        self._en_hilo(lambda: self._tarea_graficar(seleccion, nombre, estructura),
                      libera=True)

    def _tarea_graficar(self, instrumentos, nombre, estructura):
        """Solo consulta la base. El dibujo se hace en el hilo principal."""
        ini, fin = self._rango_fechas()

        df_pz = preparar_piezometros(self.bd.piezometros(ini, fin, instrumentos))
        self.cola.put(("log", f"   {len(df_pz):,} lecturas piezométricas"))

        df_rain = pd.DataFrame()
        if self.v_precip.get() and self.v_estacion.get():
            df_rain = preparar_precipitacion(
                self.bd.precipitacion(ini, fin, self.v_estacion.get()),
                diaria=self.v_diaria.get())
            self.cola.put(("log", f"   {len(df_rain):,} registros de lluvia "
                                  f"({self.v_estacion.get()})"))

        self.cola.put(("datos", (df_pz, df_rain, instrumentos, nombre, estructura)))

    def _pintar_grafica(self, df_pz, df_rain, instrumentos, nombre, estructura):
        """Redibuja sobre la figura permanente: nada de Tk se destruye."""
        ok = dibujar_seccion(self.figura, df_pz, df_rain, instrumentos, nombre,
                             estructura=estructura,
                             precip_diaria=self.v_diaria.get(),
                             mostrar_precipitacion=self.v_precip.get(),
                             log=self.log)

        if not ok:
            mensaje_en_figura(self.figura,
                              f"Sin datos para '{nombre}' en el rango indicado")
            self.hay_grafica = False
        else:
            self.hay_grafica = True

        # Reinicia el historial de zoom/pan de la barra de herramientas
        self.toolbar.update()
        self.canvas.draw_idle()
        self.log("✓ Gráfica lista" if ok else "⚠️  Nada que graficar")

    # ============================================================== exportar
    def guardar_png(self):
        if not self.hay_grafica:
            messagebox.showinfo("Sin gráfica", "Genera una gráfica primero.")
            return
        partes = [p for p in (self.estructura_elegida(),
                              self.seccion_elegida() or "TODAS") if p]
        ruta = filedialog.asksaveasfilename(
            defaultextension=".png",
            initialfile="grafico_" + "_".join(nombre_archivo(p) for p in partes) + ".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")])
        if ruta:
            self.figura.savefig(ruta, dpi=200, bbox_inches="tight",
                                facecolor="white")
            self.log(f"💾 Guardado: {ruta}")

    def exportar_todas(self):
        if self.ocupado:
            return
        if self.df_secciones.empty:
            messagebox.showwarning("Sin secciones",
                                   "Conéctate y selecciona una estructura primero.")
            return
        carpeta = filedialog.askdirectory(title="Carpeta de destino")
        if not carpeta:
            return
        estructura = self.estructura_elegida()
        sufijo = f" de {estructura}" if estructura else ""
        self.log(f"\n📦 Exportando {len(self.df_secciones)} secciones{sufijo} a {carpeta}")
        self._bloquear(True)
        self._en_hilo(lambda: self._tarea_exportar(carpeta, estructura), libera=True)

    def _tarea_exportar(self, carpeta, estructura):
        ini, fin = self._rango_fechas()

        df_rain = pd.DataFrame()
        if self.v_precip.get() and self.v_estacion.get():
            df_rain = preparar_precipitacion(
                self.bd.precipitacion(ini, fin, self.v_estacion.get()),
                diaria=self.v_diaria.get())

        generadas = 0
        for fila in self.df_secciones.itertuples():
            seccion   = fila.seccion_geotecnica
            estr_fila = estructura or getattr(fila, "estructura", None)
            self.cola.put(("log", f"\n▸ Sección {seccion}"))

            df_inst = self.bd.instrumentos(estructura, seccion,
                                           self.v_solo_activos.get())
            instrumentos = df_inst["id_instrumento"].tolist()
            if not instrumentos:
                continue

            df_pz = preparar_piezometros(self.bd.piezometros(ini, fin, instrumentos))
            fig = crear_figura(df_pz, df_rain, instrumentos, seccion,
                               estructura=estr_fila,
                               precip_diaria=self.v_diaria.get(),
                               mostrar_precipitacion=self.v_precip.get(),
                               log=lambda t: self.cola.put(("log", t)))
            if fig is None:
                continue

            partes = [p for p in (estr_fila, seccion) if p]
            archivo = "grafico_" + "_".join(nombre_archivo(p) for p in partes) + ".png"
            fig.savefig(os.path.join(carpeta, archivo), dpi=200,
                        bbox_inches="tight", facecolor="white")
            fig.clear()
            generadas += 1
            self.cola.put(("log", f"   💾 {archivo}"))

        self.cola.put(("log", f"\n✓ Exportación completa: {generadas} gráficas"))

    # ================================================================ config
    def _guardar_config(self):
        datos = {
            "host": self.v_host.get(), "port": self.v_port.get(),
            "dbname": self.v_db.get(), "user": self.v_user.get(),
            "estacion": self.v_estacion.get(),
            "recordar": self.v_recordar.get(),
            "password": self.v_pass.get() if self.v_recordar.get() else "",
        }
        try:
            with open(ARCHIVO_CONFIG, "w", encoding="utf-8") as f:
                json.dump(datos, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _cargar_config(self):
        if not os.path.exists(ARCHIVO_CONFIG):
            return
        try:
            with open(ARCHIVO_CONFIG, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        self.v_host.set(d.get("host", "localhost"))
        self.v_port.set(d.get("port", "5432"))
        self.v_db.set(d.get("dbname", "gdr_esc"))
        self.v_user.set(d.get("user", "jonper"))
        self.v_recordar.set(d.get("recordar", False))
        self.v_pass.set(d.get("password", ""))
        self._estacion_guardada = d.get("estacion", "")


if __name__ == "__main__":
    Aplicacion().mainloop()