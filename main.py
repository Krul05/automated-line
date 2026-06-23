"""Точка входа командной строки для веб-сервиса Montrac.

Модуль загружает JSON-конфигурацию линии, при необходимости переводит все
станции в mock-режим, запускает потоки контроллера и поднимает веб-интерфейс/API.
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from montrac.controller import MontracController, load_or_create_config
from montrac.web import run_http_server


def parse_args() -> argparse.Namespace:
    """Разобрать аргументы командной строки: конфиг, HTTP-адрес и mock-режим."""
    parser = argparse.ArgumentParser(
        description="Веб-сервис для IRM-станций Montrac, подключенных через Moxa virtual COM ports."
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Путь к JSON-конфигу. Если файл отсутствует, используются настройки COM1-COM6.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="HTTP-адрес для запуска сервера.")
    parser.add_argument("--port", type=int, default=8080, help="HTTP-порт для запуска сервера.")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Не открывать реальные COM-порты. Полезно для проверки UI и контроллера.",
    )
    return parser.parse_args()


def main() -> int:
    """Запустить контроллер Montrac и HTTP-сервер до остановки процесса."""
    args = parse_args()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = Path(__file__).resolve().parent / config_path

    config, config_path = load_or_create_config(config_path)
    if args.mock:
        for station in config.stations:
            station.mock = True

    controller = MontracController(config, config_path=config_path, force_mock=args.mock)

    def shutdown_handler(signum: int, frame: object) -> None:
        """Остановить рабочие потоки перед завершением процесса по сигналу."""
        controller.stop()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, shutdown_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown_handler)

    controller.start()
    try:
        run_http_server(controller, args.host, args.port)
    except KeyboardInterrupt:
        print("\nStopping Montrac service...")
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
