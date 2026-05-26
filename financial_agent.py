#!/usr/bin/env python3
"""
BaezLabs Financial Agent v2
============================
Extracción personalizada por banco:
  - Scotiabank   → PDF adjunto (password = RFC)
  - Liverpool    → Cuerpo del email (HTML/texto)
  - Banregio     → Cuerpo del email (HTML/texto)
  - Hey Banco    → Cuerpo del email (HTML/texto)
  - Amex         → Reenvío manual (instrucciones abajo) o skip

Dependencias:
  pip install google-auth google-auth-oauthlib google-api-python-client \
              anthropic notion-client pypdf pikepdf beautifulsoup4 lxml
"""

import os, json, base64, tempfile, re, argparse
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import pikepdf, pypdf
import anthropic
from notion_client import Client as NotionClient
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────

CONFIG = {
    "rfc":                  os.environ.get("RFC_TITULAR", "YOUR_RFC_HERE"),
    "notion_token":         os.environ.get("NOTION_TOKEN", ""),
    "notion_cards_db":      os.environ.get("NOTION_CARDS_DB", ""),
    "notion_payments_db":   os.environ.get("NOTION_PAYMENTS_DB", ""),
    "calendar_id":          os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
    "anthropic_api_key":    os.environ.get("ANTHROPIC_API_KEY", ""),
    "reminder_days_before": 3,
}

# ─────────────────────────────────────────────
# CONFIGURACIÓN POR BANCO
# ─────────────────────────────────────────────
# source: "pdf" | "email_body"
# gmail_query: búsqueda en Gmail
# last_4_map: mapeo dígitos → nombre legible

BANK_CONFIG = {
    "Scotiabank": {
        "source": "pdf",
        "gmail_query": (
            "from:(scotiabank.com.mx OR notificaciones.scotiabank.com.mx) "
            "subject:(estado de cuenta) newer_than:45d"
        ),
        "pdf_password": CONFIG["rfc"],   # RFC como password
        "last_4_map": {
            "XXXX": "Scotia Card 1",
            "YYYY": "Scotia Card 2",
            "ZZZZ": "Scotia Card 3",
        },
    },
    "Banregio": {
        "source": "email_body",
        "gmail_query": (
            "from:(banregio.com OR banregio.com.mx) "
            "subject:(estado de cuenta) newer_than:45d"
        ),
        "last_4_map": {"XXXX": "Banregio Platinum"},
    },
    "Liverpool": {
        "source": "email_body",
        "gmail_query": (
            "from:(liverpool.com.mx OR tarjetaliverpool.com.mx OR e-liverpool.com.mx) "
            "subject:(estado de cuenta) newer_than:45d"
        ),
        "last_4_map": {"XXXX": "Liverpool VISA"},
    },
    "Hey Banco": {
        "source": "email_body",
        "gmail_query": (
            "from:(heybanco.com OR hola@heybanco.com) "
            "subject:(estado de cuenta) newer_than:45d"
        ),
        "last_4_map": {"XXXX": "Hey Banco Crédito"},
    },
    "Amex": {
        # PDF descargado desde la app Amex y reenviado a tu propio correo
        # Asunto sugerido al reenviar: "Amex estado cuenta"
        "source": "pdf",
        "pdf_password": None,  # PDFs de Amex MX generalmente no tienen password
        "gmail_query": "subject:(amex) newer_than:45d has:attachment",
        "last_4_map": {
            "XXXX": "Amex Card 1",
            "YYYY": "Amex Card 2",
        },
        "skip_if_no_email": True,   # Avisa pero no falla si no reenviaste aún
        "manual_note": (
            "📲 AMEX: Descarga el PDF desde la app → reenvíatelo con asunto\n"
            "   exacto: 'Amex estado cuenta' → el script lo detecta automáticamente"
        ),
    },
}

# ─────────────────────────────────────────────
# PROMPT CLAUDE — extracción estructurada
# ─────────────────────────────────────────────

