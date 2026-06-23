# API веб-сервиса Montrac

Документ описывает HTTP API сервиса: какие endpoints доступны, какие JSON-тела отправлять, какие ответы ожидать и как интегрировать сервис с внешними системами, например роботом-манипулятором в локальной сети.

## Базовый адрес

Для локального запуска:

```text
http://localhost:8080
```

Для доступа из локальной сети вместо `localhost` используйте IP компьютера, на котором запущен сервис:

```text
http://192.168.1.10:8080
```

Примеры ниже используют переменную:

```powershell
$BaseUrl = "http://localhost:8080"
```

В PowerShell лучше вызывать именно `curl.exe`, потому что `curl` без `.exe` часто является alias для `Invoke-WebRequest`.

## Общие правила

- Все API-ответы сервиса возвращаются в JSON, кроме стандартной HTML-ошибки `404` для неизвестного URL.
- Для POST-запросов отправляйте заголовок `Content-Type: application/json`.
- Успешные POST-запросы обычно возвращают полный снимок состояния линии.
- Если команда не может быть выполнена из-за бизнес-логики, возможны два варианта:
  - HTTP `200`, но в ответе есть `lastAction.released: false`;
  - HTTP `400` и тело `{"ok": false, "error": "..."}` для некорректного запроса или ошибки валидации.
- В текущей версии API не имеет встроенной авторизации. Доступ нужно ограничивать сетевыми средствами или добавлять API-ключ отдельной доработкой.

## Быстрая проверка связи

### GET `/health`

Проверяет, что HTTP-сервер запущен.

Запрос:

```powershell
curl.exe "$BaseUrl/health"
```

Ответ:

```json
{
  "ok": true
}
```

Когда использовать:

- health check в службе Windows;
- проверка доступности с другого компьютера;
- мониторинг, что процесс жив.

## Получение состояния линии

### GET `/api/state`

Возвращает полный снимок состояния контроллера: активный режим, станции, перегоны, режимы и журнал.

Запрос:

```powershell
curl.exe "$BaseUrl/api/state"
```

Пример сокращенного ответа:

```json
{
  "mode": "stop_all",
  "activeModeId": "stop_all",
  "activeModeName": "Все станции · 3 с",
  "holdSeconds": 3.0,
  "loop": true,
  "configPath": "C:\\Users\\maria\\OneDrive\\Документы\\Линия Практика\\config.json",
  "stations": [
    {
      "index": 1,
      "name": "Station 1",
      "port": "COM1",
      "connected": true,
      "occupied": true,
      "shuttleId": 101,
      "waitingSeconds": 4.2,
      "lastSeen": "2026-06-19T12:30:10",
      "lastRaw": "010100535303",
      "messageCount": 5,
      "lastError": null,
      "lastCommandHex": null,
      "canRelease": true,
      "releaseBlocker": null
    }
  ],
  "segments": [
    {
      "from": "Station 1",
      "to": "Station 2",
      "occupiedBy": null,
      "occupiedByIds": [],
      "shuttleCount": 0,
      "occupiedSeconds": null
    }
  ],
  "modes": [
    {
      "id": "stop_all",
      "name": "Все станции · 3 с",
      "stationDelays": {
        "1": 3.0,
        "2": 3.0
      },
      "delays": [
        {
          "stationIndex": 1,
          "stationName": "Station 1",
          "seconds": 3.0
        }
      ]
    }
  ],
  "events": [
    {
      "at": "2026-06-19T12:30:11",
      "level": "info",
      "message": "Mode changed to Все станции · 3 с"
    }
  ]
}
```

### Поля станции

