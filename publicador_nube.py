"""
Versión para la nube (GitHub Actions): publica Reels sin necesitar tu PC.

Cada vez que GitHub la ejecuta (una vez por hora):
1. Mira si toca alguno de los horarios (los de ajustes.json, que edita la app/página,
   o, si no hay, los de configuracion.ini).
2. Si toca, elige el siguiente video pendiente de tu carpeta de Google Drive.
3. Lo descarga, lo deja en un formato válido para Instagram y lo publica
   (pasando por Cloudinary, igual que en tu PC).
4. Anota el video en estado.json para no repetirlo (el workflow guarda ese archivo).
5. Actualiza el panel web (carpeta docs): próximo horario, siguiente video, portada y caption.

Reutiliza las funciones de publicador.py, que debe estar en la misma carpeta.

Uso manual:   python publicador_nube.py --simular    (muestra qué publicaría)
              python publicador_nube.py --ahora      (publica ya, ignora horarios)
"""

import json
import os
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import publicador as p

VARIABLES = (
    "IG_TOKEN", "IG_USER_ID",
    "CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET",
    "DRIVE_FOLDER_ID",
)
LIMITE_MB = 100  # límite de Cloudinary en el plan gratuito
PANEL = p.BASE / "docs"  # carpeta que publica GitHub Pages
AJUSTES = p.BASE / "ajustes.json"  # lo edita la página/app; si algo falta se usa configuracion.ini
ORDENES = ("alfabetico", "aleatorio")


def cargar_ajustes():
    if not AJUSTES.exists():
        return {}
    try:
        datos = json.loads(AJUSTES.read_text(encoding="utf-8-sig"))
        return datos if isinstance(datos, dict) else {}
    except ValueError:
        p.log("ajustes.json tiene un formato inválido; se ignora.")
        return {}


def horas_validas(lista):
    horas = set()
    for h in lista or []:
        try:
            horas.add(datetime.strptime(str(h).strip(), "%H:%M").strftime("%H:%M"))
        except ValueError:
            p.log(f"Horario inválido en ajustes.json: {h}")
    return sorted(horas)


# ------------------------------------------------------------ Google Drive
def servicio_drive():
    """Conecta con Drive. Usa la conexión sin claves de GitHub (Federación de identidades)
    o, si existe el secreto GOOGLE_CREDENTIALS_JSON, una clave de cuenta de servicio."""
    from googleapiclient.discovery import build

    alcance = ["https://www.googleapis.com/auth/drive.readonly"]
    if os.environ.get("GOOGLE_CREDENTIALS_JSON"):
        from google.oauth2 import service_account

        info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        credenciales = service_account.Credentials.from_service_account_info(info, scopes=alcance)
    else:
        import google.auth

        credenciales, _ = google.auth.default(scopes=alcance)
    return build("drive", "v3", credentials=credenciales, cache_discovery=False)


