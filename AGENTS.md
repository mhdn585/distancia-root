# AGENTS.md — RemotePS

Guía de contexto completo del proyecto para agentes de IA y desarrolladores.
Leer antes de tocar cualquier archivo.

## 1. Qué es esto

Herramienta multiplataforma de administración remota por terminal. Un PC
"cliente" (casa) controla una terminal PowerShell de un PC "servidor" (trabajo)
a través de internet, sin abrir puertos en routers/firewalls.

- 100% Python. `servidor.py` usa solo la librería estándar (cero dependencias,
  para instalarlo en cualquier PC corporativa).
- `cliente.py` usa `prompt_toolkit` (única dependencia, solo en la PC del cliente).
- La conectividad entre redes distintas la resuelve **cloudflared** (quick
  tunnel), no código nuestro.

## 2. Arquitectura

```
PC DE CASA (cliente)                 INTERNET                 PC DEL TRABAJO (servidor)
┌────────────────┐         ┌──────────────────────┐          ┌──────────────────────────┐
│  cliente.py    │──HTTPS──▶   Cloudflare Edge    │══túnel══▶│ cloudflared              │
│  TUI local     │  POST    /trycloudflare.com/   │ saliente │    │ reenvía               │
│  168x63        │◀── JSON ─  (identifica túnel)  │◀═════════│    ▼                      │
└────────────────┘                                │          │ servidor.py               │
   bucle REPL                                     │          │   localhost:8080          │
   envía cmd / recibe stdout+stderr+rc            │          │   (solo loopback)         │
                                                  │          │    │ subprocess           │
                                                  │          │    ▼                      │
                                                  │          │ powershell.exe -Encoded…  │
                                                  └──────────┴──────────────────────────┘
```

- `servidor.py` **nunca** escucha en `0.0.0.0`: solo `127.0.0.1:8080`. Desde
  internet es invisible; la única puerta es el túnel de Cloudflare.
- `cloudflared` abre una conexión **saliente** hacia Cloudflare (por eso no
  necesita reglas de firewall). El tráfico del cliente entra por esa conexión ya
  abierta y se reenvía a localhost.
- El túnel quick es efímero: cada ejecución de cloudflared devuelve una URL
  nueva `https://xxxx.trycloudflare.com` que hay que pasarle al cliente.

## 3. Archivos del repo

| Archivo | Rol |
|---|---|
| `servidor.py` | HTTP server + ejecutor PowerShell + API de archivos. Corre en Windows. |
| `cliente.py` | TUI prompt_toolkit: REPL + explorador de archivos + editor. Corre en cualquier SO. |
| `requirements.txt` | Deps del cliente únicamente (`prompt_toolkit`). |
| `tests/test_servidor.py` | Unit tests de servidor.py (sin sockets). |
| `tests/test_cliente.py` | Unit tests de lógica pura del cliente (sin TTY, mocks). |
| `tests/test_integration.py` | E2E real: servidor HTTP en vivo + `/bin/sh` como shell sustituta. |

## 4. Contrato de la API HTTP

Todas las respuestas son JSON (excepto `/fs/download`, que devuelve bytes crudos).
Todos los endpoints POST esperan `Content-Type: application/json`.

| Endpoint | Petición | Respuesta |
|---|---|---|
| `GET /ping` | — | `{"ok":true,"hostname","user","platform","shell","cwd"}` |
| `POST /execute` | `{"cmd":"str","timeout":60}` | `{"stdout","stderr","rc","cwd","truncated","elapsed"}` |
| `POST /fs/list` | `{"path":"(opcional, rel. a cwd)"}` | `{"path","parent","entries":[{name,dir,size,mtime}]}` (dirs primero, A→Z) |
| `POST /fs/read` | `{"path"}` | `{"path","content","size","truncated"}` (texto UTF-8, tope 2 MB) |
| `POST /fs/write` | `{"path","content"}` | `{"ok","path","bytes"}` |
| `POST /fs/delete` | `{"path","recursive"}` | `{"ok","path"}` — rechaza borrar la raíz |
| `POST /fs/mkdir` | `{"path"}` | `{"ok","path"}` (crea padres) |
| `POST /fs/download` | `{"path"}` | bytes crudos (`application/octet-stream`) |
| `POST /fs/upload` | header `X-Remote-Path` (URL-quoteado) + bytes crudos del body | `{"ok","path","bytes"}` |