EXTRACTION_PROMPT = """Eres un asistente especializado en analizar estados de cuenta bancarios mexicanos.

Analiza el texto y extrae los datos financieros. Pueden venir UNA o VARIAS tarjetas.

Devuelve un JSON array (aunque sea una sola tarjeta). Cada objeto debe tener:
{
  "banco": "nombre del banco",
  "ultimos_4_digitos": "4 dígitos",
  "nombre_tarjeta": "nombre del producto",
  "balance_actual": número o null,
  "deuda_total": número o null,        <- "pago para no generar intereses"
  "pago_minimo": número o null,
  "limite_credito": número o null,
  "dia_de_corte": número entero o null,
  "fecha_limite_pago": "YYYY-MM-DD" o null,
  "tiene_msi": true/false,
  "monto_msi_mensual": número o null,
  "notas": "info adicional relevante"
}

Reglas:
- Montos en MXN, solo números sin símbolos ni comas
- "deuda_total" es el monto TOTAL para quedar a cero (no generar intereses)
- Si ves "pago para no generar intereses", "saldo total", "saldo a pagar" → va en deuda_total
- Si el texto viene de email HTML, ignora navegación, botones y publicidad
- SOLO devuelve el JSON array, sin texto adicional ni backticks

Texto a analizar:
"""

# ─────────────────────────────────────────────
# GOOGLE AUTH
# ─────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

def get_google_services():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    gmail    = build("gmail", "v1", credentials=creds)
    calendar = build("calendar", "v3", credentials=creds)
    return gmail, calendar

# ─────────────────────────────────────────────
# GMAIL — buscar emails
# ─────────────────────────────────────────────

def search_emails(gmail, query: str) -> list:
    result = gmail.users().messages().list(userId="me", q=query, maxResults=3).execute()
    return result.get("messages", [])

def get_message_full(gmail, message_id: str) -> dict:
    return gmail.users().messages().get(userId="me", id=message_id, format="full").execute()

# ─────────────────────────────────────────────
# EXTRACCIÓN POR FUENTE
# ─────────────────────────────────────────────

def extract_email_body(msg: dict) -> str:
    """Extrae y limpia el cuerpo de texto de un email (HTML o plain text)."""

    def decode_data(data: str) -> str:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

    def get_parts_text(parts) -> str:
        plain_text = ""
        html_text  = ""
        for part in parts:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data", "")
            sub  = part.get("parts", [])
            if sub:
                sub_plain, sub_html = get_parts_text(sub).split("|||SEP|||")
                plain_text += sub_plain
                html_text  += sub_html
            elif mime == "text/plain" and data:
                plain_text += decode_data(data)
            elif mime == "text/html" and data:
                html_text  += decode_data(data)
        return plain_text + "|||SEP|||" + html_text

    payload = msg.get("payload", {})
    parts   = payload.get("parts", [])

    if parts:
        raw = get_parts_text(parts)
        plain, html = raw.split("|||SEP|||", 1)
    else:
        data  = payload.get("body", {}).get("data", "")
        plain = decode_data(data) if data else ""
        html  = ""

    # Preferir plain text; si no hay, limpiar HTML
    if plain.strip():
        text = re.sub(r"\n{3,}", "\n\n", plain.strip())
        return text[:40000]

    if html.strip():
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        text = re.sub(r"\n{3,}", "\n\n", text.strip())
        return text[:40000]

    return ""

def extract_pdf_text(pdf_bytes: bytes, password: str) -> str:
    """Desbloquea PDF con password (RFC) y extrae texto."""
    passwords = [password, password.upper(), password.lower(), password[:10]]

    tmp_in  = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_out = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_in.write(pdf_bytes)
    tmp_in.close()
    tmp_out.close()

    unlocked = False
    for pwd in passwords:
        try:
            with pikepdf.open(tmp_in.name, password=pwd) as pdf:
                pdf.save(tmp_out.name)
            print(f"    🔓 PDF desbloqueado ({pwd[:4]}****)")
            unlocked = True
            break
        except pikepdf.PasswordError:
            continue
        except Exception as e:
            print(f"    ⚠️  pikepdf error: {e}")
            break

    target = tmp_out.name if unlocked else tmp_in.name
    text = ""
    try:
        with open(target, "rb") as f:
            reader = pypdf.PdfReader(f)
            for page in reader.pages:
                text += (page.extract_text() or "") + "\n"
    except Exception as e:
        print(f"    ⚠️  pypdf error: {e}")

    for path in [tmp_in.name, tmp_out.name]:
        try: os.unlink(path)
        except: pass

    return text[:50000]

