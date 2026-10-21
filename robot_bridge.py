# -*- coding: utf-8 -*-
"""
Robot bridge - ElectronBot USB communication layer (Python)
==========================================================

Replicates the ElectronBot C++ SDK USB CDC communication protocol.
Uses pyusb + libusb to communicate directly with STM32 firmware.

Protocol reference:
  ElectronBot/3.Software/SDK/ElectronBotSDK-LowLevel/src/electron_low_level.cpp

USB parameters:
  VID: 0x1001, PID: 0x8023
  Screen: 240 x 240 RGB (172,800 bytes per frame)
  extraData: 32 bytes (servo commands + readback data)

Protocol flow (SyncTask, 4-round ping-pong):
  Each round:
    1. Receive 32 bytes extraData from device (servo angle readback)
    2. Send 84 × 512 = 43,008 bytes frame chunk
    3. Send 224 bytes = 192 bytes frame tail + 32 bytes extraData (servo commands)
  Total: 4 × (43008 + 192) = 172,800 = 240×240×3

extraData format (32 bytes):
  byte 0:        enable flag (0/1)
  bytes 1-4:     servo 1 angle (float, little-endian)
  bytes 5-8:     servo 2 angle
  bytes 9-12:    servo 3 angle
  bytes 13-16:   servo 4 angle
  bytes 17-20:   servo 5 angle
  bytes 21-24:   servo 6 angle
  bytes 25-31:   reserved
"""
import struct
import threading
import time
import logging
from typing import Optional, Tuple
from io import BytesIO

import usb.core
import usb.util

logger = logging.getLogger("robot_bridge")

# === Constants ===
# Two firmware variants exist — same protocol, different USB IDs.
#   LibUsb variant:  VID=0x1001  PID=0x8023  (needs Zadig/WinUSB driver)
#   WinUsb variant:  VID=0x5241  PID=0x5241  (default Windows driver, no Zadig)
USB_DEVICES = [
    (0x1001, 0x8023),  # LibUsb firmware
    (0x5241, 0x5241),  # WinUsb firmware
]
USB_VID = USB_DEVICES[0][0]   # kept for backward compatibility
USB_PID = USB_DEVICES[0][1]
SCREEN_SIZE = 240  # 240 × 240
FRAME_SIZE = SCREEN_SIZE * SCREEN_SIZE * 3  # 172,800 bytes
CHUNK_SIZE = 43008  # bytes per round = 84 × 512
TAIL_SIZE = 192     # frame tail per round
EXTRA_SIZE = 32     # extraData size
TAIL_PACKET = TAIL_SIZE + EXTRA_SIZE  # 224
BULK_PACKET_SIZE = 512
SYNC_PASSES = 4  # 4 ping-pong rounds

# Servo joint angle limits — hardware safety guards.
# Values outside these ranges risk physical stall / burnout.
JOINT_LIMITS: dict[str, tuple[float, float]] = {
    "j1": (-5, 25),    # head
    "j2": (-5, 30),    # left arm roll
    "j3": (-20, 200),  # left arm pitch
    "j4": (-10, 50),   # right arm roll
    "j5": (-20, 200),  # right arm pitch
    "j6": (-90, 90),   # waist
}


def _clamp_joints(j1: float, j2: float, j3: float,
                  j4: float, j5: float, j6: float) -> tuple[list[float], list[str]]:
    """Clamp joint angles to safe ranges. Returns (clamped_values, warnings)."""
    limits = [
        ("j1", j1, JOINT_LIMITS["j1"]),
        ("j2", j2, JOINT_LIMITS["j2"]),
        ("j3", j3, JOINT_LIMITS["j3"]),
        ("j4", j4, JOINT_LIMITS["j4"]),
        ("j5", j5, JOINT_LIMITS["j5"]),
        ("j6", j6, JOINT_LIMITS["j6"]),
    ]
    clamped: list[float] = []
    warnings: list[str] = []
    for name, val, (lo, hi) in limits:
        if val < lo:
            warnings.append(f"{name}={val:.1f} clamped to min {lo}")
            clamped.append(lo)
        elif val > hi:
            warnings.append(f"{name}={val:.1f} clamped to max {hi}")
            clamped.append(hi)
        else:
            clamped.append(val)
    return clamped, warnings


