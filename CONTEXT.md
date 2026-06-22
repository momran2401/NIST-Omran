# Context Handoff & Project Summary

This session is about integrating a Deepwave AIR-T / AIR8201-B SDR into a live visualization workflow for Mustafa Omran’s NIST SURF 2026 project: “Development of visualization frontends for cellular 5G-NR measurements.”

Main goal: keep the existing Mac PyQtGraph live viewer UI, but replace the raw server-side SoapySDR acquisition path with the installed `striqt` library on the AIR-T. Desired architecture:

```text
Deepwave AIR-T / AIR8201-B
  runs striqt-based live server
  captures IQ from RX ports 0 and 1
  computes FFT/spectrogram frames
  sends frames over TCP port 5005

MacBook
  runs PyQtGraph viewer
  connects over direct Ethernet to AIR-T
  displays RX1/RX2 waterfalls and PSD
```

Current status: striqt capture and server-side SDR acquisition work on the Deepwave. The current blocker is the Mac direct-Ethernet route to `192.168.50.1` repeatedly working briefly, then disappearing before or during viewer launch. The next session should continue from persistent Mac Ethernet configuration, then test the viewer protocol only after `ping` and `nc` are stable.

---

## Current Progress & Milestones

- Deepwave project folder:
  ```text
  ~/airt-striqt-live/
    live/
      airt_live_server_striqt.py
      airt_live_server_test.py
      live_viewer_mac.py
      test_striqt_capture.py
  ```

- Mac project folder currently has literal colons in names:
  ```text
  /Users/mustafaomran/Downloads/airt-striqt-live:
    README.md
    TASK.md
    exeriments:
    live:
    striqt:
  ```
  Current Mac viewer command while this remains true:
  ```bash
  cd "/Users/mustafaomran/Downloads/airt-striqt-live:"
  python3 "live:/live_viewer_mac.py" 192.168.50.1
  ```

- Original raw Soapy server fallback should be kept:
  ```text
  live/airt_live_server_test.py
  ```

- New striqt server was created:
  ```text
  live/airt_live_server_striqt.py
  ```

- striqt capture smoke test was created:
  ```text
  live/test_striqt_capture.py
  ```

- Installed `striqt` environment on the Deepwave is the one that works. Do not force local `striqt/src` onto `sys.path` unless there is a strong reason.

- Correct installed striqt API:
  ```python
  from striqt.sensor import specs
  from striqt.sensor.lib.sources.deepwave import Air8201BSourceSpec, Airstack1Source

  source_spec = Air8201BSourceSpec(
      master_clock_rate=125e6,
      array_backend="numpy",
      time_source="host",
      time_sync_at="open",
      clock_source="internal",
      gapless=True,
      receive_retries=0,
  )

  source = Airstack1Source.from_spec(source_spec)
  ```

- Important API facts discovered:
  ```text
  Airstack1Source.from_spec(source_spec) works.
  Airstack1Source(spec) is wrong.
  source.setup() does not exist.
  source.trigger() does not exist.
  source.arm_spec(capture) exists.
  source._read_stream(...) exists and is used.
  source.close() exists.
  ```

- Correct Soapy capture pattern:
  ```python
  specs.SoapyCapture(
      port=(0, 1),
      center_frequency=1955e6,
      gain=(0.0, 0.0),
      duration=duration_sec,
      sample_rate=15.36e6,
      backend_sample_rate=15.36e6,
      host_resample=False,
      analysis_bandwidth=float("inf"),
      lo_shift="none",
  )
  ```

- Stream open issue was diagnosed and fixed. Error was:
  ```text
  AssertionError: expected open stream since stream_all_rx_ports=True
  ```
  Fix was to explicitly open RX stream before `arm_spec()`:
  ```python
  def get_device(source):
      return getattr(source, "_device", getattr(source, "device", None))

  def get_rx_stream(source):
      return getattr(source, "_rx_stream", getattr(source, "rx_stream", None))

  def open_stream(source):
      rx_stream = get_rx_stream(source)
      dev = get_device(source)
      if rx_stream is None or dev is None:
          raise RuntimeError("striqt source has no RX stream/device")
      if getattr(rx_stream, "stream", None) is None:
          rx_stream.open(dev)
  ```

