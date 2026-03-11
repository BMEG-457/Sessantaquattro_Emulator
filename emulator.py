"""
Sessantaquattro+ Device Emulator

Emulates the OT Bioelettronica Sessantaquattro+ by connecting as a TCP client
to the app's server and streaming synthetic EMG data in the exact protocol format.

Usage:
    python emulator.py                          # defaults: localhost:45454, sine signal
    python emulator.py --host 192.168.1.1       # connect to specific IP
    python emulator.py --signal emg             # realistic synthetic EMG
    python emulator.py --signal ramp            # linear ramp (device test mode)
    python emulator.py --signal noise           # EMG with artifacts (60Hz, dropout)
"""

import argparse
import socket
import struct
import sys
import time

import numpy as np


# ---------------------------------------------------------------------------
# Protocol constants — per Sessantaquattro+ TCP Communication Protocol v2.1
# ---------------------------------------------------------------------------

DEFAULT_PORT = 45454


def get_num_channels(nch_code, mode):
    # MODE=3 (accelerometers): always 8 bio + 2 AUX + 2 acc = 12, regardless of NCH
    if mode == 3:
        return 12
    # NCH sets bio channels; MODE=1 (bipolar AD8x1SP) halves them
    # Total = bio + 2 AUX + 2 accessory (+4 overhead matched to app's expectation)
    table = {
        0: {0: 16, 1: 12},
        1: {0: 24, 1: 16},
        2: {0: 40, 1: 24},
        3: {0: 72, 1: 40},
    }
    return table.get(nch_code, {}).get(mode if mode in (0, 1) else 0, 72)


def get_sampling_frequency(fsamp_code, mode):
    if mode == 3:
        return {0: 2000, 1: 4000, 2: 8000, 3: 16000}.get(fsamp_code, 2000)
    return {0: 500, 1: 1000, 2: 2000, 3: 4000}.get(fsamp_code, 2000)


def parse_command(raw: bytes):
    """Parse the 2-byte command sent by the app.

    Wire format (big-endian, 2 bytes = Control Byte 0 | Control Byte 1):
      Byte 0: [GETSET | FSAMP1 | FSAMP0 | NCH1 | NCH0 | MODE2 | MODE1 | MODE0]
      Byte 1: [HRES   | HPF    | GAIN1  | GAIN0 | TRIG1 | TRIG0 | REC   | GO/STOP]
    """
    (cmd,) = struct.unpack(">H", raw)  # unsigned to cleanly inspect bit 15
    return {
        # Control Byte 1 (low byte)
        "go":     (cmd >> 0) & 0x1,
        "rec":    (cmd >> 1) & 0x1,
        "trig":   (cmd >> 2) & 0x3,
        "gain":   (cmd >> 4) & 0x3,   # preamp gain (00=8/2, 01=4, 10=6, 11=8)
        "hpf":    (cmd >> 6) & 0x1,
        "hres":   (cmd >> 7) & 0x1,
        # Control Byte 0 (high byte)
        "mode":   (cmd >> 8) & 0x7,
        "nch":    (cmd >> 11) & 0x3,
        "fsamp":  (cmd >> 13) & 0x3,
        "getset": (cmd >> 15) & 0x1,  # 0=SET command, 1=GET info
        "raw":    cmd,
    }


# Emulated device info for GET commands
FIRMWARE_VERSION = (1, 26)   # v1.26
BATTERY_LEVEL = 85           # percentage


def handle_get_command(sock, info_code):
    """Respond to a GET command (GETSET=1) based on INFO<2:0> bits."""
    if info_code == 0b000:
        # Reply with 13 bytes of current settings (default config)
        settings = bytes(13)  # all zeros = default settings
        sock.sendall(settings)
        print("  GET: sent current settings (13 bytes)")
    elif info_code == 0b001:
        # Reply with 2 bytes: firmware version digits
        sock.sendall(bytes(FIRMWARE_VERSION))
        print(f"  GET: sent firmware version {FIRMWARE_VERSION[0]}.{FIRMWARE_VERSION[1]}")
    elif info_code == 0b010:
        # Reply with 1 byte: battery level percentage
        sock.sendall(bytes([BATTERY_LEVEL]))
        print(f"  GET: sent battery level {BATTERY_LEVEL}%")
    else:
        print(f"  GET: INFO code {info_code:03b} not implemented")


# ---------------------------------------------------------------------------
# Signal generators
# ---------------------------------------------------------------------------

class SignalGenerator:
    """Base class for signal generation."""

    def __init__(self, n_channels, fs):
        self.n_channels = n_channels
        self.fs = fs
        self.sample_index = 0  # running sample counter for continuity

    def generate(self, n_samples):
        """Return an (n_channels, n_samples) int16 array."""
        raise NotImplementedError


