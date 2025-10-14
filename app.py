# app.py
# WhatsApp Cloud API + Flask + SQLite
# Flujo con confirmación ("sí/no") y expiración de sesión

import os, re, sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests

load_dotenv()

# ---- ENV ----
WA_TOKEN        = os.getenv("WA_TOKEN", "")
WA_PHONE_ID     = os.getenv("WA_PHONE_ID", "")
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "verify_me")
MAKE_WEBHOOK    = os.getenv("MAKE_WEBHOOK_URL", "")

CLABE           = os.getenv("CLABE", "012345678901234567")
BANCO           = os.getenv("BANCO", "BBVA")
BENEFICIARIO    = os.getenv("BENEFICIARIO", "TU BODEGA SA DE CV")

DB_PATH         = os.getenv("DB_PATH", "bot.db")

# ---- Flask ----
app = Flask(__name__)

# ---- DB ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(
  phone TEXT PRIMARY KEY,
  name  TEXT,
  email TEXT,
  city  TEXT
);
CREATE TABLE IF NOT EXISTS states(
  phone      TEXT PRIMARY KEY,
  state      TEXT,    -- ask_city, menu, ask_name, ask_email, ask_wine, ask_qty, confirming, awaiting_payment, closed
  city       TEXT,
  wine       TEXT,
  qty        INTEGER,
  last_msg_at DATETIME,
  close_by    DATETIME
);
CREATE TABLE IF NOT EXISTS orders(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  phone     TEXT,
  city      TEXT,
  wine      TEXT,
  qty       INTEGER,
  total     REAL,
  status    TEXT,     -- collecting, awaiting_payment, paid
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
with db() as conn:
    conn.executescript(SCHEMA)

# ---- Utils ----
def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    rep = {"á":"a","é":"e","í":"i","ó":"o","ú":"u"}
    for k,v in rep.items(): s = s.replace(k, v)
    return re.sub(r"\s+", " ", s)

def now_iso():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
def in_minutes(m: int):
    return (datetime.utcnow() + timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")
def in_hours(h: int):
    return (datetime.utcnow() + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")

def expired_session(row):
    try:
        if not row or not row["close_by"]: return False
        close_dt = datetime.strptime(row["close_by"], "%Y-%m-%d %H:%M:%S")
        return datetime.utcnow() >= close_dt
    except:
        return False

def mark_closed(conn, phone):
    conn.execute("UPDATE states SET state='closed', close_by=NULL WHERE phone=?", (phone,))

# ---- Catálogo (2 vinos a $290) ----
CATALOG = {
    "vino tinto scala tempranillo": 290,
    "vino espumoso scala moscatel": 290
}
def title_wine(key: str) -> str:
    mapping = {
        "vino tinto scala tempranillo": "Vino Tinto Scala – Tempranillo",
        "vino espumoso scala moscatel": "Vino Espumoso Scala – Moscatel de Alejandría"
    }
    return mapping.get(key, key.title())

# Alias (sinónimos)
ALIAS = {
    "vino tinto scala tempranillo": [
        "tempranillo", "vino tinto", "tinto scala", "scala tempranillo"
    ],
    "vino espumoso scala moscatel": [
        "espumoso", "moscatel", "vino espumoso", "scala moscatel", "moscatel de alejandria"
    ],
}
def resolve_alias(ntext: str) -> str:
    n = normalize(ntext)
    for key, names in ALIAS.items():
        if any(n == normalize(name) for name in names):
            return key
    return n

# ---- WhatsApp API helpers ----
def wa_url():
    return f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/messages"
HEADERS = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}

def send_wa_text(to: str, text: str):
    requests.post(wa_url(), headers=HEADERS, json={
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]}
    }, timeout=30)

def send_wa_buttons(to: str, body: str, buttons: list):
    btns = [{"type":"reply","reply":{"id":bid,"title":btitle}} for bid,btitle in buttons][:3]
    requests.post(wa_url(), headers=HEADERS, json={
        "messaging_product":"whatsapp",
        "to": to,
        "type":"interactive",
        "interactive":{"type":"button","body":{"text": body[:1024]},"action":{"buttons": btns}}
    }, timeout=30)

def ask_city(to: str):
    send_wa_buttons(to, "👋 ¿En qué ciudad te encuentras?", [("cdmx","CDMX"), ("qro","Querétaro"), ("otra","Otra")])
def show_menu(to: str):
    send_wa_buttons(to, "Menú principal:\n• Características\n• Precio\n• Comprar",
                    [("caracteristicas","Características"), ("precios","Precio"), ("comprar","Comprar")])
