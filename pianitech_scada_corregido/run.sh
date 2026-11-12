#!/bin/bash
cd "$(dirname "$0")"
echo "PIANITECH - Arduino > MQTT > Panel web"
# Si no se detecta solo:  export ARDUINO_PORT=/dev/ttyUSB0   (o /dev/ttyACM0, /dev/cu.usbmodem...)
# Si la flama sale al revés:  export INVERTIR_FUEGO=1
pip install -r requirements.txt
python3 app.py