class SineGenerator(SignalGenerator):
    """Clean sine waves — each channel at a different frequency."""

    def __init__(self, n_channels, fs):
        super().__init__(n_channels, fs)
        # Spread frequencies from 5 Hz to 80 Hz across channels
        self.freqs = np.linspace(5, 80, n_channels)
        self.amplitude = 1000  # well within int16 range

    def generate(self, n_samples):
        t = (self.sample_index + np.arange(n_samples)) / self.fs
        self.sample_index += n_samples
        # (n_channels, n_samples)
        signals = (self.amplitude * np.sin(2 * np.pi * self.freqs[:, None] * t[None, :])).astype(np.int16)
        return signals


class RampGenerator(SignalGenerator):
    """Linear ramp — mimics device built-in test mode (MODE=111)."""

    def generate(self, n_samples):
        ramp = np.arange(self.sample_index, self.sample_index + n_samples, dtype=np.int16)
        self.sample_index += n_samples
        # Same ramp on every channel
        return np.tile(ramp, (self.n_channels, 1))


class EMGGenerator(SignalGenerator):
    """
    Realistic synthetic sEMG: band-limited Gaussian noise (20-450 Hz).
    Produces burst patterns to simulate muscle activation.
    """

    def __init__(self, n_channels, fs):
        super().__init__(n_channels, fs)
        self.amplitude = 500
        # Pre-compute FIR bandpass coefficients (20-450 Hz)
        self._design_filter()
        # Burst pattern: 2s on / 1s off cycle
        self.burst_period = int(3 * fs)
        self.burst_on = int(2 * fs)

    def _design_filter(self):
        """Simple FIR bandpass using windowed-sinc."""
        n_taps = 101
        nyq = self.fs / 2
        low, high = 20 / nyq, min(450 / nyq, 0.99)
        n = np.arange(n_taps)
        mid = n_taps // 2
        # High-pass component
        h_hp = np.sinc(2 * low * (n - mid)) * np.hamming(n_taps)
        h_hp[mid] = 1 - 2 * low
        h_hp = -h_hp
        h_hp[mid] += 1
        # Low-pass component
        h_lp = np.sinc(2 * high * (n - mid)) * np.hamming(n_taps)
        h_lp /= h_lp.sum()
        # Convolve to get bandpass
        self.fir = np.convolve(h_hp, h_lp)

    def generate(self, n_samples):
        # Generate white noise and filter it
        raw = np.random.randn(self.n_channels, n_samples + len(self.fir))
        filtered = np.zeros((self.n_channels, n_samples))
        for ch in range(self.n_channels):
            conv = np.convolve(raw[ch], self.fir, mode="full")
            filtered[ch] = conv[:n_samples]

        # Apply burst envelope
        envelope = np.ones(n_samples)
        for i in range(n_samples):
            phase = (self.sample_index + i) % self.burst_period
            if phase >= self.burst_on:
                envelope[i] = 0.05  # near-silence during rest

        self.sample_index += n_samples
        return (filtered * envelope[None, :] * self.amplitude).clip(-32000, 32000).astype(np.int16)


class NoiseGenerator(EMGGenerator):
    """EMG + common artifacts: 60 Hz powerline, baseline wander, channel dropout."""

    def __init__(self, n_channels, fs):
        super().__init__(n_channels, fs)
        # Pick 2 channels to randomly drop out
        self.dropout_channels = np.random.choice(n_channels, size=min(2, n_channels), replace=False)

    def generate(self, n_samples):
        data = super().generate(n_samples).astype(np.float64)
        t = (self.sample_index - n_samples + np.arange(n_samples)) / self.fs

        # 60 Hz powerline interference (all channels)
        powerline = 200 * np.sin(2 * np.pi * 60 * t)
        data += powerline[None, :]

        # Baseline wander (0.3 Hz drift)
        wander = 300 * np.sin(2 * np.pi * 0.3 * t)
        data += wander[None, :]

        # Channel dropout
        for ch in self.dropout_channels:
            data[ch, :] = 0

        return data.clip(-32000, 32000).astype(np.int16)


GENERATORS = {
    "sine": SineGenerator,
    "ramp": RampGenerator,
    "emg":  EMGGenerator,
    "noise": NoiseGenerator,
}


# ---------------------------------------------------------------------------
# Emulator core
# ---------------------------------------------------------------------------