| Поле | Тип | Описание |
| --- | --- | --- |
| `index` | number | Номер станции в маршруте. |
| `name` | string | Название станции из конфигурации. |
| `port` | string | COM-порт станции. |
| `connected` | boolean | Подключен ли serial-порт станции. |
| `occupied` | boolean | Есть ли тележка на станции по карте сервиса. |
| `shuttleId` | number/null | ID тележки на станции. |
| `waitingSeconds` | number/null | Сколько секунд тележка ожидает на станции. |
| `lastSeen` | string/null | Время последнего сообщения от станции. |
| `lastRaw` | string/null | Последний IRM-кадр в HEX. |
| `messageCount` | number | Количество сообщений от станции. |
| `lastError` | string/null | Последняя ошибка COM-порта или отправки. |
| `lastCommandHex` | string/null | Последняя отправленная команда в HEX. |
| `canRelease` | boolean | Можно ли сейчас отправить тележку с этой станции. |
| `releaseBlocker` | string/null | Причина блокировки отправки. |

### Поля перегона

| Поле | Тип | Описание |
| --- | --- | --- |
| `from` | string | Название станции отправления. |
| `to` | string | Название следующей станции. |
| `occupiedBy` | number/null | Первый ID тележки в перегоне, оставлено для совместимости. |
| `occupiedByIds` | number[] | Все ID тележек, которые сервис считает находящимися в перегоне. |
| `shuttleCount` | number | Количество тележек в перегоне. |
| `occupiedSeconds` | number/null | Сколько секунд перегон занят. |

Важно: текущее правило движения запрещает отправку, если `shuttleCount > 0` в следующем перегоне.

## Выбор режима работы

### POST `/api/actions/select_mode`

Включает режим работы или переводит сервис в ожидание.

Запрос включения режима:

```powershell
curl.exe -X POST "$BaseUrl/api/actions/select_mode" `
  -H "Content-Type: application/json" `
  -d "{\"modeId\":\"stop_all\"}"
```

Тело:

```json
{
  "modeId": "stop_all"
}
```

Ответ: полный снимок состояния. Важные поля:

```json
{
  "mode": "stop_all",
  "activeModeId": "stop_all",
  "activeModeName": "Все станции · 3 с"
}
```

Остановка режима:

```powershell
curl.exe -X POST "$BaseUrl/api/actions/select_mode" `
  -H "Content-Type: application/json" `
  -d "{\"modeId\":null}"
```

Тело:

```json
{
  "modeId": null
}
```

Если передать несуществующий режим:

```powershell
curl.exe -X POST "$BaseUrl/api/actions/select_mode" `
  -H "Content-Type: application/json" `
  -d "{\"modeId\":\"unknown_mode\"}"
```

Ответ:

```json
{
  "ok": false,
  "error": "Mode does not exist: unknown_mode"
}
```

HTTP-статус: `400`.

### Совместимые endpoints старых режимов

Для обратной совместимости остались endpoints без тела:

```powershell
curl.exe -X POST "$BaseUrl/api/actions/stop_2_4"
curl.exe -X POST "$BaseUrl/api/actions/stop_all"
```

Предпочтительный новый способ - `/api/actions/select_mode`, потому что он работает с любыми режимами, созданными через интерфейс.

## Запуск тележки со станции

### POST `/api/actions/release_station`

Отправляет `START FORWARD` тележке, которая сейчас находится на указанной станции.

Запрос:

```powershell
curl.exe -X POST "$BaseUrl/api/actions/release_station" `
  -H "Content-Type: application/json" `
  -d "{\"station\":1}"
```

Тело:

```json
{
  "station": 1
}
```

Успешный ответ содержит полный снимок состояния и `lastAction`:

```json
{
  "lastAction": {
    "released": true,
    "station": "Station 1",
    "targetStation": "Station 2",
    "shuttleId": 101,
    "commandHex": "010065513503"
  }
}
```

Если на станции нет тележки:

```json
{
  "lastAction": {
    "released": false,
    "station": "Station 1",
    "reason": "station is empty"
  }
}
```

Если следующий перегон занят:

```json
{
  "lastAction": {
    "released": false,
    "station": "Station 1",
    "reason": "segment Station 1->Station 2 already has 1 shuttles"
  }
}
```

Для этих отказов HTTP-статус остается `200`, потому что запрос корректный, но команда не прошла проверку состояния линии.

