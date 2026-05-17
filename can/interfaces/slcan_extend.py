"""
Interface for slcan compatible interfaces (win32/linux).

Extended to support custom firmware with cfg_index-based sample point selection.

Modified command format:
  Classic CAN:  S<bitrate_code><cfg_index>  e.g. "S600" = 500K index 0
  CAN FD data:  Y<fd_code><cfg_index>       e.g. "Y205" = 2Mbps index 5

If cfg_index is None (default), falls back to original SLCAN behavior.
"""

import io
import logging
import time
import warnings
from queue import SimpleQueue
from typing import Any, Optional, Union, cast

from can import BitTiming, BitTimingFd, BusABC, CanProtocol, Message, typechecking
from can.exceptions import (
    CanInitializationError,
    CanInterfaceNotImplementedError,
    CanOperationError,
    error_check,
)
from can.util import (
    CAN_FD_DLC,
    check_or_adjust_timing_clock,
    deprecated_args_alias,
    len2dlc,
)

logger = logging.getLogger(__name__)

try:
    import serial
except ImportError:
    logger.warning(
        "You won't be able to use the slcan can backend without "
        "the serial module installed!"
    )
    serial = None

# ---------------------------------------------------------------------------
# Bitrate → SLCAN command code mapping
# ---------------------------------------------------------------------------
_BITRATE_CODE = {
    10000:   0,
    20000:   1,
    50000:   2,
    100000:  3,
    125000:  4,
    250000:  5,
    500000:  6,
    750000:  7,
    1000000: 8,
    83300:   9,
}

_FD_BITRATE_CODE = {
    2000000: 2,
    5000000: 5,
}