def get_pdf_attachments(gmail, msg: dict) -> list[bytes]:
    """Retorna lista de bytes de PDFs adjuntos en el email."""
    pdfs = []
    message_id = msg["id"]

    def process_parts(parts):
        for part in parts:
            if part.get("parts"):
                process_parts(part["parts"])
            mime = part.get("mimeType", "")
            fn   = part.get("filename", "")
            if "pdf" in mime.lower() or fn.lower().endswith(".pdf"):
                att_id = part.get("body", {}).get("attachmentId")
                data   = part.get("body", {}).get("data")
                if att_id:
                    att  = gmail.users().messages().attachments().get(
                        userId="me", messageId=message_id, id=att_id
                    ).execute()
                    data = att.get("data", "")
                if data:
                    pdfs.append(base64.urlsafe_b64decode(data + "=="))

    process_parts(msg.get("payload", {}).get("parts", []))
    return pdfs

# ─────────────────────────────────────────────
# CLAUDE — extraer datos financieros
# ─────────────────────────────────────────────

def extract_with_claude(text: str, client) -> list[dict]:
    """Llama a Claude y parsea el JSON resultante."""
    if not text.strip():
        return []

    response = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 2000,
        messages   = [{"role": "user", "content": EXTRACTION_PROMPT + text}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"```json\s*|\s*```", "", raw).strip()

    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError as e:
        print(f"    ⚠️  Error JSON de Claude: {e} | raw: {raw[:150]}")
        return []

# ─────────────────────────────────────────────
# NOTION — actualizar tarjetas
# ─────────────────────────────────────────────

def find_notion_card(nc, last_4: str) -> str | None:
    res = nc.search(query=f"***{last_4}", filter={"value": "page", "property": "object"})
    for p in res.get("results", []):
        title_prop = p.get("properties", {}).get("title") or p.get("properties", {}).get("Name")
        if not title_prop:
            continue
        titles = title_prop.get("title", [])
        text = titles[0].get("plain_text", "") if titles else ""
        if last_4 in text:
            return p["id"]
    return None

def update_notion_card(nc, page_id: str, d: dict):
    props = {}
    num_fields = {
        "balance_actual":   "Balance Actual",
        "deuda_total":      "Deuda Total",
        "pago_minimo":      "Pago Mínimo",
        "limite_credito":   "Limite de Crédito",
        "monto_msi_mensual":"Intereses Estimados",
    }
    for key, notion_key in num_fields.items():
        if d.get(key) is not None:
            props[notion_key] = {"number": float(d[key])}

    if d.get("dia_de_corte") is not None:
        props["Día de Corte"] = {"number": int(d["dia_de_corte"])}

    if d.get("fecha_limite_pago"):
        props["Fecha Límite de Pago"] = {"date": {"start": d["fecha_limite_pago"]}}

    if d.get("notas"):
        props["Notas"] = {"rich_text": [{"text": {"content": str(d["notas"])[:2000]}}]}

    if props:
        nc.pages.update(page_id=page_id, properties=props)
        print(f"    ✅ Notion: {len(props)} campos actualizados")
    else:
        print(f"    ⚠️  Notion: sin campos para actualizar")

# ─────────────────────────────────────────────
# CALENDAR — crear recordatorios de pago
# ─────────────────────────────────────────────

