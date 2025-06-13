# Minka WebSocket Server

This repository contains the source code for **Minka**, a Tornado based WebSocket server that enables real time communication between web and mobile clients. The server uses Redis to keep track of session state and queued messages so that clients can reconnect and continue receiving data even after temporary disconnections.

## Setup

1. Install **Python 3.10** or later.
2. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
3. Install the project requirements:
   ```bash
   pip install -r requirements.txt
   ```

## Running the server

Once the environment is ready, start the WebSocket server with:
```bash
python minka_plantilla/server/run.py
```
The server listens on the host and port defined in `minka_plantilla/server/config.py`.

## Documentation

More detailed documentation of the architecture, endpoints and usage examples can be found in [`minka_plantilla/server/DOCUMENTATION.md`](minka_plantilla/server/DOCUMENTATION.md).
