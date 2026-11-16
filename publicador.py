"""
Publicador de Reels en Instagram (API oficial de Meta, inicio de sesión de Instagram).

Cómo funciona:
- En configuracion.ini eliges las subcarpetas de "videos" que quieres usar,
  el caption de cada una y los horarios de publicación.
- Cada vez que se ejecuta, si toca un horario, toma el siguiente video pendiente
  de las carpetas elegidas, lo publica y lo anota en estado.json para no repetirlo.
- Publica como máximo UN video por ejecución.

Uso normal (lo ejecuta el Programador de tareas cada 15 min):   python publicador.py
Ver qué publicaría, sin publicar nada:                           python publicador.py --simular
Publicar el siguiente video ya, ignorando los horarios:          python publicador.py --ahora
"""

import configparser
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE = Path(__file__).parent
CARPETA_VIDEOS = BASE / "videos"
CONFIG = BASE / "configuracion.ini"
ESTADO = BASE / "estado.json"
LOG = BASE / "publicador.log"

API_VERSION = "v23.0"  # si Meta da error de versión, prueba con una más reciente
# El token que generaste (inicio de sesión unificado / usuario del sistema) se usa con graph.facebook.com
API = f"https://graph.facebook.com/{API_VERSION}"
EXTENSIONES = {".mp4", ".mov"}
VENTANA_HORAS = 3  # si la PC estaba apagada, publica un horario perdido hasta 3 h después


# ---------------------------------------------------------------- utilidades
def log(mensaje):
    linea = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {mensaje}"
    print(linea)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def cargar_env():
    ruta = BASE / ".env"
    if not ruta.exists():
        sys.exit("No encuentro el archivo .env junto al script.")
    for linea in ruta.read_text(encoding="utf-8-sig").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            clave, valor = linea.split("=", 1)
            os.environ[clave.strip()] = valor.strip().strip('"').strip("'")
    for clave in ("IG_TOKEN", "IG_USER_ID"):
        if not os.environ.get(clave):
            sys.exit(f"Falta {clave} en el archivo .env")


def revisar(respuesta, paso):
    if not respuesta.ok:
        raise RuntimeError(f"Error en '{paso}' ({respuesta.status_code}): {respuesta.text}")
    return respuesta.json() if respuesta.content else {}


# ------------------------------------------------------------- configuración
def leer_config():
    if not CONFIG.exists():
        sys.exit("No encuentro configuracion.ini junto al script.")
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str  # respeta mayúsculas en los nombres de carpeta
    cfg.read(CONFIG, encoding="utf-8-sig")

    pub = cfg["publicacion"] if cfg.has_section("publicacion") else {}
    horarios = [h.strip() for h in pub.get("horarios", "").split(",") if h.strip()]
    for h in horarios:
        try:
            datetime.strptime(h, "%H:%M")
        except ValueError:
            sys.exit(f"Horario inválido '{h}' en configuracion.ini. Usa el formato HH:MM, por ejemplo 18:30")

    orden = pub.get("orden", "alfabetico").strip().lower()
    alternar = pub.get("alternar_carpetas", "si").strip().lower() in ("si", "sí", "yes", "true", "1")
    carpetas = dict(cfg["carpetas"]) if cfg.has_section("carpetas") else {}

    if not carpetas:
        # Sin subcarpetas: se usan todos los videos de la carpeta "videos"
        # Si existe caption.txt se usa ese texto (admite varias líneas y hashtags sin problema);
        # si no, se usa el "caption" de configuracion.ini
        archivo_caption = BASE / "caption.txt"
        if archivo_caption.exists() and archivo_caption.read_text(encoding="utf-8-sig").strip():
            texto = archivo_caption.read_text(encoding="utf-8-sig").strip()
        else:
            texto = pub.get("caption", "").strip()
        carpetas = {".": texto}
    return horarios, orden, alternar, carpetas


# -------------------------------------------------------------------- estado
def cargar_estado():
    if ESTADO.exists():
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    return {"publicados": [], "horarios_hechos": [], "ultima_carpeta": -1}