- striqt smoke test worked on the Deepwave:
  ```bash
  python -m py_compile live/test_striqt_capture.py live/airt_live_server_striqt.py
  python live/test_striqt_capture.py
  ```
  Output included:
  ```text
  opened AIR8201 via installed striqt
  channels=(0, 1)
   1 ms: shape=(2, 15360), dtype=complex64, elapsed=2.2763s, stream_ports=(0, 1), stream_mtu=4194304
   5 ms: shape=(2, 76800), dtype=complex64, elapsed=1.5180s, stream_ports=(0, 1), stream_mtu=4194304
  10 ms: shape=(2, 153600), dtype=complex64, elapsed=1.5241s, stream_ports=(0, 1), stream_mtu=4194304
  20 ms: shape=(2, 307200), dtype=complex64, elapsed=1.5335s, stream_ports=(0, 1), stream_mtu=4194304
  source control closed
  ```
  This proves:
  ```text
  installed striqt opens AIR8201
  RX ports 0 and 1 work
  returned samples are complex64 with shape (2, N)
  stream MTU is 4,194,304
  chunked reads work
  ```

- Deepwave striqt live server starts and captures successfully:
  ```bash
  cd ~/airt-striqt-live
  python live/airt_live_server_striqt.py
  ```
  Output:
  ```text
  Radio armed through installed striqt: center 1955.00 MHz, 15.360 MS/s, channels (0, 1)
  source=Airstack1Source, capture=SoapyCapture, stream_ports=(0, 1), stream_mtu=4194304
  [initial] center=1955.00 MHz, sample_rate=15.360 MS/s, gain=0.0 dB, nfft=1024, rows=12, requested_capture_samples=12288, ring_capacity=4194304, max_read_chunk=262144, stream_ports=(0, 1)
  FFT backend: CPU (numpy, batched)
  Listening on 0.0.0.0:5005 -- start live_viewer_mac.py on the Mac.
  striqt returned sample shape/dtype: (2, 262144) complex64
  ```