## Редактирование станций

### POST `/api/config/stations`

Сохраняет список станций в рабочий `config.json`.

Ограничение: менять станции можно только когда линия пустая. То есть:

- нет занятых станций;
- нет тележек в перегонах.

Запрос:

```powershell
curl.exe -X POST "$BaseUrl/api/config/stations" `
  -H "Content-Type: application/json" `
  -d "{\"stations\":[{\"index\":1,\"name\":\"Station 1\",\"port\":\"COM1\"},{\"index\":2,\"name\":\"Station 2\",\"port\":\"COM2\"}]}"
```

Тело:

```json
{
  "stations": [
    {
      "index": 1,
      "name": "Station 1",
      "port": "COM1"
    },
    {
      "index": 2,
      "name": "Station 2",
      "port": "COM2"
    }
  ]
}
```

Можно передавать дополнительные поля:

```json
{
  "index": 1,
  "name": "Station 1",
  "port": "COM1",
  "baudrate": 9600,
  "group": 1,
  "read_timeout": 0.05,
  "reconnect_interval": 3.0,
  "mock": false
}
```

Если дополнительные поля не переданы, сервис сохранит старые значения для существующих станций или применит defaults для новых.

Успешный ответ:

```json
{
  "saved": true,
  "stations": [
    {
      "index": 1,
      "name": "Station 1",
      "port": "COM1"
    }
  ]
}
```

Если линия не пустая:

```json
{
  "ok": false,
  "error": "Нельзя менять конфигурацию, пока есть занятые станции или перегоны"
}
```

HTTP-статус: `400`.

## Редактирование режимов

### POST `/api/config/modes`

Сохраняет список режимов работы в `config.json`.

Запрос:

```powershell
curl.exe -X POST "$BaseUrl/api/config/modes" `
  -H "Content-Type: application/json" `
  -d "{\"modes\":[{\"id\":\"stop_all\",\"name\":\"Все станции · 3 с\",\"stationDelays\":{\"1\":3,\"2\":3,\"3\":3}}]}"
```

Тело:

```json
{
  "modes": [
    {
      "id": "stop_all",
      "name": "Все станции · 3 с",
      "stationDelays": {
        "1": 3,
        "2": 3,
        "3": 3
      }
    },
    {
      "id": "fast_pass",
      "name": "Без остановок",
      "stationDelays": {
        "1": 0,
        "2": 0,
        "3": 0
      }
    }
  ]
}
```

Правила:

- `id` должен быть уникальным;
- `name` отображается на кнопке режима;
- ключи `stationDelays` - номера станций строками;
- значения `stationDelays` - задержка в секундах;
- `0` означает отправлять сразу, когда следующий перегон свободен.

Успешный ответ содержит:

```json
{
  "saved": true,
  "modes": [
    {
      "id": "stop_all",
      "name": "Все станции · 3 с"
    }
  ]
}
```

Если `id` повторяется:

```json
{
  "ok": false,
  "error": "Mode id repeats: stop_all"
}
```

HTTP-статус: `400`.

## Mock-события для проверки

### POST `/api/mock/presence`

Имитирует сообщение присутствия тележки на станции. Endpoint полезен для проверки интерфейса и логики в `--mock` режиме.

Не используйте этот endpoint на реальной линии, если не хотите вручную менять карту состояния сервиса.

Запрос:

```powershell
curl.exe -X POST "$BaseUrl/api/mock/presence" `
  -H "Content-Type: application/json" `
  -d "{\"station\":1,\"shuttleId\":101,\"group\":1}"
```

Тело:

```json
{
  "station": 1,
  "shuttleId": 101,
  "group": 1
}
```

Ответ: полный снимок состояния, где указанная станция станет занятой тележкой `101`.

## Типовой сценарий проверки без оборудования

1. Запустить сервис в mock-режиме:

   ```powershell
   python main.py --mock --port 8080
   ```