def listar_videos(servicio, carpeta_id):
    """Lista los videos de la carpeta de Drive y de todas sus subcarpetas."""
    videos = []
    carpetas = [carpeta_id]
    while carpetas:
        actual = carpetas.pop()
        pagina = None
        while True:
            r = servicio.files().list(
                q=f"'{actual}' in parents and trashed=false",
                fields="nextPageToken, files(id, name, mimeType, size)",
                pageSize=1000,
                pageToken=pagina,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for f in r.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    carpetas.append(f["id"])
                elif f["mimeType"].startswith("video/") or Path(f["name"]).suffix.lower() in p.EXTENSIONES:
                    videos.append(f)
            pagina = r.get("nextPageToken")
            if not pagina:
                break
    return videos


def elegir_video(videos, estado, orden, forzado=""):
    """Devuelve el video que toca: el elegido a mano, si hay; si no, el ya fijado como 'siguiente'; si no, por orden."""
    pendientes = {v["id"]: v for v in videos if v["id"] not in estado["publicados"]}
    if not pendientes:
        return None
    if forzado in pendientes:
        return pendientes[forzado]
    fijado = (estado.get("siguiente") or {}).get("id")
    if fijado in pendientes:
        return pendientes[fijado]
    if orden == "aleatorio":
        return random.choice(list(pendientes.values()))
    return sorted(pendientes.values(), key=lambda v: v["name"].lower())[0]


def fijar_siguiente(videos, estado, orden, forzado=""):
    """Decide cuál será el próximo video y lo anota, para que el panel y la publicación coincidan."""
    if (estado.get("siguiente") or {}).get("forzado") and not forzado:
        estado["siguiente"] = {}  # quitaron su elección a mano: se vuelve al orden normal
    elegido = elegir_video(videos, estado, orden, forzado)
    if elegido:
        estado["siguiente"] = {
            "id": elegido["id"],
            "nombre": elegido["name"],
            "forzado": bool(forzado and elegido["id"] == forzado),
        }
    else:
        estado["siguiente"] = {}
    return elegido


def descargar(servicio, archivo, destino):
    from googleapiclient.http import MediaIoBaseDownload

    pedido = servicio.files().get_media(fileId=archivo["id"], supportsAllDrives=True)
    with open(destino, "wb") as f:
        descarga = MediaIoBaseDownload(f, pedido, chunksize=50 * 1024 * 1024)
        terminado = False
        while not terminado:
            _, terminado = descarga.next_chunk()


# ---------------------------------------------------------------- ffmpeg
def cumple_requisitos(ruta):
    """True si el video ya es H.264 8 bits, ancho <= 1920 y audio AAC (o sin audio)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,pix_fmt,width", "-of", "json", str(ruta)],
        capture_output=True, text=True,
    )
    try:
        flujos = json.loads(r.stdout)["streams"]
    except (ValueError, KeyError):
        return False
    video = [s for s in flujos if s.get("codec_type") == "video"]
    audio = [s for s in flujos if s.get("codec_type") == "audio"]
    if not video:
        return False
    v = video[0]
    if v.get("codec_name") != "h264" or v.get("pix_fmt") != "yuv420p" or int(v.get("width", 0)) > 1920:
        return False
    return all(a.get("codec_name") == "aac" for a in audio)


def duracion(ruta):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(ruta)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def ejecutar_ffmpeg(comando):
    r = subprocess.run(comando, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falló: {r.stderr[-500:]}")


def convertir(origen, destino, video_kbps=4000):
    ejecutar_ffmpeg([
        "ffmpeg", "-y", "-i", str(origen),
        "-vf", "scale='min(1920,iw)':-2",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.5)}k", "-bufsize", f"{video_kbps * 2}k",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        "-movflags", "+faststart", str(destino),
    ])


def preparar_video(origen, destino):
    """Deja el video listo para Instagram y con un peso que Cloudinary acepte (menos de LIMITE_MB)."""
    if cumple_requisitos(origen):
        ejecutar_ffmpeg(["ffmpeg", "-y", "-i", str(origen), "-c", "copy", "-movflags", "+faststart", str(destino)])
    else:
        p.log("El video no cumple el formato de Instagram; convirtiendo...")
        convertir(origen, destino)

    if destino.stat().st_size / 1_000_000 > LIMITE_MB:
        segundos = duracion(origen) or 60.0
        kbps = int(LIMITE_MB * 0.85 * 8000 / segundos) - 128  # apunta al 85 % del límite
        kbps = max(kbps, 500)
        p.log(f"El video pesa más de {LIMITE_MB} MB; comprimiendo a unos {kbps} kbps...")
        convertir(origen, destino, kbps)

    peso = destino.stat().st_size / 1_000_000
    if peso > LIMITE_MB:
        raise RuntimeError(f"El video sigue pesando {peso:.0f} MB tras comprimirlo (límite {LIMITE_MB} MB).")


# ---------------------------------------------------------------- panel web
def proximo_y_anterior(horarios, estado, ahora):
    """Próximo horario pendiente y el horario inmediatamente anterior (para el anillo del reloj)."""
    todos = []
    for d in range(-2, 3):
        dia = ahora.date() + timedelta(days=d)
        for h in horarios:
            todos.append(datetime.combine(dia, datetime.strptime(h, "%H:%M").time()))
    todos.sort()
    ventana = timedelta(hours=p.VENTANA_HORAS)
    proximo = next(
        (m for m in todos
         if m.strftime("%Y-%m-%d %H:%M") not in estado["horarios_hechos"] and m + ventana >= ahora),
        None,
    )
    anterior = max((m for m in todos if proximo and m < proximo), default=None)
    return proximo, anterior


def generar_portada(servicio, video, destino):
    """Descarga el video y guarda un fotograma como portada."""
    with tempfile.TemporaryDirectory() as tmp:
        origen = Path(tmp) / ("v" + (Path(video["name"]).suffix or ".mp4"))
        descargar(servicio, video, origen)
        for segundo in ("1", "0"):  # si el video dura menos de 1 s, usa el primer fotograma
            subprocess.run(
                ["ffmpeg", "-y", "-ss", segundo, "-i", str(origen), "-frames:v", "1",
                 "-vf", "scale=720:-2", "-q:v", "4", str(destino)],
                capture_output=True, text=True,
            )
            if destino.exists() and destino.stat().st_size > 0:
                return
    raise RuntimeError("ffmpeg no pudo sacar un fotograma del video")


def escribir_json_si_cambia(ruta, datos, ahora):
    """Reescribe el archivo solo si cambió algo, para no llenar el historial de commits."""
    previo = {}
    if ruta.exists():
        try:
            previo = json.loads(ruta.read_text(encoding="utf-8"))
        except ValueError:
            previo = {}
    previo.pop("actualizado", None)
    if datos != previo:
        datos = dict(datos, actualizado=ahora.astimezone().isoformat(timespec="seconds"))
        ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


def actualizar_panel(servicio, videos, estado, ctx):
    """Escribe docs/proximo.json, docs/videos.json y docs/portada.jpg, que usan la página y la app."""
    try:
        PANEL.mkdir(exist_ok=True)
        portada = PANEL / "portada.jpg"
        id_siguiente = (estado.get("siguiente") or {}).get("id")
        siguiente = next((v for v in videos if v["id"] == id_siguiente), None)

        if siguiente and (estado.get("portada_de") != siguiente["id"] or not portada.exists()):
            try:
                generar_portada(servicio, siguiente, portada)
                estado["portada_de"] = siguiente["id"]
            except Exception as e:
                p.log(f"No pude crear la portada: {e}")

        ahora = datetime.now()
        horarios = ctx["horarios"]
        proximo, anterior = proximo_y_anterior(horarios, estado, ahora) if horarios else (None, None)
        tiene_portada = bool(siguiente) and estado.get("portada_de") == siguiente["id"] and portada.exists()
        pendientes = sorted((v for v in videos if v["id"] not in estado["publicados"]), key=lambda v: v["name"].lower())

        datos = {
            "zona": os.environ.get("TZ", ""),
            "proximo_horario": proximo.astimezone().isoformat(timespec="seconds") if proximo else None,
            "anterior_horario": anterior.astimezone().isoformat(timespec="seconds") if anterior else None,
            "pausado": ctx["pausado"],
            "siguiente": {
                "id": siguiente["id"],
                "nombre": Path(siguiente["name"]).stem,
                "caption": ctx["descripciones"].get(siguiente["id"]) or ctx["descripcion"],
                "propia": siguiente["id"] in ctx["descripciones"],
                "elegido": bool((estado.get("siguiente") or {}).get("forzado")),
            } if siguiente else None,
            "portada": f"portada.jpg?v={estado['portada_de']}" if tiene_portada else None,
            "pendientes": len(pendientes),
            "ultimo": estado.get("ultimo"),
            "ajustes": {
                "horarios": horarios,
                "descripcion": ctx["descripcion"],
                "orden": ctx["orden"],
                "pausado": ctx["pausado"],
            },
        }
        escribir_json_si_cambia(PANEL / "proximo.json", datos, ahora)
        lista = {"videos": [{"id": v["id"], "nombre": Path(v["name"]).stem} for v in pendientes]}
        escribir_json_si_cambia(PANEL / "videos.json", lista, ahora)
    except Exception as e:  # el panel nunca debe impedir una publicación
        p.log(f"No pude actualizar el panel: {e}")


# ------------------------------------------------------------------ main
def main():
    simular = "--simular" in sys.argv
    forzar = "--ahora" in sys.argv

    faltan = [v for v in VARIABLES if not os.environ.get(v)]
    if faltan:
        sys.exit("Faltan estos secretos en GitHub: " + ", ".join(faltan))
    if not (os.environ.get("GOOGLE_CREDENTIALS_JSON") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")):
        sys.exit("No hay conexión con Google: faltan los secretos WIF_PROVIDER y WIF_SERVICE_ACCOUNT (o GOOGLE_CREDENTIALS_JSON).")

    # Ajustes: lo que se editó desde la página/app (ajustes.json) manda; lo que falte sale de configuracion.ini
    horarios_ini, orden_ini, _alternar, carpetas = p.leer_config()
    ajustes = cargar_ajustes()
    horarios = horas_validas(ajustes.get("horarios")) or horarios_ini
    orden = ajustes.get("orden") if ajustes.get("orden") in ORDENES else orden_ini
    descripcion = str(ajustes.get("descripcion") or "").strip() or next(iter(carpetas.values()))
    descripciones = {
        str(k): v.strip() for k, v in (ajustes.get("descripciones") or {}).items()
        if isinstance(v, str) and v.strip()
    }
    pausado = bool(ajustes.get("pausado"))
    elegido_a_mano = str(ajustes.get("forzar_siguiente") or "")
    ctx = {"horarios": horarios, "orden": orden, "descripcion": descripcion,
           "descripciones": descripciones, "pausado": pausado}

    estado = p.cargar_estado()
    estado.setdefault("nombres", {})
    ahora = datetime.now()

    clave_horario = None
    if not (simular or forzar) and not pausado:
        if not horarios:
            sys.exit("Pon al menos un horario en configuracion.ini (sección [publicacion]).")
        clave_horario = p.horario_pendiente(horarios, estado, ahora)

    servicio = servicio_drive()
    videos = listar_videos(servicio, os.environ["DRIVE_FOLDER_ID"])

    if clave_horario is None and not (simular or forzar):
        # No toca publicar (o está en pausa): solo se deja el panel al día
        fijar_siguiente(videos, estado, orden, elegido_a_mano)
        actualizar_panel(servicio, videos, estado, ctx)
        p.guardar_estado(estado)
        print(f"{ahora:%Y-%m-%d %H:%M} - " + ("Publicación en pausa." if pausado else "Todavía no toca publicar."))
        return

    elegido = elegir_video(videos, estado, orden, elegido_a_mano)
    if elegido is None:
        p.log("No quedan videos pendientes en la carpeta de Drive.")
        if simular:
            return
        if clave_horario:
            estado["horarios_hechos"].append(clave_horario)
        estado["siguiente"] = {}
        actualizar_panel(servicio, videos, estado, ctx)
        p.guardar_estado(estado)
        return

    caption = descripciones.get(elegido["id"]) or descripcion
    if simular:
        print(f"Publicaría: {elegido['name']}  ({len(videos)} videos en Drive)\nDescripción: {caption}")
        return

    with tempfile.TemporaryDirectory() as tmp:
        origen = Path(tmp) / ("origen" + (Path(elegido["name"]).suffix or ".mp4"))
        listo = Path(tmp) / "listo.mp4"
        try:
            p.log(f"Descargando {elegido['name']} de Drive...")
            descargar(servicio, elegido, origen)
            preparar_video(origen, listo)
            media_id = p.publicar_reel(os.environ["IG_TOKEN"], os.environ["IG_USER_ID"], listo, caption)
        except Exception as e:
            p.log(f"FALLÓ {elegido['name']}: {e}")
            sys.exit(1)  # GitHub te avisa por correo; se reintenta en la próxima hora

    estado["publicados"].append(elegido["id"])
    estado["nombres"][elegido["id"]] = elegido["name"]
    estado["ultimo"] = {
        "nombre": Path(elegido["name"]).stem,
        "fecha": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if clave_horario:
        estado["horarios_hechos"].append(clave_horario)

    estado["siguiente"] = {}
    fijar_siguiente(videos, estado, orden, elegido_a_mano)  # ya con el video recién publicado descartado
    actualizar_panel(servicio, videos, estado, ctx)
    p.guardar_estado(estado)
    p.log(f"PUBLICADO {elegido['name']} (id {media_id})")


if __name__ == "__main__":
    main()
