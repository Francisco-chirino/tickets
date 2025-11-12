# mi_app_tickets.py
# Este es el archivo principal de tu servidor (Backend).
# Ejecutar con: python mi_app_tickets.py

import sqlite3
import qrcode
import io
import hmac
import hashlib
import base64
import os
from flask import Flask, jsonify, request, send_file, g, abort
from flask_cors import CORS

app = Flask(__name__)
# CORS permite que tu archivo escaner.html (que puede estar en otro dominio)
# se comunique con este servidor.
CORS(app)

# --- CONFIGURACIÓN ---
DATABASE = 'tickets.db'

# ¡IMPORTANTE! 
# Debes reemplazar este texto con el 'Client Secret' real de tu App de Shopify.
# Si estás probando en local sin Shopify real, puedes dejar esto, pero la validación del webhook fallará.
SHOPIFY_API_SECRET = os.environ.get('SHOPIFY_SECRET', "TU_SECRET_COMPARTIDO_DE_SHOPIFY")

# --- GESTIÓN DE BASE DE DATOS (SQLite) ---

def get_db():
    """Abre una conexión a la base de datos SQLite."""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        # Esto permite acceder a las columnas por nombre (ej. row['email'])
        db.row_factory = sqlite3.Row 
    return db

@app.teardown_appcontext
def close_connection(exception):
    """Cierra la conexión a la BD al terminar la petición."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    """Inicializa la base de datos creando la tabla 'tickets' si no existe."""
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
                fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.commit()
        print("--- Base de datos 'tickets.db' verificada/inicializada ---")

# --- SEGURIDAD ---

def verificar_webhook(data, hmac_header):
    """
    Verifica que la notificación realmente provenga de Shopify
    comparando las firmas criptográficas (HMAC).
    """
    if not hmac_header or not SHOPIFY_API_SECRET:
        return False
        
    digest = hmac.new(
        SHOPIFY_API_SECRET.encode('utf-8'),
        data,
        hashlib.sha256
    ).digest()
    
    computed_hmac = base64.b64encode(digest)
    
    # Comparación segura para evitar ataques de tiempo
    return hmac.compare_digest(computed_hmac, hmac_header.encode('utf-8'))

# --- RUTAS (ENDPOINTS) ---

@app.route('/')
def home():
    return "El servidor de tickets está funcionando correctamente."

@app.route("/shopify/webhook/orden_pagada", methods=['POST'])
def webhook_orden_pagada():
    """
    Recibe la notificación de Shopify cuando se paga una orden.
    Genera los tickets en la base de datos.
    """
    # 1. Verificación de seguridad
    hmac_header = request.headers.get('X-Shopify-Hmac-Sha256')
    data = request.get_data()
    
    # Nota: Si estás probando manualmente sin Shopify, puedes comentar este if temporalmente
    if SHOPIFY_API_SECRET != "TU_SECRET_COMPARTIDO_DE_SHOPIFY":
        if not verificar_webhook(data, hmac_header):
            print("Error de seguridad: Firma HMAC inválida.")
            abort(401)
    
    # 2. Procesar datos del pedido
    try:
        pedido = request.json
        cliente_email = pedido.get('email')
        orden_id = str(pedido.get('id'))
        
        print(f"Procesando Orden ID: {orden_id} - Email: {cliente_email}")
        
        db = get_db()
        cursor = db.cursor()
        tickets_creados = 0
        
        # Recorremos los ítems comprados
        for item in pedido.get('line_items', []):
            sku = item.get('sku')
            cantidad = item.get('quantity')
            
            # LÓGICA: Si tiene SKU, asumimos que es una entrada/ticket.
            # Puedes filtrar aquí por SKUs específicos si vendes otras cosas.
            if sku:
                for i in range(cantidad):
                    # Generamos ID único: TICKET-{Orden}-{Item}-{Indice}
                    ticket_id = f"TICKET-{orden_id}-{item.get('id')}-{i+1}"
                    
                    try:
                        cursor.execute(
                            """
                            INSERT INTO tickets (ticket_id, evento_sku, cliente_email, orden_id, usado)
                            VALUES (?, ?, ?, ?, 0)
                            """,
                            (ticket_id, sku, cliente_email, orden_id)
                        )
                        tickets_creados += 1
                        print(f"Ticket generado: {ticket_id}")
                    except sqlite3.IntegrityError:
                        # Si el ticket ya existe, lo ignoramos (idempotencia)
                        print(f"El ticket {ticket_id} ya existía.")
        
        db.commit()
        print(f"Proceso completado. Total tickets nuevos: {tickets_creados}")
        return jsonify({"status": "success"}), 200
        
    except Exception as e:
        print(f"Error procesando webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/verificar_ticket/<string:ticket_id>")
def verificar_ticket(ticket_id):
    """
    Endpoint consultado por el ESCÁNER.
    Verifica si el ticket existe y si ya fue usado.
    """
    print(f"Solicitud de verificación para: {ticket_id}")
    
    db = get_db()
    cursor = db.cursor()
    
    cursor.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,))
    ticket = cursor.fetchone()

    # CASO 1: El ticket no existe en la BD
    if not ticket:
        return jsonify({
            "valido": False,
            "mensaje": "❌ TICKET NO ENCONTRADO O INVÁLIDO"
        })

    # CASO 2: El ticket ya fue usado anteriormente
    if ticket["usado"]:
        return jsonify({
            "valido": False,
            "mensaje": f"⚠️ ALERTA: ESTE TICKET YA FUE USADO.\nSKU: {ticket['evento_sku']}"
        })

    # CASO 3: Ticket válido (Check-in exitoso)
    # Lo marcamos como usado ahora mismo.
    cursor.execute("UPDATE tickets SET usado = 1 WHERE ticket_id = ?", (ticket_id,))
    db.commit()
    
    return jsonify({
        "valido": True,
        "mensaje": f"✅ ACCESO PERMITIDO\nEvento: {ticket['evento_sku']}\nTitular: {ticket['cliente_email']}"
    })

@app.route("/generar_qr/<string:ticket_id>")
def generar_qr(ticket_id):
    """
    Genera la imagen del código QR dinámicamente.
    Esta URL es la que se inserta en el correo de Shopify.
    """
    # Opcional: Verificar que el ticket exista antes de generar QR
    # db = get_db() ...
    
    # Crear imagen QR
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(ticket_id)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Guardar en memoria (buffer) para no crear archivos temporales
    buffer = io.BytesIO()
    img.save(buffer, 'PNG')
    buffer.seek(0)
    
    return send_file(buffer, mimetype='image/png')

if __name__ == '__main__':
    # Inicializamos la BD al arrancar
    init_db()
    # '0.0.0.0' hace que el servidor sea visible externamente (necesario para Render/Docker)
    app.run(debug=True, host='0.0.0.0', port=8080)