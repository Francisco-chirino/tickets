import unittest
import threading
import os
import sys
import requests
import time
import subprocess
from mi_app_tickets import app, init_db

# We need a separate process for the race condition test because it uses requests against a running server
# But for the QR test, we can use the flask test client.

class TestTicketSystem(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True
        # Ensure a clean DB for testing
        if os.path.exists('tickets.db'):
            os.remove('tickets.db')
        # Initialize DB
        init_db()

    def tearDown(self):
        if os.path.exists('tickets.db'):
            os.remove('tickets.db')

    def test_qr_generation_always_succeeds(self):
        """
        Test that generating a QR code for a non-existent ticket returns 200.
        This ensures emails show the QR code even if the webhook hasn't processed the order yet.
        """
        ticket_id = "NON-EXISTENT-TICKET-ID"
        response = self.app.get(f'/generar_qr/{ticket_id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/png')

    def test_race_condition_prevention(self):
        """
        Test that multiple concurrent requests cannot redeem the same ticket twice.
        This verifies the atomic update fix.
        """
        # 1. Create a valid ticket in the DB
        with app.app_context():
            import sqlite3
            conn = sqlite3.connect('tickets.db')
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO tickets (ticket_id, evento_sku, cliente_email, orden_id, usado) VALUES (?, ?, ?, ?, 0)",
                ('RACE-TEST-TICKET', 'SKU-123', 'race@example.com', 'ORDER-RACE')
            )
            conn.commit()
            conn.close()

        # 2. Launch server in a separate process
        server_process = subprocess.Popen(
            [sys.executable, 'mi_app_tickets.py'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(2) # Wait for server to start

        try:
            url = "http://localhost:8080/verificar_ticket/RACE-TEST-TICKET"
            results = []

            def call_endpoint():
                try:
                    res = requests.get(url)
                    results.append(res.json())
                except Exception as e:
                    results.append({"error": str(e)})

            # 3. Fire concurrent requests
            threads = []
            for _ in range(5):
                t = threading.Thread(target=call_endpoint)
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            # 4. Verify only one success
            success_count = sum(1 for r in results if r.get('valido') is True)
            self.assertEqual(success_count, 1, f"Expected 1 success, got {success_count}. Results: {results}")

        finally:
            server_process.terminate()
            server_process.wait()

    def test_whitespace_sanitization(self):
        """
        Test that trailing whitespace in the verification request is ignored/sanitized.
        """
        # 1. Create a ticket "TEST-SPACE"
        with app.app_context():
            import sqlite3
            conn = sqlite3.connect('tickets.db')
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO tickets (ticket_id, evento_sku, cliente_email, orden_id, usado) VALUES (?, ?, ?, ?, 0)",
                ('TEST-SPACE', 'SKU-SPACE', 'space@example.com', 'ORDER-SPACE')
            )
            conn.commit()
            conn.close()

        # 2. Request verification with trailing space: "TEST-SPACE "
        # We manually construct the URL with encoded space to ensure it reaches the server as intended
        ticket_id_with_space = "TEST-SPACE "
        response = self.app.get(f'/verificar_ticket/{ticket_id_with_space}')
        data = response.get_json()

        # It should succeed now because we added .strip() in mi_app_tickets.py
        self.assertTrue(data['valido'], "Verification failed despite whitespace fix")
        self.assertIn("ACCESO PERMITIDO", data['mensaje'])

    def test_scanner_page_served(self):
        """
        Test that the scanner HTML is served at /escaner
        """
        response = self.app.get('/escaner')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Scanner de Eventos v3', response.data)

if __name__ == '__main__':
    unittest.main()
