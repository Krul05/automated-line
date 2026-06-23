"""Вспомогательные функции для 7-байтовых serial-кадров Montrac IRM.

Формат кадра:
    01 group id_high id_low command checksum 03

Контрольная сумма считается как XOR полей group, id_high, id_low и command.
"""

from __future__ import annotations

from dataclasses import dataclass

START_BYTE = 0x01
END_BYTE = 0x03
FRAME_LENGTH = 7

COMMAND_START_BACKWARD = 49
COMMAND_START_FORWARD = 51
COMMAND_WRITE_ID = 53
COMMAND_PRESENCE = 83
COMMAND_LOCK = 149
COMMAND_UNLOCK = 156

MAX_BUFFER_BYTES = 1024


@dataclass(frozen=True)
class IrmMessage:
    """Разобранный IRM-кадр, полученный с serial-порта станции."""

    group: int
    shuttle_id: int
    command: int
    raw: bytes

    @property
    def is_presence(self) -> bool:
        """Вернуть True, если кадр сообщает о присутствии тележки на станции."""
        return self.command == COMMAND_PRESENCE

    @property
    def hex(self) -> str:
        """Вернуть исходный кадр в виде HEX-строки для интерфейса и отладки."""
        return self.raw.hex().upper()


def _byte(value: int, name: str) -> int:
    """Проверить, что поле протокола помещается в один байт."""
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be in range 0..255, got {value}")
    return value


def calculate_checksum(group: int, id_high: int, id_low: int, command: int) -> int:
    """Посчитать контрольную сумму XOR для четырех байтов полезной нагрузки IRM."""
    return _byte(group, "group") ^ _byte(id_high, "id_high") ^ _byte(id_low, "id_low") ^ _byte(
        command, "command"
    )


def build_frame(group: int, shuttle_id: int, command: int) -> bytes:
    """Собрать 7-байтовый IRM-кадр для команды тележке."""
    if not 0 <= shuttle_id <= 0xFFFF:
        raise ValueError(f"shuttle_id must be in range 0..65535, got {shuttle_id}")

    id_high = (shuttle_id >> 8) & 0xFF
    id_low = shuttle_id & 0xFF
    checksum = calculate_checksum(group, id_high, id_low, command)
    return bytes([START_BYTE, group, id_high, id_low, command, checksum, END_BYTE])


def parse_frame(frame: bytes) -> IrmMessage:
    """Проверить и разобрать один полный IRM-кадр."""
    if len(frame) != FRAME_LENGTH:
        raise ValueError(f"IRM frame must be {FRAME_LENGTH} bytes, got {len(frame)}")
    if frame[0] != START_BYTE or frame[-1] != END_BYTE:
        raise ValueError("IRM frame has invalid start/end bytes")

    expected = calculate_checksum(frame[1], frame[2], frame[3], frame[4])
    if frame[5] != expected:
        raise ValueError(f"IRM frame checksum mismatch: got {frame[5]:02X}, expected {expected:02X}")

    shuttle_id = (frame[2] << 8) | frame[3]
    return IrmMessage(group=frame[1], shuttle_id=shuttle_id, command=frame[4], raw=bytes(frame))


def extract_messages(buffer: bytearray) -> list[IrmMessage]:
    """Извлечь все валидные IRM-сообщения из изменяемого буфера serial-порта.

    Невалидные начальные байты отбрасываются. Неполный кадр в конце остается
    в буфере, чтобы следующее чтение из serial-порта могло его дозаполнить.
    """
    messages: list[IrmMessage] = []
    cursor = 0

    while len(buffer) - cursor >= FRAME_LENGTH:
        if buffer[cursor] != START_BYTE:
            cursor += 1
            continue

        candidate = bytes(buffer[cursor : cursor + FRAME_LENGTH])
        try:
            messages.append(parse_frame(candidate))
            cursor += FRAME_LENGTH
        except ValueError:
            cursor += 1

    if cursor:
        del buffer[:cursor]

    if len(buffer) > MAX_BUFFER_BYTES:
        del buffer[: -FRAME_LENGTH + 1]

    return messages