def create_payment_reminders(cal, d: dict):
    fecha_str = d.get("fecha_limite_pago")
    if not fecha_str:
        print(f"    ⚠️  Sin fecha límite, no se crea recordatorio")
        return

    fecha_pago   = datetime.strptime(fecha_str, "%Y-%m-%d")
    fecha_alerta = fecha_pago - timedelta(days=CONFIG["reminder_days_before"])
    banco  = d.get("banco", "Tarjeta")
    last_4 = d.get("ultimos_4_digitos", "????")
    total  = d.get("deuda_total") or d.get("balance_actual") or 0
    minimo = d.get("pago_minimo") or 0

    desc = (
        f"🏦 {banco} *{last_4}\n"
        f"📅 Vence: {fecha_pago.strftime('%d/%m/%Y')}\n"
        f"💰 Para no generar intereses: ${total:,.2f} MXN\n"
        f"💸 Pago mínimo: ${minimo:,.2f} MXN"
    )

    # Eliminar duplicados del mismo mes
    existing = cal.events().list(
        calendarId   = CONFIG["calendar_id"],
        timeMin      = fecha_alerta.strftime("%Y-%m-%dT00:00:00Z"),
        timeMax      = (fecha_pago + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z"),
        q            = f"{banco} *{last_4}",
        singleEvents = True,
    ).execute()
    for ev in existing.get("items", []):
        cal.events().delete(calendarId=CONFIG["calendar_id"], eventId=ev["id"]).execute()

    # Alerta anticipada
    cal.events().insert(calendarId=CONFIG["calendar_id"], body={
        "summary":     f"💳 PAGO {banco} *{last_4} — vence {fecha_pago.strftime('%d/%m')}",
        "description": desc,
        "start":       {"date": fecha_alerta.strftime("%Y-%m-%d")},
        "end":         {"date": (fecha_alerta + timedelta(days=1)).strftime("%Y-%m-%d")},
        "colorId":     "5",
        "reminders":   {"useDefault": False, "overrides": [
            {"method": "popup", "minutes": 480},
            {"method": "email", "minutes": 1440},
        ]},
    }).execute()

    # Día exacto del vencimiento
    cal.events().insert(calendarId=CONFIG["calendar_id"], body={
        "summary":     f"🚨 HOY VENCE {banco} *{last_4} — ${total:,.0f}",
        "description": desc,
        "start":       {"date": fecha_pago.strftime("%Y-%m-%d")},
        "end":         {"date": (fecha_pago + timedelta(days=1)).strftime("%Y-%m-%d")},
        "colorId":     "11",
        "reminders":   {"useDefault": False, "overrides": [
            {"method": "popup", "minutes": 120},
            {"method": "email", "minutes": 480},
        ]},
    }).execute()

    print(f"    📅 Calendar: alerta {fecha_alerta.strftime('%d/%m')} + vencimiento {fecha_pago.strftime('%d/%m')}")

# ─────────────────────────────────────────────
# CONFIGURACIÓN — recibos de pago
# ─────────────────────────────────────────────

PAYMENT_SOURCES = {
    "Hey Banco": {
        "gmail_queries": [
            "from:noreply@heybanco.com subject:(pago a tarjeta) newer_than:45d",
        ],
    },
    "Banregio": {
        "gmail_queries": [
            "from:noreply@banregio.com subject:(pago a tarjeta) newer_than:45d",
        ],
    },
    "Amex": {
        "gmail_queries": [
            "from:AmericanExpress@welcome.americanexpress.com subject:(pago ha sido recibido) newer_than:45d",
            "from:AmericanExpress@welcome.americanexpress.com subject:(mensaje de servicio) newer_than:45d",
        ],
    },
}

PAYMENT_EXTRACTION_PROMPT = """Eres un asistente especializado en analizar comprobantes de pago de tarjetas de crédito mexicanas.

Analiza el texto y extrae los datos del pago. Puede haber uno o varios pagos en el texto.

Devuelve un JSON array. Cada objeto debe tener exactamente estas claves:
{
  "banco_origen": "nombre del banco que realizó el pago (Hey Banco, Banregio, Amex, etc.)",
  "tarjeta_destino_last4": "últimos 4 dígitos de la tarjeta que recibió el pago",
  "monto": número sin símbolos ni comas,
  "fecha_pago": "YYYY-MM-DD",
  "descripcion": "descripción breve del pago",
  "referencia": "número de referencia u operación si aparece, o null"
}

Reglas:
- Montos en MXN, solo números
- Si no encuentras los últimos 4 dígitos de la tarjeta destino, usa null
- Para emails de Amex, la terminación puede aparecer como 6 dígitos, por ejemplo "terminación: 062005". En ese caso los últimos 4 dígitos son "2005" — usa SIEMPRE solo los últimos 4 dígitos
- SOLO devuelve el JSON array, sin texto adicional ni backticks

Texto a analizar:
"""

# ─────────────────────────────────────────────
# PAGOS — extracción y Notion
# ─────────────────────────────────────────────

def extract_payments_with_claude(text: str, client) -> list[dict]:
    if not text.strip():
        return []
    response = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 1000,
        messages   = [{"role": "user", "content": PAYMENT_EXTRACTION_PROMPT + text}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError as e:
        print(f"    ⚠️  Error JSON de Claude (pagos): {e} | raw: {raw[:150]}")
        return []

def payment_already_exists(nc, last4: str, fecha_pago: str) -> bool:
    """Check notion_payments_db for a payment matching last4 + exact date."""
    res = nc.search(query=f"*{last4}", filter={"value": "page", "property": "object"})
    for p in res.get("results", []):
        if p.get("parent", {}).get("database_id") != CONFIG["notion_payments_db"]:
            continue
        props = p.get("properties", {})
        stored = ((props.get("Fecha de Pago") or {}).get("date") or {}).get("start", "")
        if stored[:10] == fecha_pago[:10]:   # compare date-only; stored may include time
            return True
    return False

def create_notion_payment(nc, p: dict) -> bool:
    """Crea una página de pago en notion_payments_db. Devuelve True si se creó."""
    last4       = p.get("tarjeta_destino_last4") or "????"
    fecha_iso   = p.get("fecha_pago") or ""
    banco       = p.get("banco_origen", "Banco")
    monto       = p.get("monto") or 0
    descripcion = p.get("descripcion") or ""

    try:
        dt = datetime.strptime(fecha_iso, "%Y-%m-%d")
        fecha_titulo = dt.strftime("%-d/%b/%Y")
    except ValueError:
        fecha_titulo = fecha_iso

    title = f"Pago {banco} *{last4} - {fecha_titulo}"

    if payment_already_exists(nc, last4, fecha_iso):
        print(f"    ⏭️  Ya existe en Notion: {title}")
        return False

    props = {
        "Descripción":  {"title": [{"text": {"content": title}}]},
        "Monto Pagado": {"number": float(monto)},
        "Notas":        {"rich_text": [{"text": {"content": f"{banco}: {descripcion}"[:2000]}}]},
    }
    if fecha_iso:
        props["Fecha de Pago"] = {"date": {"start": fecha_iso}}

    nc.pages.create(
        parent     = {"database_id": CONFIG["notion_payments_db"]},
        properties = props,
    )
    print(f"    ✅ Notion: página creada — {title}")
    return True

def process_payment_receipts(gmail, nc, ac) -> list[dict]:
    payment_summary = []

    print(f"\n{'═'*50}")
    print("💸 RECIBOS DE PAGO")
    print(f"{'═'*50}")

    for bank_name, pcfg in PAYMENT_SOURCES.items():
        print(f"\n  🏦 {bank_name}")

        # Collect unique messages across all queries for this bank
        seen_ids = set()
        unique = []
        for query in pcfg["gmail_queries"]:
            result = gmail.users().messages().list(
                userId="me", q=query, maxResults=10
            ).execute()
            for m in result.get("messages", []):
                if m["id"] not in seen_ids:
                    seen_ids.add(m["id"])
                    unique.append(m)

        if not unique:
            print(f"     ⚠️  Sin comprobantes encontrados")
            continue

        print(f"     📧 {len(unique)} email(s) encontrado(s)")

        seen_payments = set()
        for msg_ref in unique:
            msg  = get_message_full(gmail, msg_ref["id"])
            text = extract_email_body(msg)
            if not text.strip():
                continue

            payments = extract_payments_with_claude(text, ac)
            for p in payments:
                p["banco_origen"] = bank_name  # always the source, not Claude's guess
                last4 = p.get("tarjeta_destino_last4") or "????"
                fecha = p.get("fecha_pago", "N/A")
                payment_key = (last4, fecha)
                if payment_key in seen_payments:
                    continue
                seen_payments.add(payment_key)
                monto = p.get("monto") or 0
                print(f"\n     → *{last4}  ${monto:,.2f} MXN  {fecha}")
                created = create_notion_payment(nc, p)
                payment_summary.append({
                    "banco":  bank_name,
                    "last4":  last4,
                    "monto":  monto,
                    "fecha":  fecha,
                    "nuevo":  created,
                })

    return payment_summary

# ─────────────────────────────────────────────
# CRUCE PAGOS vs ESTADOS DE CUENTA
# ─────────────────────────────────────────────

def fetch_notion_payments_for_card(nc, last4: str, after_date) -> list[dict]:
    """Search Notion for payment pages for this card recorded after after_date.

    Accepts pages from either the dedicated payments DB (Monto Pagado / Fecha de Pago)
    or the cards DB used by older script versions (Monto / Fecha).
    """
    from datetime import date as date_cls

    res = nc.search(query=f"*{last4}", filter={"value": "page", "property": "object"})
    payments = []
    seen: set = set()

    for p in res.get("results", []):
        parent_db = p.get("parent", {}).get("database_id", "")
        # Only consider pages that live in a known payments database
        if parent_db not in (CONFIG["notion_payments_db"], CONFIG["notion_cards_db"]):
            continue

        props = p.get("properties", {})

        # Support the real payments DB schema (Monto Pagado / Fecha de Pago)
        # and the older script-created schema (Monto / Fecha)
        monto_prop = props.get("Monto Pagado") or props.get("Monto") or {}
        fecha_prop  = props.get("Fecha de Pago") or props.get("Fecha") or {}

        monto = monto_prop.get("number") or 0
        fecha_str = (fecha_prop.get("date") or {}).get("start")

        if not fecha_str or not monto:
            continue

        if after_date:
            try:
                if date_cls.fromisoformat(fecha_str) <= after_date:
                    continue
            except ValueError:
                continue

        key = (last4, fecha_str)
        if key not in seen:
            seen.add(key)
            payments.append({"last4": last4, "monto": monto, "fecha": fecha_str})

    return payments


def cross_reference_payments(nc, card_summary: list, payment_summary: list) -> list:
    from datetime import date as date_cls

    print("\n" + "="*60)
    print("🔄 CRUCE: PAGOS vs ESTADOS DE CUENTA")
    print("="*60)

    today = date_cls.today()
    applied = []

    for card in card_summary:
        if not card.get("page_id"):
            continue

        last4      = card["last4"]
        dia_corte  = card.get("dia_de_corte")
        deuda      = card["deuda"]

        # Derive the most recent statement cut date from dia_de_corte
        corte = None
        if dia_corte:
            try:
                if today.day >= dia_corte:
                    corte = date_cls(today.year, today.month, dia_corte)
                else:
                    if today.month == 1:
                        corte = date_cls(today.year - 1, 12, dia_corte)
                    else:
                        corte = date_cls(today.year, today.month - 1, dia_corte)
            except ValueError:
                corte = None

        # Query Notion for all payments for this card recorded after the cut date.
        # This is the source of truth — catches payments from previous runs too.
        notion_payments = fetch_notion_payments_for_card(nc, last4, corte)

        # Merge with in-memory payments from the current run (may include ones
        # just created that haven't been committed long enough to query reliably).
        seen_keys: set = set()
        post_cut: list = []
        for p in notion_payments:
            key = (p["last4"], p["fecha"])
            if key not in seen_keys:
                seen_keys.add(key)
                post_cut.append(p)
        for p in payment_summary:
            if p["last4"] != last4:
                continue
            try:
                if corte and date_cls.fromisoformat(p["fecha"]) <= corte:
                    continue
            except ValueError:
                pass
            key = (p["last4"], p["fecha"])
            if key not in seen_keys:
                seen_keys.add(key)
                post_cut.append(p)

        if not post_cut:
            continue

        total_paid  = sum(p["monto"] for p in post_cut)
        new_balance = max(0.0, deuda - total_paid)

        print(f"\n  💳 {card['tarjeta']} — {card['nombre']}")
        if corte:
            print(f"     Fecha de corte:  {corte.strftime('%d/%m/%Y')}")
        print(f"     Deuda en estado: ${deuda:,.2f} MXN")
        for p in post_cut:
            print(f"     Pago post-corte: ${p['monto']:,.2f} MXN  ({p['fecha']})")
        print(f"     Nuevo balance:   ${new_balance:,.2f} MXN")

        nota = (
            f"Pago(s) post-corte: ${total_paid:,.2f} MXN aplicado(s) el "
            f"{today.strftime('%d/%m/%Y')}. Balance ajustado a ${new_balance:,.2f} MXN."
        )
        nc.pages.update(
            page_id    = card["page_id"],
            properties = {
                "Balance Actual": {"number": new_balance},
                "Notas": {"rich_text": [{"text": {"content": nota[:2000]}}]},
            },
        )
        print(f"     ✅ Notion: Balance Actual y Notas actualizados")

        applied.append({
            "tarjeta":      card["tarjeta"],
            "nombre":       card["nombre"],
            "deuda_orig":   deuda,
            "pagado":       total_paid,
            "nuevo_balance": new_balance,
        })

    if not applied:
        print("   Sin pagos post-corte encontrados para cruzar.")
    print("="*60)
    return applied

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def _setup():
    """Authenticate and return (gmail, calendar, nc, ac)."""
    print("🔐 Autenticando Google...")
    gmail, calendar = get_google_services()
    nc = NotionClient(auth=CONFIG["notion_token"])
    ac = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])
    print("✅ Listo\n")
    return gmail, calendar, nc, ac