Errores: siempre JSON `{"error":"mensaje"}` con status HTTP correcto
(400 body/path inválido, 401 sin token, 404 ruta inexistente, 403 permisos,
404 endpoint desconocido, 413 archivo gigante, 500 interno).

### Mecánica de `/execute` (clave del diseño)

1. `wrap_script(cmd)` genera un micro-script que, después del comando del
   usuario, imprime una **línea centinela** con el estado:
   `__REMOTEPS_META__|<cwd-efectivo>|<rc>`
   (en PowerShell: `Get-Location` + `$LASTEXITCODE`/`$?`; en POSIX: `$PWD` + `$?`).
2. PowerShell recibe el script entero con `-EncodedCommand` (base64 de
   UTF-16LE): elimina por completo el problema del quoting/escapes/comillas.
3. El encabezado del script fuerza `[Console]::OutputEncoding=UTF8` para que
   la salida redirigida no salga en código de página OEM (acentos/ñ).
4. `subprocess` corre con el cwd de sesión, `CREATE_NO_WINDOW` (Win) /
   `start_new_session` (POSIX), `communicate(timeout)`.
5. Al expirar el timeout: `taskkill /T /F` (Win) o `killpg(SIGKILL)` (POSIX) →
   mata el **árbol completo** (sin eso, un `sleep 5` hijo mantiene el pipe
   abierto y el servidor espera 5s igualmente; bug ya encontrado y corregido).
6. `parse_meta()` extrae la última línea centinela, devuelve stdout limpio y
   **persiste el cwd** en el estado global del servidor. Por eso un simple
   `cd C:\otra\ruta` "funciona como en una terminal real" aunque cada comando
   sea un proceso nuevo.
7. Salida > 512 KB se trunca conservando principio y fin (el centinela está al
   final, así que sobrevive al truncado). El cliente ve `"truncated": true`.

### Estado del servidor

- `_session = {"cwd": ...}` protegido con `_lock` (es un ThreadingHTTPServer).
- Si el cwd guardado desaparece (borraron la carpeta), el siguiente spawn falla
  → el servidor resetea a `~` y reintenta una vez. Evita deadlock.
- La sesión PowerShell NO es stateful: las variables no persisten entre
  comandos. Decisión deliberada (simpleza + robustez) ver §7.

### Autenticación

`check_auth()` lee `REMOTEPS_TOKEN` al arrancar. Si está vacío: sin auth (la
URL aleatoria del túnel es la única credencial — opción elegida por el usuario).
Si tiene valor: exige header `Authorization: Bearer <token>` en CUALQUIER
request, incluso `/ping`. Para activarla: setear la variable de entorno antes
de lanzar el servidor. No requiere tocar el código.

## 5. Cliente: estructura y TUI

Adaptado a terminal 168×63 (pero se degrada sin romperse a otros tamaños).

- **REPL** (PromptSession): prompt `[hostname cwd] >`, historial persistente
  (`~/.remoteps-history`), autocompletado de `/comandos`, `bottom_toolbar` con
  estado (online/offline, URL, cwd remoto, último rc/tiempo).
- Cualquier línea sin `/` al inicio se envía cruda a `/execute` (PS 5.1).
- Todo lo que devuelve el servidor se pinta con ANSI: stdout normal, stderr
  rojo, `[rc=N]` rojo si ≠ 0, aviso amarillo si hubo truncado.
- **Explorador** (`/ls`): pantalla completa (Application de prompt_toolkit).
  ↑↓ mover · Enter/→ entrar o preview de texto · ←/Backspace padre · `d` bajar
  archivo · `u` subir (pide ruta local) · `m` mkdir · `x` borrar (segunda `x`
  confirma) · `e` editar · `r` recargar · `q`/Esc salir.
- **Editor** (`/edit` o `e`): `/fs/read` → archivo temporal con el basename
  real → `$EDITOR` (Windows: `notepad /wait` vía `start`) → si cambió,
  `/fs/write` de vuelta.
- `do_get`/`put`: subida/bajada cruda (bytes, sin base64).
- Comandos: `/salir /ping /cd /ls /cat /edit /get /put /rm /mkdir /url /timeout
  /clear /help` + también `salir` sin barra.

## 6. Cómo ejecutar

El modo recomendado es **URL fija** (named tunnel con dominio propio o gratis
`.eu.org`). Quick tunnel queda como alternativa exprés sin cuenta.