2. Создать тележку на станции 1:

   ```powershell
   curl.exe -X POST "$BaseUrl/api/mock/presence" `
     -H "Content-Type: application/json" `
     -d "{\"station\":1,\"shuttleId\":101}"
   ```

3. Проверить состояние:

   ```powershell
   curl.exe "$BaseUrl/api/state"
   ```

4. Отправить тележку со станции 1:

   ```powershell
   curl.exe -X POST "$BaseUrl/api/actions/release_station" `
     -H "Content-Type: application/json" `
     -d "{\"station\":1}"
   ```

5. Убедиться, что перегон `Station 1 -> Station 2` занят:

   ```json
   {
     "from": "Station 1",
     "to": "Station 2",
     "occupiedByIds": [101],
     "shuttleCount": 1
   }
   ```

6. Имитировать приход тележки на станцию 2:

   ```powershell
   curl.exe -X POST "$BaseUrl/api/mock/presence" `
     -H "Content-Type: application/json" `
     -d "{\"station\":2,\"shuttleId\":101}"
   ```

7. Перегон освободится, а станция 2 станет занятой.

## Пример интеграции с Python

Без сторонних зависимостей, через стандартный `urllib`:

```python
import json
from urllib.request import Request, urlopen

BASE_URL = "http://192.168.1.10:8080"


def get_state():
    with urlopen(f"{BASE_URL}/api/state", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(path, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


state = get_state()
print(state["activeModeName"])

post_json("/api/actions/select_mode", {"modeId": "stop_all"})
post_json("/api/actions/release_station", {"station": 1})
post_json("/api/actions/select_mode", {"modeId": None})
```

## Пример интеграции с JavaScript/fetch

```javascript
const baseUrl = "http://192.168.1.10:8080";

async function getState() {
  const response = await fetch(`${baseUrl}/api/state`, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function selectMode(modeId) {
  const response = await fetch(`${baseUrl}/api/actions/select_mode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ modeId }),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

await selectMode("stop_all");
await selectMode(null);
```

## Пример для робота-манипулятора

Типовой безопасный сценарий:

1. Робот проверяет доступность сервиса через `/health`.
2. Робот получает `/api/state`.
3. Робот выбирает режим через `/api/actions/select_mode`.
4. Робот периодически читает `/api/state`, чтобы понимать текущий режим и занятость станций.
5. Робот не меняет `/api/config/*` во время работы линии.

Пример выбора режима:

```powershell
curl.exe -X POST "http://192.168.1.10:8080/api/actions/select_mode" `
  -H "Content-Type: application/json" `
  -d "{\"modeId\":\"fast_pass\"}"
```

Пример остановки автоматического режима:

```powershell
curl.exe -X POST "http://192.168.1.10:8080/api/actions/select_mode" `
  -H "Content-Type: application/json" `
  -d "{\"modeId\":null}"
```

Рекомендуется не давать роботу доступ к endpoints конфигурации:

```text
POST /api/config/stations
POST /api/config/modes
POST /api/mock/presence
```

Для промышленной эксплуатации лучше добавить отдельный endpoint, например `POST /api/robot/mode`, и защитить его API-ключом.

## Ошибки и статусы

| Ситуация | HTTP | Ответ |
| --- | --- | --- |
| Сервер жив | `200` | `{"ok": true}` |
| Состояние получено | `200` | Снимок состояния |
| Команда корректна, но движение заблокировано состоянием линии | `200` | Снимок состояния + `lastAction.released: false` |
| Неверное тело запроса, несуществующий режим, ошибка валидации | `400` | `{"ok": false, "error": "..."}` |
| Неизвестный URL | `404` | Стандартная HTML-ошибка HTTP-сервера |

## Минимальные рекомендации по защите

Сейчас API не проверяет пользователя или ключ. Поэтому:

- запускайте сервис только в закрытой сети линии;
- ограничьте порт `8080` в Windows Firewall по IP;
- не пробрасывайте порт сервиса наружу;
- для робота-манипулятора выделите отдельный endpoint и API-ключ;
- не используйте `/api/mock/presence` на реальной линии.