def run_statements_mode(gmail, calendar, nc, ac) -> list:
    """Process bank statements → Notion + Calendar reminders. Returns card summary."""
    summary = []

    for bank_name, bcfg in BANK_CONFIG.items():
        print(f"{'─'*50}")
        print(f"🏦 {bank_name}  [{bcfg['source']}]")

        emails = search_emails(gmail, bcfg["gmail_query"])
        if not emails:
            if bcfg.get("skip_if_no_email"):
                note = bcfg.get("manual_note", "")
                print(f"   ⚠️  Sin email de {bank_name} encontrado")
                if note:
                    print(f"   {note}")
            else:
                print(f"   ⚠️  Sin emails encontrados en Gmail\n")
            continue

        msg = get_message_full(gmail, emails[0]["id"])
        print(f"   📧 Email encontrado")

        text = ""

        if bcfg["source"] == "pdf":
            pdfs = get_pdf_attachments(gmail, msg)
            if not pdfs:
                print(f"   ⚠️  Email sin PDF adjunto")
                text = extract_email_body(msg)
                print(f"   ↩️  Usando cuerpo del email como fallback")
            else:
                for i, pdf_bytes in enumerate(pdfs, 1):
                    print(f"   📄 PDF {i}/{len(pdfs)} — extrayendo texto...")
                    pwd = bcfg.get("pdf_password") or ""
                    text += extract_pdf_text(pdf_bytes, pwd)

        elif bcfg["source"] == "email_body":
            text = extract_email_body(msg)
            print(f"   📝 Cuerpo extraído ({len(text):,} chars)")

        if not text.strip():
            print(f"   ❌ Sin texto extraíble\n")
            continue

        print(f"   🧠 Analizando con Claude...")
        cards = extract_with_claude(text, ac)

        if not cards:
            print(f"   ❌ Claude no pudo extraer datos\n")
            continue

        print(f"   💳 {len(cards)} tarjeta(s) identificada(s)")

        for card in cards:
            if not card.get("ultimos_4_digitos") and len(bcfg["last_4_map"]) == 1:
                card["ultimos_4_digitos"] = next(iter(bcfg["last_4_map"]))
            last_4 = card.get("ultimos_4_digitos", "")
            nombre = bcfg["last_4_map"].get(last_4, card.get("nombre_tarjeta", ""))
            print(f"\n   → {nombre or bank_name} *{last_4}")
            print(f"     Deuda total:  ${card.get('deuda_total') or 0:,.2f}")
            print(f"     Pago mínimo:  ${card.get('pago_minimo') or 0:,.2f}")
            print(f"     Vence:        {card.get('fecha_limite_pago', 'N/A')}")

            page_id = find_notion_card(nc, last_4)
            if page_id:
                update_notion_card(nc, page_id, card)
            else:
                print(f"    ⚠️  *{last_4} no encontrada en Notion")

            create_payment_reminders(calendar, card)

            summary.append({
                "banco":        bank_name,
                "tarjeta":      f"*{last_4}",
                "last4":        last_4,
                "nombre":       nombre,
                "deuda":        card.get("deuda_total") or 0,
                "minimo":       card.get("pago_minimo") or 0,
                "vence":        card.get("fecha_limite_pago"),
                "dia_de_corte": card.get("dia_de_corte"),
                "page_id":      page_id,
                "ok":           bool(page_id),
            })

        print()

    return summary


