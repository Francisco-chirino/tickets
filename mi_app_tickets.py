import sqlite3
import qrcode
import io
import hmac
import hashlib
import base64
import os
import logging
from flask import Flask, jsonify, request, send_file, g, abort
from flask_cors import CORS
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Configuración de Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
# CORRECCIÓN: Permite conexiones desde CUALQUIER origen (*)
CORS(app, resources={r"/*": {"origins": "*"}})

# --- Configuración ---
DATABASE = 'tickets.db'
SHOPIFY_API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "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")

if not SHOPIFY_API_SECRET:
    logger.warning("SHOPIFY_API_SECRET no está configurado. Los webhooks fallarán.")

# --- Funciones de la Base de Datos (SQLite) ---

def get_db():
    """Abre una conexión a la base de datos"""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    """Cierra la conexión al final de la petición"""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    """Crea la tabla de la base de datos si no existe"""
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id TEXT PRIMARY KEY,
                evento_sku TEXT NOT NULL,
                cliente_email TEXT,
                orden_id TEXT,
                usado BOOLEAN NOT NULL DEFAULT 0,
                mensaje_verificacion TEXT
            )
            """
        )
        db.commit()
        logger.info("Base de datos inicializada y tabla 'tickets' asegurada.")

# --- Función de Seguridad del Webhook ---
def verificar_webhook(data, hmac_header):
    """Verifica que la petición venga de Shopify"""
    if not hmac_header:
        logger.error("Error: No se encontró la cabecera HMAC.")
        return False

    if not SHOPIFY_API_SECRET:
        logger.error("Error: SHOPIFY_API_SECRET no configurado.")
        return False

    digest = hmac.new(
        SHOPIFY_API_SECRET.encode('utf-8'),
        data,
        hashlib.sha256
    ).digest()

    computed_hmac = base64.b64encode(digest)

    return hmac.compare_digest(computed_hmac, hmac_header.encode('utf-8'))


# --- 1. ENDPOINT: El Webhook que escucha a Shopify ---
@app.route("/shopify/webhook/orden_pagada", methods=['POST'])
def webhook_orden_pagada():

    hmac_header = request.headers.get('X-Shopify-Hmac-Sha256')
    data = request.get_data()

    if not verificar_webhook(data, hmac_header):
        logger.warning("¡ALERTA DE SEGURIDAD! HMAC inválido.")
        abort(401)

    pedido = request.json
    cliente_email = pedido.get('email')
    orden_id = pedido.get('id')

    logger.info(f"Procesando pedido: {orden_id} para {cliente_email}")

    try:
        db = get_db()
        cursor = db.cursor()

        for item in pedido.get('line_items', []):
            sku = item.get('sku')
            cantidad = item.get('quantity')

            # (Usaremos la lógica de SKU por ahora, es más seguro)
            if sku:
                logger.info(f"Producto '{item.get('title')}' (SKU: {sku}) es un ticket. Cantidad: {cantidad}")

                for i in range(cantidad):
                    ticket_id = f"TICKET-{orden_id}-{item.get('id')}-{i+1}"

                    cursor.execute(
                        """
                        INSERT INTO tickets (ticket_id, evento_sku, cliente_email, orden_id, usado)
                        VALUES (?, ?, ?, ?, 0)
                        ON CONFLICT(ticket_id) DO NOTHING
                        """,
                        (ticket_id, sku, cliente_email, str(orden_id))
                    )
                    logger.info(f"Ticket {ticket_id} creado en la base de datos.")

        db.commit()

    except Exception as e:
        logger.error(f"Error al procesar el pedido: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "success"}), 200


# --- 2. ENDPOINT: El Escáner de Check-in ---
@app.route("/verificar_ticket/<path:ticket_id>")
def verificar_ticket(ticket_id):

    # Clean up whitespace from the ticket ID
    ticket_id = ticket_id.strip()

    # Support scanning full URLs: extract the last part if it looks like a URL
    if ticket_id.startswith("http://") or ticket_id.startswith("https://"):
        # Remove trailing slash if present
        if ticket_id.endswith("/"):
            ticket_id = ticket_id[:-1]
        ticket_id = ticket_id.split("/")[-1]

        # Remove query parameters if present (e.g. ?source=qr)
        if "?" in ticket_id:
            ticket_id = ticket_id.split("?")[0]

    logger.info(f"Solicitud de verificación para: {ticket_id}")
    db = get_db()
    cursor = db.cursor()

    # Esta línea era la que fallaba
    cursor.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,))
    ticket = cursor.fetchone()

    if not ticket:
        # Permitir tickets de prueba si empiezan con TEST (para debugging cuando la BD está vacía)
        if ticket_id.startswith("TEST"):
             return jsonify({
                "valido": True,
                "mensaje": "MODO PRUEBA: Ticket válido (Simulado)."
            })

        return jsonify({
            "valido": False,
            "mensaje": "ACCESO DENEGADO: Ticket inválido o no existe."
        })

    if ticket["usado"]:
        return jsonify({
            "valido": False,
            "mensaje": f"ALERTA: Este ticket (SKU: {ticket['evento_sku']}) YA FUE USADO."
        })

    # Fix: Atomic update to prevent race condition
    cursor.execute("UPDATE tickets SET usado = 1 WHERE ticket_id = ? AND usado = 0", (ticket_id,))
    db.commit()

    if cursor.rowcount == 0:
        return jsonify({
            "valido": False,
            "mensaje": f"ALERTA: Este ticket (SKU: {ticket['evento_sku']}) YA FUE USADO."
        })

    logger.info(f"Ticket {ticket_id} marcado como usado.")

    return jsonify({
        "valido": True,
        "mensaje": f"ACCESO PERMITIDO: Ticket válido (SKU: {ticket['evento_sku']})."
    })

# --- 3. ENDPOINT: El Cliente genera/ve su QR ---
@app.route("/generar_qr/<string:ticket_id>")
def generar_qr(ticket_id):
    # No verificamos la BD aquí para evitar problemas de sincronización (race conditions)
    # con el webhook. Si el ID está en el correo, mostramos el QR.
    # La validación real ocurre en el escáner.

    img_qr = qrcode.make(ticket_id)

    memoria_img = io.BytesIO()
    img_qr.save(memoria_img, 'PNG')
    memoria_img.seek(0)

    return send_file(memoria_img, mimetype='image/png')

# --- 4. ENDPOINT: Raíz (para "despertar" el servidor) ---
@app.route("/")
def hello_world():
    return "El servidor de tickets está funcionando correctamente. La base de datos ha sido inicializada."

# --- 5. ENDPOINT: Servir el Escáner ---
@app.route("/escaner")
def serve_scanner():
    return send_file("escaner.html")


# --- Ejecución de la App ---

# ¡¡¡AQUÍ ESTÁ LA CORRECCIÓN!!!
# Llamamos a init_db() aquí, fuera del bloque __name__
# Esto asegura que gunicorn (Render) SIEMPRE cree la tabla al iniciar.
init_db()

if __name__ == '__main__':
    # El init_db() ya se llamó arriba
    app.run(debug=False, host='0.0.0.0', port=8080)
