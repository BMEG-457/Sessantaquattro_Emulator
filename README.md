# Sessantaquattro+ Device Emulator

Emulates the OT Bioelettronica Sessantaquattro+ over TCP, allowing the PyQt5 desktop app to be developed and tested without the physical device.

## How It Works

The real Sessantaquattro+ connects as a **TCP client** to the app's server on port `45454`. This emulator does the same: it connects to the app, receives the 2-byte configuration command, parses it, and streams synthetic sample data back in the exact binary format the device uses.

```
┌─────────────┐    TCP port 45454    ┌──────────────┐
│  PyQt5 App  │◄────────────────────►│   Emulator   │
│  (server)   │  command + data      │   (client)   │
└─────────────┘                      └──────────────┘
```

## Requirements

- Python 3.8+
- NumPy

```bash
pip install numpy
```

## Using with the App

The app does **not** run the TCP server on startup. It only opens the server socket when you press **Stream**, **Record**, or **Calibrate** in the Live Data view. The app also normally requires the PC to be on the device's `192.168.1.x` WiFi network — emulator mode bypasses this check.

### Setup (one-time)

Set the environment variable before launching the app:

```bash
# Windows (Command Prompt)
set SESSANTAQUATTRO_EMULATOR=1

# Windows (PowerShell)
$env:SESSANTAQUATTRO_EMULATOR = "1"

# macOS / Linux
export SESSANTAQUATTRO_EMULATOR=1
```

### Step-by-step workflow

1. **Start the app** with the env var set — navigate to the Live Data screen
2. **Press Stream** (or Record / Calibrate) — the app opens the TCP server on port 45454 and blocks waiting for a connection (10-second timeout)
3. **Run the emulator** within that 10-second window:
   ```bash
   python emulator.py
   ```
4. The emulator connects, receives the start command, and begins streaming synthetic data
5. **Press Stop** in the app to end the session — the emulator detects the stop command, halts, and waits to reconnect

If you start the emulator before the app opens the server, it will print "Connection refused" and retry every 2 seconds until the server is available.

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | IP address of the machine running the app |
| `--port` | `45454` | TCP port the app is listening on |
| `--signal` | `sine` | Signal type: `sine`, `ramp`, `emg`, or `noise` |

### Examples

```bash
# Default: sine waves on localhost
python emulator.py

# Connect to app on another machine
python emulator.py --host 192.168.1.100

# Realistic synthetic EMG (band-limited noise with burst patterns)
python emulator.py --signal emg

# Linear ramp (matches device test mode, MODE=111)
python emulator.py --signal ramp

# EMG with artifacts: 60 Hz powerline, baseline wander, channel dropout
python emulator.py --signal noise
```

## Signal Modes

### `sine` (default)
Clean sine waves with each channel at a different frequency (5-80 Hz spread). Use this for:
- Verifying connectivity and channel mapping
- Confirming the plotting pipeline renders all channels correctly
- Checking that channel ordering matches expectations