### 6.1 URL FIJA — named tunnel (config única, luego todo automático)

**Requisitos:** cuenta Cloudflare gratuita + 1 dominio en esa cuenta.
Sin comprar dominio: registrá `algo.eu.org` (gratis en
https://register.eu.org, tarda horas/días en aprobarse) y conectalo a
Cloudflare como sitio.

Una sola vez, en la PC del trabajo (Windows):
```bat
:: 1) binario: winget install --id Cloudflare.cloudflared  (o el .msi oficial)
cloudflared tunnel login                :: abre navegador: elegi cuenta+dominio
cloudflared tunnel create remoteps      :: genera %USERPROFILE%\.cloudflared\remoteps.json
cloudflared tunnel route dns remoteps pc-trabajo.tudominio.eu.org

:: 2) crear %USERPROFILE%\.cloudflared\config.yml con:
::    tunnel: remoteps
::    credentials-file: C:\Users\<user>\.cloudflared\remoteps.json
::    ingress:
::      - hostname: pc-trabajo.tudominio.eu.org
::        service: http://localhost:8080
::      - service: http_status:404

:: 3) autoarranque del tuneL como SERVICIO de Windows (run like Administrator):
cloudflared service install

:: 4) token compartido (ambas PCs) y autoarranque del servidor:
setx REMOTEPS_TOKEN "token-largo-aleatorio"
schtasks /Create /TN "RemotePS-Servidor" /TR "python C:\ruta\servidor.py" /SC ONSTART /RU SYSTEM /F
```

Cada sesión, desde casa (ya no hay URLs que copiar — es SIEMPRE la misma):
```bash
export REMOTEPS_URL=https://pc-trabajo.tudominio.eu.org
export REMOTEPS_TOKEN="token-largo-aleatorio"     # o --token en CLI
.venv/bin/python cliente.py
```
El cliente manda `Authorization: Bearer <token>` en TODOS los requests
(env `REMOTEPS_TOKEN` o `--token`). El servidor responde 401 si falta o difiere.

### 6.2 Exprés — quick tunnel (sin cuenta ni dominio, URL cambia cada vez)

**PC del trabajo:**
```bat
set REMOTEPS_TOKEN=            (opcional: definir para exigir Bearer token)
python servidor.py             (como Administrador si hace falta elevar)
cloudflared.exe tunnel --url http://localhost:8080
:: copiar la URL https://xxxx.trycloudflare.com que imprime cloudflared
```

**PC de casa:**
```bash
.venv/bin/python cliente.py --url https://xxxx.trycloudflare.com
# o: REMOTEPS_URL=https://xxxx... python cliente.py
```

**Cierre (solo modo exprés):** `salir` en el cliente → Ctrl+C en cloudflared →
Ctrl+C en servidor. En modo fijo no se cierra nada: todo queda corriendo como
servicio/tarea.

Requisitos previos: solo cloudflared instalado en la PC del trabajo
(https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/).

Limitación del modo fijo: hay que encender la PC del trabajo físicamente (el
servicio arranca con Windows). "Despertarla" desde casa queda como mejora
futura: Wake-on-LAN con un segundo dispositivo en la LAN.

## 7. Decisiones de diseño (y por qué)

| Decisión | Justificación |
|---|---|
| Solo stdlib en servidor | PC corporativa sin permisos para pip install. Cero fricción. |
| Quick tunnel (URL aleatoria/sesión) | Cero configuración, sin cuenta. Es la opción exprés; la fija usa named tunnel (ver §6.1). |
| Proceso PS nuevo por comando + cwd persistente | Stateful de verdad (un powershell residente) exige framing ad-hoc para detectar fin de salida y riesgo de bloqueo. `cd` persistente cubre el 95% del valor. |
| `-EncodedCommand` base64 | Único método que escapa 100% de comillas/dólares/backticks sin bugs. |
| Sin auth por elección del usuario | Ver §9 — es el punto más débil del sistema. |
| Envío binario crudo en upload/download | 33% más chico que base64 y el servidor puede streaming a temp file. |
| Timeout cliente 90s default | Quick tunnels cierran conexiones idle a los ~100 s. `/timeout` permite 1–300 (el servidor clampa a 300). |
| `/bin/sh` como shell de tests (var `REMOTEPS_SHELL`) | Permite testear toda la lógica HTTP/cwd/truncado/timeout en Linux sin Windows. |

## 8. Tests

```bash
.venv/bin/python -m unittest discover tests -v        # 54 tests, ~3 s
```

| Suite | Qué cubre |
|---|---|
| `test_servidor.py` | `build_command` (posix + rama PowerShell con `-EncodedCommand`/UTF-16LE/base64), `parse_meta` (centinela, rutas con `\|`, ausencia), `_cap` (truncado preserva el final), `expand_path` (relativo/abs/root/null-byte), fs_* directas (roundtrip, orden dirs, mkdir recursivo, bloqueo raíz), `run_command` real vía `/bin/sh` (echo, stderr+rc≠0, comando inexistente, **persistencia de cd**, **timeout mata en <6s**, salida 600 KB truncada). |
| `test_cliente.py` | `parse_input` (remoto vs `/local` vs `salir`), `join_url`, `human_size`, `shorten`, `remote_pjoin`, `local_basename`, `format_result` (rc/stderr/truncado), transporte `Remote._req` con `urlopen` mockeado: JSON/bytes/URLError→"sin conexion"/HTTPError con JSON de error/`X-Remote-Path` en upload, tokens del toolbar. |
| `test_integration.py` | Servidor HTTP real en puerto efímero con `/bin/sh`: 11 tests E2E — ping, execute ok/comillas raras/error, cd persistente, timeout, truncado, ciclo completo fs (mkdir→write→read→list→upload→download byte-exact→delete), 404/400, token 401+200, bloqueo delete raíz. |

Limitaciones conocidas de los tests:
- `test_integration.py` corre solo en POSIX (requiere `/bin/sh`); se salta en Windows.
- La TUI (explorador/editor) no tiene tests automatizados: se valida con el
  smoke test pty manual de abajo y a mano.

### Smoke test de TUI bajo pseudo-terminal

```bash
REMOTEPS_SHELL=/bin/sh REMOTEPS_PORT=18080 python servidor.py &   # servidor fake
python cliente.py --url http://127.0.0.1:18080                    # usar /ls, /ping, etc.
```
Nota descubierta: `bottom_toolbar` de prompt_toolkit solo se pinta si el
terminal responde CPR (`ESC[6n`). Un harness pty que no responde CPR hace
desaparecer el toolbar tras ~2 s — es limitación del harness, no del código.

## 9. Seguridad (leer antes de exponer)

- **La URL del túnel ES la credencial** (se eligió sin auth). Cualquiera que la
  vea (historial, share, logs) puede ejecutar **cualquier cosa** en la PC del
  trabajo, con los privilegios del proceso servidor (Admin = control total).
- Mitigaciones recomendadas:
  1. Setear `REMOTEPS_TOKEN` en ambas PCs (activa Bearer en servidor; el
     cliente aún no lo envía → agregar en `Remote._req` headers).
  2. Cloudflare Access (Zero Trust) delante del hostname → auth real gratis.
  3. No compartir la URL; considerarla secreta y rotar (reiniciar cloudflared).
- Riesgos heredados del concepto: el servidor ejecuta sin allowlist ni
  confirmación. Un `Remove-Item C:\ -Recurse` viaja igual que un `Get-Process`.
- El truncado de salida (512 KB) y los timeouts evitan DoS accidental al cliente.
- Uploads al tope de 200 MB; `/fs/read` al tope de 2 MB por request (preview).

## 10. Configuración (variables de entorno)

Servidor: `REMOTEPS_PORT` (8080), `REMOTEPS_TOKEN` (""), `REMOTEPS_SHELL`
(powershell.exe / /bin/sh en tests).
Cliente: `REMOTEPS_URL` (URL del túnel), `EDITOR` (para /edit).

## 11. Mejoras futuras (no implementadas)

- Named tunnel con hostname fijo (cuenta Cloudflare + dominio) → URL permanente.
- Token Bearer también en el cliente (env `REMOTEPS_TOKEN` + header) + doc.
- Sesión PowerShell residente stateful (variables/jobs persisten).
- Streaming de salida larga (chunked) en vez de esperar el fin del comando.
- Historial de comandos por-PC, sincronizar cwd con `!` del prompt de PS.
- Empaquetado `.exe` con PyInstaller para la PC del trabajo.
- Transferencias con progreso y hashing (verificación íntegra).
