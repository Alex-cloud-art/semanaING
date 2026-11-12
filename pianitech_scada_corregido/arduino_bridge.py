"""Puente USB-serial <-> MQTT.

Arduino --USB--> [este puente] --MQTT--> broker --> backend Flask / navegador

  * Lee las líneas del Arduino y publica los datos en  pianitech/sensores
  * Escucha  pianitech/comandos  y reenvía cada comando al Arduino por serial
  * Detecta el puerto solo (o usa ARDUINO_PORT) y se reconecta si se desconecta el cable

Acepta dos formatos de Arduino:
  1) Firmware nuevo  ->  línea  "DATA:<agua>,<fuego>,<motor>"
  2) Tu sketch original (sin DATA) -> se interpretan las líneas de texto:
        "💧 Agua: 312 (Normal) | Ambiente normal 455 -> ..."
     (en este modo no se conoce el estado del motor)
"""
import json
import os
import re
import threading
import time

import paho.mqtt.client as mqtt

from mqtt_config import MQTT_HOST, MQTT_PORT, T_ARDUINO, T_COMANDOS, T_SENSORES

try:
    import serial
    from serial.tools import list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

BAUDIOS = int(os.getenv("ARDUINO_BAUD", "9600"))
_CLAVES_ARDUINO = ("arduino", "ch340", "ch341", "wch", "ftdi", "cp210", "usb serial",
                   "usb-serial", "usb2.0-serial", "2341:", "1a86:", "0403:", "10c4:")
_RE_AGUA = re.compile(r"Agua:\s*(\d+)")


def detectar_puerto():
    manual = os.getenv("ARDUINO_PORT", "AUTO").strip()
    if manual and manual.upper() != "AUTO":
        return manual
    for p in list_ports.comports():
        info = f"{p.description} {p.manufacturer} {p.hwid}".lower()
        if any(k in info for k in _CLAVES_ARDUINO):
            return p.device
    return None


class ArduinoBridge(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="puente-arduino")
        self.ser = None
        self.puerto = None
        self.lock = threading.Lock()
        self.ultimo_data = 0.0
        self.cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pianitech-puente")
        self.cliente.will_set(T_ARDUINO, json.dumps({"conectado": False, "puerto": None}), retain=True)
        self.cliente.on_connect = self._on_connect
        self.cliente.on_message = self._on_message

    # ---------- MQTT ----------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        client.subscribe(T_COMANDOS)
        self._publicar_conexion(self.ser is not None)

    def _on_message(self, client, userdata, msg):
        cmd = msg.payload.decode("utf-8", errors="ignore").strip()
        if not cmd:
            return
        with self.lock:
            if self.ser is not None and self.ser.is_open:
                try:
                    self.ser.write(cmd[:1].encode())
                    print(f"[PUENTE] MQTT -> Arduino: '{cmd[:1]}'")
                except Exception as e:  # noqa
                    print(f"[PUENTE] Error escribiendo al Arduino: {e}")

    def _publicar_conexion(self, conectado):
        self.cliente.publish(T_ARDUINO, json.dumps({"conectado": conectado, "puerto": self.puerto if conectado else None}),
                             qos=1, retain=True)

    def _publicar_sensores(self, agua, fuego, motor):
        self.cliente.publish(T_SENSORES, json.dumps({"agua": agua, "fuego": fuego, "motor": motor, "ts": time.time()}), qos=0)

    # ---------- Parseo ----------
    def _procesar(self, linea):
        if linea.startswith("DATA:"):
            try:
                a, f, m = linea[5:].split(",")[:3]
                self.ultimo_data = time.time()
                self._publicar_sensores(int(a), int(f) == 1, int(m) == 1)
            except ValueError:
                pass
            return
        # Formato antiguo (sin DATA), solo si el firmware nuevo no está enviando DATA
        if time.time() - self.ultimo_data > 3:
            m = _RE_AGUA.search(linea)
            if m:
                self._publicar_sensores(int(m.group(1)), "Alerta de Fuego" in linea, None)

    # ---------- Bucle principal ----------
    def run(self):
        if not HAS_SERIAL:
            print("[PUENTE] Falta pyserial (pip install pyserial).")
            return
        self.cliente.connect_async(MQTT_HOST, MQTT_PORT)
        self.cliente.loop_start()

        aviso = 0
        while True:
            puerto = detectar_puerto()
            if not puerto:
                if time.time() - aviso > 15:
                    print("[PUENTE] No encuentro el Arduino. Conéctalo por USB (o define ARDUINO_PORT=COMx).")
                    aviso = time.time()
                time.sleep(2)
                continue
            try:
                ser = serial.Serial(puerto, BAUDIOS, timeout=1)
                time.sleep(2.0)  # el Arduino se reinicia al abrir el puerto
                ser.reset_input_buffer()
                with self.lock:
                    self.ser = ser
                self.puerto = puerto
                print(f"[PUENTE] Arduino conectado en {puerto}")
                self._publicar_conexion(True)
                while True:
                    raw = ser.readline()
                    if raw:
                        self._procesar(raw.decode("utf-8", errors="ignore").strip())
            except (serial.SerialException, OSError) as e:
                print(f"[PUENTE] Conexión perdida ({e}). Reintentando...")
            finally:
                with self.lock:
                    if self.ser is not None:
                        try:
                            self.ser.close()
                        except Exception:  # noqa
                            pass
                    self.ser = None
                self._publicar_conexion(False)
                time.sleep(2)