def print_statements_summary(summary: list, cross_summary: list):
    adjusted = {r["tarjeta"]: r["nuevo_balance"] for r in cross_summary}
    print("\n" + "="*60)
    print("📊 RESUMEN — ESTADOS DE CUENTA")
    print("="*60)
    total = 0
    for r in summary:
        balance = adjusted.get(r["tarjeta"], r["deuda"])
        if balance == 0.0:
            continue
        icon = "✅" if r["ok"] else "⚠️ "
        total += balance
        tag = "  ✔ pagado parcial" if r["tarjeta"] in adjusted else ""
        print(f"{icon} {r['banco']} {r['tarjeta']} — ${balance:>10,.2f} MXN — vence {r.get('vence','?')}{tag}")
    print(f"\n💰 TOTAL PENDIENTE REAL: ${total:,.2f} MXN")
    print("="*60)


def print_payments_summary(payment_summary: list, cross_summary: list):
    print("\n" + "="*60)
    print("💸 RESUMEN — PAGOS REGISTRADOS")
    print("="*60)
    total_pagado = 0
    for r in payment_summary:
        icon = "🆕" if r["nuevo"] else "⏭️ "
        total_pagado += r["monto"]
        print(f"{icon} {r['banco']} *{r['last4']} — ${r['monto']:>10,.2f} MXN — {r['fecha']}")
    if payment_summary:
        print(f"\n💸 TOTAL PAGADO: ${total_pagado:,.2f} MXN")
    else:
        print("   Sin pagos encontrados")
    print("="*60)

    if cross_summary:
        print("\n" + "="*60)
        print("🔄 RESUMEN — BALANCES AJUSTADOS POR PAGOS")
        print("="*60)
        for r in cross_summary:
            print(f"  💳 {r['tarjeta']} {r['nombre']}")
            print(f"     ${r['deuda_orig']:,.2f}  →  ${r['nuevo_balance']:,.2f}  (pagado ${r['pagado']:,.2f})")
        print("="*60)


