#!/usr/bin/env python3


# Zebra webprint bridge to linux Cups
#
# Version 1.0 21-9-2026
#

from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver
import threading
import subprocess
import json
import ssl
import urllib.parse
import os
import select
import socket
import re

LAST_READ_BUFFER = b""

def parse_has_hardware_error(response_bytes):
    """Controleert of de printerrespons hardwarefouten bevat."""
    if not response_bytes:
        return True

    try:
        text = response_bytes.decode('utf-8', errors='ignore')
        
        # 1. Controle op ~HQES respons (Zebra Extended Status)
        if "ERRORS:" in text:
            for line in text.splitlines():
                if "ERRORS:" in line:
                    errors_part = line.split("ERRORS:")[1].strip()
                    parts = errors_part.split()
                    
                    for p in parts:
                        try:
                            # Converteer hexadecimale bitfields (bijv. '00000000') naar een integer
                            if int(p, 16) != 0:
                                return True
                        except ValueError:
                            if p != '0':
                                return True
                    return False

        # 2. Controle op ~HS respons (Host Status)
        if text.startswith("\x02") and "," in text:
            parts = text.split(",")
            if len(parts) > 1 and parts[1] != '0':
                return True
            return False

    except Exception as e:
        print(f"[PARSER] Fout bij ontleden status: {e}", flush=True)

    return True

def get_printer_device_uri(printer_name):
    """Haalt de URI op via CUPS."""
    try:
        output = subprocess.check_output(['lpstat', '-v', printer_name], text=True)
        if ':' in output:
            return output.split(':', 1)[1].strip()
    except Exception:
        pass
    return None

def forward_query_to_network(ip, query_bytes):
    """Stuurt het ZPL status commando naar poort 9100 met actieve socket-cleanup."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(1.5)
        s.connect((ip, 9100))
        s.sendall(query_bytes + b"\r\n")
        res = s.recv(1024)
        s.close()
        if res and len(res) > 5:
            return res
    except Exception as e:
        print(f"[PASSTHROUGH] Fout of timeout op {ip}:9100 : {e}", flush=True)
        if s:
            try:
                s.close()
            except Exception:
                pass
    return None

def forward_query_to_usb(usb_device, query_bytes):
    """Stuurt het ZPL status commando naar de USB poort."""
    if not os.path.exists(usb_device):
        return None
    try:
        fd = os.open(usb_device, os.O_RDWR | os.O_NONBLOCK)
        os.write(fd, query_bytes + b"\r\n")
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            res = os.read(fd, 1024)
            os.close(fd)
            if res and len(res) > 5:
                return res
        os.close(fd)
    except Exception:
        pass
    return None

def get_live_hardware_status(printer_name, query_bytes):
    """Routeert status-aanvraag en filtert hardwarestoringen."""
    if not printer_name:
        printer_name = "Zebra1"

    uri = get_printer_device_uri(printer_name)
    raw_res = None

    if uri:
        ip_match = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', uri)
        if ip_match:
            ip = ip_match.group(1)
            raw_res = forward_query_to_network(ip, query_bytes)

    if not raw_res:
        raw_res = forward_query_to_usb('/dev/usb/lp0', query_bytes)

    # Analyseer het resultaat
    if raw_res and not parse_has_hardware_error(raw_res):
        print(f"[STATUS RESULT] Printer ONLINE op {printer_name}.", flush=True)
        return raw_res
    else:
        print(f"[STATUS RESULT] Printer OFFLINE / FOUT (Media Out/Klep Open/Netwerkfout). Geef leeg antwoord voor JS reject().", flush=True)
        return b""

class ZebraHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def do_OPTIONS(self):
        try:
            self.send_response(200)
            self._send_cors_headers()
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def get_printers(self):
        printers = []
        try:
            output = subprocess.check_output(['lpstat', '-a'], text=True)
            for line in output.strip().split('\n'):
                if line:
                    name = line.split()[0]
                    name_lower = name.lower()
                    if 'zebra' in name_lower or 'hc100' in name_lower:
                        printers.append({
                            "name": name,
                            "deviceType": "printer",
                            "connection": "network",
                            "uid": name,
                            "provider": "cups",
                            "manufacturer": "Zebra Technologies"
                        })
        except Exception:
            pass
        return printers

    def do_GET(self):
        global LAST_READ_BUFFER
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path

        try:
            if path in ['/available', '/default', '/']:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self._send_cors_headers()
                self.end_headers()
                printers = self.get_printers()
                self.wfile.write(json.dumps({"printer": printers}).encode('utf-8'))

            elif path in ['/read', '/read/']:
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(LAST_READ_BUFFER)

            else:
                self.send_response(404)
                self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        global LAST_READ_BUFFER
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path

        try:
            if path in ['/write', '/write/', '/']:
                content_length = int(self.headers.get('Content-Length', 0))
                raw_post_data = self.rfile.read(content_length)

                zpl_bytes = raw_post_data
                try:
                    json_payload = json.loads(raw_post_data.decode('utf-8', errors='ignore'))
                    if isinstance(json_payload, dict) and 'data' in json_payload:
                        zpl_bytes = json_payload['data'].encode('utf-8', errors='ignore')
                except Exception:
                    pass

                clean_data = zpl_bytes.strip()

                qs = urllib.parse.parse_qs(parsed_path.query)
                printer_name = None
                if 'device' in qs:
                    printer_name = qs['device'][0]
                elif 'uid' in qs:
                    printer_name = qs['uid'][0]
                if not printer_name:
                    printers = self.get_printers()
                    printer_name = printers[0]['name'] if printers else "Zebra1"

                status_queries = [b'~HQES', b'~HS', b'~HI', b'~HD']
                is_status_query = False
                if len(clean_data) < 100 and b'^XA' not in clean_data:
                    if any(clean_data.startswith(q) for q in status_queries):
                        is_status_query = True

                if is_status_query:
                    LAST_READ_BUFFER = get_live_hardware_status(printer_name, clean_data)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{}')
                    return

                print(f"[POST /write] Printopdracht ontvangen ({len(zpl_bytes)} bytes)", flush=True)

                proc = subprocess.Popen(['lp', '-o', 'raw', '-d', printer_name], stdin=subprocess.PIPE)
                proc.communicate(input=zpl_bytes)

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(b'{}')

            elif path in ['/read', '/read/']:
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(LAST_READ_BUFFER)

            else:
                self.send_response(404)
                self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            try:
                self.send_response(500)
                self._send_cors_headers()
                self.end_headers()
            except Exception:
                pass

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    pass

def run_http_server():
    server = ThreadedHTTPServer(('0.0.0.0', 9100), ZebraHandler)
    server.serve_forever()

def run_https_server():
    server = ThreadedHTTPServer(('0.0.0.0', 9102), ZebraHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile='/etc/ssl/certs/zebra-cert.pem', keyfile='/etc/ssl/private/zebra-key.pem')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()

if __name__ == '__main__':
    t1 = threading.Thread(target=run_http_server, daemon=True)
    t2 = threading.Thread(target=run_https_server, daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()