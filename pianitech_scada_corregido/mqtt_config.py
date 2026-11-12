"""Configuración común de la arquitectura MQTT de Pianitech."""
import os

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))       # TCP  (Python <-> broker)
MQTT_WS_PORT = int(os.getenv("MQTT_WS_PORT", "9001"))  # WebSocket (navegador <-> broker)
MQTT_BIND = os.getenv("MQTT_BIND", "0.0.0.0")

# Tópicos
T_SENSORES = "pianitech/sensores"          # Puente serial -> {agua, fuego, motor, ts}
T_ARDUINO = "pianitech/enlace_usb"          # Puente serial -> {conectado, puerto}   (retenido)
T_COMANDOS = "pianitech/comandos"          # Backend -> puente serial -> Arduino ('0','1','F','R')
T_ESTADO = "pianitech/estado"              # Backend -> navegador (estado completo del panel, retenido)