def ask_close_or_continue(to: str):
    send_wa_buttons(to, "¿Deseas *seguir comprando* o *cerrar* la conversación?", [("seguir","Seguir"), ("cerrar","Cerrar")])

FEATURES_MSG = (
    "Tenemos:\n"
    "• *Vino Tinto Scala – Tempranillo* 🍷\n"
    "  100% Tempranillo (Valle de la Grulla, Ensenada). Frutal, dulce y equilibrado.\n\n"
    "• *Vino Espumoso Scala – Moscatel de Alejandría* 🍾\n"
    "  Fresco, floral y afrutado, de burbuja fina. Ideal para mariscos, postres y celebraciones.\n"
)
PRICES_MSG = (
    "Precios Scala Dei:\n"
    "• Vino Tinto Scala – Tempranillo — $290\n"
    "• Vino Espumoso Scala – Moscatel de Alejandría — $290\n"
)

def payment_instructions(total: float, order_id: int) -> str:
    return (
        f"Excelente. Total: ${total:.2f} MXN.\n\n"
        f"💳 Depósito/transferencia a:\n"
        f"{BANCO}\nBeneficiario: {BENEFICIARIO}\nCLABE: {CLABE}\n"
        f"Concepto: Pedido {order_id}\n\n"
        "📸 Cuando tengas el comprobante, envía la foto aquí o escribe *PAGADO*."
    )

# ---- Webhook verification ----
@app.get("/wa/webhook")
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return challenge, 200
    return "forbidden", 403

