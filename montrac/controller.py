"""Рабочий контроллер линии Montrac.

Контроллер хранит логическую карту станций и перегонов, принимает IRM-кадры
присутствия от serial-воркеров, отправляет команды START FORWARD, применяет
автоматические режимы и сохраняет редактируемую конфигурацию станций/режимов.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .protocol import COMMAND_PRESENCE, COMMAND_START_FORWARD, IrmMessage, build_frame, extract_messages


MAX_SEGMENT_SHUTTLES_BEFORE_RELEASE = 0


class OperatingMode(str, Enum):
    """Встроенные идентификаторы режимов для обратной совместимости API."""

    IDLE = "idle"
    STOP_2_4 = "stop_2_4"
    STOP_ALL = "stop_all"


@dataclass
class StationConfig:
    """Постоянная конфигурация одной IRM-станции и ее COM-порта."""

    index: int
    port: str
    baudrate: int = 9600
    group: int = 1
    name: str = ""
    read_timeout: float = 0.05
    reconnect_interval: float = 3.0
    mock: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"COM{self.index}"


@dataclass
class ModeConfig:
    """Постоянный режим работы с задержками по станциям в секундах."""

    id: str
    name: str
    station_delays: dict[int, float] = field(default_factory=dict)


@dataclass
class LineConfig:
    """Постоянная конфигурация всей линии и поведения планировщика."""

    stations: list[StationConfig]
    modes: list[ModeConfig] = field(default_factory=list)
    hold_seconds: float = 3.0
    loop: bool = True
    departure_grace_seconds: float = 1.5
    scheduler_interval_seconds: float = 0.1
    event_limit: int = 200


@dataclass
class StationState:
    """Текущее состояние станции на основе IRM-сообщений и команд контроллера."""

    index: int
    port: str
    name: str
    group: int
    connected: bool = False
    occupied: bool = False
    shuttle_id: int | None = None
    waiting_since: float | None = None
    last_seen: float | None = None
    last_raw: str | None = None
    message_count: int = 0
    last_error: str | None = None
    last_command_hex: str | None = None
    release_sent_at: float | None = None
    released_shuttle_id: int | None = None


@dataclass
class SegmentState:
    """Текущее состояние перегона между двумя соседними станциями."""

    from_station: int
    to_station: int
    occupied_by: int | None = None
    since: float | None = None
    shuttle_ids: list[int] = field(default_factory=list)
    shuttle_since: dict[int, float] = field(default_factory=dict)

    def shuttle_count(self) -> int:
        """Вернуть количество тележек, которые сейчас числятся в перегоне."""
        self._sync_from_legacy()
        return len(self.shuttle_ids)

    def add_shuttle(self, shuttle_id: int, since: float) -> None:
        """Отметить тележку как движущуюся по этому перегону."""
        self._sync_from_legacy()
        if shuttle_id in self.shuttle_ids:
            self.shuttle_since[shuttle_id] = since
        else:
            self.shuttle_ids.append(shuttle_id)
            self.shuttle_since[shuttle_id] = since
        self._sync_legacy()

    def remove_shuttle(self, shuttle_id: int) -> bool:
        """Удалить тележку из перегона и вернуть, была ли она там."""
        self._sync_from_legacy()
        if shuttle_id not in self.shuttle_ids:
            return False
        self.shuttle_ids = [item for item in self.shuttle_ids if item != shuttle_id]
        self.shuttle_since.pop(shuttle_id, None)
        self._sync_legacy()
        return True

    def _sync_from_legacy(self) -> None:
        if self.occupied_by is None or self.occupied_by in self.shuttle_ids:
            return
        self.shuttle_ids.insert(0, self.occupied_by)
        if self.since is not None:
            self.shuttle_since.setdefault(self.occupied_by, self.since)

    def _sync_legacy(self) -> None:
        if not self.shuttle_ids:
            self.occupied_by = None
            self.since = None
            return
        self.occupied_by = self.shuttle_ids[0]
        known_times = [self.shuttle_since[item] for item in self.shuttle_ids if item in self.shuttle_since]
        self.since = min(known_times) if known_times else None


@dataclass
class Event:
    """Событие контроллера с временем для отображения в журнале интерфейса."""

    at: float
    level: str
    message: str


class SerialWorker(threading.Thread):
    """Фоновый поток чтения/записи для COM-порта одной станции."""

    def __init__(self, controller: "MontracController", station: StationConfig):
        super().__init__(name=f"station-{station.index}-reader", daemon=True)
        self.controller = controller
        self.station = station
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._serial: Any = None
        self._buffer = bytearray()
        self.sent_frames: list[bytes] = []

    def run(self) -> None:
        """Открывать порт станции, читать IRM-кадры и переподключаться при сбоях."""
        if self.station.mock:
            self.controller.set_connection(self.station.index, True, "mock port")
            while not self._stop_event.wait(0.5):
                pass
            return

        while not self._stop_event.is_set():
            if self._serial is None:
                self._open_port()
                if self._serial is None:
                    self._stop_event.wait(self.station.reconnect_interval)
                    continue

            try:
                waiting = getattr(self._serial, "in_waiting", 0)
                if waiting:
                    data = self._serial.read(waiting)
                    self._buffer.extend(data)
                    for message in extract_messages(self._buffer):
                        self.controller.on_message(self.station.index, message)
                else:
                    self._stop_event.wait(self.station.read_timeout)
            except Exception as exc:  # pragma: no cover - hardware path
                self.controller.set_connection(self.station.index, False, str(exc))
                self._close_port()
                self._stop_event.wait(self.station.reconnect_interval)

    def _open_port(self) -> None:
        try:
            import serial  # type: ignore

            self._serial = serial.Serial(
                self.station.port,
                self.station.baudrate,
                timeout=self.station.read_timeout,
            )
            self._serial.reset_input_buffer()
            self.controller.set_connection(self.station.index, True, None)
        except Exception as exc:  # pragma: no cover - hardware path
            self._serial = None
            self.controller.set_connection(self.station.index, False, str(exc))

    def send(self, frame: bytes) -> None:
        """Записать сырой IRM-кадр в порт станции или сохранить его в mock-режиме."""
        with self._write_lock:
            if self.station.mock:
                self.sent_frames.append(frame)
                return
            if self._serial is None:
                raise RuntimeError(f"{self.station.port} is not connected")
            self._serial.write(frame)
            self._serial.flush()

    def stop(self) -> None:
        """Передать воркеру сигнал остановки и закрыть serial-порт."""
        self._stop_event.set()
        self._close_port()

    def _close_port(self) -> None:
        with self._write_lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                finally:
                    self._serial = None


class MontracController:
    """Координировать состояние станций, проверки движения, режимы и сохранение.

    Тележка может быть отправлена только когда следующий перегон не содержит
    тележек. Это правило одинаково применяется к кнопкам станций, автоматическим
    режимам и API-вызовам.
    """

    def __init__(self, config: LineConfig, config_path: Path | None = None, force_mock: bool = False):
        self.config = config
        self.config_path = config_path
        self.force_mock = force_mock
        self.active_mode_id: str | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._started = False
        self._station_order: list[int] = []
        self._station_config: dict[int, StationConfig] = {}
        self.stations: dict[int, StationState] = {}
        self.segments: dict[tuple[int, int], SegmentState] = {}
        self.workers: dict[int, SerialWorker] = {}
        self._mode_config: dict[str, ModeConfig] = {}
        self.events: list[Event] = []
        self._rebuild_runtime_locked(config.stations)
        self._rebuild_modes_locked(config.modes)

    def start(self) -> None:
        """Запустить все рабочие потоки станций и планировщик режима."""
        with self._lock:
            self._started = True
            self._log("info", "Montrac controller started")
            workers = list(self.workers.values())
        for worker in workers:
            worker.start()
        self._scheduler = threading.Thread(target=self._scheduler_loop, name="montrac-scheduler", daemon=True)
        self._scheduler.start()

    def stop(self) -> None:
        """Остановить планировщик и рабочие потоки станций с коротким ожиданием."""
        self._stop.set()
        self._started = False
        workers = list(self.workers.values())
        for worker in workers:
            worker.stop()
        for worker in workers:
            if worker.is_alive():
                worker.join(timeout=1)
        if self._scheduler and self._scheduler.is_alive():
            self._scheduler.join(timeout=1)
        self._log("info", "Montrac controller stopped")

    def set_connection(self, station_index: int, connected: bool, error: str | None) -> None:
        """Обновить видимое состояние подключения для потока одной станции."""
        with self._lock:
            if station_index not in self.stations:
                return
            state = self.stations[station_index]
            was_connected = state.connected
            state.connected = connected
            state.last_error = error
            if connected and not was_connected:
                self._log("info", f"{state.name} connected")
            elif not connected and was_connected:
                self._log("error", f"{state.name} disconnected: {error}")
            elif not connected and error:
                state.last_error = error

    def set_mode(self, mode: str | OperatingMode | None) -> dict[str, Any]:
        """Выбрать автоматический режим или вернуться в режим ожидания."""
        with self._lock:
            mode_id = mode.value if isinstance(mode, OperatingMode) else mode
            if mode_id in (None, "", OperatingMode.IDLE.value):
                self.active_mode_id = None
                self._log("info", "Mode changed to idle")
                return self.snapshot_locked()
            if mode_id not in self._mode_config:
                raise ValueError(f"Mode does not exist: {mode_id}")
            self.active_mode_id = mode_id
            self._log("info", f"Mode changed to {self._mode_config[mode_id].name}")
            self._auto_release_due_locked(time.time())
            return self.snapshot_locked()

    def manual_next(self) -> dict[str, Any]:
        """Отправить первую ожидающую тележку, прошедшую проверку безопасности."""
        with self._lock:
            release = self._release_next_locked("manual button")
            state = self.snapshot_locked()
            state["lastAction"] = release
            return state

    def release_station(self, station_index: int) -> dict[str, Any]:
        """Отправить тележку, которая сейчас ожидает на указанной станции."""
        with self._lock:
            release = self._release_station_locked(station_index, "station button")
            state = self.snapshot_locked()
            state["lastAction"] = release
            return state

    def update_station_config(self, raw_stations: list[dict[str, Any]]) -> dict[str, Any]:
        """Заменить и сохранить конфигурацию станций, когда линия пустая."""
        with self._lock:
            if self._line_has_active_shuttles_locked():
                raise ValueError("Нельзя менять конфигурацию, пока есть занятые станции или перегоны")
            new_stations = self._normalize_station_config_locked(raw_stations)
            old_workers = list(self.workers.values())

        for worker in old_workers:
            worker.stop()
        for worker in old_workers:
            if worker.is_alive():
                worker.join(timeout=1)

        with self._lock:
            self.config.stations = new_stations
            self.config.modes = normalize_modes_for_stations(self.config.modes, new_stations, self.config.hold_seconds)
            if self.config_path is not None:
                self.config_path = save_config(self.config, self.config_path)
            self._rebuild_runtime_locked(new_stations)
            self._rebuild_modes_locked(self.config.modes)
            if self._started and not self._stop.is_set():
                for worker in self.workers.values():
                    worker.start()
            self._log("info", "Station configuration updated")
            state = self.snapshot_locked()
            state["saved"] = True
            return state

    def update_modes_config(self, raw_modes: list[dict[str, Any]]) -> dict[str, Any]:
        """Заменить конфигурацию режимов, нормализовать задержки и сохранить."""
        with self._lock:
            modes = normalize_modes_for_stations(parse_modes(raw_modes, self.config.stations), self.config.stations, self.config.hold_seconds)
            self.config.modes = modes
            self._rebuild_modes_locked(modes)
            if self.active_mode_id not in self._mode_config:
                self.active_mode_id = None
            if self.config_path is not None:
                self.config_path = save_config(self.config, self.config_path)
            self._log("info", "Mode configuration updated")
            state = self.snapshot_locked()
            state["saved"] = True
            return state

    def on_message(self, station_index: int, message: IrmMessage) -> None:
        """Обработать разобранное IRM-сообщение от потока станции."""
        with self._lock:
            if station_index not in self.stations:
                return
            station = self.stations[station_index]
            station.message_count += 1
            station.last_seen = time.time()
            station.last_raw = message.hex
            station.group = message.group

            if message.command == COMMAND_PRESENCE:
                self._handle_presence_locked(station_index, message)
            else:
                self._log("debug", f"{station.name} received command {message.command} for shuttle {message.shuttle_id}")

    def inject_presence(self, station_index: int, shuttle_id: int, group: int = 1) -> dict[str, Any]:
        """Подставить синтетическое сообщение присутствия для проверок без железа."""
        frame = build_frame(group, shuttle_id, COMMAND_PRESENCE)
        self.on_message(station_index, IrmMessage(group=group, shuttle_id=shuttle_id, command=COMMAND_PRESENCE, raw=frame))
        with self._lock:
            return self.snapshot_locked()

    def _handle_presence_locked(self, station_index: int, message: IrmMessage) -> None:
        """Применить кадр присутствия к состоянию станции и перегонов."""
        now = time.time()
        station = self.stations[station_index]
        shuttle_id = message.shuttle_id

        if (
            station.released_shuttle_id == shuttle_id
            and station.release_sent_at is not None
            and now - station.release_sent_at < self.config.departure_grace_seconds
        ):
            return

        for segment in self.segments.values():
            if segment.remove_shuttle(shuttle_id):
                self._log("info", f"Shuttle {shuttle_id} arrived at {station.name}")

        for other in self.stations.values():
            if other.index != station_index and other.occupied and other.shuttle_id == shuttle_id:
                other.occupied = False
                other.shuttle_id = None
                other.waiting_since = None

        if not station.occupied or station.shuttle_id != shuttle_id:
            if station.occupied and station.shuttle_id != shuttle_id:
                self._log(
                    "warning",
                    f"{station.name} changed occupied shuttle from {station.shuttle_id} to {shuttle_id}",
                )
            station.occupied = True
            station.shuttle_id = shuttle_id
            station.waiting_since = now
            station.release_sent_at = None
            station.released_shuttle_id = None
            self._log("info", f"Shuttle {shuttle_id} detected at {station.name}")

    def _scheduler_loop(self) -> None:
        while not self._stop.wait(self.config.scheduler_interval_seconds):
            with self._lock:
                self._auto_release_due_locked(time.time())

    def _auto_release_due_locked(self, now: float) -> None:
        """Отправить тележки, у которых истекла задержка активного режима."""
        if self.active_mode_id is None:
            return
        mode = self._mode_config.get(self.active_mode_id)
        if mode is None:
            self.active_mode_id = None
            return

        for station_index in self._station_order:
            station = self.stations[station_index]
            if not station.occupied or station.waiting_since is None:
                continue
            hold_seconds = mode.station_delays.get(station_index, 0.0)
            if now - station.waiting_since >= hold_seconds:
                self._release_station_locked(station_index, f"mode {mode.name}")

    def _release_next_locked(self, reason: str) -> dict[str, Any]:
        occupied = [
            station
            for station in self.stations.values()
            if station.occupied and station.shuttle_id is not None and station.waiting_since is not None
        ]
        occupied.sort(key=lambda item: (item.waiting_since or 0, item.index))

        blockers: list[dict[str, Any]] = []
        for station in occupied:
            ok, blocker = self._can_release_locked(station.index)
            if ok:
                return self._release_station_locked(station.index, reason)
            blockers.append({"station": station.name, "reason": blocker})

        message = "No shuttle can be released safely"
        self._log("warning", message)
        return {"released": False, "message": message, "blockers": blockers}

    def _release_station_locked(self, station_index: int, reason: str) -> dict[str, Any]:
        """Отправить START FORWARD и перенести тележку из станции в перегон."""
        ok, blocker = self._can_release_locked(station_index)
        station = self.stations.get(station_index)
        if station is None:
            return {
                "released": False,
                "station": station_index,
                "reason": "station does not exist",
            }
        if not ok:
            return {
                "released": False,
                "station": station.name,
                "reason": blocker,
            }

        target = self.next_station(station_index)
        assert target is not None
        segment = self.segments[(station_index, target)]
        shuttle_id = station.shuttle_id
        assert shuttle_id is not None
        frame = build_frame(station.group, shuttle_id, COMMAND_START_FORWARD)

        try:
            self.workers[station_index].send(frame)
        except Exception as exc:
            station.last_error = str(exc)
            self._log("error", f"Could not send START FORWARD to {station.name}: {exc}")
            return {
                "released": False,
                "station": station.name,
                "shuttleId": shuttle_id,
                "reason": str(exc),
            }

        now = time.time()
        station.occupied = False
        station.shuttle_id = None
        station.waiting_since = None
        station.release_sent_at = now
        station.released_shuttle_id = shuttle_id
        station.last_command_hex = frame.hex().upper()
        for other_segment in self.segments.values():
            if other_segment is not segment:
                other_segment.remove_shuttle(shuttle_id)
        segment.add_shuttle(shuttle_id, now)

        self._log("info", f"START FORWARD sent to shuttle {shuttle_id} at {station.name} ({reason})")
        return {
            "released": True,
            "station": station.name,
            "targetStation": self.stations[target].name,
            "shuttleId": shuttle_id,
            "commandHex": frame.hex().upper(),
        }

    def _can_release_locked(self, station_index: int) -> tuple[bool, str | None]:
        """Проверить, может ли станция безопасно отправить текущую тележку."""
        if station_index not in self.stations:
            return False, "station does not exist"
        station = self.stations[station_index]
        if not station.occupied or station.shuttle_id is None:
            return False, "station is empty"

        target = self.next_station(station_index)
        if target is None:
            return False, "station has no next station"

        segment = self.segments[(station_index, target)]
        shuttle_count = segment.shuttle_count()
        if shuttle_count > MAX_SEGMENT_SHUTTLES_BEFORE_RELEASE:
            target_station = self.stations[target]
            return (
                False,
                f"segment {station.name}->{target_station.name} already has {shuttle_count} shuttles",
            )

        return True, None

    def next_station(self, station_index: int) -> int | None:
        """Вернуть номер следующей станции согласно настроенному маршруту."""
        if station_index not in self._station_order:
            return None
        position = self._station_order.index(station_index)
        if position < len(self._station_order) - 1:
            return self._station_order[position + 1]
        return self._station_order[0] if self.config.loop and self._station_order else None

    def snapshot(self) -> dict[str, Any]:
        """Вернуть JSON-сериализуемый снимок всего состояния контроллера."""
        with self._lock:
            return self.snapshot_locked()

    def snapshot_locked(self) -> dict[str, Any]:
        """Собрать снимок состояния; вызывающий код уже должен держать блокировку."""
        active_mode = self._mode_config.get(self.active_mode_id) if self.active_mode_id else None
        return {
            "mode": self.active_mode_id or OperatingMode.IDLE.value,
            "activeModeId": self.active_mode_id,
            "activeModeName": active_mode.name if active_mode else None,
            "holdSeconds": self.config.hold_seconds,
            "loop": self.config.loop,
            "configPath": str(self.config_path) if self.config_path else None,
            "stations": [self._station_snapshot(index) for index in self._station_order],
            "modes": [self._mode_snapshot(mode) for mode in self.config.modes],
            "segments": [self._segment_snapshot(segment) for segment in self.segments.values()],
            "events": [self._event_snapshot(event) for event in self.events[-50:]][::-1],
        }

    def _station_snapshot(self, station_index: int) -> dict[str, Any]:
        station = self.stations[station_index]
        can_release, blocker = self._can_release_locked(station_index)
        return {
            "index": station.index,
            "name": station.name,
            "port": station.port,
            "connected": station.connected,
            "occupied": station.occupied,
            "shuttleId": station.shuttle_id,
            "waitingSeconds": round(time.time() - station.waiting_since, 1) if station.waiting_since else None,
            "lastSeen": _format_time(station.last_seen),
            "lastRaw": station.last_raw,
            "messageCount": station.message_count,
            "lastError": station.last_error,
            "lastCommandHex": station.last_command_hex,
            "canRelease": can_release,
            "releaseBlocker": blocker,
        }

    def _segment_snapshot(self, segment: SegmentState) -> dict[str, Any]:
        shuttle_count = segment.shuttle_count()
        shuttle_ids = list(segment.shuttle_ids)
        return {
            "from": self.stations[segment.from_station].name,
            "to": self.stations[segment.to_station].name,
            "occupiedBy": segment.occupied_by,
            "occupiedByIds": shuttle_ids,
            "shuttleCount": shuttle_count,
            "occupiedSeconds": round(time.time() - segment.since, 1) if segment.since else None,
        }

    def _event_snapshot(self, event: Event) -> dict[str, Any]:
        return {"at": _format_time(event.at), "level": event.level, "message": event.message}

    def _mode_snapshot(self, mode: ModeConfig) -> dict[str, Any]:
        return {
            "id": mode.id,
            "name": mode.name,
            "stationDelays": {str(index): mode.station_delays.get(index, 0.0) for index in self._station_order},
            "delays": [
                {
                    "stationIndex": index,
                    "stationName": self.stations[index].name,
                    "seconds": mode.station_delays.get(index, 0.0),
                }
                for index in self._station_order
            ],
        }

    def _log(self, level: str, message: str) -> None:
        self.events.append(Event(time.time(), level, message))
        if len(self.events) > self.config.event_limit:
            del self.events[: len(self.events) - self.config.event_limit]

    def _rebuild_runtime_locked(self, stations: list[StationConfig]) -> None:
        normalized = sorted(stations, key=lambda item: item.index)
        if self.force_mock:
            for station in normalized:
                station.mock = True

        self._station_order = [station.index for station in normalized]
        self._station_config = {station.index: station for station in normalized}
        self.stations = {
            station.index: StationState(
                index=station.index,
                port=station.port,
                name=station.name,
                group=station.group,
            )
            for station in normalized
        }
        self.segments = {
            (station, next_station): SegmentState(station, next_station)
            for station in self._station_order
            for next_station in [self.next_station(station)]
            if next_station is not None
        }
        self.workers = {station.index: SerialWorker(self, station) for station in normalized}

    def _rebuild_modes_locked(self, modes: list[ModeConfig]) -> None:
        normalized = normalize_modes_for_stations(modes, self.config.stations, self.config.hold_seconds)
        self.config.modes = normalized
        self._mode_config = {mode.id: mode for mode in normalized}
        if self.active_mode_id not in self._mode_config:
            self.active_mode_id = None

    def _line_has_active_shuttles_locked(self) -> bool:
        return any(station.occupied for station in self.stations.values()) or any(
            segment.shuttle_count() > 0 for segment in self.segments.values()
        )

    def _normalize_station_config_locked(self, raw_stations: list[dict[str, Any]]) -> list[StationConfig]:
        if not raw_stations:
            raise ValueError("Добавьте хотя бы одну станцию")

        seen_indexes: set[int] = set()
        stations: list[StationConfig] = []
        for raw in raw_stations:
            index = int(raw.get("index", 0))
            if index <= 0:
                raise ValueError("Номер станции должен быть положительным")
            if index in seen_indexes:
                raise ValueError(f"Номер станции {index} повторяется")
            seen_indexes.add(index)

            port = str(raw.get("port", "")).strip()
            if not port:
                raise ValueError(f"У станции {index} не указан COM-порт")

            old = self._station_config.get(index)
            name = str(raw.get("name", "")).strip() or f"COM{index}"
            stations.append(
                StationConfig(
                    index=index,
                    port=port,
                    baudrate=int(raw.get("baudrate", old.baudrate if old else 9600)),
                    group=int(raw.get("group", old.group if old else 1)),
                    name=name,
                    read_timeout=float(raw.get("read_timeout", old.read_timeout if old else 0.05)),
                    reconnect_interval=float(
                        raw.get("reconnect_interval", old.reconnect_interval if old else 3.0)
                    ),
                    mock=self.force_mock or bool(raw.get("mock", old.mock if old else False)),
                )
            )

        return sorted(stations, key=lambda item: item.index)


def _format_time(value: float | None) -> str | None:
    """Отформатировать Unix timestamp для JSON-снимков."""
    if value is None:
        return None
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")


def default_config() -> LineConfig:
    """Создать встроенную конфигурацию COM1-COM6 как резервный вариант."""
    stations = [StationConfig(index=i, port=f"COM{i}", name=f"COM{i}") for i in range(1, 7)]
    return LineConfig(stations=stations, modes=default_modes(stations, 3.0))


def default_modes(stations: list[StationConfig], hold_seconds: float) -> list[ModeConfig]:
    """Создать два базовых режима для переданного списка станций."""
    station_indexes = {station.index for station in stations}
    return [
        ModeConfig(
            id=OperatingMode.STOP_2_4.value,
            name="Станции 2 и 4 · 3 с",
            station_delays={index: hold_seconds if index in {2, 4} else 0.0 for index in station_indexes},
        ),
        ModeConfig(
            id=OperatingMode.STOP_ALL.value,
            name="Все станции · 3 с",
            station_delays={index: hold_seconds for index in station_indexes},
        ),
    ]


def parse_modes(raw_modes: Any, stations: list[StationConfig]) -> list[ModeConfig]:
    """Разобрать объекты режимов из JSON-конфига или тела API-запроса."""
    if raw_modes is None:
        return []
    if not isinstance(raw_modes, list):
        raise ValueError("modes must be a list")

    modes: list[ModeConfig] = []
    used_ids: set[str] = set()
    for position, raw in enumerate(raw_modes, start=1):
        if not isinstance(raw, dict):
            raise ValueError("mode must be an object")
        name = str(raw.get("name") or f"Режим {position}").strip()
        mode_id = str(raw.get("id") or slugify_mode_id(name, position)).strip()
        if not mode_id:
            mode_id = f"mode_{position}"
        if mode_id in used_ids:
            raise ValueError(f"Mode id repeats: {mode_id}")
        used_ids.add(mode_id)

        raw_delays = raw.get("stationDelays", raw.get("delays", {}))
        delays = parse_station_delays(raw_delays)
        modes.append(ModeConfig(id=mode_id, name=name, station_delays=delays))

    return modes


def parse_station_delays(raw_delays: Any) -> dict[int, float]:
    """Разобрать задержки станций из dict- или list-формата."""
    delays: dict[int, float] = {}
    if isinstance(raw_delays, dict):
        iterator = raw_delays.items()
    elif isinstance(raw_delays, list):
        iterator = []
        for item in raw_delays:
            if isinstance(item, dict):
                iterator.append((item.get("stationIndex"), item.get("seconds", 0)))
    else:
        iterator = []

    for raw_index, raw_seconds in iterator:
        if raw_index in (None, ""):
            continue
        index = int(raw_index)
        seconds = float(raw_seconds or 0)
        if seconds < 0:
            raise ValueError("Delay seconds must be non-negative")
        delays[index] = seconds
    return delays


def normalize_modes_for_stations(
    modes: list[ModeConfig], stations: list[StationConfig], hold_seconds: float
) -> list[ModeConfig]:
    """Убедиться, что у каждого режима уникальный ID и задержка для всех станций."""
    if not modes:
        modes = default_modes(stations, hold_seconds)

    station_indexes = [station.index for station in stations]
    normalized: list[ModeConfig] = []
    used_ids: set[str] = set()
    for position, mode in enumerate(modes, start=1):
        mode_id = mode.id or slugify_mode_id(mode.name, position)
        if mode_id in used_ids:
            raise ValueError(f"Mode id repeats: {mode_id}")
        used_ids.add(mode_id)
        normalized.append(
            ModeConfig(
                id=mode_id,
                name=mode.name or f"Режим {position}",
                station_delays={
                    index: float(mode.station_delays.get(index, 0.0))
                    for index in station_indexes
                },
            )
        )
    return normalized


def slugify_mode_id(name: str, fallback: int) -> str:
    """Создать стабильный ASCII-идентификатор режима из читаемого названия."""
    result = []
    for char in name.lower():
        if char.isascii() and char.isalnum():
            result.append(char)
        elif char in (" ", "-", "_"):
            result.append("_")
    slug = "".join(result).strip("_")
    return slug or f"mode_{fallback}"


def load_config(path: Path) -> LineConfig:
    """Загрузить конфигурацию линии из JSON или вернуть встроенные настройки."""
    if not path.exists():
        return default_config()

    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return default_config()
    raw = json.loads(raw_text)
    stations = [
        StationConfig(
            index=int(item["index"]),
            port=str(item.get("port", f"COM{item['index']}")),
            baudrate=int(item.get("baudrate", 9600)),
            group=int(item.get("group", 1)),
            name=str(item.get("name") or f"COM{item['index']}"),
            read_timeout=float(item.get("read_timeout", 0.05)),
            reconnect_interval=float(item.get("reconnect_interval", 3.0)),
            mock=bool(item.get("mock", False)),
        )
        for item in raw.get("stations", [])
    ]
    if not stations:
        stations = default_config().stations

    modes = parse_modes(raw.get("modes"), stations)
    return LineConfig(
        stations=stations,
        modes=normalize_modes_for_stations(modes, stations, float(raw.get("hold_seconds", 3.0))),
        hold_seconds=float(raw.get("hold_seconds", 3.0)),
        loop=bool(raw.get("loop", True)),
        departure_grace_seconds=float(raw.get("departure_grace_seconds", 1.5)),
        scheduler_interval_seconds=float(raw.get("scheduler_interval_seconds", 0.1)),
        event_limit=int(raw.get("event_limit", 200)),
    )


def load_or_create_config(path: Path) -> tuple[LineConfig, Path]:
    """Загрузить конфиг или создать его из config.example.json/встроенных настроек."""
    if path.exists():
        return load_config(path), path

    example_path = path.with_name("config.example.json")
    config = load_config(example_path) if example_path.exists() else default_config()
    saved_path = save_config(config, path)
    return config, saved_path


def save_config(config: LineConfig, path: Path) -> Path:
    """Сохранить конфигурацию линии как UTF-8 JSON и вернуть путь записи."""
    target = path.with_name("config.json") if path.name == "config.example.json" else path
    payload = {
        "hold_seconds": config.hold_seconds,
        "loop": config.loop,
        "departure_grace_seconds": config.departure_grace_seconds,
        "scheduler_interval_seconds": config.scheduler_interval_seconds,
        "event_limit": config.event_limit,
        "stations": [
            {
                "index": station.index,
                "name": station.name,
                "port": station.port,
                "baudrate": station.baudrate,
                "group": station.group,
            }
            for station in sorted(config.stations, key=lambda item: item.index)
        ],
        "modes": [
            {
                "id": mode.id,
                "name": mode.name,
                "stationDelays": {
                    str(index): mode.station_delays.get(index, 0.0)
                    for index in sorted(station.index for station in config.stations)
                },
            }
            for mode in config.modes
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
