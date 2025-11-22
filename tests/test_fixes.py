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
        # We can use the app context to insert directly
        with app.app_context():
            import sqlite3
            # Connect directly to avoid Flask g object issues in setup if not in request context
            # But init_db uses get_db which uses g.
            # Let's just use sqlite3 directly for setup
            conn = sqlite3.connect('tickets.db')
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO tickets (ticket_id, evento_sku, cliente_email, orden_id, usado) VALUES (?, ?, ?, ?, 0)",
                ('RACE-TEST-TICKET', 'SKU-123', 'race@example.com', 'ORDER-RACE')
            )
            conn.commit()
            conn.close()

        # 2. Launch server in a separate process
        # We need to run the actual server to test concurrency with requests
        # Start server
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

if __name__ == '__main__':
    unittest.main()