- CuPy/GPU was disabled because importing CuPy caused SoapySDR/libstdc++ conflict:
  ```text
  ImportError: /usr/lib/aarch64-linux-gnu/libstdc++.so.6: version `GLIBCXX_3.4.32' not found
  ```
  Current intentional fix:
  ```python
  _cp = None
  USE_GPU = False
  ```

- Direct Ethernet setup:
  ```text
  Deepwave eth0: 192.168.50.1/24
  Mac en5:      192.168.50.2/24
  TCP port:     5005
  ```

- Mac Ethernet adapter:
  ```text
  en5
  MAC: 00:e0:4c:69:85:86
  IP: 192.168.50.2/24
  status: active
  media: 1000baseT full-duplex
  ```

- Deepwave Ethernet:
  ```text
  eth0
  MAC: 00:04:4b:c7:a8:7e
  IP: 192.168.50.1/24
  MTU: 1280
  state: UP, LOWER_UP
  ```

- Direct Ethernet worked multiple times:
  ```bash
  ping -c 3 192.168.50.1
  nc -vz 192.168.50.1 5005
  ```
  Successful output:
  ```text
  0% packet loss
  Connection to 192.168.50.1 port 5005 succeeded
  ```

- Deepwave server listening confirmation:
  ```bash
  ss -ltnp | grep 5005
  ```
  Output:
  ```text
  LISTEN 0 1 0.0.0.0:5005 0.0.0.0:* users:(("python",pid=2009,fd=8))
  ```

- Viewer was modified to print tracebacks from `Receiver.run()` and `on_frame()`. Current failure:
  ```text
  Receiver exception: OSError(65, 'No route to host')
  socket.create_connection((self.host, self.port), timeout=5)
  ```
  This is before receiving data, so it is network route failure, not Qt/plot/protocol failure.

- Earlier Deepwave did once show:
  ```text
  Viewer connected from ('192.168.50.2', 51614)
  Viewer disconnected: [Errno 32] Broken pipe
  Waiting for the viewer to reconnect ...
  ```
  But later failures were route loss before connection.

---

## Technical Stack & Constraints

- Hardware:
  ```text
  Deepwave AIR-T / AIR8201-B
  AIR8201 connected directly to Mac over Ethernet
  AIR-T has its own monitor/keyboard/mouse
  MacBook runs GUI viewer
  ```

- Software:
  ```text
  Deepwave: installed striqt Python environment
  Server: live/airt_live_server_striqt.py
  Viewer: PyQt6 + pyqtgraph on Mac
  Network protocol: TCP, port 5005
  ```

- Network design:
  ```text
  Deepwave eth0: 192.168.50.1/24
  Mac en5:      192.168.50.2/24
  Server bind:  0.0.0.0:5005
  Viewer host:  192.168.50.1
  ```

- Current Mac route reset commands that temporarily work:
  ```bash
  sudo ifconfig en5 down
  sudo ifconfig en5 up
  sudo ifconfig en5 inet 192.168.50.2 netmask 255.255.255.0 up
  sudo route -n delete -host 192.168.50.1 2>/dev/null
  sudo route -n delete -net 192.168.50.0/24 2>/dev/null
  sudo route -n add -net 192.168.50.0/24 -interface en5
  sudo arp -d -a
  ```

- Recommended next Mac network approach:
  ```text
  System Settings → Network
  Select adapter with MAC 00:e0:4c:69:85:86
  Configure IPv4: Manually
  IP Address: 192.168.50.2
  Subnet Mask: 255.255.255.0
  Router/Gateway: blank
  DNS: blank
  Apply
  ```

- Server must remain CPU-only until the full link works:
  ```python
  _cp = None
  USE_GPU = False
  ```

- Do not modify Python viewer/server until this passes immediately before launch:
  ```bash
  ping -c 3 192.168.50.1
  nc -vz 192.168.50.1 5005
  ```

- Current viewer default:
  ```python
  AIRT_HOST, AIRT_PORT = "192.168.50.1", 5005
  ```

- User wants concise, exact, no-fluff debugging. If code is requested, provide full files, not fragments.

---

## The Sandbox / Open Tasks

1. Make Mac Ethernet IP persistent using macOS System Settings. The temporary `ifconfig` route works briefly, then disappears.

2. Configure the adapter with MAC `00:e0:4c:69:85:86`:
   ```text
   IPv4: Manual
   IP: 192.168.50.2
   Subnet: 255.255.255.0
   Router: blank
   DNS: blank
   ```

3. Verify Mac route:
   ```bash
   ifconfig en5 | grep "status\|inet "
   route get 192.168.50.1
   ping -c 3 192.168.50.1
   nc -vz 192.168.50.1 5005
   ```

4. Keep Deepwave server running:
   ```bash
   cd ~/airt-striqt-live
   python live/airt_live_server_striqt.py
   ```

5. Confirm Deepwave server:
   ```bash
   ip addr show eth0
   ip route
   ss -ltnp | grep 5005
   ```

6. Launch viewer only after `ping` and `nc` both pass:
   ```bash
   cd "/Users/mustafaomran/Downloads/airt-striqt-live:"
   python3 "live:/live_viewer_mac.py" 192.168.50.1
   ```

7. If viewer still gives `No route to host`, do not debug Python. Re-check:
   ```bash
   ifconfig en5
   route get 192.168.50.1
   arp -an | grep 192.168.50
   ```

8. If `ping` and `nc` pass but viewer connects then disconnects, debug protocol:
   - Deepwave logs: `Viewer connected from ...`, then `Broken pipe`.
   - Mac terminal traceback from modified viewer.
   - Check `header["shape"]`, `header["channels"]`, payload sizes, and blocks.

9. Expected payload per channel for default `rows=12`, `nfft=1024`:
   ```text
   12 * 1024 * 4 = 49152 bytes
   ```

10. Clean Mac folder names after functional test:
    ```bash
    cd ~/Downloads
    mv "airt-striqt-live:" airt-striqt-live
    cd airt-striqt-live
    mv "live:" live
    mv "striqt:" striqt
    mv "exeriments:" experiments
    ```

11. Later: reduce server spam from repeated:
    ```text
    striqt returned sample shape/dtype: (2, 262144) complex64
    ```

12. Later: persist Deepwave `eth0` IP if needed:
    ```bash
    sudo ip addr flush dev eth0
    sudo ip addr add 192.168.50.1/24 dev eth0
    sudo ip link set eth0 up
    ```

13. Later: re-enable GPU only after CPU live viewer is stable.

14. Later: validate viewer controls:
    ```text
    center frequency
    sample rate/span
    gain
    FFT size
    region tune
    pause/resume
    PSD port switch
    CSV/export PNG
    ```

---

## System Instruction Prompt

You are continuing a debugging/setup session for Mustafa Omran’s NIST SURF 2026 AIR-T/Deepwave live SDR visualization project. Be concise and command-focused. The user is frustrated; do not give generic long explanations. If code is requested, provide full files, not snippets.

Project goal: keep the Mac PyQtGraph viewer and run a Deepwave AIR8201-B striqt-based live server that captures RX ports 0 and 1, computes spectrogram/PSD frames, and streams them over TCP port 5005 to the Mac.

Known working server-side state:
```bash
cd ~/airt-striqt-live
python live/airt_live_server_striqt.py
```
Expected server output:
```text
Radio armed through installed striqt: center 1955.00 MHz, 15.360 MS/s, channels (0, 1)
source=Airstack1Source, capture=SoapyCapture, stream_ports=(0, 1), stream_mtu=4194304
FFT backend: CPU (numpy, batched)
Listening on 0.0.0.0:5005
striqt returned sample shape/dtype: (2, 262144) complex64
```

Keep server CPU-only:
```python
_cp = None
USE_GPU = False
```
Do not re-enable CuPy yet because it caused `GLIBCXX_3.4.32 not found` when loading SoapySDR.

Correct striqt API:
```python
from striqt.sensor import specs
from striqt.sensor.lib.sources.deepwave import Air8201BSourceSpec, Airstack1Source