def run_emulator(host, port, signal_type):
    print(f"Sessantaquattro+ Emulator")
    print(f"  Target:  {host}:{port}")
    print(f"  Signal:  {signal_type}")
    print()

    while True:
        # --- Connect to the app's TCP server ---
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        print(f"Connecting to {host}:{port} ...")
        try:
            sock.connect((host, port))
        except ConnectionRefusedError:
            print("  Connection refused — is the app running? Retrying in 2s ...")
            sock.close()
            time.sleep(2)
            continue
        except OSError as e:
            print(f"  Connection error: {e}. Retrying in 2s ...")
            sock.close()
            time.sleep(2)
            continue

        print("  Connected!")

        try:
            # --- Wait for command ---
            print("Waiting for command from app ...")
            cmd_data = b""
            while len(cmd_data) < 2:
                chunk = sock.recv(2 - len(cmd_data))
                if not chunk:
                    raise ConnectionError("Socket closed before command received")
                cmd_data += chunk

            cfg = parse_command(cmd_data)

            # Handle GET commands (GETSET=1)
            if cfg["getset"]:
                info_code = cfg["raw"] & 0x07
                print(f"\nGET command received (INFO={info_code:03b})")
                handle_get_command(sock, info_code)
                # After GET, wait for next command (loop back)
                continue

            # SET command (GETSET=0)
            n_channels = get_num_channels(cfg["nch"], cfg["mode"])
            fs = get_sampling_frequency(cfg["fsamp"], cfg["mode"])
            hres = cfg["hres"]
            go = cfg["go"]

            gain_labels = {0: "8 (HRES=0) / 2 (HRES=1)", 1: "4", 2: "6", 3: "8"}
            print(f"\nSET command received (0x{cfg['raw']:04X}):")
            print(f"  Channels:   {n_channels}  (NCH={cfg['nch']}, MODE={cfg['mode']})")
            print(f"  Frequency:  {fs} Hz  (FSAMP={cfg['fsamp']})")
            print(f"  Resolution: {'24-bit' if hres else '16-bit'}")
            print(f"  HPF:        {'ON' if cfg['hpf'] else 'OFF'}")
            print(f"  Gain:       {gain_labels.get(cfg['gain'], '?')}")
            print(f"  GO:         {go}")

            if not go:
                print("GO=0 — not streaming. Closing socket.")
                sock.close()
                continue

            # --- Prepare streaming ---
            samples_per_packet = fs // 16
            packet_interval = 1.0 / 16  # seconds between packets
            bytes_per_sample = 2  # 16-bit; 24-bit not yet supported
            packet_size = n_channels * samples_per_packet * bytes_per_sample

            print(f"\nStreaming: {samples_per_packet} samples/packet, "
                  f"{packet_size} bytes/packet, interval={packet_interval:.4f}s")
            print("-" * 50)

            gen = GENERATORS[signal_type](n_channels, fs)

            packet_count = 0
            t_start = time.perf_counter()
            stats_interval = 100  # print stats every N packets

            # --- Streaming loop ---
            while True:
                t_packet_start = time.perf_counter()

                # Check for incoming stop command (non-blocking)
                # Per protocol: GO/STOP=0 means "Stop and close the socket"
                sock.setblocking(False)
                try:
                    stop_data = sock.recv(2)
                    if stop_data and len(stop_data) >= 2:
                        stop_cfg = parse_command(stop_data[:2])
                        if stop_cfg["getset"]:
                            handle_get_command(sock, stop_cfg["raw"] & 0x07)
                        elif not stop_cfg["go"]:
                            print("\nStop command received — halting stream.")
                            break
                except BlockingIOError:
                    pass  # no data waiting — continue streaming
                finally:
                    sock.setblocking(True)

                # Generate and pack data
                samples = gen.generate(samples_per_packet)  # (n_channels, samples_per_packet)
                # Interleave: transpose to (samples, channels) then flatten
                interleaved = samples.T.flatten()
                packet = struct.pack(f">{len(interleaved)}h", *interleaved)

                # Send
                try:
                    sock.sendall(packet)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    print("\nConnection lost.")
                    break

                packet_count += 1
                if packet_count % stats_interval == 0:
                    elapsed = time.perf_counter() - t_start
                    rate = packet_count / elapsed
                    print(f"  Packets: {packet_count:>6}  |  Rate: {rate:.1f} pkt/s  |  "
                          f"Elapsed: {elapsed:.1f}s")

                # Pace the output
                t_elapsed = time.perf_counter() - t_packet_start
                t_sleep = packet_interval - t_elapsed
                if t_sleep > 0:
                    time.sleep(t_sleep)

        except ConnectionError as e:
            print(f"Connection error: {e}")
        except KeyboardInterrupt:
            print("\nStopped by user.")
            sock.close()
            sys.exit(0)
        finally:
            sock.close()

        print("\nReconnecting in 1s ...\n")
        time.sleep(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sessantaquattro+ Device Emulator — streams synthetic data to the OTB app"
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="App server IP (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"App server port (default: {DEFAULT_PORT})")
    parser.add_argument("--signal", choices=list(GENERATORS.keys()), default="sine",
                        help="Signal type: sine, ramp, emg, noise (default: sine)")
    args = parser.parse_args()
    run_emulator(args.host, args.port, args.signal)


if __name__ == "__main__":
    main()