def guardar_estado(estado):
    estado["horarios_hechos"] = estado["horarios_hechos"][-50:]
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------- qué publicar
def horario_pendiente(horarios, estado, ahora):
    """Devuelve la clave del horario que toca ahora (o None)."""
    for dia in (ahora.date() - timedelta(days=1), ahora.date()):
        for h in sorted(horarios):
            hora = datetime.strptime(h, "%H:%M").time()
            momento = datetime.combine(dia, hora)
            clave = momento.strftime("%Y-%m-%d %H:%M")
            if momento <= ahora <= momento + timedelta(hours=VENTANA_HORAS) \
                    and clave not in estado["horarios_hechos"]:
                return clave
    return None


def siguiente_video(carpetas, estado, orden, alternar):
    """Elige el próximo video pendiente entre las carpetas indicadas."""
    nombres = list(carpetas)
    total = len(nombres)
    inicio = (estado["ultima_carpeta"] + 1) % total if alternar else 0

    for i in range(total):
        indice = (inicio + i) % total
        nombre = nombres[indice]
        carpeta = CARPETA_VIDEOS / nombre
        if not carpeta.is_dir():
            log(f"La carpeta '{nombre}' no existe dentro de 'videos'.")
            continue

        videos = [p for p in carpeta.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONES]
        pendientes = [
            p for p in videos
            if p.relative_to(CARPETA_VIDEOS).as_posix() not in estado["publicados"]
        ]
        if not pendientes:
            continue

        elegido = random.choice(pendientes) if orden == "aleatorio" else sorted(pendientes)[0]
        return indice, nombre, elegido
    return None


def obtener_caption(video, caption_carpeta):
    """Si existe un .txt con el mismo nombre del video, se usa como caption propio."""
    txt = video.with_suffix(".txt")
    if txt.exists():
        return txt.read_text(encoding="utf-8-sig").strip()
    return caption_carpeta


# ---------------------------------------------------------- API de Instagram
def esperar_procesamiento(token, contenedor):
    """Espera a que Instagram termine de procesar el video."""
    for _ in range(90):
        r = requests.get(
            f"{API}/{contenedor}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=60,
        )
        estado = revisar(r, "consultar estado").get("status_code")
        if estado == "FINISHED":
            return
        if estado in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram no pudo procesar el video: {r.text}")
        time.sleep(10)
    raise TimeoutError("Instagram tardó demasiado en procesar el video.")


def publicar_contenedor(token, user_id, contenedor):
    r = requests.post(
        f"{API}/{user_id}/media_publish",
        data={"creation_id": contenedor, "access_token": token},
        timeout=60,
    )
    return revisar(r, "publicar")["id"]


# ---- Método A: subida directa a Instagram (la que se usa si no hay Cloudinary)
def publicar_reel_directo(token, user_id, ruta_video, caption):
    r = requests.post(
        f"{API}/{user_id}/media",
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "access_token": token,
        },
        timeout=60,
    )
    datos = revisar(r, "crear contenedor")
    contenedor = datos["id"]
    uri = datos.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}/{contenedor}"

    tamano = ruta_video.stat().st_size
    log(f"Subiendo {ruta_video.name} ({tamano / 1_000_000:.1f} MB) a {uri} ...")
    with open(ruta_video, "rb") as f:
        r = requests.post(
            uri,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(tamano),
                # curl --data-binary envía este tipo de contenido por defecto
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=f,
            timeout=1800,
        )
    revisar(r, "subir video")

    esperar_procesamiento(token, contenedor)
    return publicar_contenedor(token, user_id, contenedor)


# ---- Método B: Instagram descarga el video desde un enlace público (Cloudinary)
def usar_cloudinary():
    claves = ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")
    return all(os.environ.get(c) for c in claves)


def firmar_cloudinary(parametros):
    base = "&".join(f"{k}={parametros[k]}" for k in sorted(parametros))
    return hashlib.sha1((base + os.environ["CLOUDINARY_API_SECRET"]).encode("utf-8")).hexdigest()


