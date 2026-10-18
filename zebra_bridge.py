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

READ_BUFFERS = {}
READ_LOCK = threading.Lock()

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

    def _reply(self, status, value=b'', content_type='application/json'):
        body = json.dumps(value).encode('utf-8') if isinstance(value, (dict, list)) else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._reply(200)

    def get_printers(self):
        printers = []
        try:
            output = subprocess.check_output(['lpstat', '-a'], text=True, timeout=5)
            for line in output.strip().splitlines():
                if not line.strip():
                    continue
                name = line.split()[0]
                if 'zebra' in name.lower() or 'hc100' in name.lower():
                    printers.append({'name': name, 'deviceType': 'printer', 'connection': 'network',
                                     'uid': name, 'provider': 'cups', 'manufacturer': 'Zebra Technologies'})
        except (OSError, subprocess.SubprocessError):
            pass
        return printers

    def _select_printer(self, payload=None):
        printers = self.get_printers()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        device = (payload or {}).get('device', {})
        if not isinstance(device, dict):
            raise ValueError('Invalid device')
        requested = device.get('uid') or device.get('name') or query.get('device', query.get('uid', [None]))[0]
        if requested:
            return next((p for p in printers if p['uid'] == requested or p['name'] == requested), None)
        try:
            default = subprocess.check_output(['lpstat', '-d'], text=True, timeout=5).strip().split(':', 1)[-1].strip()
            chosen = next((p for p in printers if p['uid'] == default), None)
            if chosen:
                return chosen
        except (OSError, subprocess.SubprocessError):
            pass
        return printers[0] if printers else None

    def _body(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length < 0 or length > 1024 * 1024:
            raise ValueError('Invalid request size')
        raw = self.rfile.read(length)
        if not raw:
            return {}, b''
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            if self.headers.get_content_type() == 'application/json':
                raise ValueError('Invalid JSON')
            return {}, raw
        if not isinstance(payload, dict):
            raise ValueError('Invalid payload')
        return payload, raw

    def _read_status(self, payload=None):
        printer = self._select_printer(payload)
        if not printer:
            self._reply(404, {'error': 'Printer not available'})
            return
        with READ_LOCK:
            status = READ_BUFFERS.pop(printer['uid'], b'')
        self._reply(200, status, 'text/plain')

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip('/') or '/'
        if path in ['/available', '/']:
            self._reply(200, {'printer': self.get_printers()})
        elif path == '/default':
            printer = self._select_printer()
            # The SDK expects a single device object, or an empty body for no default.
            self._reply(200, printer if printer else b'')
        elif path == '/read':
            self._read_status()
        else:
            self._reply(404)

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path.rstrip('/') or '/'
            if path not in ['/write', '/read', '/']:
                self._reply(404)
                return
            payload, raw = self._body()
            if path == '/read':
                self._read_status(payload)
                return
            printer = self._select_printer(payload)
            if not printer:
                self._reply(404, {'error': 'Printer not available'})
                return
            data = payload.get('data') if payload else raw
            if isinstance(data, str):
                data = data.encode('utf-8')
            if not isinstance(data, bytes) or not data:
                raise ValueError('Missing print data')
            clean = data.strip()
            if len(clean) < 100 and b'^XA' not in clean and any(
                    clean.startswith(q) for q in [b'~HQES', b'~HS', b'~HI', b'~HD']):
                with READ_LOCK:
                    READ_BUFFERS.pop(printer['uid'], None)
                status = get_live_hardware_status(printer['name'], clean)
                with READ_LOCK:
                    READ_BUFFERS[printer['uid']] = status
                self._reply(200, {})
                return
            # HTTP success means that CUPS accepted this job, not physical completion.
            result = subprocess.run(['lp', '-o', 'raw', '-d', printer['name']], input=data,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if result.returncode != 0:
                self._reply(503, {'error': 'CUPS rejected the print job'})
                return
            self._reply(200, {})
        except subprocess.TimeoutExpired:
            self._reply(504, {'error': 'CUPS did not respond in time'})
        except (ValueError, TypeError):
            self._reply(400, {'error': 'Invalid print request'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._reply(500, {'error': 'Print service failed'})

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