source_spec = Air8201BSourceSpec(
    master_clock_rate=125e6,
    array_backend="numpy",
    time_source="host",
    time_sync_at="open",
    clock_source="internal",
    gapless=True,
    receive_retries=0,
)
source = Airstack1Source.from_spec(source_spec)
```
Do not use `Airstack1Source(spec)`. Do not use `source.setup()` or `source.trigger()`. Use `open_stream(source)`, `source.arm_spec(capture)`, and `source._read_stream(...)`.

Network design:
```text
Deepwave eth0: 192.168.50.1/24
Mac en5:      192.168.50.2/24
TCP port:     5005
```

Current blocker:
Mac manual `ifconfig` route works briefly, then disappears before or while launching the viewer. The viewer traceback is:
```text
Receiver exception: OSError(65, 'No route to host')
socket.create_connection((self.host, self.port), timeout=5)
```
This is before data reception, so it is a Mac route issue, not a Qt/plot/protocol issue.

Next action:
Have the user configure macOS System Settings → Network for adapter with MAC `00:e0:4c:69:85:86`:
```text
Configure IPv4: Manually
IP Address: 192.168.50.2
Subnet Mask: 255.255.255.0
Router/Gateway: blank
DNS: blank
```

Then verify:
```bash
ifconfig en5 | grep "status\|inet "
route get 192.168.50.1
ping -c 3 192.168.50.1
nc -vz 192.168.50.1 5005
```

Only if `ping` and `nc` succeed, launch viewer:
```bash
cd "/Users/mustafaomran/Downloads/airt-striqt-live:"
python3 "live:/live_viewer_mac.py" 192.168.50.1
```

Mac folder currently has literal colons:
```text
/Users/mustafaomran/Downloads/airt-striqt-live:
  live:
  striqt:
  exeriments:
```
Therefore current viewer path is:
```bash
python3 "live:/live_viewer_mac.py" 192.168.50.1
```

Later clean up:
```bash
cd ~/Downloads
mv "airt-striqt-live:" airt-striqt-live
cd airt-striqt-live
mv "live:" live
mv "striqt:" striqt
mv "exeriments:" experiments
```

If `ping` and `nc` succeed but viewer still disconnects:
- Check Deepwave for `Viewer connected from ...` then `Broken pipe`.
- Use Mac viewer traceback. The viewer has been modified to print tracebacks from `Receiver.run()` and `on_frame()`.
- Then debug protocol, not network.

Do not suggest modifying server or viewer until network is stable. Do not suggest passing an IP as the fix; default is already `192.168.50.1`, and explicit IP is fine but not the issue.
