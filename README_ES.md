# SpotifySaver 🎵✨

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![PyPI Version](https://img.shields.io/pypi/v/spotifysaver?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/spotifysaver/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-Required-orange?logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-2023.7.6%2B-red)](https://github.com/yt-dlp/yt-dlp)
[![YouTube Music](https://img.shields.io/badge/YouTube_Music-API-yellow)](https://ytmusicapi.readthedocs.io/)
[![Spotify](https://img.shields.io/badge/Spotify-API-1ED760?logo=spotify)](https://developer.spotify.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Preguntale a DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/gabrielbaute/spotify-saver)

> ⚠️ Este repositorio está bajo una fuerte etapa de desarrollo, espere cambios constantes. Si encuentra algún error o bug, por favor abra un issue.

Herramienta todo-en-uno para descargar y organizar música con metadata de Spotify para Jellyfin.

La app se conecta a las API's de Spotify y de YoutubeMusic. El objetivo es generar un archivo .nfo en xml para completar la metadata que requiere jellyfin cuando construye las librerías de música.

Lee este archivo en [inglés](README.md)

## 🌟 Características
- ✅ Descarga audio de YouTube Music con metadata de Spotify
- ✅ Letras sincronizadas (.lrc) desde LRC Lib
- ✅ Generación de archivos `.info` compatibles con Jellyfin (aún hay cosas que mejorar aquí! ⚠️)
- ✅ Estructura automática de carpetas (Artista/Álbum)
- ✅ Interfaz de línea de comandos (CLI)
- ✅ Soporte para playlist
- ✅ API
- ✅ Conversión a mp3
- ✅ Soporte para varios bitrates (128, 180, 220, etc.)

### Requisitos
- Python 3.8+
- FFmpeg
- [Cuenta de desarrollador en Spotify](https://developer.spotify.com/dashboard/)

```bash
# Instalación con Poetry (recomendado)
git clone https://github.com/gabrielbaute/spotify-saver.git
cd spotify-saver
poetry install

# O con pip
pip install git+https://github.com/gabrielbaute/spotify-saver.git
```

⚠️ IMPORTANTE: Debes acceder a tu cuenta de spotify como desarrollador, crear una app y obtener así una "client id" y un "client secret", debes colocar esa info en un archivo .env en el directorio raíz del proyecto.

## ⚙️ Configuración

Una vez situado en el directorio de tu proyecto, ejecuta:

```bash
spotifysaver init
```
Esto creará un archivo `.env` local con las variables de entorno que se solicitarán:

| Variable                  | Descripción                                      | Valor por defecto                            |
|---------------------------|--------------------------------------------------|----------------------------------------------|
| `SPOTIFY_CLIENT_ID`       | ID de la app de spotify que creaste              | -                                            |
| `SPOTIFY_CLIENT_SECRET`   | Clave secreta generada para tu app de spotify    | -                                            |
| `SPOTIFY_REDIRECT_URI`    | URI de validación de la API de Spotify           | `http://localhost:8888/callback`             |
| `SPOTIFYSAVER_OUTPUT_DIR` | Ruta para directorio personalizado  (opcional)   | `./Music `                                   |
| `YTDLP_COOKIES_PATH`      | Ruta para el archivo de cookies (opcional)       | -                                            |
| `API_PORT`                | Puerto del servidor de la API (opcional)         | `8000`                                       |
| `API_HOST`                | Host para la API (opcional)                      | `0.0.0.0`                                    |


La variable `YTDLP_COOKIES_PATH` indicará la ubicación del archivo con las cookies de Youtube Music, en caso de que tengamos problemas con restricciones a yt-dlp, en concreto es para casos en que youtube bloquee la app por "comportarse como bot" (guiño, guiño)

También puedes consultar el archivo .example.env

## 📚 Documentación

Mantenemos una [documentación con DeepWiki](https://deepwiki.com/gabrielbaute/spotify-saver), que trackea constantemente el repositorio. Pueden consultarla en todo momento.

La **documentación para el uso de la API**, por su parte, pueden ubicarla en este mismo repositorio aquí: [Documentación de la API](API_IMPLEMENTATION_SUMMARY.md)

## 💻 Uso de la CLI

### Comandos disponibles

| Comando                | Descripción                                      | Ejemplo                                      |
|------------------------|--------------------------------------------------|----------------------------------------------|
| `init`                 | Configura las variables de entorno               | `spotifysaver init"`                         |
| `download [URL]`       | Descarga track/álbum de Spotify                  | `spotifysaver download "URL_SPOTIFY"`        |
| `inspect`              | Muestra la metadata de spotify (album, playlist) | `spotifysaver inspect "URL_SPOTIFY"`         |
| `show-log`             | Muestra el log de la aplicación                  | `spotifysaver show-log`                      |
| `version`              | Muestra la versión instalada                     | `spotifysaver version`                       |

### Opciones de download

| Opción               | Descripción                              | Valores aceptados            |
|----------------------|------------------------------------------|------------------------------|
| `--lyrics`           | Descargar letras sincronizadas (.lrc)    | Flag (sin valor)             |
| `--output DIR`       | Directorio de salida                     | Ruta válida                  |
| `--format FORMATO`   | Formato de audio                         | `m4a` (default), `mp3`       |
| `--cover/--no-cover` | Descarga la portada como `cover.jpg` (activada por defecto) | Flag (sin valor)       |
| `--nfo`              | Genera un archivo .nfo con la metadata (para Jellyfin)| Flag (sin valor) |
| `--explain`          | Muestra (sin descargar) los puntajes de cada opción en youtube| Flag (sin valor) |
| `--dry-run`          | Simula la descarga de un link de spotify sin descargar nada| Flag (sin valor) |

### Opciones de show-log

| Opción            | Descripción                              | Valores aceptados             |
|-------------------|------------------------------------------|-------------------------------|
| `--lines`         | Número de líneas del log que mostrar     | `--lines 25` --> `int`        |
| `--level`         | Filtra por nivel de log                  | INFO, WARNING, DEBUG, ERROR   |
| `--path`          | Muestra la ubicación del archivo de log  | Flag (sin valor)              |

## 💡 Ejemplos de uso
```bash
# Establece la configuración para spotifysaver
spotifysaver init

# Descargar álbum con letras sincronizadas
spotifysaver download "https://open.spotify.com/album/..." --lyrics

# Descargar album con metadata; la portada se guarda como cover.jpg por defecto
spotifysaver download "https://open.spotify.com/album/..." --nfo

# Descargar canción en formato MP3
spotifysaver download "https://open.spotify.com/track/..." --format mp3
```

## Usando la API

Puedes usar la API de SpotifySaver para interactuar con la aplicación programáticamente. Aquí tienes un ejemplo básico de cómo hacerlo:

```bash
spotifysaver-api
```

El servidor estara ejecutándose en `http://localhost:8000` por defecto. Podrán encontrár la [documentación de la API aquí](API_IMPLEMENTATION_SUMMARY.md), donde se describe con detalles los aspectos técnicos y su uso.


## 📂 Estructura de salida
```
Music/
├── Artista/
│   ├── Álbum (Año)/
│   │   ├── 01 - Canción.m4a
│   │   ├── 01 - Canción.lrc
│   │   └── portada.jpg
│   └── artist_info.nfo
```

## 🤝 Contribuciones
1. Haz fork del proyecto
2. Crea tu rama (`git checkout -b feature/nueva-funcion`)
3. Haz commit de tus cambios (`git commit -m 'Agrega función increíble'`)
4. Push a la rama (`git push origin feature/nueva-funcion`)
5. Abre un Pull Request

## 📄 Licencia

MIT © [TGabriel Baute](https://github.com/gabrielbaute)