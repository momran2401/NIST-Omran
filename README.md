# NIST-Omran

## Overview

This repository is for working with raw IQ data from SDR/radio hardware and
visualizing live RF measurements. The main project-specific code is in `live/`,
which contains several versions of a live two-channel spectrogram and PSD
viewer workflow.

## About striqt

`striqt/` is the supporting acquisition and analysis library (courtesy of the folks at NIST - Dr. Dan Kuester & Aric Sanders) used here for RF/IQ acquisition and analysis workflows. It has its own README and
documentation, so this file focuses on the live visualization scripts in this repo.

## Live Visualization Scripts

`live/` contains multiple versions of the same general live IQ visualization
workflow:

- `striqt_frontend_TCP.py` + `striqt_server_TCP.py`
  - Frontend/backend (respectively) version for two devices.
  - The server runs on the AIR-T/AIR8201 side, uses `striqt` for acquisition,
    and streams spectrogram frames over TCP.
  - The viewer runs on another machine, connects to the server, and provides the
    PyQt/pyqtgraph spectrogram and PSD UI.
  - Use this when the radio host and display machine are separate.

- `striqt_standalone_terminal.py`
  - Terminal-only standalone live monitor.
  - Runs acquisition and display in one process using a curses UI, without Qt or
    pyqtgraph.
  - Use this over SSH or when a graphical desktop is not available.
  - Codex made this, so if anything weird happens, I'd like you all to blame W. Tyler Reichenberg of Purdue University.

- `striqt_standalone.py`
  - Full standalone GUI version.
  - Combines the `striqt` radio backend and the PyQt/pyqtgraph viewer in one
    process, with no TCP server/client split.
  - Use this when the radio host can also run the GUI.

- `striqt_web_server.py` + `live/web/`
  - Browser-based live viewer accessible from anywhere.
  - Runs on the Deepwave/AIR-T; streams spectrogram frames over WebSocket to
    any browser (desktop, phone, tablet).
  - Add Cloudflare Tunnel for internet access without port-forwarding.
  - Feature parity with `striqt_standalone.py`: dual waterfalls, PSD, band
    monitor, peak hold/marker, min trace, RX1−RX2 diff, absolute/baseband axes,
    CSV and PNG export, Boring/Cool mode.
  - `--demo` flag generates synthetic IQ so the web app works on a laptop
    without any radio hardware (useful for development/testing).


## Quick Usage

Run commands from the repository root unless noted otherwise.

- Networked `striqt` server and viewer:

  ```sh
  python3 live/striqt_server_TCP.py
  python3 live/striqt_frontend_TCP.py <server-ip>
  ```

- Terminal standalone monitor (adjust settings accordingly):

  ```sh
  python3 live/striqt_standalone_terminal.py --center-mhz 1955 --rate-msps 15.36 --nfft 1024 --rows 40 --fps 3
  ```

- Full standalone GUI:

  ```sh
  python3 live/striqt_standalone.py
  ```

- Web viewer (browser-accessible from anywhere):

  ```sh
  # Install once (on the Deepwave or your dev machine)
  pip install fastapi 'uvicorn[standard]'

  # Test on a laptop with no radio (opens at http://localhost:8000)
  python3 live/striqt_web_server.py --demo

  # Real radio, LAN-only
  python3 live/striqt_web_server.py

  # Real radio + internet access via Cloudflare Tunnel (one-step launcher)
  bash live/run_web.sh

  # Low-bandwidth option (uint8 waterfall, ~4× smaller frames)
  bash live/run_web.sh --quantize
  ```

  After running `run_web.sh`, cloudflared prints a public
  `*.trycloudflare.com` URL — open it on any browser.

## Notes

- The live scripts assume the needed SDR hardware, drivers, Python packages, and
  `striqt` imports are available on the machine running acquisition.
- `striqt_frontend_TCP.py` defaults to `192.168.50.1:5005`; pass a host or `--port` when
  using a different network setup.

## License and Attribution

This work was developed in connection with the NIST SURF project “Development of visualization frontends for cellular 5G-NR measurements.” Reuse, redistribution, or derivative work should be approved by the appropriate NIST project mentors (Dr. Aric Sanders & Dr. Dan Kuester) and the repository maintainer before use outside the intended NIST research context.

This repository is currently not licensed for public reuse. Unless a separate license is added, all rights are reserved for the project-specific code in this repository.

The `striqt/` library included in this repository was developed separately and is not authored by Mustafa Omran. It is included here as a supporting RF/IQ acquisition and analysis library for the live visualization workflow. Its own README, notices, and license terms should be preserved and followed.
