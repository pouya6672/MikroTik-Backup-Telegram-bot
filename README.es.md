# MikroTik Backup Bot

[English](README.md) | **Español** | [Русский](README.ru.md) | [中文](README.zh.md) | [فارسی](README.fa.md)

Un bot de Telegram que hace copias de seguridad automáticas de tus routers MikroTik y las entrega a un chat privado, grupo o canal, con la programación que elijas.

El bot se conecta al router por SSH, crea la copia, la descarga por SFTP, la elimina del router y la envía a Telegram. Cada tarea se puede editar después, y el propio bot habla 🇬🇧 English, 🇪🇸 Español, 🇷🇺 Русский, 🇨🇳 中文 y 🇮🇷 فارسی.

## Características

- **Tipos de copia:** copia binaria (`.backup`, con o sin contraseña), base de datos de User Manager (`.umb`), o ambas
- **ZIP opcional:** ZIP normal o ZIP cifrado con **AES-256**
- **Destinos flexibles:** chat privado, grupo o canal
- **Programación flexible:** valores predefinidos de 1 hora a 7 días, o un intervalo personalizado como `30m`, `2h`, `1d` (hasta 30 días)
- **Reintento automático** ante fallos transitorios, con mensajes de error claros y accionables
- **Zona horaria por usuario** para nombres de archivo, textos de las capturas y «última ejecución»
- **Multiusuario:** límite de tareas por usuario, límite global de concurrencia, control de acceso
- **Seguro por defecto:** credenciales cifradas, fijación de la clave de host SSH, bloqueo de IP privadas, límites contra abuso
- **Listo para Docker**, se ejecuta como usuario sin privilegios

## Requisitos