class ElectronBot:
    """ElectronBot USB device controller"""

    def __init__(self, vid: int = USB_VID, pid: int = USB_PID):
        self.vid = vid
        self.pid = pid
        self._dev: Optional[usb.core.Device] = None
        self._ep_out: Optional[int] = None
        self._ep_in: Optional[int] = None
        self._iface: int = 0
        self.is_connected = False
        self._lock = threading.Lock()
        self._sync_thread: Optional[threading.Thread] = None
        self._stop_sync = threading.Event()

        # Double-buffer (ping-pong)
        self._pingpong = 0
        self._frame_buf = [bytearray(FRAME_SIZE), bytearray(FRAME_SIZE)]
        self._extra_tx = [bytearray(EXTRA_SIZE), bytearray(EXTRA_SIZE)]
        self._extra_rx = bytearray(EXTRA_SIZE)

        # Servo angle readback
        self._joint_angles = [0.0] * 6

    # ==================== Connection management ====================

    def _release_device(self) -> None:
        """Safely release USB device resources and clear the reference."""
        if self._dev is None:
            return
        try:
            usb.util.dispose_resources(self._dev)
        except Exception:
            pass
        self._dev = None

    def connect(self) -> bool:
        """Connect to ElectronBot device.

        Tries all known VID/PID pairs (LibUsb firmware then WinUsb firmware)
        so both driver configurations work without manual switching.
        """
        if self.is_connected:
            return True

        # Try each known VID/PID pair
        for vid, pid in USB_DEVICES:
            try:
                dev = usb.core.find(idVendor=vid, idProduct=pid)
                if dev is None:
                    continue

                self._dev = dev
                self.vid = vid
                self.pid = pid

                # Detach kernel driver from both interfaces if needed
                for if_idx in (0, 1):
                    try:
                        if dev.is_kernel_driver_active(if_idx):
                            dev.detach_kernel_driver(if_idx)
                    except Exception:
                        pass

                # Set configuration
                try:
                    dev.set_configuration()
                except usb.core.USBError:
                    pass

                cfg = dev.get_active_configuration()

                # Scan all interfaces for bulk endpoints (interface layout
                # may differ between LibUsb and WinUsb firmware variants).
                ep_out_addr = None
                ep_in_addr = None
                found_iface = None
                for if_idx in range(cfg.bNumInterfaces):
                    iface = cfg[(if_idx, 0)]
                    for ep in iface:
                        addr = ep.bEndpointAddress
                        if usb.util.endpoint_direction(addr) == usb.util.ENDPOINT_OUT:
                            if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK:
                                if ep_out_addr is None or addr < 0x80:
                                    ep_out_addr = addr
                        elif usb.util.endpoint_direction(addr) == usb.util.ENDPOINT_IN:
                            if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK:
                                if ep_in_addr is None:
                                    ep_in_addr = addr
                    if ep_out_addr and ep_in_addr:
                        found_iface = if_idx
                        break

                if ep_out_addr is None or ep_in_addr is None:
                    logger.warning(f"No bulk endpoints on VID={vid:#06x} PID={pid:#06x}")
                    self._release_device()
                    continue

                self._ep_out = ep_out_addr
                self._ep_in = ep_in_addr
                self._iface = found_iface if found_iface is not None else 0
                self.is_connected = True
                self._stop_sync.clear()

                logger.info(
                    f"Connected to ElectronBot VID={vid:#06x} PID={pid:#06x} "
                    f"(EP_OUT={self._ep_out:#04x}, EP_IN={self._ep_in:#04x})"
                )
                return True

            except usb.core.USBError as e:
                logger.warning(f"USB error on VID={vid:#06x} PID={pid:#06x}: {e}")
                self._release_device()
                continue
            except Exception as e:
                logger.warning(f"Connect failed on VID={vid:#06x} PID={pid:#06x}: {e}")
                self._release_device()
                continue

        logger.error(
            f"ElectronBot not found. Tried: "
            f"{', '.join(f'VID={v:#06x}/PID={p:#06x}' for v, p in USB_DEVICES)}"
        )
        return False

    def disconnect(self) -> bool:
        """Disconnect from ElectronBot"""
        was_connected = self.is_connected
        self.is_connected = False  # mark disconnected immediately
        self._stop_sync.set()

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=12.0)  # ≥ worst-case USB read (5s) + retries

        self._release_device()

        if was_connected:
            logger.info("Disconnected from ElectronBot")
        return True

    # ==================== USB low-level I/O ====================

    def _receive_packet(self, count: int, size: int, buffer: Optional[bytearray] = None) -> bool:
        """Receive a specified number of packets (mirrors ReceivePacket).

        If `buffer` is provided, the last-received packet payload is copied into it.
        For the sync task (count=1, size=32), this lets us capture the extraData
        readback without a second USB read that would stall waiting for the next
        frame chunk.

        Returns False on timeout or persistent USB error after max_retries.
        """
        remaining = count
        last_data: Optional[bytes] = None
        fail_streak = 0
        max_fails = 20  # ~100s total with 5s timeout

        while remaining > 0:
            try:
                data = self._dev.read(self._ep_in, size, timeout=5000)
                if len(data) != size:
                    fail_streak += 1
                    if fail_streak > max_fails:
                        logger.error("_receive_packet: too many short reads, aborting")
                        return False
                    continue
                last_data = data
                remaining -= 1
                fail_streak = 0
            except usb.core.USBError as e:
                fail_streak += 1
                if fail_streak > max_fails:
                    logger.error(f"_receive_packet: persistent USB error after {max_fails} attempts: {e}")
                    return False
                continue
        if buffer is not None and last_data is not None:
            buffer[:] = last_data
        return remaining == 0

    def _transmit_packet(self, buf: bytes, count: int, size: int) -> bool:
        """Transmit a specified number of packets (mirrors TransmitPacket).

        USB specification requirement: when a bulk OUT transfer size is an
        exact multiple of the endpoint's max packet size (512 for high-speed
        bulk), a zero-length packet (ZLP) MUST be sent to signal the end of
        the transfer.  Without ZLPs the device's USB hardware will not flush
        received data to the MCU, and the protocol will stall.

        Returns False on persistent USB error after max_retries.
        """
        remaining = count
        offset = 0
        fail_streak = 0
        max_fails = 20

        while remaining > 0:
            try:
                written = self._dev.write(
                    self._ep_out,
                    buf[offset:offset + size],
                    timeout=2000
                )
                if written != size:
                    fail_streak += 1
                    if fail_streak > max_fails:
                        logger.error("_transmit_packet: too many short writes, aborting")
                        return False
                    continue

                # Send ZLP when transfer is a multiple of the bulk packet size
                if size % BULK_PACKET_SIZE == 0:
                    try:
                        self._dev.write(self._ep_out, b"", timeout=2000)
                    except usb.core.USBError:
                        pass  # ZLP may fail on some backends; non-critical

                offset += size
                remaining -= 1
                fail_streak = 0
            except usb.core.USBError as e:
                fail_streak += 1
                if fail_streak > max_fails:
                    logger.error(f"_transmit_packet: persistent USB error after {max_fails} attempts: {e}")
                    return False
                continue
        return remaining == 0

    # ==================== Sync task ====================

    def start_sync(self) -> bool:
        """Start background sync thread"""
        if not self.is_connected:
            return False

        self._stop_sync.clear()
        self._sync_thread = threading.Thread(
            target=self._sync_loop,
            daemon=True,
            name="robot_sync"
        )
        self._sync_thread.start()
        return True

    def stop_sync(self):
        """Stop sync thread"""
        self._stop_sync.set()

    def _sync_loop(self):
        """Main sync loop: continuously send frame data + extraData to device and read back"""
        logger.info("Sync thread started")
        frame_offset = 0
        fail_count = 0

        while not self._stop_sync.is_set():
            try:
                index = self._pingpong
                self._pingpong = 1 - self._pingpong
                frame_offset = 0

                for p in range(SYNC_PASSES):
                    if self._stop_sync.is_set():
                        break

                    # 1. Wait for MCU request, receive 32 bytes extraData readback
                    self._receive_packet(1, EXTRA_SIZE, buffer=self._extra_rx)

                    # Parse servo angle values from the received data
                    self._parse_rx_extra()

                    # 2. Send frame chunk (84 × 512 = 43008)
                    chunk = bytes(self._frame_buf[index][frame_offset:frame_offset + CHUNK_SIZE])
                    self._transmit_packet(chunk, 84, BULK_PACKET_SIZE)
                    frame_offset += CHUNK_SIZE

                    # 3. Assemble 224-byte tail packet (192 frame tail + 32 extraData)
                    tail_packet = bytearray(TAIL_PACKET)
                    tail_packet[:TAIL_SIZE] = self._frame_buf[index][
                        frame_offset:frame_offset + TAIL_SIZE
                    ]
                    tail_packet[TAIL_SIZE:] = self._extra_tx[index]
                    self._transmit_packet(bytes(tail_packet), 1, TAIL_PACKET)
                    frame_offset += TAIL_SIZE

                fail_count = 0
                time.sleep(0.001)

            except Exception as e:
                fail_count += 1
                logger.error(f"Sync loop error (fail #{fail_count}): {e}")
                if fail_count > 3:
                    logger.critical("Sync loop failing repeatedly, marking disconnected")
                    self.is_connected = False
                    break
                time.sleep(0.5)

    def _parse_rx_extra(self):
        """Parse servo angles from self._extra_rx (filled by _receive_packet in sync loop)."""
        try:
            with self._lock:
                for j in range(6):
                    offset = 1 + j * 4
                    self._joint_angles[j] = struct.unpack_from('<f', self._extra_rx, offset)[0]
        except Exception:
            pass

    # ==================== Image setting ====================

    def set_image(self, image_data: bytes, fmt: str = "rgb") -> bool:
        """
        Set the screen image.

        Args:
            image_data: Raw image bytes (RGB or JPEG/PNG)
            fmt: Format - "rgb" (240×240×3 raw), "jpeg", "png"
        """
        if fmt == "rgb" and len(image_data) == FRAME_SIZE:
            raw = image_data
        elif fmt in ("jpeg", "png"):
            try:
                from PIL import Image
                img = Image.open(BytesIO(image_data))
                img = img.convert("RGB")
                img = img.resize((SCREEN_SIZE, SCREEN_SIZE), Image.LANCZOS)
                # The ElectronBot firmware expects BGR888 byte order.
                # Swap R and B channels.
                r, g, b = img.split()
                img = Image.merge("RGB", (b, g, r))
                raw = img.tobytes()
            except ImportError:
                logger.error("Pillow not installed, cannot decode image")
                return False
            except Exception as e:
                logger.error(f"Image decode failed: {e}")
                return False
        else:
            if len(image_data) == FRAME_SIZE:
                raw = image_data
            else:
                logger.error(f"Image data size mismatch: {len(image_data)} != {FRAME_SIZE}")
                return False

        if len(raw) != FRAME_SIZE:
            logger.error(f"Converted image size {len(raw)} != {FRAME_SIZE}")
            return False

        with self._lock:
            self._frame_buf[0][:] = raw
            self._frame_buf[1][:] = raw
        return True

    def set_image_numpy(self, ndarray) -> bool:
        """Set image from numpy array (OpenCV format compatible)"""
        try:
            import numpy as np
            # Ensure it is 240×240×3 RGB
            if ndarray.shape != (SCREEN_SIZE, SCREEN_SIZE, 3):
                from PIL import Image
                img = Image.fromarray(ndarray)
                img = img.resize((SCREEN_SIZE, SCREEN_SIZE), Image.LANCZOS)
                img = img.convert("RGB")
                raw = img.tobytes()
            else:
                raw = ndarray.astype(np.uint8).tobytes()
            return self.set_image(raw, fmt="rgb")
        except ImportError:
            logger.error("numpy not installed")
            return False
        except Exception as e:
            logger.error(f"numpy image set failed: {e}")
            return False

    def set_black_screen(self):
        """Set black screen"""
        raw = b'\x00' * FRAME_SIZE
        with self._lock:
            self._frame_buf[0][:] = raw
            self._frame_buf[1][:] = raw

    # ==================== Servo control ====================

    def set_joints(self, j1: float, j2: float, j3: float,
                   j4: float, j5: float, j6: float,
                   enable: bool = True) -> tuple[bool, list[str]]:
        """
        Set target angles for all 6 servos. Angles are auto-clamped to safe ranges.

        Args:
            j1..j6: Servo angles (degrees, float)
            enable: Whether to enable servos

        Returns:
            (ok, warnings) — warnings is a list of clamp messages (empty if all safe).
        """
        clamped, warnings = _clamp_joints(j1, j2, j3, j4, j5, j6)
        c1, c2, c3, c4, c5, c6 = clamped
        if warnings:
            logger.warning(f"[JOINT CLAMP] {', '.join(warnings)}")

        with self._lock:
            # Write to BOTH ping-pong buffers so the sync loop always
            # transmits the same servo data regardless of which buffer
            # is active this frame.
            for buf in (self._extra_tx[0], self._extra_tx[1]):
                buf[0] = 1 if enable else 0
                struct.pack_into('<f', buf, 1, c1)
                struct.pack_into('<f', buf, 5, c2)
                struct.pack_into('<f', buf, 9, c3)
                struct.pack_into('<f', buf, 13, c4)
                struct.pack_into('<f', buf, 17, c5)
                struct.pack_into('<f', buf, 21, c6)
                # bytes 25-31 remain zero
        return True, warnings

    def get_joints(self) -> Tuple[float, ...]:
        """Get current read-back servo angles (from device)"""
        with self._lock:
            return tuple(self._joint_angles)

    def set_extra_data(self, data: bytes):
        """Set custom extraData (max 32 bytes)"""
        if len(data) > EXTRA_SIZE:
            data = data[:EXTRA_SIZE]
        with self._lock:
            self._extra_tx[self._pingpong][:len(data)] = data

    def get_extra_data(self) -> bytes:
        """Get read-back extraData"""
        with self._lock:
            return bytes(self._extra_rx)

    # ==================== Convenience methods ====================

    def get_status(self) -> dict:
        """Get device status"""
        with self._lock:
            return {
                "connected": self.is_connected,
                "joints": list(self._joint_angles),
            }

    def self_test(self) -> dict:
        """Check whether ElectronBot hardware is reachable via USB."""
        found_vid = None
        found_pid = None
        found_dev = None
        for vid, pid in USB_DEVICES:
            try:
                dev = usb.core.find(idVendor=vid, idProduct=pid)
                if dev is not None:
                    found_vid = vid
                    found_pid = pid
                    found_dev = dev
                    break
            except Exception:
                pass

        # Release the temporary device reference
        if found_dev is not None:
            try:
                usb.util.dispose_resources(found_dev)
            except Exception:
                pass

        found = found_vid is not None
        tried = ", ".join(f"VID={v:#06x}/PID={p:#06x}" for v, p in USB_DEVICES)
        return {
            "found": found,
            "connected": self.is_connected,
            "vid": f"0x{found_vid:04x}" if found_vid else None,
            "pid": f"0x{found_pid:04x}" if found_pid else None,
            "message": (
                f"Device found (VID=0x{found_vid:04x}, PID=0x{found_pid:04x})."
                if found
                else f"No ElectronBot found. Tried: {tried}. Check USB cable."
            ),
        }


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------
_ROBOT_INSTANCE: Optional[ElectronBot] = None
_ROBOT_LOCK = threading.Lock()


def get_robot() -> ElectronBot:
    """Get or create the singleton ElectronBot instance."""
    global _ROBOT_INSTANCE
    if _ROBOT_INSTANCE is None:
        with _ROBOT_LOCK:
            if _ROBOT_INSTANCE is None:
                _ROBOT_INSTANCE = ElectronBot()
    return _ROBOT_INSTANCE
