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

## Notes

- The live scripts assume the needed SDR hardware, drivers, Python packages, and
  `striqt` imports are available on the machine running acquisition.
- `striqt_frontend_TCP.py` defaults to `192.168.50.1:5005`; pass a host or `--port` when
  using a different network setup.

## License

This software was developed by employees of the National Institute of Standards and Technology (NIST), an agency of the U.S. Federal Government. Pursuant to title 17 United States Code Section 105, works of NIST employees are not subject to copyright protection in the United States and are considered to be in the public domain.

This software is provided "AS IS." NIST makes no warranty of any kind.