def subir_a_cloudinary(ruta_video):
    nube = os.environ["CLOUDINARY_CLOUD_NAME"]
    public_id = f"ig_{int(time.time())}_{random.randint(1000, 9999)}"
    firmados = {"public_id": public_id, "timestamp": int(time.time())}
    datos = dict(firmados)
    datos["api_key"] = os.environ["CLOUDINARY_API_KEY"]
    datos["signature"] = firmar_cloudinary(firmados)

    tamano = ruta_video.stat().st_size
    log(f"Subiendo {ruta_video.name} ({tamano / 1_000_000:.1f} MB) a Cloudinary...")
    with open(ruta_video, "rb") as f:
        r = requests.post(
            f"https://api.cloudinary.com/v1_1/{nube}/video/upload",
            data=datos,
            files={"file": (ruta_video.name, f)},
            timeout=1800,
        )
    info = revisar(r, "subir a Cloudinary")
    return info["secure_url"], info["public_id"]


def borrar_de_cloudinary(public_id):
    try:
        nube = os.environ["CLOUDINARY_CLOUD_NAME"]
        firmados = {"public_id": public_id, "timestamp": int(time.time())}
        datos = dict(firmados)
        datos["api_key"] = os.environ["CLOUDINARY_API_KEY"]
        datos["signature"] = firmar_cloudinary(firmados)
        requests.post(f"https://api.cloudinary.com/v1_1/{nube}/video/destroy", data=datos, timeout=60)
    except Exception as e:
        log(f"No pude borrar el video temporal de Cloudinary ({public_id}): {e}")


def publicar_reel_url(token, user_id, ruta_video, caption):
    url, public_id = subir_a_cloudinary(ruta_video)
    try:
        log("Pidiendo a Instagram que descargue el video...")
        r = requests.post(
            f"{API}/{user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": url,
                "caption": caption,
                "access_token": token,
            },
            timeout=120,
        )
        contenedor = revisar(r, "crear contenedor")["id"]
        esperar_procesamiento(token, contenedor)
        return publicar_contenedor(token, user_id, contenedor)
    finally:
        borrar_de_cloudinary(public_id)


def publicar_reel(token, user_id, ruta_video, caption):
    if usar_cloudinary():
        return publicar_reel_url(token, user_id, ruta_video, caption)
    return publicar_reel_directo(token, user_id, ruta_video, caption)


# ---------------------------------------------------------------------- main
def main():
    simular = "--simular" in sys.argv
    forzar = "--ahora" in sys.argv

    horarios, orden, alternar, carpetas = leer_config()
    estado = cargar_estado()
    ahora = datetime.now()

    clave_horario = None
    if not (simular or forzar):
        if not horarios:
            sys.exit("Pon al menos un horario en configuracion.ini (sección [publicacion]).")
        clave_horario = horario_pendiente(horarios, estado, ahora)
        if clave_horario is None:
            print(f"{ahora:%H:%M} - Todavía no toca publicar.")
            return

    resultado = siguiente_video(carpetas, estado, orden, alternar)
    if resultado is None:
        log("No quedan videos pendientes en las carpetas elegidas.")
        if clave_horario:
            estado["horarios_hechos"].append(clave_horario)
            guardar_estado(estado)
        return

    indice, nombre_carpeta, video = resultado
    caption = obtener_caption(video, carpetas[nombre_carpeta])
    relativo = video.relative_to(CARPETA_VIDEOS).as_posix()

    if simular:
        print(f"Publicaría: {relativo}\nCaption: {caption}")
        return

    cargar_env()
    try:
        media_id = publicar_reel(os.environ["IG_TOKEN"], os.environ["IG_USER_ID"], video, caption)
    except Exception as e:
        log(f"FALLÓ {relativo}: {e}")
        return  # se reintenta en la próxima ejecución, dentro de la ventana del horario

    estado["publicados"].append(relativo)
    estado["ultima_carpeta"] = indice
    if clave_horario:
        estado["horarios_hechos"].append(clave_horario)
    guardar_estado(estado)
    log(f"PUBLICADO {relativo} (id {media_id})")


if __name__ == "__main__":
    main()