def run():
    parser = argparse.ArgumentParser(description="BaezLabs Financial Agent v2")
    parser.add_argument(
        "--mode",
        choices=["statements", "payments", "all"],
        default="all",
        help="statements: estados de cuenta + Notion + Calendar | "
             "payments: recibos de pago + cruce | "
             "all: todo (default)",
    )
    args = parser.parse_args()
    mode = args.mode

    print("\n" + "="*60)
    print("🤖 BaezLabs Financial Agent v2")
    print(f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')} CST")
    print(f"🔧 Modo: {mode}")
    print("="*60 + "\n")

    for key in ["anthropic_api_key", "notion_token"]:
        if not CONFIG[key]:
            raise ValueError(f"❌ Variable de entorno faltante: {key.upper()}")

    gmail, calendar, nc, ac = _setup()

    if mode == "statements":
        summary       = run_statements_mode(gmail, calendar, nc, ac)
        cross_summary = cross_reference_payments(nc, summary, [])
        print_statements_summary(summary, cross_summary)

    elif mode == "payments":
        payment_summary = process_payment_receipts(gmail, nc, ac)
        cross_summary   = cross_reference_payments(nc, [], payment_summary)
        print_payments_summary(payment_summary, cross_summary)

    else:  # all
        summary         = run_statements_mode(gmail, calendar, nc, ac)
        payment_summary = process_payment_receipts(gmail, nc, ac)
        cross_summary   = cross_reference_payments(nc, summary, payment_summary)
        print_statements_summary(summary, cross_summary)
        print_payments_summary(payment_summary, cross_summary)

    print()


if __name__ == "__main__":
    run()