- Python **3.10+** (o Docker)
- Un token de bot de [@BotFather](https://t.me/BotFather)
- Un router MikroTik con SSH activado y accesible desde el servidor del bot
- Dependencias de Python: `python-telegram-bot[job-queue]`, `asyncssh`, `pyzipper`, `cryptography`, `python-dotenv`, `tzdata`

## Instalación

### Opción 1: Docker (recomendada)

```bash
git clone https://github.com/pouya6672/MikroTik-Backup-Telegram-bot.git
cd MikroTik-Backup-Telegram-bot
cp .env.example .env        # define BOT_TOKEN y ALLOWED_USERS
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs -f
```

- La base de datos y la clave de cifrado viven en el volumen `mtbackup-data` (`/app/data`). **Conserva siempre este volumen.**
- El contenedor arranca como root solo para corregir los permisos del volumen y luego pasa al usuario sin privilegios `bot` (uid 10001) mediante `gosu`.
- `docker/entrypoint.sh` valida todas las variables de entorno al arrancar y muestra un error (o aviso) claro si algo falla, por ejemplo falta `BOT_TOKEN`, `FERNET_KEY` no válida o `ALLOWED_USERS` vacío.
- El bot usa long polling, así que **no hace falta abrir ningún puerto de entrada**.

Sin Compose:

```bash
docker build -f docker/Dockerfile -t mikrotik-backup-bot .
docker run -d --name mtbackup --restart unless-stopped \
  --env-file .env -v mtbackup-data:/app/data mikrotik-backup-bot
```

### Opción 2: Python directo

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # define BOT_TOKEN y ALLOWED_USERS
python bot.py
```

## Configuración de MikroTik (por router)

1. Comprueba que el **servicio SSH** esté activado y accesible desde el servidor del bot (puerto 22 por defecto, o cualquier puerto personalizado):
   ```
   /ip service print
   ```
2. Crea un usuario dedicado y permite el acceso solo desde la IP del servidor del bot:
   ```
   /user add name=tgbackup group=full password=STRONG_PASSWORD address=BOT_SERVER_IP/32
   ```
   `full` es la opción más sencilla. Si creas un grupo más restringido, necesita al menos estas políticas: `ssh, ftp, read, write, policy, test, sensitive`.
3. Para las copias de User Manager debe estar instalado el paquete `user-manager` en el router.

## Uso

| Comando | Descripción |
|---|---|
| `/new` | Crear una tarea de copia |
| `/jobs` | Listar tareas: ejecutar ahora, editar, pausar/reanudar, eliminar |
| `/timezone` | Configurar tu zona horaria |
| `/language` | Cambiar el idioma del bot |
| `/cancel` | Cancelar el paso actual |

### Crear una tarea (`/new`)

1. **Dirección:** IP o dominio, con puerto SSH opcional (`1.2.3.4` o `1.2.3.4:2222`)
2. **Usuario**
3. **Contraseña:** tu mensaje se borra justo después de leerlo y la conexión se prueba de inmediato
4. **Tipo de copia:** MikroTik / User Manager / ambas, con o sin contraseña de copia
5. **ZIP** (opcional): sin ZIP / ZIP sin contraseña / ZIP con contraseña
6. **Carpeta temporal en el router**
7. **Destino:** chat privado, grupo o canal
8. **Intervalo:** 1 hora … 7 días, o personalizado (`30m`, `2h`, `1d`)

### Archivos ZIP (opcional)

Si se activa, todos los archivos de una ejecución se empaquetan en **un único ZIP**, que es lo que se envía; por ejemplo `MyRouter_2026-01-31_12-00.zip`, con el `.backup` y, si elegiste «ambas», también el `.umb`. Por defecto es «sin ZIP», así que las tareas antiguas siguen funcionando igual.

- **Sin contraseña:** ZIP normal (Deflate).
- **Con contraseña:** ZIP cifrado con **AES-256** (estándar WinZip). Se abre con 7-Zip, WinRAR, Keka, The Unarchiver y herramientas similares. El extractor integrado del Explorador de Windows no admite AES. El método antiguo y débil ZipCrypto no se usa deliberadamente.
- La contraseña del ZIP es independiente de la contraseña de copia de MikroTik. Puedes usar ambas (una copia cifrada dentro de un ZIP cifrado).
- Si falla la creación del ZIP, los archivos sin comprimir **no** se envían, para que un archivo que querías cifrado nunca llegue sin protección. Se avisa del error al propietario.
- El ZIP final también debe quedar por debajo del límite de 50 MB de Telegram.
- Se puede cambiar después en `/jobs` → tarea → **✏️ Editar** → **🗜 ZIP**.

### Carpeta del router

Una ruta como `flash/backups`, `disk1/mt-backup` o `backups`. Envía `-` (o pulsa el botón por defecto) para usar la raíz del router.

- Las carpetas inexistentes (incluidas subcarpetas anidadas) se crean antes de cada copia con `/file add type=directory`. En versiones sin este comando, la carpeta se crea al guardar el archivo.
- Si el disco (`flash`, `disk1`, `usb1`, …) no existe, el bot muestra un error claro.
- Los archivos se eliminan del router tras la descarga; la carpeta permanece.

### Editar una tarea

`/jobs` → elige una tarea → **✏️ Editar**. Se puede cambiar: dirección/puerto, usuario, contraseña, tipo de copia, contraseña de la copia, ZIP, carpeta del router, destino e intervalo.

Los cambios de dirección/usuario/contraseña se verifican con una conexión de prueba antes de guardar; si falla, no se cambia nada. Guardar una dirección (incluso la misma) vuelve a aceptar la clave de host SSH del router, que es lo que necesitas tras restablecer un dispositivo.

### Reintento automático

Si falla una copia **programada** (la primera copia tras crear una tarea y «Ejecutar ahora» nunca se reintentan), el bot reintenta hasta `RETRY_ATTEMPTS` veces más, cada `RETRY_DELAY_MINUTES` minutos, en lugar de esperar a la siguiente ejecución. El propietario de la tarea recibe aviso de todo:

- cada intento fallido: un mensaje con una **causa clara** (p. ej. «autenticación fallida», «conexión rechazada», «tiempo de conexión agotado» más detalles técnicos), el número de intento («intento 2 de 4») y la hora del siguiente
- éxito en un reintento: «se logró en el intento n»
- todos los intentos fallidos: un mensaje final con el último error; la siguiente ejecución sigue la programación normal

**Solo se reintentan los errores transitorios:** tiempos de espera, conexiones rechazadas o cortadas, errores de red/DNS y errores de red de Telegram. Los demás errores no se arreglan esperando, así que **no se reintentan**; el mensaje de fallo llega de inmediato, con una solución concreta:

| Error | Solución indicada |
|---|---|
| Usuario/contraseña incorrectos | Editar usuario/contraseña |
| Clave de host SSH cambiada | Guardar de nuevo la dirección |
| Disco/carpeta inexistente o almacenamiento lleno | Editar la carpeta del router y revisar el espacio libre |
| El usuario del router no tiene la política `ftp` | Añadir los permisos necesarios |
| No es un dispositivo RouterOS / permisos insuficientes | Revisar la dirección y los permisos |
| Archivo de más de 50 MB | Copiar un solo tipo o reducir los datos |
| Falló la creación del ZIP | Desactivar el ZIP o cambiar la contraseña |
| Bot eliminado del destino / sin permiso | Volver a hacerlo administrador o elegir otro destino |
| Dirección privada/no permitida | Usar una dirección pública |
| Error desconocido | Revisar la configuración y el log del servidor |

Cada uno de estos cuenta como un fallo para la pausa automática.

**Nota de seguridad:** la causa detallada solo se muestra para **tareas existentes** (su destino se verificó al crearlas). Las pruebas de conexión de `/new` y de edición siguen la configuración antiabuso y devuelven un error uniforme cuando así se configura.

**Notas**

- Cuando llega la siguiente ejecución programada, los reintentos pendientes se cancelan y los sustituye la nueva ejecución. Pausar o eliminar una tarea también cancela sus reintentos pendientes.
- Los reintentos pendientes se guardan en memoria y se pierden al reiniciar (la programación normal no se ve afectada).
- `RETRY_ATTEMPTS=0` desactiva los reintentos.

### Más comportamientos

- **Zona horaria:** `/timezone` ofrece zonas habituales u «Otra» (un nombre IANA como `Asia/Tehran`, o un desfase como `+03:30`). Es por usuario y afecta a los nombres de archivo, la hora de los textos y «última ejecución».
- Con el tipo «ambas», si un archivo no se puede crear (por ejemplo falta el paquete `user-manager`), el otro se envía igualmente y el error de la parte fallida se comunica al propietario. La tarea no se pausa por ello.
- Cada tarea ejecuta como máximo una copia a la vez; «Ejecutar ahora» durante una copia en curso no inicia una segunda.
- Si una copia se interrumpe (timeout), los archivos temporales `tgbk-*` sobrantes se eliminan del router.
- En el router el bot ejecuta `/system backup save` (y `user-manager database save`), descarga el archivo por SFTP, lo elimina del router y lo envía a Telegram.
- Tras **5 fallos consecutivos** la tarea se pausa automáticamente y se avisa al propietario (un «fallo» es una ejecución completa cuyos reintentos fallaron todos).
- Para un **canal o grupo**, el bot debe ser administrador de ese chat, y el usuario también.

## Seguridad

- **Secretos cifrados:** la contraseña del router, la de la copia y la del ZIP se cifran con Fernet y se guardan en SQLite. La clave viene de `FERNET_KEY` o de `data/.secret.key` (creado con modo `0600`). **Haz una copia de ella y protégela.**
- **Fijación de la clave de host SSH (TOFU):** la clave de host se guarda en la primera conexión y se verifica después. Tras restablecer un router, abre `/jobs` → tarea → **Editar** → **Dirección / puerto** y guarda la misma dirección de nuevo para aceptar la clave nueva; no hace falta borrar y recrear la tarea.
- **Protección SSRF:** las direcciones privadas/locales están bloqueadas por defecto (para que el bot no pueda escanear la red del propio servidor). La dirección se resuelve una vez y la conexión va directamente a esa IP, lo que evita el DNS rebinding.
- **Define `ALLOWED_USERS`.** Si está vacío, cualquiera puede usar el bot y este abrirá conexiones SSH a cualquier host y puerto públicos.
- **Protección contra abuso (escaneo de puertos / fuerza bruta):**
  - Cada prueba de conexión tiene límite: `TEST_LIMIT_PER_HOUR` por usuario y `TARGET_LIMIT_PER_10MIN` por `host:port` en todo el bot. Editar dirección/usuario/contraseña cuenta como prueba; las copias programadas no.
  - En un bot público (`ALLOWED_USERS` vacío), todos los fallos de conexión devuelven un mensaje idéntico («no se pudo conectar») y tardan al menos `CONN_FAIL_MIN_SECONDS`, de modo que ni el texto del error ni el tiempo de respuesta revelan si un puerto está cerrado, filtrado o si la contraseña es incorrecta. La causa real solo se registra en el log del servidor (`INFO`). Dos excepciones no explotables que importan al usuario: un cambio de clave SSH de *su propio* router guardado, y «esto no es RouterOS» (solo tras un inicio de sesión correcto).
  - **Lista blanca:** pon los ID de usuarios de confianza en `TRUSTED_USERS`. Quedan exentos de las cuotas de prueba y del error/tiempo uniformes (ven errores completos y sin retraso). El resto de límites (direcciones privadas, máximo de tareas, intervalo mínimo) siguen aplicándose. Esta lista no sustituye a `ALLOWED_USERS`: si `ALLOWED_USERS` está definido, el usuario de confianza también debe figurar en él.
  - En un servidor privado con `ALLOWED_USERS` definido se muestran los errores completos (fuerza el comportamiento uniforme con `HIDE_CONN_ERRORS=true`).
- **Entrada acotada:** la salida de los comandos del router y el tamaño de los archivos descargados tienen un tope, de modo que un servidor SSH malicioso no puede agotar la memoria.
- **Archivos sensibles:** las copias contienen datos sensibles. Envíalas solo a chats privados y define siempre una contraseña de copia.
- **Límite de Telegram:** no se pueden enviar archivos de más de 50 MB.

## Configuración

Todos los ajustes son variables de entorno, cargadas desde `.env` (o pasadas al contenedor).

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `BOT_TOKEN` | — | Token del bot (**obligatorio**) |
| `ALLOWED_USERS` | vacío | ID numéricos de Telegram con permiso para usar el bot, separados por comas. Vacío = todos (no recomendado) |
| `TRUSTED_USERS` | vacío | ID separados por comas, exentos de las cuotas de prueba de conexión y de los errores uniformes |
| `FERNET_KEY` | — | Clave de cifrado. Si no se define, se genera `data/.secret.key` |
| `DATA_DIR` | `data` (`/app/data` en Docker) | Ubicación de la base de datos y la clave |
| `MAX_JOBS_PER_USER` | `5` | Límite de tareas por usuario |
| `MIN_INTERVAL_MINUTES` | `10` | Intervalo personalizado más corto permitido |
| `MAX_PARALLEL_BACKUPS` | `3` | Copias simultáneas en todo el bot |
| `ALLOW_PRIVATE_IPS` | `false` | Permitir direcciones privadas/locales (solo en una red aislada y de confianza) |
| `RETRY_ATTEMPTS` | `3` | Reintentos tras una copia programada fallida (`0` = desactivado) |
| `RETRY_DELAY_MINUTES` | `5` | Minutos entre reintentos |
| `TEST_LIMIT_PER_HOUR` | `10` | Pruebas de conexión por usuario y hora (`0` = desactivado) |
| `TARGET_LIMIT_PER_10MIN` | `5` | Pruebas de conexión por `host:port` cada 10 minutos, todos los usuarios (`0` = desactivado) |
| `CONN_FAIL_MIN_SECONDS` | `20` | Duración mínima de una prueba fallida (igualación del tiempo de respuesta) |
| `HIDE_CONN_ERRORS` | `auto` | `auto` = errores uniformes solo cuando `ALLOWED_USERS` está vacío; `true` / `false` para forzar |

Generar una clave:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Para averiguar tu ID numérico de Telegram, escribe a un bot como [@userinfobot](https://t.me/userinfobot).

## Estructura del proyecto

```
.
├── bot.py               # el bot: manejadores, planificador, SSH/SFTP, cifrado, base de datos
├── i18n.py              # traducciones (en, es, ru, zh, fa)
├── requirements.txt
├── .env.example         # plantilla de configuración
├── README.md            # English
├── README.es.md
├── README.ru.md
├── README.zh.md
├── README.fa.md
└── docker/
    ├── Dockerfile
    ├── docker-compose.yml
    ├── entrypoint.sh    # valida variables, corrige permisos, reduce privilegios
    └── Dockerfile.dockerignore
```

## Actualización

Sustituye los archivos y ejecuta `pip install -r requirements.txt` (el paquete `pyzipper` es necesario para el ZIP cifrado). Con Docker, reconstruye: `docker compose --env-file .env -f docker/docker-compose.yml up -d --build`. Las nuevas columnas de la base de datos se añaden automáticamente y las tareas existentes siguen funcionando (carpeta = raíz, zona horaria = UTC, ZIP = desactivado).

## Licencia

Publicado bajo la [licencia MIT](LICENSE).