class slcanExtendBus(BusABC):
    """
    slcan interface — extended with cfg_index support for custom sample point.

    Original usage (unchanged, backward compatible)::

        bus = can.Bus(interface="slcan", channel="/dev/ttyUSB0", bitrate=500000)

    Extended usage — pick exact sample point via table index::

        bus = can.Bus(
            interface="slcan",
            channel="/dev/ttyUSB0",
            bitrate=500000,
            bitrate_cfg_index=0,        # index 0 → 87.5% sample point
            data_bitrate=2000000,
            data_bitrate_cfg_index=5,
        )

    Use ``can.interfaces.slcan_sp_tables.lookup_cfg_index()`` to find the index
    for a desired sample point percentage.
    """

    # kept for backward compat / validation
    _BITRATES = {
        10000:   "S0",
        20000:   "S1",
        50000:   "S2",
        100000:  "S3",
        125000:  "S4",
        250000:  "S5",
        500000:  "S6",
        750000:  "S7",
        1000000: "S8",
        83300:   "S9",
    }
    _DATA_BITRATES = {
        0:       "",
        2000000: "Y2",
        5000000: "Y5",
    }

    _SLEEP_AFTER_SERIAL_OPEN = 2  # in seconds

    _OK = b"\r"
    _ERROR = b"\a"

    LINE_TERMINATOR = b"\r"

    @deprecated_args_alias(
        deprecation_start="4.5.0",
        deprecation_end="5.0.0",
        ttyBaudrate="tty_baudrate",
    )
    def __init__(
        self,
        channel: typechecking.ChannelStr,
        tty_baudrate: int = 115200,
        bitrate: Optional[int] = None,
        bitrate_cfg_index: Optional[int] = None,
        data_bitrate: Optional[int] = None,
        data_bitrate_cfg_index: Optional[int] = None,
        timing: Optional[Union[BitTiming, BitTimingFd]] = None,
        sleep_after_open: float = _SLEEP_AFTER_SERIAL_OPEN,
        rtscts: bool = False,
        listen_only: bool = False,
        timeout: float = 0.001,
        **kwargs: Any,
    ) -> None:
        """
        :param channel:
            Serial port (e.g. ``/dev/ttyUSB0``, ``COM8``).
            Can include baudrate suffix: ``/dev/ttyUSB0@115200``.
        :param tty_baudrate:
            Serial baud rate (ignored if set via channel suffix).
        :param bitrate:
            Nominal CAN bitrate in bit/s.
        :param bitrate_cfg_index:
            **[Extended]** Index into MCU lookup table for the nominal bitrate.
            Selects the exact prescaler/timeSeg combination (sample point).
            ``None`` → original behavior, send ``S<code>`` without index.
        :param data_bitrate:
            CAN FD data-phase bitrate in bit/s (2_000_000 or 5_000_000).
        :param data_bitrate_cfg_index:
            **[Extended]** Index into MCU FD data-rate lookup table.
            ``None`` → original behavior, send ``Y<code>`` without index.
        :param timing:
            Optional :class:`~can.BitTiming` / :class:`~can.BitTimingFd`
            for raw BTR-based configuration (bypasses index mechanism).
        :param sleep_after_open:
            Seconds to wait after opening the serial port.
        :param rtscts:
            Enable RTS/CTS hardware handshake.
        :param listen_only:
            Open channel in listen-only mode (``L`` command).
        :param timeout:
            Serial read timeout in seconds.

        :raise ValueError: if the channel is invalid or bitrate/btr conflict
        :raise CanInterfaceNotImplementedError: if pyserial is not installed
        :raise CanInitializationError: if the serial connection fails
        """
        self._listen_only = listen_only

        if serial is None:
            raise CanInterfaceNotImplementedError("The serial module is not installed")

        btr: Optional[str] = kwargs.get("btr", None)
        if btr is not None:
            warnings.warn(
                "The 'btr' argument is deprecated since python-can v4.5.0 "
                "and scheduled for removal in v5.0.0. "
                "Use the 'timing' argument instead.",
                DeprecationWarning,
                stacklevel=1,
            )

        if not channel:
            raise ValueError("Must specify a serial port.")
        if "@" in channel:
            (channel, baudrate) = channel.split("@")
            tty_baudrate = int(baudrate)

        with error_check(exception_type=CanInitializationError):
            self.serialPortOrig = serial.serial_for_url(
                channel,
                baudrate=tty_baudrate,
                rtscts=rtscts,
                timeout=timeout,
            )

        self._queue: SimpleQueue[str] = SimpleQueue()
        self._buffer = bytearray()
        self._can_protocol = CanProtocol.CAN_20

        time.sleep(sleep_after_open)

        with error_check(exception_type=CanInitializationError):
            if isinstance(timing, BitTiming):
                timing = check_or_adjust_timing_clock(timing, valid_clocks=[8_000_000])
                self.set_bitrate_reg(f"{timing.btr0:02X}{timing.btr1:02X}")
            elif isinstance(timing, BitTimingFd):
                self.set_bitrate(timing.nom_bitrate, timing.data_bitrate)
            else:
                if bitrate is not None and btr is not None:
                    raise ValueError("Bitrate and btr mutually exclusive.")
                if bitrate is not None:
                    self.set_bitrate(
                        bitrate,
                        data_bitrate=data_bitrate,
                        bitrate_cfg_index=bitrate_cfg_index,
                        data_bitrate_cfg_index=data_bitrate_cfg_index,
                    )
                if btr is not None:
                    self.set_bitrate_reg(btr)
            self.open()

        super().__init__(channel, **kwargs)

    # ------------------------------------------------------------------
    # Internal command builders
    # ------------------------------------------------------------------

    def _build_bitrate_cmd(
        self, bitrate: int, cfg_index: Optional[int]
    ) -> str:
        """
        Build the nominal bitrate command string.

        Without cfg_index:  ``"S6"``   (original SLCAN format)
        With    cfg_index:  ``"S683"`` (extended format: code + zero-padded index)
        """
        if bitrate not in _BITRATE_CODE:
            valid = ", ".join(str(k) for k in _BITRATE_CODE)
            raise ValueError(f"Invalid bitrate, choose one of {valid}.")
        code = _BITRATE_CODE[bitrate]
        if cfg_index is None:
            return f"S{code}"
        if cfg_index < 0:
            raise ValueError("cfg_index must be >= 0.")
        # Zero-pad to at least 2 digits so firmware can split code vs index
        return f"S{code}{cfg_index:02d}"

    def _build_data_bitrate_cmd(
        self, data_bitrate: int, cfg_index: Optional[int]
    ) -> str:
        """
        Build the FD data bitrate command string.

        Without cfg_index:  ``"Y2"``   (original SLCAN format)
        With    cfg_index:  ``"Y205"`` (extended format)
        """
        if data_bitrate == 0:
            return ""
        if data_bitrate not in _FD_BITRATE_CODE:
            valid = ", ".join(str(k) for k in _FD_BITRATE_CODE)
            raise ValueError(f"Invalid FD data bitrate, choose one of {valid}.")
        code = _FD_BITRATE_CODE[data_bitrate]
        if cfg_index is None:
            return f"Y{code}"
        if cfg_index < 0:
            raise ValueError("data_bitrate_cfg_index must be >= 0.")
        return f"Y{code}{cfg_index:02d}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_bitrate(
        self,
        bitrate: int,
        data_bitrate: Optional[int] = None,
        bitrate_cfg_index: Optional[int] = None,
        data_bitrate_cfg_index: Optional[int] = None,
    ) -> None:
        """
        Set nominal (and optionally FD data) bitrate.

        :param bitrate:
            Nominal bitrate in bit/s.
        :param data_bitrate:
            FD data-phase bitrate in bit/s, or ``None`` / 0 to disable FD.
        :param bitrate_cfg_index:
            Lookup-table row for the nominal bitrate (extended firmware only).
            ``None`` = use original one-byte command.
        :param data_bitrate_cfg_index:
            Lookup-table row for the FD data bitrate (extended firmware only).
            ``None`` = use original one-byte command.

        :raise ValueError: if bitrate or data_bitrate is invalid
        """
        nom_cmd = self._build_bitrate_cmd(bitrate, bitrate_cfg_index)

        _data_bitrate = data_bitrate if data_bitrate is not None else 0
        fd_cmd = self._build_data_bitrate_cmd(_data_bitrate, data_bitrate_cfg_index)

        self._can_protocol = (
            CanProtocol.CAN_FD if _data_bitrate != 0 else CanProtocol.CAN_20
        )

        self.close()
        self._write(nom_cmd)
        if fd_cmd:
            self._write(fd_cmd)
        self.open()

    def set_bitrate_reg(self, btr: str) -> None:
        """
        :param btr:
            BTR register value to set custom can speed as a string ``xxyy`` where
            xx is the BTR0 value in hex and yy is the BTR1 value in hex.
        """
        self.close()
        self._write("s" + btr)
        self.open()

    # ------------------------------------------------------------------
    # Low-level serial helpers (unchanged from upstream)
    # ------------------------------------------------------------------

    def _write(self, string: str) -> None:
        if not string:
            return
        with error_check("Could not write to serial device"):
            self.serialPortOrig.write(string.encode() + self.LINE_TERMINATOR)
            self.serialPortOrig.flush()

    def _read(self, timeout: Optional[float]) -> Optional[str]:
        _timeout = serial.Timeout(timeout)

        with error_check("Could not read from serial device"):
            while True:
                in_waiting = self.serialPortOrig.in_waiting
                for _ in range(max(1, in_waiting)):
                    new_byte = self.serialPortOrig.read(1)
                    if new_byte:
                        self._buffer.extend(new_byte)
                    else:
                        break

                    if new_byte in (self._ERROR, self._OK):
                        string = self._buffer.decode()
                        self._buffer.clear()
                        return string

                if _timeout.expired():
                    break

            return None

    def flush(self) -> None:
        self._buffer.clear()
        with error_check("Could not flush"):
            self.serialPortOrig.reset_input_buffer()

    def open(self) -> None:
        if self._listen_only:
            self._write("L")
        else:
            self._write("O")

    def close(self) -> None:
        self._write("C")

    # ------------------------------------------------------------------
    # Receive / Send (unchanged from upstream)
    # ------------------------------------------------------------------

    def _recv_internal(
        self, timeout: Optional[float]
    ) -> tuple[Optional[Message], bool]:
        canId = None
        remote = False
        extended = False
        data = None
        isFd = False
        fdBrs = False

        if self._queue.qsize():
            string: Optional[str] = self._queue.get_nowait()
        else:
            string = self._read(timeout)

        if not string:
            pass
        elif string[0] in (
            "T",
            "x",
        ):
            # extended frame
            canId = int(string[1:9], 16)
            dlc = int(string[9])
            extended = True
            data = bytearray.fromhex(string[10: 10 + dlc * 2])
        elif string[0] == "t":
            # normal frame
            canId = int(string[1:4], 16)
            dlc = int(string[4])
            data = bytearray.fromhex(string[5: 5 + dlc * 2])
        elif string[0] == "r":
            # remote frame
            canId = int(string[1:4], 16)
            dlc = int(string[4])
            remote = True
        elif string[0] == "R":
            # remote extended frame
            canId = int(string[1:9], 16)
            dlc = int(string[9])
            extended = True
            remote = True
        elif string[0] == "d":
            # FD standard frame
            canId = int(string[1:4], 16)
            dlc = int(string[4], 16)
            isFd = True
            data = bytearray.fromhex(string[5: 5 + CAN_FD_DLC[dlc] * 2])
        elif string[0] == "D":
            # FD extended frame
            canId = int(string[1:9], 16)
            dlc = int(string[9], 16)
            extended = True
            isFd = True
            data = bytearray.fromhex(string[10: 10 + CAN_FD_DLC[dlc] * 2])
        elif string[0] == "b":
            # FD with bitrate switch
            canId = int(string[1:4], 16)
            dlc = int(string[4], 16)
            isFd = True
            fdBrs = True
            data = bytearray.fromhex(string[5: 5 + CAN_FD_DLC[dlc] * 2])
        elif string[0] == "B":
            # FD extended with bitrate switch
            canId = int(string[1:9], 16)
            dlc = int(string[9], 16)
            extended = True
            isFd = True
            fdBrs = True
            data = bytearray.fromhex(string[10: 10 + CAN_FD_DLC[dlc] * 2])

        if canId is not None:
            msg = Message(
                arbitration_id=canId,
                is_extended_id=extended,
                timestamp=time.time(),
                is_remote_frame=remote,
                is_fd=isFd,
                bitrate_switch=fdBrs,
                dlc=CAN_FD_DLC[dlc],
                data=data,
            )
            return msg, False
        return None, False

    def send(self, msg: Message, timeout: Optional[float] = None) -> None:
        if timeout != self.serialPortOrig.write_timeout:
            self.serialPortOrig.write_timeout = timeout
        if msg.is_remote_frame:
            if msg.is_extended_id:
                sendStr = f"R{msg.arbitration_id:08X}{msg.dlc:d}"
            else:
                sendStr = f"r{msg.arbitration_id:03X}{msg.dlc:d}"
        elif msg.is_fd:
            fd_dlc = len2dlc(msg.dlc)
            if msg.bitrate_switch:
                if msg.is_extended_id:
                    sendStr = f"B{msg.arbitration_id:08X}{fd_dlc:X}"
                else:
                    sendStr = f"b{msg.arbitration_id:03X}{fd_dlc:X}"
                sendStr += msg.data.hex().upper()
            else:
                if msg.is_extended_id:
                    sendStr = f"D{msg.arbitration_id:08X}{fd_dlc:X}"
                else:
                    sendStr = f"d{msg.arbitration_id:03X}{fd_dlc:X}"
                sendStr += msg.data.hex().upper()
        else:
            if msg.is_extended_id:
                sendStr = f"T{msg.arbitration_id:08X}{msg.dlc:d}"
            else:
                sendStr = f"t{msg.arbitration_id:03X}{msg.dlc:d}"
            sendStr += msg.data.hex().upper()
        self._write(sendStr)

    def shutdown(self) -> None:
        super().shutdown()
        self.close()
        with error_check("Could not close serial socket"):
            self.serialPortOrig.close()

    def fileno(self) -> int:
        try:
            return cast("int", self.serialPortOrig.fileno())
        except io.UnsupportedOperation:
            raise NotImplementedError(
                "fileno is not implemented using current CAN bus on this platform"
            ) from None
        except Exception as exception:
            raise CanOperationError("Cannot fetch fileno") from exception

    def get_version(
        self, timeout: Optional[float]
    ) -> tuple[Optional[int], Optional[int]]:
        """Get HW and SW version of the slcan interface."""
        _timeout = serial.Timeout(timeout)
        cmd = "V"
        self._write(cmd)

        while True:
            if string := self._read(_timeout.time_left()):
                if string[0] == cmd:
                    hw_version = int(string[1:3])
                    sw_version = int(string[3:5])
                    return hw_version, sw_version
                else:
                    self._queue.put_nowait(string)
            if _timeout.expired():
                break
        return None, None

    def get_serial_number(self, timeout: Optional[float]) -> Optional[str]:
        """Get serial number of the slcan interface."""
        _timeout = serial.Timeout(timeout)
        cmd = "N"
        self._write(cmd)

        while True:
            if string := self._read(_timeout.time_left()):
                if string[0] == cmd:
                    serial_number = string[1:-1]
                    return serial_number
                else:
                    self._queue.put_nowait(string)
            if _timeout.expired():
                break
        return None