### `ramp`
Linear ramp on all channels, matching the device's built-in test mode (`MODE=111`). Use this for:
- Protocol-level validation (compare output against the real device's test mode)
- Verifying data integrity (any dropped or reordered samples will break the ramp)

### `emg`
Band-limited Gaussian noise shaped to the 20-450 Hz sEMG frequency band with a 2-second-on / 1-second-off burst pattern. Use this for:
- Testing the signal processing pipeline (bandpass filters, TKEO, RMS, MDF)
- Verifying that activation detection works on realistic waveforms
- UI/UX testing with physiologically plausible signals

### `noise`
Same as `emg` but with injected artifacts:
- **60 Hz powerline interference** on all channels
- **Baseline wander** (0.3 Hz drift) on all channels
- **Channel dropout** (2 random channels output zero)

Use this for:
- Stress-testing error handling and signal quality indicators
- Verifying that the notch filter removes powerline noise
- Confirming that dead-channel detection works

## Protocol Details

Implements the Sessantaquattro+ TCP Communication Protocol v2.1 (firmware v1.26+).

### Command Format

The app sends a 2-byte big-endian command. The emulator parses it as two control bytes:

**Control Byte 0** (high byte):

| Bit | Field | Values |
|-----|-------|--------|
| 7 | GETSET | 0 = SET (configure & stream), 1 = GET (query info) |
| 6-5 | FSAMP | 00=500 Hz, 01=1000 Hz, 10=2000 Hz, 11=4000 Hz |
| 4-3 | NCH | 00=8+4, 01=16+4, 10=32+4, 11=64+4 channels |
| 2-0 | MODE | 000=Monopolar, 001=Bipolar, 010=Differential, 011=Accelerometers, 111=Test |

**Control Byte 1** (low byte):

| Bit | Field | Values |
|-----|-------|--------|
| 7 | HRES | 0=16-bit, 1=24-bit samples |
| 6 | HPF | 0=DC, 1=High-pass filter on (cutoff = Fsamp/190) |
| 5-4 | GAIN | 00=8/2, 01=4, 10=6, 11=8 (preamp gain) |
| 3-2 | TRIG | 00=GO/STOP bit, 01=internal, 10=external, 11=button/REC |
| 1 | REC | SD card recording (0=stop, 1=record) |
| 0 | GO/STOP | 1=start streaming, 0=stop and close socket |

### Data Format

- **Encoding:** Big-endian signed 16-bit integers (`>h`)
- **Layout:** Samples are interleaved — each sample frame contains one value per channel, then the next frame, etc.
- **Packet size:** `channels * 2 * (frequency / 16)` bytes
- **Packet rate:** 16 packets/second (fixed, regardless of sample rate)

Example at default settings (72 channels, 2000 Hz):
- 125 samples per packet
- 18,000 bytes per packet
- 16 packets/second = 288,000 bytes/second

### GET Commands

When the app sends a command with `GETSET=1`, the emulator responds with:

| INFO code | Response |
|-----------|----------|
| `000` | 13 bytes of current device settings |
| `001` | 2 bytes: firmware version (1, 26) |
| `010` | 1 byte: battery level (85%) |

## Testing with Android (Kivy App)

A build flag in `mobile_app/app/core/config.py` controls emulator mode for APK builds. Set it before building to bypass the 192.168.1.x WiFi check; your phone and PC just need to be on the same WiFi network.

### Build and install

In `mobile_app/app/core/config.py`, set the build flag:

```python
EMULATOR_BUILD = True
```

Then build and install:

```bash
# In the mobile_app directory
buildozer android debug
adb install -r bin/*.apk
```

### Step-by-step workflow

1. Connect your phone and PC to the **same WiFi network**
2. Find your phone's local IP: **Settings > WiFi > tap the network > IP address**
3. Open the app on your phone > **Live Data** > press **Stream**
4. Within 10 seconds, run on your PC:
   ```bash
   python emulator.py --host <phone-ip>
   # e.g. python emulator.py --host 192.168.0.42
   ```
5. The emulator connects and the phone displays live streaming data

### Before building a production APK

Revert the flag in `mobile_app/app/core/config.py`:

```python
EMULATOR_BUILD = False
```

## Debugging Live Plot Lag

This emulator is particularly useful for isolating live plotting performance issues. The recommended approach:

1. Start with default settings and confirm plots render smoothly
2. If lag exists, reduce channels: modify the app to send `NCH=00` (16 channels) and see if lag disappears
3. If still laggy at 16 channels, the bottleneck is render rate, not data volume
4. Profile your rendering loop — aim for 20-30 redraws/second maximum, decoupled from the data ingestion rate

The emulator prints packet rate statistics every 100 packets so you can verify it's sending data at the correct rate (16 pkt/s).

## Limitations

- **16-bit resolution only.** 24-bit (`HRES=1`) sample packing is not implemented.
- **No WiFi AP emulation.** The emulator connects over localhost or your existing network. Desktop builds use the `SESSANTAQUATTRO_EMULATOR=1` env var; Android APKs use the `EMULATOR_BUILD` flag in `config.py` (see "Testing with Android" above).
- **No real physiological data.** The emulator tests your software pipeline and communication layer only. Final validation must use the real device with actual electrodes and a human subject.
- **SD card recording commands (`REC`) are acknowledged but ignored** since there is no physical storage to write to.
