# Política de privacidad · Privacy policy

*Vigente desde el 7 de noviembre de 2026 · Effective November 7, 2026*

Esta política cubre el servidor MCP público `https://indec-mcp-production.up.railway.app/mcp` («el servicio»). El servicio es un proyecto independiente y **no oficial**: no está afiliado al INDEC.

## Español

**Qué datos recibe el servicio.** El servicio solo recibe las consultas de datos que tu asistente de IA le envía, por ejemplo: «inflación de Alimentos desde 2025» o «exportaciones a China en 2025». No recibe tu conversación, tu nombre ni tu cuenta. No requiere registro ni inicio de sesión.

**Qué guardamos y para qué.** Para medir el uso del servicio, cuidar su estabilidad y prevenir abusos, guardamos un registro por cada pedido con:
- un **identificador seudónimo**: un hash HMAC-SHA256 de la dirección IP de la conexión, calculado con una clave secreta. **La dirección IP en sí no se guarda**;
- el nombre de la aplicación cliente que ella misma informa (por ejemplo, «claude-code»);
- la herramienta usada y un **tema general** derivado de sus parámetros estructurados (por ejemplo, «Núcleo@GBA» o «China»). **Nunca se guarda el texto de las consultas SQL** (solo los nombres de las tablas consultadas);
- la fecha y hora, si hubo error, el tiempo de respuesta y si el pedido llegó desde los servidores de un proveedor de IA (Anthropic u OpenAI), según sus rangos de IP publicados.

Para aplicar los límites de uso, el servicio mantiene en memoria contadores por conexión, que se pierden al reiniciarse. No usamos cookies ni rastreadores, y no creamos perfiles.

**Dónde se guarda.** En una base de datos del propio servicio, alojada en Railway (Railway Corporation, Estados Unidos). Railway puede llevar sus propios registros técnicos de red según [su política de privacidad](https://railway.com/legal/privacy).

**Con quién compartimos.** Con nadie. No vendemos, cedemos ni compartimos estos datos con terceros, salvo que la ley lo exija. Las estadísticas agregadas (por ejemplo, «X consultas sobre inflación este mes») pueden publicarse.

**Cuánto tiempo.** Los registros de uso se borran automáticamente a los **90 días**.

**Tu asistente de IA.** Tu conversación la procesa la aplicación que uses (Claude, ChatGPT, Cursor…), según sus propias políticas. Este servicio no tiene acceso a ella.

**Contacto.** Para consultas sobre privacidad, o para pedir que eliminemos datos, abrí un issue en <https://github.com/MagcDEV/indec-mcp/issues>. Como no guardamos direcciones IP ni datos de identidad, en general no podemos asociar registros a una persona en particular.

**Cambios.** Si esta política cambia, se actualizará este archivo, y su historial queda visible en GitHub.

## English

**What the service receives.** The service only receives the data queries your AI assistant sends it, for example "Food inflation since 2025" or "exports to China in 2025". It never receives your conversation, name or account, and it needs no signup or login.

**What we store and why.** To measure usage, keep the service stable and prevent abuse, we store one record per request with:
- a **pseudonymous ID**: an HMAC-SHA256 of the connection's IP address, computed with a secret key. **The IP address itself is not stored**;
- the client application's self-reported name (for example "claude-code");
- the tool used and a **coarse topic** derived from its structured arguments (for example "Núcleo@GBA" or "China"). **SQL query text is never stored**, only the names of the tables it queried;
- the timestamp, whether the request failed, the latency, and whether the request came from an AI provider's servers (Anthropic or OpenAI), based on their published IP ranges.

To enforce usage limits, the service keeps per-connection counters in memory, which are lost when it restarts. There are no cookies or trackers, and no profiling.

**Where it's stored.** In the service's own database, hosted on Railway (Railway Corporation, United States). Railway may keep its own network logs under [its privacy policy](https://railway.com/legal/privacy).

**Sharing.** None. We don't sell, transfer or share this data with third parties, unless the law requires it. Aggregate statistics (for example "X inflation queries this month") may be published.

**Retention.** Usage records are deleted automatically after **90 days**.

**Your AI assistant.** Your conversation is processed by the app you use (Claude, ChatGPT, Cursor…) under its own policies. This service has no access to it.

**Contact.** For privacy questions or deletion requests, open an issue at <https://github.com/MagcDEV/indec-mcp/issues>. Because we store no IP addresses or identity data, we generally can't link records to a specific person.

**Changes.** If this policy changes, this file is updated, and its history is visible on GitHub.
