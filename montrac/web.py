"""HTTP API и встроенный веб-интерфейс для контроллера Montrac.

Базовый адрес
-------------

Локально сервис обычно доступен по адресу::

    http://localhost:8080

При запуске с ``--host 0.0.0.0`` к сервису можно обращаться из локальной сети
по IP компьютера, например::

    http://192.168.1.10:8080

Все POST-запросы принимают JSON и должны отправляться с заголовком
``Content-Type: application/json``. Успешные управляющие запросы обычно
возвращают полный JSON-снимок состояния линии.

GET /health
-----------

Проверка, что HTTP-сервер запущен.

Пример::

    curl.exe http://localhost:8080/health

Ответ::

    {"ok": true}

GET /api/state
--------------

Возвращает полный снимок состояния контроллера:

``mode``
    ID активного режима или ``idle``.
``activeModeId``
    ID активного режима или ``None``.
``activeModeName``
    Название активного режима или ``None``.
``stations``
    Список станций. Для каждой станции возвращаются ``index``, ``name``,
    ``port``, ``connected``, ``occupied``, ``shuttleId``, ``waitingSeconds``,
    ``lastSeen``, ``lastRaw``, ``messageCount``, ``lastError``,
    ``lastCommandHex``, ``canRelease`` и ``releaseBlocker``.
``segments``
    Список перегонов. Для каждого перегона возвращаются ``from``, ``to``,
    ``occupiedBy``, ``occupiedByIds``, ``shuttleCount`` и ``occupiedSeconds``.
    По текущему правилу движения отправка разрешена только если в следующем
    перегоне ``shuttleCount == 0``.
``modes``
    Список режимов с ``id``, ``name`` и задержками ``stationDelays``.
``events``
    Последние события журнала контроллера.

Пример::

    curl.exe http://localhost:8080/api/state

POST /api/actions/select_mode
-----------------------------

Выбирает автоматический режим или останавливает автоматический режим.

Тело для выбора режима::

    {"modeId": "stop_all"}

Тело для остановки режима::

    {"modeId": null}

Пример::

    curl.exe -X POST http://localhost:8080/api/actions/select_mode ^
      -H "Content-Type: application/json" ^
      -d "{\"modeId\":\"stop_all\"}"

Если ``modeId`` не существует, сервис вернет HTTP 400::

    {"ok": false, "error": "Mode does not exist: unknown_mode"}

POST /api/actions/release_station
---------------------------------

Отправляет команду ``START FORWARD`` тележке, которая сейчас находится на
указанной станции.

Тело::

    {"station": 1}

Пример::

    curl.exe -X POST http://localhost:8080/api/actions/release_station ^
      -H "Content-Type: application/json" ^
      -d "{\"station\":1}"

При успешной отправке в ответе будет ``lastAction.released == true`` и поля
``station``, ``targetStation``, ``shuttleId`` и ``commandHex``.

Если запуск запрещен состоянием линии, HTTP-статус остается 200, но
``lastAction.released`` будет ``false``. Например, если следующий перегон уже
занят::

    {
      "lastAction": {
        "released": false,
        "station": "Station 1",
        "reason": "segment Station 1->Station 2 already has 1 shuttles"
      }
    }

POST /api/config/stations
-------------------------

Заменяет список станций и сохраняет его в рабочий ``config.json``. Команда
разрешена только когда линия пустая: нет занятых станций и тележек в перегонах.

Минимальное тело::

    {
      "stations": [
        {"index": 1, "name": "Station 1", "port": "COM1"},
        {"index": 2, "name": "Station 2", "port": "COM2"}
      ]
    }

Дополнительно можно передавать ``baudrate``, ``group``, ``read_timeout``,
``reconnect_interval`` и ``mock``. Если линия не пустая, сервис вернет
HTTP 400 с русским текстом ошибки.

POST /api/config/modes
----------------------

Заменяет список режимов и сохраняет его в ``config.json``.

Тело::

    {
      "modes": [
        {
          "id": "stop_all",
          "name": "Все станции · 3 с",
          "stationDelays": {"1": 3, "2": 3, "3": 3}
        },
        {
          "id": "fast_pass",
          "name": "Без остановок",
          "stationDelays": {"1": 0, "2": 0, "3": 0}
        }
      ]
    }

Правила: ``id`` должен быть уникальным, ``name`` отображается на кнопке
режима, значения ``stationDelays`` задают задержку на станции в секундах.
Задержка ``0`` означает отправку сразу после освобождения следующего перегона.

POST /api/mock/presence
-----------------------

Имитирует IRM-сообщение присутствия тележки на станции. Endpoint нужен для
проверки сервиса в ``--mock`` режиме.

Тело::

    {"station": 1, "shuttleId": 101, "group": 1}

Пример::

    curl.exe -X POST http://localhost:8080/api/mock/presence ^
      -H "Content-Type: application/json" ^
      -d "{\"station\":1,\"shuttleId\":101}"

Не используйте этот endpoint на реальной линии без явной необходимости: он
вручную меняет карту состояния сервиса.

Статусы ошибок
--------------

``200``
    Запрос принят. Для команды движения дополнительно проверяйте
    ``lastAction.released``.
``400``
    Некорректный JSON, ошибка валидации, несуществующий режим или попытка
    изменить станции при непустой линии.
``404``
    Неизвестный URL.

Безопасность
------------

В текущей реализации API не имеет встроенной авторизации. Для эксплуатации в
локальной сети ограничивайте доступ к порту сервиса через Windows Firewall,
не публикуйте порт наружу и не давайте внешним системам доступ к endpoints
``/api/config/*`` и ``/api/mock/presence`` без отдельной защиты.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .controller import MontracController, OperatingMode


def run_http_server(controller: MontracController, host: str, port: int) -> None:
    """Запустить блокирующий ThreadingHTTPServer для переданного контроллера."""
    handler = _make_handler(controller)
    server = ThreadingHTTPServer((host, port), handler)
    _safe_print(f"Montrac web service is running at http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _safe_print(message: str) -> None:
    """Напечатать сообщение запуска/остановки без падения при закрытом stdout."""
    try:
        print(message, flush=True)
    except OSError:
        pass


def _make_handler(controller: MontracController) -> type[BaseHTTPRequestHandler]:
    """Создать обработчик запросов, привязанный к конкретному контроллеру.

    Обработчик предоставляет:
    - GET / и /api/state для интерфейса оператора и опроса состояния;
    - POST /api/actions/* для команд движения и выбора режима;
    - POST /api/config/* для редактирования станций и режимов;
    - POST /api/mock/presence для проверок без оборудования.
    """

    class Handler(BaseHTTPRequestHandler):
        """HTTP-обработчик запросов для одного экземпляра контроллера Montrac."""

        def do_GET(self) -> None:
            """Отдать UI, проверку работоспособности или JSON-снимок состояния."""
            if self.path == "/" or self.path.startswith("/?"):
                self._send_html(build_index_html(controller.snapshot()))
            elif self.path == "/api/state":
                self._send_json(controller.snapshot())
            elif self.path == "/health":
                self._send_json({"ok": True})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            """Передать JSON POST-команды соответствующим методам контроллера."""
            try:
                if self.path == "/api/actions/stop_2_4":
                    self._send_json(controller.set_mode(OperatingMode.STOP_2_4))
                elif self.path == "/api/actions/stop_all":
                    self._send_json(controller.set_mode(OperatingMode.STOP_ALL))
                elif self.path == "/api/actions/select_mode":
                    payload = self._read_json()
                    self._send_json(controller.set_mode(payload.get("modeId")))
                elif self.path == "/api/actions/release_station":
                    payload = self._read_json()
                    self._send_json(controller.release_station(int(payload["station"])))
                elif self.path == "/api/config/stations":
                    payload = self._read_json()
                    self._send_json(controller.update_station_config(list(payload.get("stations", []))))
                elif self.path == "/api/config/modes":
                    payload = self._read_json()
                    self._send_json(controller.update_modes_config(list(payload.get("modes", []))))
                elif self.path == "/api/mock/presence":
                    payload = self._read_json()
                    station = int(payload.get("station", 1))
                    shuttle_id = int(payload.get("shuttleId", payload.get("shuttle_id", 255)))
                    group = int(payload.get("group", 1))
                    self._send_json(controller.inject_presence(station, shuttle_id, group))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except (KeyError, TypeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def _read_json(self) -> dict[str, Any]:
            """Прочитать и декодировать JSON-тело запроса."""
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            """Отправить JSON-ответ с заголовками запрета кэширования."""
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_html(self, html: str) -> None:
            """Отправить встроенный интерфейс оператора как HTML-ответ."""
            raw = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            """Отключить стандартное логирование каждого запроса в консоль."""
            return

    return Handler


def build_index_html(initial_state: dict[str, Any]) -> str:
    """Встроить начальное состояние контроллера в одностраничный UI оператора."""
    state_json = json.dumps(initial_state, ensure_ascii=False).replace("</", "<\\/")
    return INDEX_HTML.replace("__INITIAL_STATE_JSON__", state_json)


INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Montrac</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7f8;
      --panel: #ffffff;
      --ink: #172024;
      --muted: #637077;
      --line: #d9e0e3;
      --teal: #00796b;
      --amber: #f0a202;
      --blue: #255f99;
      --red: #bd3b3b;
      --green: #23824a;
      --shadow: 0 12px 28px rgba(23, 32, 36, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Arial, Helvetica, sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0;
    }
    main {
      width: min(1180px, 100%);
      margin: 0 auto;
      padding: 20px;
    }
    .mode {
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      padding: 5px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
      font-size: 14px;
      background: #fafcfc;
    }
    .actions {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }
    button {
      border: 0;
      border-radius: 8px;
      letter-spacing: 0;
      cursor: pointer;
    }
    .action-button {
      min-height: 76px;
      padding: 12px 14px;
      color: #fff;
      font-size: 16px;
      font-weight: 700;
      box-shadow: var(--shadow);
    }
    button:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .b1 { background: var(--teal); }
    .b2 { background: var(--blue); }
    .mode-button { background: var(--blue); }
    .mode-button.active {
      outline: 3px solid rgba(35, 130, 74, 0.32);
      background: var(--teal);
    }
    .stop-button {
      color: var(--ink);
      background: #e9eef0;
    }
    .release-button {
      width: 100%;
      min-height: 38px;
      margin-top: 12px;
      padding: 8px 10px;
      background: var(--green);
      color: #fff;
      font-size: 14px;
      font-weight: 700;
    }
    .release-button:disabled {
      background: #9aa5aa;
    }
    .layout {
      display: grid;
      grid-template-columns: 1.3fr 0.7fr;
      gap: 16px;
      align-items: start;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .panel h2 {
      margin: 0;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      font-size: 17px;
      letter-spacing: 0;
    }
    .stations {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      padding: 14px;
    }
    .station {
      min-height: 156px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfc;
    }
    .station.occupied { border-color: #b88700; background: #fff8e6; }
    .station.ready { border-color: #8bc79f; background: #f0fbf3; }
    .station.disconnected { border-color: #e4b8b8; background: #fff5f5; }
    .station-title {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-bottom: 10px;
      font-weight: 700;
    }
    .dot {
      width: 10px;
      height: 10px;
      flex: 0 0 10px;
      border-radius: 50%;
      background: var(--red);
    }
    .dot.on { background: var(--green); }
    .kv {
      display: grid;
      grid-template-columns: 92px 1fr;
      gap: 6px;
      font-size: 13px;
      line-height: 1.28;
    }
    .kv span:nth-child(odd) { color: var(--muted); }
    .segments, .events, .config-form {
      padding: 12px 14px 14px;
    }
    .segment {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      padding: 8px 0;
      border-bottom: 1px solid var(--line);
      font-size: 14px;
    }
    .segment:last-child { border-bottom: 0; }
    .busy { color: #9b6500; font-weight: 700; }
    .free { color: var(--green); }
    .event {
      display: grid;
      grid-template-columns: 84px 1fr;
      gap: 8px;
      padding: 7px 0;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
    }
    .event:last-child { border-bottom: 0; }
    .event time { color: var(--muted); }
    .error-text { color: var(--red); }
    .config-row {
      display: grid;
      grid-template-columns: 52px 1fr 1fr 38px;
      gap: 8px;
      align-items: end;
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
    }
    .config-row:first-child { padding-top: 0; }
    .config-row label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
    }
    .config-row input {
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 7px 9px;
      color: var(--ink);
      font-size: 14px;
      background: #fff;
    }
    .station-number {
      min-height: 34px;
      display: flex;
      align-items: center;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }
    .config-actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 12px;
    }
    .config-button {
      min-height: 38px;
      padding: 8px 10px;
      color: #fff;
      font-size: 14px;
      font-weight: 700;
      background: var(--blue);
    }
    .config-button.secondary {
      color: var(--ink);
      background: #e9eef0;
    }
    .icon-button {
      width: 38px;
      min-height: 34px;
      color: #fff;
      background: var(--red);
      font-size: 18px;
      font-weight: 700;
    }
    .config-status {
      min-height: 20px;
      margin: 10px 0 0;
      color: var(--muted);
      font-size: 13px;
    }
    .config-status.error-text { color: var(--red); }
    .mode-row {
      padding: 12px 0;
      border-bottom: 1px solid var(--line);
    }
    .mode-row:first-child { padding-top: 0; }
    .mode-header {
      display: grid;
      grid-template-columns: 1fr 38px;
      gap: 8px;
      align-items: end;
      margin-bottom: 10px;
    }
    .mode-header label, .delay-cell label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
    }
    .mode-header input, .delay-cell input {
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 7px 9px;
      color: var(--ink);
      font-size: 14px;
      background: #fff;
    }
    .delay-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .delay-cell input {
      min-height: 32px;
    }
    @media (max-width: 860px) {
      header { align-items: flex-start; flex-direction: column; }
      .actions, .layout, .stations { grid-template-columns: 1fr; }
      .action-button { min-height: 68px; }
      .config-row { grid-template-columns: 48px 1fr; }
      .config-row .icon-button { grid-column: 2; width: 100%; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Montrac</h1>
    <div class="mode" id="mode">Режим: ожидание</div>
  </header>
  <main>
    <section class="actions" id="modeActions" aria-label="Режимы"></section>
    <section class="layout">
      <div class="panel">
        <h2>Станции</h2>
        <div class="stations" id="stations"></div>
      </div>
      <div class="stack">
        <div class="panel">
          <h2>Перегоны</h2>
          <div class="segments" id="segments"></div>
        </div>
        <div class="panel" style="margin-top:16px">
          <h2>Журнал</h2>
          <div class="events" id="events"></div>
        </div>
        <div class="panel" style="margin-top:16px">
          <h2>Режимы работы</h2>
          <form class="config-form" id="modesForm">
            <div id="modeRows"></div>
            <div class="config-actions">
              <button class="config-button secondary" type="button" id="addMode">Добавить</button>
              <button class="config-button" type="submit">Сохранить</button>
            </div>
            <p class="config-status" id="modesStatus"></p>
          </form>
        </div>
        <div class="panel" style="margin-top:16px">
          <h2>Конфигурация</h2>
          <form class="config-form" id="configForm">
            <div id="configRows"></div>
            <div class="config-actions">
              <button class="config-button secondary" type="button" id="addStation" onclick="addNextStationRow()">Добавить</button>
              <button class="config-button" type="submit">Сохранить</button>
            </div>
            <p class="config-status" id="configStatus"></p>
          </form>
        </div>
      </div>
    </section>
  </main>
  <script>
    let configDirty = false;
    let modesDirty = false;
    let lastStations = [];
    let lastModes = [];
    let busy = false;
    let renderedConfigKey = "";
    let renderedModesKey = "";
    window.initialState = __INITIAL_STATE_JSON__;

    function text(value) {
      return value === null || value === undefined || value === "" ? "—" : String(value);
    }

    function escapeHtml(value) {
      return text(value).replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function shortTime(value) {
      if (!value) return "—";
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return value;
      return parsed.toLocaleTimeString();
    }

    async function postAction(path) {
      setBusy(true);
      try {
        const response = await fetch(path, { method: "POST" });
        render(await response.json());
      } finally {
        setBusy(false);
      }
    }

    async function postJson(path, payload) {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const state = await response.json();
      if (!response.ok) {
        throw new Error(state.error || "Ошибка запроса");
      }
      return state;
    }

    function setBusy(value) {
      busy = value;
      document.querySelectorAll(".action-button, .config-button, .icon-button").forEach(button => {
        button.disabled = value;
      });
      document.querySelectorAll(".release-button").forEach(button => {
        const station = lastStations.find(item => item.index === Number(button.dataset.releaseStation));
        button.disabled = value || !station || !station.canRelease;
      });
    }

    async function loadState() {
      try {
        const response = await fetch("/api/state", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        render(await response.json());
      } catch (error) {
        setConfigStatus(`Не удалось загрузить состояние: ${error.message}`, true);
      }
    }

    function render(state) {
      if (!state || !Array.isArray(state.stations)) {
        setConfigStatus("Сервер вернул состояние без списка станций", true);
        return;
      }
      document.getElementById("mode").textContent = `Режим: ${state.activeModeName || "ожидание"}`;
      lastStations = state.stations || [];
      lastModes = state.modes || [];
      renderModeActions(lastModes, state.activeModeId);
      renderStations(state.stations || []);
      renderSegments(state.segments || []);
      renderEvents(state.events || []);
      renderModesConfig(lastModes, lastStations);
      renderConfig(state.stations || []);
    }

    function renderModeActions(modes, activeModeId) {
      const root = document.getElementById("modeActions");
      root.innerHTML = "";
      modes.forEach(mode => {
        const button = document.createElement("button");
        button.className = `action-button mode-button ${mode.id === activeModeId ? "active" : ""}`;
        button.type = "button";
        button.dataset.modeId = mode.id;
        button.textContent = mode.name;
        root.appendChild(button);
      });
      const stop = document.createElement("button");
      stop.className = "action-button stop-button";
      stop.type = "button";
      stop.dataset.modeId = "";
      stop.textContent = "Остановить режим";
      root.appendChild(stop);
    }

    function renderStations(stations) {
      const root = document.getElementById("stations");
      root.innerHTML = "";
      stations.forEach(station => {
        const item = document.createElement("article");
        item.className = "station";
        if (!station.connected) item.classList.add("disconnected");
        if (station.occupied) item.classList.add("occupied");
        if (station.canRelease) item.classList.add("ready");
        item.innerHTML = `
          <div class="station-title">
            <span>${escapeHtml(station.name)}</span>
            <i class="dot ${station.connected ? "on" : ""}"></i>
          </div>
          <div class="kv">
            <span>Порт</span><span>${escapeHtml(station.port)}</span>
            <span>Тележка</span><span>${escapeHtml(station.shuttleId)}</span>
            <span>Ожидание</span><span>${station.waitingSeconds === null ? "—" : station.waitingSeconds + " с"}</span>
            <span>Последний</span><span>${shortTime(station.lastSeen)}</span>
            <span>HEX</span><span>${escapeHtml(station.lastRaw)}</span>
            <span>Блок</span><span>${escapeHtml(station.releaseBlocker)}</span>
            <span>Ошибка</span><span class="error-text">${escapeHtml(station.lastError)}</span>
          </div>
          <button class="release-button" data-release-station="${station.index}" ${station.canRelease && !busy ? "" : "disabled"}>Запустить</button>
        `;
        root.appendChild(item);
      });
    }

    function renderSegments(segments) {
      const root = document.getElementById("segments");
      root.innerHTML = "";
      segments.forEach(segment => {
        const item = document.createElement("div");
        const ids = Array.isArray(segment.occupiedByIds)
          ? segment.occupiedByIds
          : (segment.occupiedBy !== null && segment.occupiedBy !== undefined ? [segment.occupiedBy] : []);
        const count = Number.isFinite(segment.shuttleCount) ? segment.shuttleCount : ids.length;
        const busy = count > 0;
        const label = busy
          ? `${count} тел. · ID ${escapeHtml(ids.join(", "))}`
          : "свободен";
        item.className = "segment";
        item.innerHTML = `
          <span>${escapeHtml(segment.from)} → ${escapeHtml(segment.to)}</span>
          <span class="${busy ? "busy" : "free"}">${label}</span>
        `;
        root.appendChild(item);
      });
    }

    function renderEvents(events) {
      const root = document.getElementById("events");
      root.innerHTML = "";
      events.slice(0, 12).forEach(event => {
        const item = document.createElement("div");
        item.className = "event";
        item.innerHTML = `<time>${shortTime(event.at)}</time><span>${escapeHtml(event.message)}</span>`;
        root.appendChild(item);
      });
    }

    function renderConfig(stations) {
      if (configDirty) return;
      const configKey = stations.map(station => `${station.index}:${station.name}:${station.port}`).join("|");
      if (configKey === renderedConfigKey && document.querySelectorAll(".config-row").length === stations.length) {
        return;
      }
      renderedConfigKey = configKey;
      const root = document.getElementById("configRows");
      root.innerHTML = "";
      stations.forEach(station => addConfigRow(station));
      if (stations.length === 0) {
        setConfigStatus("Список станций пуст. Нажмите «Добавить» или проверьте config.json.", true);
      }
    }

    function addConfigRow(station) {
      const root = document.getElementById("configRows");
      const item = document.createElement("div");
      item.className = "config-row";
      item.dataset.index = station.index;
      const nameId = `station-${station.index}-name`;
      const portId = `station-${station.index}-port`;
      item.innerHTML = `
        <div class="station-number">#${station.index}</div>
        <label for="${nameId}">Название
          <input id="${nameId}" name="${nameId}" autocomplete="off" data-field="name" value="${escapeHtml(station.name)}">
        </label>
        <label for="${portId}">COM порт
          <input id="${portId}" name="${portId}" autocomplete="off" data-field="port" value="${escapeHtml(station.port)}">
        </label>
        <button class="icon-button" type="button" data-delete-station="${station.index}" title="Удалить">×</button>
      `;
      root.appendChild(item);
    }

    function addNextStationRow() {
      const rows = collectConfigRows();
      const nextIndex = rows.reduce((max, station) => Math.max(max, station.index), 0) + 1;
      addConfigRow({ index: nextIndex, name: `COM${nextIndex}`, port: `COM${nextIndex}` });
      configDirty = true;
      renderedConfigKey = "";
      setConfigStatus("Есть несохранённые изменения");
      return false;
    }

    function collectConfigRows() {
      return [...document.querySelectorAll(".config-row")].map(row => ({
        index: Number(row.dataset.index),
        name: row.querySelector('[data-field="name"]').value.trim(),
        port: row.querySelector('[data-field="port"]').value.trim()
      }));
    }

    function renderModesConfig(modes, stations) {
      if (modesDirty) return;
      const modesKey = JSON.stringify(modes.map(mode => ({
        id: mode.id,
        name: mode.name,
        stationDelays: mode.stationDelays
      })));
      if (modesKey === renderedModesKey && document.querySelectorAll(".mode-row").length === modes.length) {
        return;
      }
      renderedModesKey = modesKey;
      const root = document.getElementById("modeRows");
      root.innerHTML = "";
      modes.forEach(mode => addModeRow(mode, stations));
      if (modes.length === 0) {
        setModesStatus("Список режимов пуст. Нажмите «Добавить».", true);
      }
    }

    function addModeRow(mode, stations) {
      const root = document.getElementById("modeRows");
      const item = document.createElement("div");
      item.className = "mode-row";
      item.dataset.modeId = mode.id;
      const nameId = `mode-${mode.id}-name`;
      const delays = mode.stationDelays || {};
      item.innerHTML = `
        <div class="mode-header">
          <label for="${escapeHtml(nameId)}">Название режима
            <input id="${escapeHtml(nameId)}" name="${escapeHtml(nameId)}" autocomplete="off" data-mode-field="name" value="${escapeHtml(mode.name)}">
          </label>
          <button class="icon-button" type="button" data-delete-mode="${escapeHtml(mode.id)}" title="Удалить">×</button>
        </div>
        <div class="delay-grid">
          ${stations.map(station => {
            const inputId = `mode-${mode.id}-station-${station.index}`;
            const seconds = delays[String(station.index)] ?? delays[station.index] ?? 0;
            return `
              <div class="delay-cell">
                <label for="${escapeHtml(inputId)}">${escapeHtml(station.name)}
                  <input id="${escapeHtml(inputId)}" name="${escapeHtml(inputId)}" autocomplete="off" type="number" min="0" step="0.1" data-delay-station="${station.index}" value="${escapeHtml(seconds)}">
                </label>
              </div>
            `;
          }).join("")}
        </div>
      `;
      root.appendChild(item);
    }

    function addNextModeRow() {
      const rows = collectModeRows();
      const nextIndex = rows.length + 1;
      const modeId = `mode_${Date.now()}`;
      addModeRow(
        {
          id: modeId,
          name: `Режим ${nextIndex}`,
          stationDelays: Object.fromEntries(lastStations.map(station => [String(station.index), 0]))
        },
        lastStations
      );
      modesDirty = true;
      renderedModesKey = "";
      setModesStatus("Есть несохранённые изменения");
      return false;
    }

    function collectModeRows() {
      return [...document.querySelectorAll(".mode-row")].map(row => {
        const stationDelays = {};
        row.querySelectorAll("[data-delay-station]").forEach(input => {
          stationDelays[input.dataset.delayStation] = Number(input.value || 0);
        });
        return {
          id: row.dataset.modeId,
          name: row.querySelector('[data-mode-field="name"]').value.trim(),
          stationDelays
        };
      });
    }

    function setConfigStatus(message, isError = false) {
      const status = document.getElementById("configStatus");
      status.textContent = message;
      status.classList.toggle("error-text", isError);
    }
    function setModesStatus(message, isError = false) {
      const status = document.getElementById("modesStatus");
      status.textContent = message;
      status.classList.toggle("error-text", isError);
    }

    document.getElementById("modeActions").addEventListener("click", async event => {
      const button = event.target.closest("[data-mode-id]");
      if (!button) return;
      setBusy(true);
      try {
        render(await postJson("/api/actions/select_mode", { modeId: button.dataset.modeId || null }));
      } finally {
        setBusy(false);
      }
    });
    document.getElementById("stations").addEventListener("click", async event => {
      const button = event.target.closest("[data-release-station]");
      if (!button) return;
      setBusy(true);
      try {
        render(await postJson("/api/actions/release_station", { station: Number(button.dataset.releaseStation) }));
      } finally {
        setBusy(false);
      }
    });
    document.getElementById("configRows").addEventListener("input", () => {
      configDirty = true;
      setConfigStatus("Есть несохранённые изменения");
    });
    document.getElementById("configRows").addEventListener("click", event => {
      const button = event.target.closest("[data-delete-station]");
      if (!button) return;
      button.closest(".config-row").remove();
      configDirty = true;
      renderedConfigKey = "";
      setConfigStatus("Есть несохранённые изменения");
    });
    document.getElementById("modeRows").addEventListener("input", () => {
      modesDirty = true;
      setModesStatus("Есть несохранённые изменения");
    });
    document.getElementById("modeRows").addEventListener("click", event => {
      const button = event.target.closest("[data-delete-mode]");
      if (!button) return;
      button.closest(".mode-row").remove();
      modesDirty = true;
      renderedModesKey = "";
      setModesStatus("Есть несохранённые изменения");
    });
    document.getElementById("addMode").addEventListener("click", event => {
      event.preventDefault();
      addNextModeRow();
    });
    document.getElementById("modesForm").addEventListener("submit", async event => {
      event.preventDefault();
      setBusy(true);
      try {
        const state = await postJson("/api/config/modes", { modes: collectModeRows() });
        modesDirty = false;
        renderedModesKey = "";
        render(state);
        setModesStatus("Сохранено");
      } catch (error) {
        setModesStatus(error.message, true);
      } finally {
        setBusy(false);
      }
    });
    document.getElementById("addStation").addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
    });
    document.addEventListener("click", event => {
      if (!(event.target instanceof Element)) return;
      const button = event.target.closest("#addStation");
      if (!button) return;
      event.preventDefault();
      if (!button.disabled) {
        addNextStationRow();
      }
    });
    document.getElementById("configForm").addEventListener("submit", async event => {
      event.preventDefault();
      setBusy(true);
      try {
        const state = await postJson("/api/config/stations", { stations: collectConfigRows() });
        configDirty = false;
        renderedConfigKey = "";
        render(state);
        setConfigStatus("Сохранено");
      } catch (error) {
        setConfigStatus(error.message, true);
      } finally {
        setBusy(false);
      }
    });
    render(window.initialState);
    loadState();
    setInterval(loadState, 1000);
  </script>
</body>
</html>
"""