# ---- Webhook receiver ----
@app.post("/wa/webhook")
def receive():
    data = request.get_json(force=True, silent=True) or {}
    try:
        entry = data.get("entry",[{}])[0]
        changes = entry.get("changes",[{}])[0]
        value = changes.get("value",{})
        messages = value.get("messages",[])
        contacts = value.get("contacts",[{}])
        if not messages:
            return jsonify(ok=True)

        msg = messages[0]
        from_num = msg.get("from")
        profile_name = contacts[0].get("profile",{}).get("name")

        # Upsert + timestamp
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO customers(phone,name) VALUES(?,?)",(from_num, profile_name))
            conn.execute("INSERT OR IGNORE INTO states(phone,state,last_msg_at) VALUES(?,?,?)",(from_num, "ask_city", now_iso()))
            conn.execute("UPDATE states SET last_msg_at=? WHERE phone=?", (now_iso(), from_num))

        with db() as conn:
            st = conn.execute("SELECT state,city,wine,qty,last_msg_at,close_by FROM states WHERE phone=?", (from_num,)).fetchone()
        current_state = st["state"] if st else None
        current_city  = st["city"]  if st else None

        # Expirada o cerrada
        if expired_session(st) or (current_state == "closed"):
            send_wa_text(from_num, "La sesión anterior finalizó ✅\nEscribe *hola* para empezar una nueva compra 🍷")
            with db() as conn:
                mark_closed(conn, from_num)
            return jsonify(ok=True)

        # Tipo de mensaje
        mtype = msg.get("type")
        text_raw = msg.get("text",{}).get("body","") if mtype=="text" else ""
        ntext = normalize(text_raw)

        # ---- Interactive (botones)
        if mtype=="interactive":
            itype = msg.get("interactive",{}).get("type")
            if itype=="button_reply":
                bid = msg["interactive"]["button_reply"]["id"]

                # Cerrar / Seguir
                if bid == "cerrar":
                    with db() as conn: mark_closed(conn, from_num)
                    send_wa_text(from_num, "Conversación finalizada. ¡Gracias por tu compra! 🍇\nEscribe *hola* para iniciar otra.")
                    return jsonify(ok=True)
                if bid == "seguir":
                    with db() as conn: conn.execute("UPDATE states SET state='menu', close_by=NULL WHERE phone=?", (from_num,))
                    show_menu(from_num); return jsonify(ok=True)

                # Ciudad
                if bid in ("cdmx","qro","otra"):
                    with db() as conn:
                        if bid=="otra":
                            send_wa_text(from_num, "Por ahora solo entregamos en *CDMX y Querétaro*. ¡Pronto más ciudades! 🙏")
                            mark_closed(conn, from_num); return jsonify(ok=True)
                        city = "cdmx" if bid=="cdmx" else "queretaro"
                        conn.execute("UPDATE states SET state='menu', city=?, close_by=NULL WHERE phone=?", (city, from_num))
                        conn.execute("UPDATE customers SET city=? WHERE phone=?", (city, from_num))
                    send_wa_text(from_num, f"¡Perfecto! Entregas personales en *{ 'CDMX' if city=='cdmx' else 'Querétaro' }* 🍷")
                    show_menu(from_num); return jsonify(ok=True)

                # Menú
                if bid=="caracteristicas":
                    send_wa_text(from_num, FEATURES_MSG); show_menu(from_num); return jsonify(ok=True)
                if bid=="precios":
                    send_wa_text(from_num, PRICES_MSG); show_menu(from_num); return jsonify(ok=True)
                if bid=="comprar":
                    with db() as conn:
                        row = conn.execute("SELECT city FROM states WHERE phone=?", (from_num,)).fetchone()
                        if not row or not row["city"]:
                            ask_city(from_num)
                            conn.execute("UPDATE states SET state='ask_city', close_by=NULL WHERE phone=?", (from_num,))
                            return jsonify(ok=True)
                        conn.execute("UPDATE states SET state='ask_name', close_by=? WHERE phone=?", (in_minutes(30), from_num))
                    send_wa_text(from_num, "Perfecto. ¿Cuál es tu *nombre completo*?")
                    return jsonify(ok=True)

        # ---- Texto: arranque / cierre manual
        if mtype=="text" and any(k in ntext for k in ["hola","menu","menú","buenas","start","inicio"]):
            with db() as conn: conn.execute("UPDATE states SET state='ask_city', close_by=NULL WHERE phone=?", (from_num,))
            ask_city(from_num); return jsonify(ok=True)
        if mtype=="text" and any(k in ntext for k in ["cerrar","gracias","no gracias","listo"]):
            with db() as conn: mark_closed(conn, from_num)
            send_wa_text(from_num, "Conversación finalizada. Escribe *hola* para empezar de nuevo 🍷")
            return jsonify(ok=True)

        # ---- Flujo guiado
        if not current_state or current_state=="ask_city":
            ask_city(from_num)
            with db() as conn: conn.execute("UPDATE states SET state='ask_city', close_by=NULL WHERE phone=?", (from_num,))
            return jsonify(ok=True)

        if current_state=="ask_name" and mtype=="text":
            with db() as conn:
                conn.execute("UPDATE customers SET name=? WHERE phone=?", (text_raw.strip(), from_num))
                conn.execute("UPDATE states SET state='ask_email', close_by=? WHERE phone=?", (in_minutes(30), from_num))
            send_wa_text(from_num, "Gracias. ¿Cuál es tu *correo electrónico*?")
            return jsonify(ok=True)

        if current_state=="ask_email" and mtype=="text":
            email = text_raw.strip()
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                send_wa_text(from_num, "Correo no válido. Intenta de nuevo (ej. nombre@gmail.com)")
                return jsonify(ok=True)
            with db() as conn:
                conn.execute("UPDATE customers SET email=? WHERE phone=?", (email, from_num))
                conn.execute("UPDATE states SET state='ask_wine', close_by=? WHERE phone=?", (in_minutes(30), from_num))
            send_wa_text(from_num, "¿Qué vino deseas?\n- Vino Tinto Scala – Tempranillo\n- Vino Espumoso Scala – Moscatel de Alejandría")
            return jsonify(ok=True)

        if current_state=="ask_wine" and mtype=="text":
            wine_key = resolve_alias(normalize(text_raw))
            price = CATALOG.get(wine_key)
            if not price:
                send_wa_text(from_num, "No encontré ese vino 😅. Elige de la lista:\n- Vino Tinto Scala – Tempranillo\n- Vino Espumoso Scala – Moscatel de Alejandría")
                return jsonify(ok=True)
            with db() as conn:
                conn.execute("UPDATE states SET state='ask_qty', wine=?, close_by=? WHERE phone=?", (wine_key, in_minutes(30), from_num))
            send_wa_text(from_num, f"Anotado: *{title_wine(wine_key)}*. ¿Cuántas botellas deseas?")
            return jsonify(ok=True)

        # 5) Cantidad → muestra RESUMEN y pasa a confirming
        if current_state == "ask_qty" and mtype == "text":
            qty = int(re.sub(r"\D", "", text_raw)) if re.search(r"\d+", text_raw) else 1
            with db() as conn:
                row = conn.execute("SELECT wine, city FROM states WHERE phone=?", (from_num,)).fetchone()
                wine_key = row["wine"]; city = row["city"]
            if wine_key not in CATALOG:
                send_wa_text(from_num, "No reconozco ese vino, por favor elige uno de la lista.")
                return jsonify(ok=True)
            price = CATALOG[wine_key]; total = qty * price; wine_name = title_wine(wine_key)
            with db() as conn:
                conn.execute("UPDATE states SET state='confirming', qty=?, close_by=? WHERE phone=?", (qty, in_minutes(30), from_num))
            msg = (f"📝 *Resumen de tu pedido:*\n"
                   f"• Vino: {wine_name}\n"
                   f"• Cantidad: {qty} botella(s)\n"
                   f"• Total: ${total} MXN\n\n"
                   f"¿Deseas confirmar tu pedido? (Responde 'sí' o 'no')")
            send_wa_text(from_num, msg)
            return jsonify(ok=True)

        # 6) Confirmación sí/no
        if current_state == "confirming" and mtype == "text":
            if "si" in ntext or "sí" in ntext:
                with db() as conn:
                    strow = conn.execute("SELECT city,wine,qty FROM states WHERE phone=?", (from_num,)).fetchone()
                    city = strow["city"]; wine_key = strow["wine"]; qty = strow["qty"]
                    total = CATALOG.get(wine_key,0) * int(qty or 1)
                    conn.execute("INSERT INTO orders(phone,city,wine,qty,total,status) VALUES(?,?,?,?,?,?)",
                                 (from_num, city, wine_key, qty, total, "awaiting_payment"))
                    order_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
                    conn.execute("UPDATE states SET state='awaiting_payment', close_by=? WHERE phone=?", (in_hours(2), from_num))
                confirm_msg = (
                    f"🍷 ¡Excelente! Has confirmado tu pedido de {qty} {title_wine(wine_key)} "
                    f"por un total de ${total} MXN.\n\n{payment_instructions(total, order_id)}"
                )
                send_wa_text(from_num, confirm_msg)
                return jsonify(ok=True)

            elif "no" in ntext:
                with db() as conn: conn.execute("UPDATE states SET state='menu', close_by=NULL WHERE phone=?", (from_num,))
                send_wa_text(from_num, "🛑 Pedido cancelado. Escribe *menu* para volver a empezar.")
                return jsonify(ok=True)

            else:
                send_wa_text(from_num, "Por favor responde 'sí' o 'no' para confirmar o cancelar tu pedido.")
                return jsonify(ok=True)

        # 7) Comprobante (imagen/documento) o palabra "pagado"
        is_paid = False
        if current_state=="awaiting_payment":
            if mtype in ("image","document"): is_paid = True
            elif mtype=="text" and "pagado" in ntext: is_paid = True

        if is_paid:
            with db() as conn:
                cust = conn.execute("SELECT name,email,city FROM customers WHERE phone=?", (from_num,)).fetchone()
                ord_row = conn.execute(
                    "SELECT id,wine,qty,total,city FROM orders WHERE phone=? AND status='awaiting_payment' ORDER BY id DESC LIMIT 1",
                    (from_num,)
                ).fetchone()
                if ord_row:
                    conn.execute("UPDATE orders SET status='paid' WHERE id=?", (ord_row["id"],))
                conn.execute("UPDATE states SET state='menu', close_by=? WHERE phone=?", (in_hours(2), from_num))

            payload = {
                "nombre":   (cust["name"] if cust else None),
                "telefono": from_num,
                "correo":   (cust["email"] if cust else None),
                "ciudad":   (ord_row["city"] if ord_row else (cust["city"] if cust else None)),
                "vino":     title_wine(ord_row["wine"]) if ord_row else None,
                "cantidad": ord_row["qty"] if ord_row else None,
                "total":    ord_row["total"] if ord_row else None,
                "pedido_id":ord_row["id"] if ord_row else None,
                "tipo_comprobante": mtype
            }
            if MAKE_WEBHOOK:
                try: requests.post(MAKE_WEBHOOK, json=payload, timeout=30)
                except Exception as e: print("Error POST Make:", e)

            send_wa_text(from_num, "¡Gracias! Recibimos tu comprobante ✅ En breve te contactaremos para coordinar la entrega 🍷")
            ask_close_or_continue(from_num)
            return jsonify(ok=True)

        # En menú: recordar
        if current_state == "menu":
            show_menu(from_num); return jsonify(ok=True)

        # Fallback
        send_wa_text(from_num, "No entendí eso 🤔. Escribe *hola* para empezar o usa los botones del menú.")
        return jsonify(ok=True)

    except Exception as e:
        print("Webhook error:", e)
        return jsonify(ok=True)

# ---- Healthcheck ----
@app.get("/")
def root():
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
